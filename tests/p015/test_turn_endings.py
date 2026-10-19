"""The 2026-10-19 development revision: the clarified task (`docthread-task/2`), the turn-ending policies
(`p015-turn-endings/1` and `/2`) and output-limit truncation (`p015-truncation/1`). Offline: scripted models or an
in-memory HTTP transport with a placeholder key, network blocked (conftest), temporary ledgers; no provider request."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from clm_lib.budget import Ledger, ModelPrice
from clm_lib.p015 import launcher as L
from clm_lib.p015.accounting import AccountedModel, OutputTruncated, output_truncation
from clm_lib.p015.arms import ArmSession
from clm_lib.p015.docthread.fixture import instance
from clm_lib.p015.docthread.scoring import (
    SCORER_VERSION,
    answer_block,
    judge,
    score_session,
    true_answers,
)
from clm_lib.p015.docthread.task import (
    FIELD_FORMS,
    SYSTEM_PROMPT,
    TASK_V1,
    TASK_V2,
    questions,
    user_message,
)
from clm_lib.p015.docthread.tools import EvidenceService
from clm_lib.p015.live import ModelConfig, live_model
from clm_lib.p015.records import read_jsonl
from clm_lib.p015.session import (
    ContinuationUnsupported,
    GuardedSession,
    GuardedSettings,
    unanswered_calls,
)

PRICE = ModelPrice("claude-sonnet-5", 2.0, 2.5, 4.0, 0.2, 10.0, "test", "test")
INSTANCES = ("docthread-dev-1", "docthread-dev-2")
V1_DIGEST = (
    "2c8339cfc480409ccb591256e3a3f1eea9f7af7516b1f3e0d5d3b4f3f5c33510"  # as committed in 7084e84
)


def authorise(path: Path, launch_id: str) -> None:
    path.write_text(json.dumps({"experiment": "015", "launch_id": launch_id, "programme_ceiling_usd": 50.0,
                                "block_stop_usd": 20.0, "live": False, "granted_by": "test",
                                "reason": "temporary offline ledger"}), encoding="utf-8")  # fmt: skip


def launch(tmp: Path, launch_id: str, **spec: Any) -> tuple[Path, dict[str, Any]]:
    authorise(tmp / f"{launch_id}-auth.json", launch_id)
    base = {"launch_id": launch_id, "instances": ["docthread-dev-1"], "arms": ["guarded_clm"], "questions": 8,
            "ledger": str(tmp / f"{launch_id}-ledger.json"), "ledger_ceiling_usd": 100.0,
            "authorisation": str(tmp / f"{launch_id}-auth.json")}  # fmt: skip
    L.plan(base | spec, tmp / launch_id)
    return tmp / launch_id, base | spec


def run_record(launch_dir: Path, sid: str) -> dict[str, Any]:
    return json.loads((launch_dir / "runs" / sid / "r1" / "session.json").read_text())


# ---------------------------------------------------------------------- 1. the task contract


def test_the_historical_task_wording_is_unchanged() -> None:
    h = hashlib.sha256()
    for name in INSTANCES:
        fx = instance(name)
        messages = [user_message(q, TASK_V1) for q in questions(fx, TASK_V1)]
        assert messages == [user_message(q) for q in questions(fx)]  # version 1 stays the default
        for m in messages:
            h.update(m.encode())
            h.update(b"\0")
    assert h.hexdigest() == V1_DIGEST


@pytest.mark.parametrize("name", INSTANCES)
def test_the_clarified_wording_states_each_fields_form_and_reveals_nothing(name: str) -> None:
    fx = instance(name)
    v1, v2 = questions(fx, TASK_V1), questions(fx, TASK_V2)
    assert [q.asks for q in v1] == [q.asks for q in v2]  # the same fields and rules
    assert [q.number for q in v1 if q.text != v2[q.number - 1].text] == [4]
    setting = re.sub(r"\\", "", fx.signature.pattern).split("=")[0]
    hidden = [*map(str, fx.facts.values()), setting, *fx.components, *fx.names.values(), "page"]
    for q1, q2 in zip(v1, v2, strict=True):
        m1, m2 = user_message(q1, TASK_V1), user_message(q2, TASK_V2)
        for a in q2.asks:
            assert f"- {a.key}: {FIELD_FORMS[a.check]}" in m2
        for s in hidden:  # nothing hidden appears that the historical wording did not already show
            assert (s in m2) <= (s in m1), (q2.number, s)
    assert "configuration setting" in v2[3].text and "with its value" in v2[3].text


@pytest.mark.parametrize("name", INSTANCES)
def test_the_clarified_forms_are_what_the_unchanged_scorer_accepts(name: str) -> None:
    fx = instance(name)
    corpus = "\n".join(fx.render_all().values())
    assert SCORER_VERSION == "docthread-score/1"
    qs = questions(fx, TASK_V2)
    replies = [answer_block(true_answers(fx, q)) for q in qs]
    assert score_session(fx, replies)["points"] == 10
    asks = {a.key: a for q in qs for a in q.asks}
    whole_line = fx.facts[
        "evidence_event"
    ]  # one complete journal line, as the clarified wording asks
    assert judge(asks["evidence_event"], whole_line, fx, corpus) == (True, "correct")
    # What the clarified wording asks the model not to do is still wrong, exactly as before.
    record = next(
        line for line in corpus.splitlines() if line.startswith(fx.facts["faulty_change"] + " ")
    )
    assert judge(asks["faulty_change"], record, fx, corpus) == (False, "other_evidence")
    assert (
        judge(asks["procedure_step"], fx.facts["procedure_step"] + " (restore)", fx, corpus)[0]
        is False
    )


def test_every_arm_receives_the_same_clarified_task(tmp_path: Path) -> None:
    launch_dir, _ = launch(tmp_path, "task2", task_version=TASK_V2,
                           arms=["guarded_clm", "paper_clm_shell", "token_tail_p015"])  # fmt: skip
    assert L.run(launch_dir) == L.EXIT_OK
    fx = instance("docthread-dev-1")
    expected = [user_message(q, TASK_V2) for q in questions(fx, TASK_V2)]
    for sid in ("s01-docthread-dev-1-guarded_clm", "s02-docthread-dev-1-paper_clm_shell",
                "s03-docthread-dev-1-token_tail_p015"):  # fmt: skip
        turns = read_jsonl(launch_dir / "runs" / sid / "r1" / "turns.jsonl")
        assert [t["prompt"] for t in turns] == expected, sid
        record = run_record(launch_dir, sid)
        assert record["launch"]["task_version"] == TASK_V2 and record["score"]["points"] == 10, sid
    assert (
        json.loads((launch_dir / "contract.json").read_text())["fingerprints"]["workload"]["task"]
        == TASK_V2
    )


def test_unsupported_versions_are_refused_before_anything_is_written(tmp_path: Path) -> None:
    for key, value in (
        ("task_version", "docthread-task/9"),
        ("turn_endings", "p015-turn-endings/9"),
    ):
        with pytest.raises(ValueError, match=f"{key} .* is not supported"):
            launch(tmp_path, "bad", **{key: value})
        assert not (tmp_path / "bad").exists()
    with pytest.raises(ValueError, match="unsupported task version"):
        user_message(questions(instance("docthread-dev-1"))[0], "docthread-task/9")


# ---------------------------------------------------------------------- 2. continuation after a turn limit


class Scripted:
    """Answers after one read in `answer` mode; keeps reading in `loop` mode. Records every request."""

    def __init__(self) -> None:
        self.mode, self.calls, self.seen = "answer", 0, []

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        self.seen.append(list(messages))
        self.calls += 1
        if self.mode == "answer" and self.calls >= 2:
            return ModelResponse(parts=[TextPart('Done.\n```json\n{"x": 1}\n```')])
        return ModelResponse(
            parts=[ToolCallPart("list_changes", {"component": "moss-ocr"}, f"c{len(self.seen)}")]
        )

    def turn(self, session: Any, prompt: str, mode: str) -> Any:
        self.mode, self.calls = mode, 0
        return session.run_turn(prompt)


def guarded(tmp: Path, script: Scripted, **settings: Any) -> GuardedSession:
    fx = instance("docthread-dev-1")
    return GuardedSession(FunctionModel(script.respond, model_name="scripted"), run_dir=tmp / "g", run_id="g",
                          ledger=Ledger.open(tmp / "g.json", 100.0), price=PRICE, system_prompt=SYSTEM_PROMPT,
                          task_tools=EvidenceService(fx).tools(), conversation_id="g",
                          settings=GuardedSettings(**settings))  # fmt: skip


def context_of(request: list[ModelMessage]) -> str:
    return " ".join(
        str(p.content) for m in request for p in m.parts if isinstance(p, UserPromptPart)
    )


@pytest.mark.parametrize(
    ("settings", "ending"), [({"max_steps": 3}, "step_limit"), ({"call_cap": 3}, "call_cap")]
)
def test_guarded_clm_continues_after_a_limit_ended_turn_with_valid_history(
    tmp_path: Path, settings: dict[str, Any], ending: str
) -> None:
    script = Scripted()
    s = guarded(tmp_path, script, **settings)
    prompts = [user_message(q, TASK_V2) for q in questions(instance("docthread-dev-1"), TASK_V2)]
    assert script.turn(s, prompts[0], "answer").ending == "completed"
    assert script.turn(s, prompts[1], "loop").ending == ending
    assert s.limited_messages is not None and unanswered_calls(s.limited_messages) == []
    kept = s.continue_after_limit()
    assert kept == len(s.history or []) and s.limited_messages is None
    with pytest.raises(ContinuationUnsupported):  # only once, and only after a limit
        s.continue_after_limit()
    before = len(script.seen)
    assert script.turn(s, prompts[2], "answer").ending == "completed"
    text = context_of(script.seen[before])
    ids = re.findall(r'"id": "(t\d+\.\w+)"', text)
    assert ids == [
        "t1.answer",
        "t2.request",
    ]  # the unanswered question is protected; no answer is invented
    assert prompts[2].split("\n")[0] in text and prompts[1].split("\n")[0] in text
    last_step = max(int(n) for n in re.findall(r'"id": "s(\d+)\.obs"', text))
    assert (
        last_step == len([r for r in script.seen[:before] if r]) - 1
    )  # the limited turn's last step is kept
    script.turn(s, prompts[3], "answer")
    assert re.findall(r'"id": "(t\d+\.\w+)"', context_of(script.seen[-2])) == [
        "t1.answer", "t2.request", "t3.answer", "t3.request"]  # fmt: skip
    trace = [
        r for r in read_jsonl(tmp_path / "g" / "trace.jsonl") if r.get("type") == "turn_continued"
    ]
    assert [r["after_turn"] for r in trace] == [2]


def test_the_launcher_continues_only_after_main_model_limits(tmp_path: Path) -> None:
    launch_dir, _ = launch(tmp_path, "cont", task_version=TASK_V2, turn_endings="p015-turn-endings/2",
                           arm_settings={"guarded_clm": {"max_steps": 4}})  # fmt: skip
    assert L.run(launch_dir) == L.EXIT_OK
    sid = "s01-docthread-dev-1-guarded_clm"
    record = run_record(launch_dir, sid)
    endings = record["launch_result"]["endings"]
    assert (
        len(endings) == 8
        and "step_limit" in endings
        and "completed" in endings[endings.index("step_limit") :]
    )
    assert record["launch_result"]["outcome"] == "arm_failure" and record["score"]["out_of"] == 10
    limited = [i + 1 for i, e in enumerate(endings) if e == "step_limit"]
    for q in record["score"]["questions"]:
        if q["question"] in limited:
            assert not q["finished"] and all(v["status"] == "missing" for v in q["values"].values())
    events = [json.loads(line) for line in (launch_dir / "events.jsonl").read_text().splitlines()]
    assert [e["turn"] for e in events if e["event"] == "turn_continued"] == limited
    assert record["launch"]["turn_endings"] == "p015-turn-endings/2"


def test_continuation_is_refused_for_arms_that_cannot_follow_it(tmp_path: Path) -> None:
    for arm in ("paper_clm_shell", "token_tail_p015"):
        with pytest.raises(ValueError, match=f"not supported by {arm}: the arm keeps its own copy"):
            launch(
                tmp_path, "nocont", arms=["guarded_clm", arm], turn_endings="p015-turn-endings/2"
            )
    assert not (tmp_path / "nocont").exists()
    fx = instance("docthread-dev-1")
    s = ArmSession("token_tail_p015", FunctionModel(Scripted().respond), run_dir=tmp_path / "a", run_id="a",
                   ledger=Ledger.open(tmp_path / "a.json", 100.0), price=PRICE, system_prompt=SYSTEM_PROMPT,
                   task_tools=EvidenceService(fx).tools(), conversation_id="a")  # fmt: skip
    with pytest.raises(
        ContinuationUnsupported, match="token_tail_p015: the arm keeps its own copy"
    ):
        s.continue_after_limit()


@pytest.mark.parametrize(
    ("faults", "ending"),
    [
        ({"fail_at_attempt": 3, "fail_status": 400}, "provider_failure"),
        ({"truncate_at_attempt": 3}, "output_truncated"),
    ],
)
def test_other_endings_still_stop_the_session_under_continuation(
    tmp_path: Path, faults: dict[str, Any], ending: str
) -> None:
    sid = "s01-docthread-dev-1-guarded_clm"
    launch_dir, _ = launch(tmp_path, "stop", turn_endings="p015-turn-endings/2",
                           scripted={"faults": {sid: faults}})  # fmt: skip
    L.run(launch_dir)
    endings = run_record(launch_dir, sid)["launch_result"]["endings"]
    assert endings[-1] == ending and len(endings) < 8
    events = (launch_dir / "events.jsonl").read_text()
    assert "turn_continued" not in events


def test_a_stop_still_ends_the_session_under_continuation(tmp_path: Path) -> None:
    launch_dir, _ = launch(tmp_path, "stopnow", turn_endings="p015-turn-endings/2")

    def hook(sid: str, index: int, messages: list[ModelMessage]) -> None:
        del sid, messages
        if index == 2:
            (launch_dir / "STOP").write_text("now\n")

    assert L.run(launch_dir, hook=hook) == L.EXIT_STOPPED
    assert L.read_status(launch_dir, "s01-docthread-dev-1-guarded_clm")["state"] == "stopped"


def test_a_context_limit_still_ends_the_session_under_continuation(tmp_path: Path) -> None:
    sid = "s01-docthread-dev-1-guarded_clm"
    launch_dir, _ = launch(tmp_path, "ctx", turn_endings="p015-turn-endings/2",
                           arm_settings={"guarded_clm": {"budget_tokens": 3_000}})  # fmt: skip
    L.run(launch_dir)
    endings = run_record(launch_dir, sid)["launch_result"]["endings"]
    assert endings[-1] == "context_limit" and len(endings) < 8


# ---------------------------------------------------------------------- 3. output-limit truncation


def anthropic_message(stop_reason: str, content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "msg_offline", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1500, "output_tokens": 8192, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}}  # fmt: skip


def test_the_adapters_output_limit_stop_is_recorded_charged_and_not_retried(tmp_path: Path) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200, json=anthropic_message("max_tokens", [{"type": "text", "text": "The answer is"}])
        )

    config = ModelConfig()
    model = live_model(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                       api_key="offline-placeholder")  # fmt: skip
    attempts: list[dict[str, Any]] = []

    class Sink:
        def attempt_started(self, entry: dict[str, Any]) -> None:
            pass

        def attempt_finished(self, entry: dict[str, Any]) -> None:
            attempts.append(dict(entry))

    ledger = Ledger.open(tmp_path / "l.json", 10.0)
    accounted = AccountedModel(model, ledger=ledger, price=PRICE, run_id="t", mode="live", live_authorised=True,
                               retry=config.retry_policy(), sink=Sink())  # fmt: skip
    agent: Agent[None, str] = Agent(accounted, model_settings=config.model_settings())  # type: ignore[arg-type]
    with pytest.raises(OutputTruncated, match="output limit"):
        asyncio.run(agent.run("question"))
    assert len(sent) == 1 and config.max_retries == 2  # never retried
    [attempt] = attempts
    assert (
        attempt["outcome"] == "ok"
        and attempt["cost_basis"] == "provider_usage"
        and attempt["charged_usd"] > 0
    )
    assert attempt["output_truncated"] == {"finish_reason": "length", "stop_reason": "max_tokens"}
    assert attempt["reported"]["output_tokens"] == 8192
    data = json.loads((tmp_path / "l.json").read_text())
    assert (
        data["pending"] == []
        and sum(e["charged_usd"] for e in data["entries"]) == attempt["charged_usd"]
    )


def test_only_the_output_limit_counts_as_truncation() -> None:
    def response(finish: Any, stop: str | None) -> ModelResponse:
        return ModelResponse(
            parts=[TextPart("x")], finish_reason=finish, provider_details={"finish_reason": stop}
        )

    assert output_truncation(response("length", "max_tokens")) is not None
    assert output_truncation(response("length", "model_context_window_exceeded")) is None
    for finish, stop in (("stop", "end_turn"), ("tool_call", "tool_use"), (None, None)):
        assert output_truncation(response(finish, stop)) is None


def first_summary_attempt(tmp: Path) -> int:
    launch_dir, _ = launch(tmp, "probe", arms=["token_tail_p015"])
    assert L.run(launch_dir) == L.EXIT_OK
    attempts = read_jsonl(launch_dir / "runs/s01-docthread-dev-1-token_tail_p015/r1/attempts.jsonl")
    return min(
        a["attempt"] for a in attempts if a["event"] == "finished" and a["category"] == "summary"
    )


@pytest.mark.parametrize("category", ["main", "summary"])
def test_a_truncated_response_ends_the_session_and_keeps_earlier_scores(
    tmp_path: Path, category: str
) -> None:
    arm = "guarded_clm" if category == "main" else "token_tail_p015"
    sid = f"s01-docthread-dev-1-{arm}"
    clean_dir, _ = launch(tmp_path, "clean", arms=[arm])
    assert L.run(clean_dir) == L.EXIT_OK
    clean = run_record(clean_dir, sid)["score"]["questions"]
    clean_attempts = read_jsonl(clean_dir / "runs" / sid / "r1" / "attempts.jsonl")
    if category == "main":  # an attempt part-way through the session
        index = max(a["attempt"] for a in clean_attempts) // 2
    else:
        index = first_summary_attempt(tmp_path)
    launch_dir, _ = launch(tmp_path, "cut", arms=[arm], turn_endings="p015-turn-endings/2" if arm == "guarded_clm"
                           else "p015-turn-endings/1", scripted={"faults": {sid: {"truncate_at_attempt": index}}})  # fmt: skip
    assert L.run(launch_dir) == L.EXIT_OK
    record = run_record(launch_dir, sid)
    result = record["launch_result"]
    assert result["endings"][-1] == "output_truncated" and result["outcome"] == "output_truncated"
    stopped_at = len(result["endings"])
    assert stopped_at < 8
    attempts = read_jsonl(launch_dir / "runs" / sid / "r1" / "attempts.jsonl")
    [cut] = [a for a in attempts if a["event"] == "finished" and "output_truncated" in a]
    assert cut["attempt"] == index and cut["category"] == category and cut["outcome"] == "ok"
    assert (
        max(a["attempt"] for a in attempts) == index
    )  # nothing after it: no retry, no further turn
    score = record["score"]
    assert score["out_of"] == 10
    for q in score["questions"]:
        if q["question"] < stopped_at:  # earlier completed turns keep their scores
            assert q == clean[q["question"] - 1]
        else:  # the affected and the unreached turns are unanswered
            assert not q["finished"] and all(v["status"] == "missing" for v in q["values"].values())
    with pytest.raises(ValueError, match="is a result and is not rerun"):
        L.resolve(launch_dir, sid, "rerun", "test")
