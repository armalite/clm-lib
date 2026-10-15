"""The paper arm (`paper-clm-shell/1`): the mirror format, the edit gate, step and call accounting, nudges, rollback,
the sandbox's isolation and scripted docthread sessions through the real p015 path (network blocked by conftest;
sandbox tests need Docker)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from clm_lib.budget import Ledger
from clm_lib.executor import ExecResult, ExecutorUnavailable
from clm_lib.p015.arm_errors import ArmStepLimit
from clm_lib.p015.arms import ArmSession
from clm_lib.p015.docthread.arms_offline import ArmPlan, run
from clm_lib.p015.docthread.fixture import instance
from clm_lib.p015.docthread.plain_agent import BAD_EDITS
from clm_lib.p015.docthread.task import SYSTEM_PROMPT, questions
from clm_lib.p015.export import export_session, verify_export
from clm_lib.p015.paper.capability import PaperSettings, PaperShellContext, instructions
from clm_lib.p015.paper.mirror import MirrorError, conversation_problems, parse, render
from clm_lib.p015.paper.shell import MIRROR_FILE, ShellSandbox
from clm_lib.p015.records import read_jsonl

# ---------------------------------------------------------------------- the mirror


def convo() -> list[ModelMessage]:
    return [
        ModelResponse(
            parts=[
                TextPart("Reading."),
                ToolCallPart("read_journal", {"component": "a", "page": 1}, "c1"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart("read_journal", "page one\nline two", "c1")]),
        ModelResponse(parts=[TextPart('Done.\n\n```json\n{"x": 1}\n```')]),
        ModelRequest(parts=[UserPromptPart("Next question?")]),
        ModelResponse(parts=[ToolCallPart("bash", {"command": "true"}, "b1")]),
    ]


def test_mirror_round_trip_keeps_unchanged_messages_and_validates() -> None:
    msgs = convo()
    text = render(msgs)
    assert text.startswith(
        "[[CTX_TURN 1 role=assistant]]\nReading.\n<<call id=c1 tool=read_journal>>"
    )
    assert (
        "[[CTX_TURN 2 role=tool]]\n<<result id=c1 tool=read_journal>>\npage one\nline two\n<<end result>>"
        in text
    )
    back = parse(text, msgs)
    assert all(
        a is b for a, b in zip(back, msgs, strict=True)
    )  # unchanged blocks keep their objects
    # Deleting a call and its result is valid; the rest is reused.
    blocks = text.split("\n\n[[CTX_TURN ")
    edited = "[[CTX_TURN " + "\n\n[[CTX_TURN ".join(blocks[2:])
    out = parse(edited, msgs)
    assert out == msgs[2:] and all(a is b for a, b in zip(out, msgs[2:], strict=True))
    # A rewritten block is rebuilt from its text.
    rewritten = parse(text.replace("page one\nline two", "page one (shortened)"), msgs)
    assert isinstance(rewritten[1], ModelRequest) and rewritten[1] is not msgs[1]
    part = rewritten[1].parts[0]
    assert (
        isinstance(part, ToolReturnPart)
        and part.content == "page one (shortened)"
        and part.tool_call_id == "c1"
    )
    assert conversation_problems(msgs) == []


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda t: "notes\n" + t, "text before the first"),
        (
            lambda t: t.replace('{"component":"a","page":1}', "{component}"),
            "arguments are not JSON",
        ),
        (lambda t: t.replace("Next question?", "   "), "an empty user block"),
        (lambda t: t.replace("<<end result>>", "<<end res>>"), "only result sections"),
        (lambda t: t.split("\n\n", 1)[1], "expected an assistant block"),
        (lambda t: t.replace("<<result id=c1", "<<result id=c9"), "are answered by"),
    ],
)
def test_invalid_mirror_edits_are_rejected_with_a_reason(
    edit: Callable[[str], str], message: str
) -> None:
    msgs = convo()
    with pytest.raises(MirrorError, match=message):
        parse(edit(render(msgs)), msgs)


# ---------------------------------------------------------------------- the capability, with a fake sandbox


@dataclasses.dataclass
class FakeSandbox:
    """Runs nothing: each command is looked up in `scripts`, which may edit the mirror text and give output."""

    mirror_dir: Path
    work_dir: Path
    scripts: dict[str, tuple[Callable[[str], str] | None, str]]
    image: str = "fake"
    timeout_s: int = 1
    memory: str = "0"
    capture_bytes: int = 32_004
    commands: list[str] = dataclasses.field(default_factory=list)

    def run(self, script: str) -> ExecResult:
        self.commands.append(script)
        edit, out = self.scripts[script]
        path = self.mirror_dir / "LIVE_CTX_MAIN.txt"
        if edit is not None:
            path.write_text(edit(path.read_text()))
        return ExecResult(0, out, "", False, len(out.encode()), 0, 0.01)


def drop_pairs(text: str) -> str:
    """Delete every complete call/result pair before the last block (what the scripted tidy command does)."""
    blocks = ["[[CTX_TURN " + b for b in text.split("[[CTX_TURN ") if b]
    keep, i = [], 0
    while i < len(blocks):
        if (
            i + 2 < len(blocks)
            and "role=assistant" in blocks[i]
            and "<<call id=" in blocks[i]
            and "role=tool" in blocks[i + 1]
        ):
            i += 2
            continue
        keep.append(blocks[i])
        i += 1
    return "".join(keep)


def paper_agent(
    tmp_path: Path,
    script: list[Any],
    scripts: dict[str, Any],
    settings: PaperSettings | None = None,
) -> tuple[Agent[None, str], PaperShellContext, list[list[ModelMessage]]]:
    seen: list[list[ModelMessage]] = []
    steps = iter(script)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        seen.append(list(messages))
        return next(steps)

    (tmp_path / "m").mkdir(parents=True)
    (tmp_path / "w").mkdir()
    box = FakeSandbox(tmp_path / "m", tmp_path / "w", scripts)
    cap = PaperShellContext(box, settings)  # type: ignore[arg-type]

    def read(page: int) -> str:
        return f"page {page}: " + "x" * 400

    agent: Agent[None, str] = Agent(
        FunctionModel(respond), system_prompt="sys", tools=[read], capabilities=[cap]
    )
    return agent, cap, seen


def call(tool: str, args: dict[str, Any], cid: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool, args, cid)])


def ids(messages: list[ModelMessage]) -> list[str]:
    return [p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolCallPart)]


def test_accepted_edit_shapes_the_next_request_and_is_a_free_step(tmp_path: Path) -> None:
    script = [
        call("read", {"page": 1}, "r1"),
        call("read", {"page": 2}, "r2"),
        call("bash", {"command": "tidy"}, "b1"),
        call("bash", {"command": "ls"}, "b2"),
        ModelResponse(parts=[TextPart("answer")]),
    ]
    agent, cap, seen = paper_agent(
        tmp_path, script, {"tidy": (drop_pairs, ""), "ls": (None, "notes.txt\n")}
    )
    result = asyncio.run(agent.run("question"))
    assert result.output == "answer"
    assert ids(seen[2]) == ["r1", "r2"] and ids(seen[3]) == [
        "b1"
    ]  # the deleted reads are gone from then on
    receipt = next(p for p in seen[3][-1].parts if isinstance(p, ToolReturnPart))
    assert "[context] edit applied: 5 blocks before, 1 after" in receipt.model_response_str()
    assert all("page 1:" not in json.dumps([str(p) for m in s for p in m.parts]) for s in seen[3:])
    st = cap.state
    assert (st.steps, st.free_steps, st.calls, st.edits_accepted) == (
        4,
        1,
        5,
        1,
    )  # b2 printed output: a step
    assert seen[0][0].parts[0].content == "sys"  # type: ignore[union-attr]
    mirror = (tmp_path / "m" / "LIVE_CTX_MAIN.txt").read_text()
    assert mirror.startswith(
        "[[CTX_TURN 1 role=assistant]]\n<<call id=b1 tool=bash>>"
    )  # written before b2


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (lambda t: "junk", "text before the first"),
        (lambda t: t[: t.rindex("[[CTX_TURN ")], "the last block, which holds the running command"),
        (lambda t: "[[CTX_TURN 0 role=user]]\nextra\n\n" + t, "expected an assistant block"),
        (lambda t: t.replace("x" * 400, "y" * 400_000), "over the 29,952-token limit"),
    ],
)
def test_rejected_edit_changes_nothing(
    tmp_path: Path, edit: Callable[[str], str], reason: str
) -> None:
    script = [
        call("read", {"page": 1}, "r1"),
        call("bash", {"command": "bad"}, "b1"),
        ModelResponse(parts=[TextPart("done")]),
    ]
    agent, cap, seen = paper_agent(tmp_path, script, {"bad": (edit, "")})
    asyncio.run(agent.run("question"))
    before, after = seen[1], seen[2]
    assert ids(after) == [*ids(before), "b1"]
    assert [m for m in after[: len(before) - 1]] == before[:-1]
    receipt = next(p for p in after[-1].parts if isinstance(p, ToolReturnPart)).model_response_str()
    assert "[context] edit rejected, nothing changed:" in receipt and reason in receipt
    assert cap.state.edits_rejected == 1 and cap.state.edits_accepted == 0
    assert cap.state.free_steps == 1  # the README's rule: mirror changed, no output, exit 0


def test_only_the_first_bash_command_of_a_response_runs(tmp_path: Path) -> None:
    both = ModelResponse(
        parts=[
            ToolCallPart("bash", {"command": "ls"}, "b1"),
            ToolCallPart("bash", {"command": "ls"}, "b2"),
        ]
    )
    agent, cap, seen = paper_agent(
        tmp_path, [both, ModelResponse(parts=[TextPart("ok")])], {"ls": (None, "a\n")}
    )
    asyncio.run(agent.run("q"))
    results = {
        p.tool_call_id: p.model_response_str()
        for p in seen[1][-1].parts
        if isinstance(p, ToolReturnPart)
    }
    assert results["b2"].startswith("not run: one bash command per response")
    assert cap.sandbox.commands == ["ls"]  # type: ignore[attr-defined]


def test_step_limit_call_cap_and_finalize_notice(tmp_path: Path) -> None:
    reads = [call("read", {"page": i}, f"r{i}") for i in range(10)]
    agent, cap, seen = paper_agent(tmp_path / "a", reads, {}, PaperSettings(max_steps=4))
    with pytest.raises(ArmStepLimit):
        asyncio.run(agent.run("q"))
    assert len(seen) == 4
    notes = [str(p.content) for p in seen[-1][-1].parts if isinstance(p, UserPromptPart)]
    assert any("1 step(s) left in this turn" in n for n in notes)
    free = [call("bash", {"command": "tidy"}, f"b{i}") for i in range(10)]
    agent, cap, seen = paper_agent(
        tmp_path / "b", free, {"tidy": (lambda t: t + "\n", "")}, PaperSettings(call_cap=3)
    )
    with pytest.raises(UsageLimitExceeded):
        asyncio.run(agent.run("q"))
    assert cap.state.calls == 3 and cap.state.steps == 0  # free steps count as calls only


def test_nudges_follow_the_documented_thresholds(tmp_path: Path) -> None:
    (tmp_path / "m").mkdir()
    cap = PaperShellContext(FakeSandbox(tmp_path / "m", tmp_path, {}))  # type: ignore[arg-type]
    limit = cap.settings.limit
    assert cap._notices(int(0.2 * limit)) == []
    assert "(26% of the limit)" in cap._notices(int(0.26 * limit))[0]
    assert cap._notices(int(0.27 * limit)) == []  # each threshold once
    assert (
        "(76% of the limit)" in cap._notices(int(0.76 * limit))[0]
    )  # 50% and 75% crossed together: one notice
    assert cap._notices(int(0.10 * limit)) == [] and cap.state.nudged == set()  # re-armed below
    cap.state.nudged = {0.25, 0.5, 0.75}
    near = limit - int(0.09 * limit)
    assert any(
        "tokens are left before" in n for n in cap._notices(near)
    )  # room below 10% of the limit
    cap.state.recent_outputs.extend([3_000, 100, 200])
    assert any(
        "tokens are left before" in n for n in cap._notices(limit - 5_000)
    )  # room below 2 x 3,000
    assert not any(
        "tokens are left before" in n for n in cap._notices(int(0.45 * limit))
    )  # never below 50%


def test_instructions_and_tool_schema() -> None:
    text = instructions(PaperSettings())
    assert (
        MIRROR_FILE in text
        and "29,952 tokens" in text
        and "[[CTX_TURN i role=user|assistant|tool]]" in text
    )
    assert "free" not in text  # free edit steps are not described to the model


# ---------------------------------------------------------------------- docthread sessions without the sandbox


@pytest.fixture(scope="module")
def stress(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("paper-stress")
    plans = {
        "rollback": ArmPlan(
            "paper_clm_shell", policy="never", label="stress", paper=PaperSettings(max_rollbacks=3)
        ),
        "never": ArmPlan("paper_clm_shell", policy="never", label="stress"),
    }
    return {k: (run(p, out, k), out / k) for k, p in plans.items()}


def test_rollback_then_one_final_request_then_context_limit(stress: dict[str, Any]) -> None:
    result, run_dir = stress["rollback"]
    assert result["endings"] == ["context_limit"]
    m = result["measurements"]
    assert m["rollbacks"] == 3 and m["rollback_notices"] == 3 and m["over_limit_notices"] == 1
    trace = read_jsonl(run_dir / "trace.jsonl")
    rolled = [r for r in trace if r["type"] == "paper_rollback"]
    assert all(r["to_tokens"] <= 29_952 - 2_048 < r["from_tokens"] for r in rolled)
    assert trace[-1]["type"] == "context_limit"
    # A rolled-back step never reappears: its call (in the response received) is in no later request.
    attempts = read_jsonl(run_dir / "attempts.jsonl")
    started = [a for a in attempts if a["event"] == "started"]
    replies = [a for a in attempts if a["event"] == "finished"]

    def call_ids(messages: list[dict[str, Any]]) -> set[str]:
        return {
            p["tool_call_id"] for m in messages for p in m["parts"] if p["part_kind"] == "tool-call"
        }

    later = [call_ids(a["messages"]) for a in started]
    rolled = [
        cid
        for i, r in enumerate(replies[:-1])
        for cid in call_ids([r["response"]])
        if cid not in later[i + 1]
    ]
    assert (
        len(rolled) == 3
    )  # one read per rollback (the read after the final over-limit request ends the turn)
    assert all(cid not in ids_ for cid in rolled for ids_ in later)
    assert result["score"]["points"] == 0
    assert all(
        f["status"] == "missing" for t in result["score"]["questions"] for f in t["values"].values()
    )


def test_never_editing_agent_ends_at_the_step_limit_with_rollbacks(stress: dict[str, Any]) -> None:
    result, run_dir = stress["never"]
    assert result["endings"] == ["step_limit"]
    m = result["measurements"]
    assert 0 < m["rollbacks"] < 50 and m["persistent_nudges"] > 0 and m["size_nudges"] == 3
    turns = read_jsonl(run_dir / "turns.jsonl")
    assert turns[0]["counters"]["steps"] == 60 and turns[0]["counters"]["calls"] == 60


def test_without_docker_the_command_is_refused_and_the_turn_ends(tmp_path: Path) -> None:
    box = ShellSandbox(tmp_path, tmp_path, docker_bin=str(tmp_path / "no-docker-here"))
    with pytest.raises(ExecutorUnavailable):
        box.run("true")
    session = ArmSession(
        "paper_clm_shell",
        FunctionModel(
            lambda m, i: ModelResponse(parts=[ToolCallPart("bash", {"command": "true"}, "b1")])
        ),
        run_dir=tmp_path / "run",
        run_id="t",
        ledger=Ledger.open(tmp_path / "ledger.json", 100.0),
        price=None,
    )
    session.capability.sandbox.docker_bin = box.docker_bin  # type: ignore[union-attr]
    outcome = session.run_turn("q")
    assert outcome.ending == "sandbox_unavailable"
    assert not list((tmp_path / "run" / "sandbox" / "work").iterdir())  # nothing ran anywhere


# ---------------------------------------------------------------------- the real sandbox


@pytest.mark.docker
def test_sandbox_is_isolated(tmp_path: Path) -> None:
    (tmp_path / "m").mkdir()
    (tmp_path / "w").mkdir()
    (tmp_path / "m" / "LIVE_CTX_MAIN.txt").write_text("[[CTX_TURN 1 role=assistant]]\nhi\n")
    box = ShellSandbox(tmp_path / "m", tmp_path / "w", timeout_s=20)
    probe = (
        "id -u; test -w /tmp/.live_ctx/LIVE_CTX_MAIN.txt && echo mirror-writable; "
        "touch /root-check 2>/dev/null && echo root-writable || echo root-read-only; "
        "test -e /var/run/docker.sock && echo docker-socket || echo no-docker-socket; "
        f"test -e {Path.home()} && echo host-home || echo no-host-home; "
        "env | cut -d= -f1 | sort | tr '\\n' ' '; echo; "
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\" 2>/dev/null && echo net || echo no-net; "
        "echo kept > /work/note.txt"
    )
    res = box.run(probe)
    out = res.stdout
    assert res.exit_code == 0, res.stderr
    lines = out.splitlines()
    assert lines[0] == str(os.getuid()) != "0"
    assert "mirror-writable" in out and "root-read-only" in out and "no-docker-socket" in out
    assert "no-host-home" in out and "no-net" in out
    env = next(line for line in lines if "HOME" in line).split()
    # Variable names only: the container has HOME=/tmp and the image's own variables (GPG_KEY is the public id of
    # the key that signs Python releases), and nothing from the host environment.
    image_env = {
        "PATH",
        "HOME",
        "HOSTNAME",
        "LANG",
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "PWD",
        "SHLVL",
        "_",
    }
    assert set(env) <= image_env, set(env) - image_env
    assert box.run("cat /work/note.txt").stdout == "kept\n"  # /work persists between commands


@pytest.fixture(scope="module")
def sessions(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("paper")
    plans = {
        "tidy": ArmPlan("paper_clm_shell"),
        "bad": ArmPlan("paper_clm_shell", label="stress", bad_edits=list(BAD_EDITS.values())),
        "failure": ArmPlan("paper_clm_shell", fail_at_attempt=12, label="failure"),
    }
    return {k: (run(p, out, k), out / k) for k, p in plans.items()}


@pytest.mark.docker
def test_tidy_session_through_the_real_sandbox(sessions: dict[str, Any], tmp_path: Path) -> None:
    result, run_dir = sessions["tidy"]
    assert result["endings"] == ["completed"] * 8 and result["score"]["points"] == 10
    m = result["measurements"]
    assert m["edits_applied"] == m["commands"] == m["free_steps"] > 20 and m["edits_rejected"] == 0
    assert m["refetches"] >= 3  # tidy deletes the recall sources, so memory turns read them again
    started = [a for a in read_jsonl(run_dir / "attempts.jsonl") if a["event"] == "started"]
    fx = instance("docthread-dev-1")
    first = started[0]["messages"][0]["parts"]
    assert first[0]["content"] == SYSTEM_PROMPT and questions(fx)[0].text in first[1]["content"]
    for (
        a
    ) in started:  # the pinned request leads every request; the sent instructions name the mirror
        assert a["messages"][0]["parts"][0]["content"] == SYSTEM_PROMPT
        assert MIRROR_FILE in json.dumps(a["parameters"]["instruction_parts"])
    tools = {t["name"] for t in started[0]["parameters"]["function_tools"]}
    assert tools == {"read_journal", "list_changes", "queue_stats", "read_playbook", "bash"}
    # Every turn's question reaches the model in order, and earlier answers stay (tidy keeps them).
    last = json.dumps(started[-1]["messages"])
    assert questions(fx)[7].text in last and "Scripted answer for question 7." in last
    dest = export_session(run_dir, tmp_path / "export")
    assert verify_export(dest) == []
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["arm"] == "paper_clm_shell" and manifest["guarded_clm"] is None
    assert manifest["attempts"]["by_category"] == {"main": m["requests"]}


@pytest.mark.docker
def test_rejected_edits_in_a_session_keep_the_previous_context(sessions: dict[str, Any]) -> None:
    result, run_dir = sessions["bad"]
    assert result["endings"] == ["completed"] * 8 and result["score"]["points"] == 10
    trace = read_jsonl(run_dir / "trace.jsonl")
    rejected = [r["reason"] for r in trace if r["type"] == "paper_edit_rejected"]
    assert len(rejected) == 4
    assert any("text before the first" in r for r in rejected) and any(
        "running command" in r for r in rejected
    )
    assert any("not JSON" in r for r in rejected) and any(
        "expected an assistant block" in r for r in rejected
    )
    started = [a for a in read_jsonl(run_dir / "attempts.jsonl") if a["event"] == "started"]
    calls = [
        [
            p.get("tool_call_id")
            for msg in a["messages"]
            for p in msg["parts"]
            if p["part_kind"] == "tool-call"
        ]
        for a in started
    ]
    commands = [r for r in trace if r["type"] == "paper_command"]
    bad = [c["call_id"] for c in commands if c["edit"] == "rejected"]
    for cid in bad:
        i = next(
            k for k, c in enumerate(calls) if c and c[-1] == cid
        )  # the request holding the rejected result
        assert calls[i] == calls[i - 1] + [cid]


@pytest.mark.docker
def test_provider_failure_ends_explicitly(sessions: dict[str, Any]) -> None:
    result, run_dir = sessions["failure"]
    assert result["endings"] == ["provider_failure"]
    finished = [a for a in read_jsonl(run_dir / "attempts.jsonl") if a["event"] == "finished"]
    assert finished[-1]["outcome"] == "error" and finished[-1]["error"] == "http_503"
