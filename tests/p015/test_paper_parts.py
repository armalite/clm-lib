"""`paper-clm-shell/3`: the part-level mirror (`ctx-part-mirror/1`). The parser's rules on synthetic messages, and
whole sessions through the real `ArmSession`, the Docker sandbox and Pydantic AI's Anthropic adapter over an
in-memory HTTP transport, so that the provider request bodies themselves are checked. No network; a placeholder key;
temporary ledgers; synthetic responses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from clm_lib.budget import Ledger, ModelPrice
from clm_lib.p015.arms import ArmSession
from clm_lib.p015.docthread.fixture import instance
from clm_lib.p015.docthread.task import SYSTEM_PROMPT, TASK_V2, questions, user_message
from clm_lib.p015.docthread.tools import EvidenceService
from clm_lib.p015.live import ModelConfig, live_model
from clm_lib.p015.paper.capability import PAPER_V3, PaperSettings, instructions
from clm_lib.p015.paper.parts import PartMirrorError, blocks, parse, render

PRICE = ModelPrice("claude-sonnet-5", 2.0, 2.5, 4.0, 0.2, 10.0, "test", "test")


def convo() -> list[Any]:
    """An editable context: two completed tool exchanges, then a later question."""
    return [
        ModelResponse(
            parts=[
                ThinkingPart("", signature="sig1"),
                TextPart("Looking."),
                ToolCallPart("lookup", {"name": "a"}, "c1"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart("lookup", "a: " + "x" * 2000, "c1")]),
        ModelResponse(parts=[ToolCallPart("lookup", {"name": "b"}, "c2")]),
        ModelRequest(parts=[ToolReturnPart("lookup", "b: " + "y" * 2000, "c2")]),
        ModelResponse(parts=[TextPart("Answer one.")]),
        ModelRequest(parts=[UserPromptPart("Question two?")]),
    ]


def edit(text: str, number: int, new: str) -> str:
    """Replace block `number`'s text, keeping its header."""
    head = next(line for line in text.splitlines() if line.startswith(f"[[CTX_BLOCK {number} "))
    start = text.index(head) + len(head) + 1
    end = text.find("\n\n[[CTX_BLOCK ", start)
    return text[:start] + new + (text[end:] if end >= 0 else "\n")


def drop(text: str, number: int) -> str:
    head = next(line for line in text.splitlines() if line.startswith(f"[[CTX_BLOCK {number} "))
    start = text.index(head)
    end = text.find("[[CTX_BLOCK ", start + len(head))
    return text[:start] + (text[end:] if end >= 0 else "")


# ---------------------------------------------------------------------- the parser


def test_an_unchanged_file_keeps_every_message_object() -> None:
    msgs = convo()
    out, origins = parse(render(msgs), msgs)
    assert all(a is b for a, b in zip(out, msgs, strict=True)) and origins == [
        {i} for i in range(len(msgs))
    ]
    kinds = [(b.kind, b.call_id) for b in blocks(msgs)]
    assert kinds == [("assistant", None), ("tool_call", "c1"), ("tool_result", "c1"), ("tool_call", "c2"),
                     ("tool_result", "c2"), ("assistant", None), ("user", None)]  # fmt: skip
    assert "sig1" not in render(msgs)  # reasoning is not shown


def test_a_tool_result_can_be_replaced_by_a_prose_summary_and_stays_paired() -> None:
    msgs = convo()
    out, _ = parse(edit(render(msgs), 3, "Summary: a is fine."), msgs)
    result = out[1].parts[0]
    assert isinstance(result, ToolReturnPart) and result.content == "Summary: a is fine."
    assert (result.tool_call_id, result.tool_name) == ("c1", "lookup")
    assert out[0] is msgs[0] and out[2:] == msgs[2:]  # the call's reply and the rest are untouched


def test_deleting_one_half_of_a_pair_deletes_both_and_rebuilds_the_conversation() -> None:
    msgs = convo()
    out, origins = parse(drop(render(msgs), 5), msgs)  # the second result only
    calls = [p.tool_call_id for m in out for p in m.parts if isinstance(p, ToolCallPart)]
    assert calls == ["c1"]  # its call went with it, and the emptied reply and request were dropped
    assert [type(m).__name__ for m in out] == [
        "ModelResponse",
        "ModelRequest",
        "ModelResponse",
        "ModelRequest",
    ]
    assert origins == [{0}, {1}, {4}, {5}]


def test_notes_become_user_text_after_the_tool_results() -> None:
    msgs = convo()
    text = "Notes up front.\n\n" + render(msgs).replace(
        "[[CTX_BLOCK 4 ", "[[CTX_BLOCK note]]\nA note after block 3.\n\n[[CTX_BLOCK 4 "
    )
    out, origins = parse(text, msgs)
    assert isinstance(out[0], ModelRequest) and [p.content for p in out[0].parts] == [
        "Notes up front."
    ]
    assert origins[0] == set()  # a new opening note, sent within the pinned request by the arm
    results_request = out[2].parts
    assert isinstance(results_request[0], ToolReturnPart)  # results first, then the note
    assert [p.content for p in results_request[1:]] == ["A note after block 3."]


def test_a_whole_file_rewritten_as_one_note_is_accepted() -> None:
    """Every block deleted and replaced by the model's own prose: a legitimate reduction, rebuilt as user text."""
    msgs = convo()
    out, _ = parse(
        "Everything so far: a and b were looked up; a is the source; one answer given.\n", msgs
    )
    assert len(out) == 1 and isinstance(out[0], ModelRequest)
    assert out[0].parts[0].content.startswith("Everything so far")  # type: ignore[union-attr]


def test_a_changed_reply_loses_its_reasoning_and_an_unchanged_one_keeps_it() -> None:
    msgs = convo()
    out, _ = parse(edit(render(msgs), 1, "Looking, briefly."), msgs)
    assert not any(isinstance(p, ThinkingPart) for p in out[0].parts)
    out, _ = parse(edit(render(msgs), 3, "short"), msgs)
    assert isinstance(out[0].parts[0], ThinkingPart)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda t: t.replace("[[CTX_BLOCK 2 ", "[[CTX_BLOCK 99 "), "there is no block 99"),
        (lambda t: t + "[[CTX_BLOCK 2 kind=tool_call]]\n{}\n", "appears twice"),
        (lambda t: edit(t, 2, "not json"), "must stay a JSON object"),
        (lambda t: edit(t, 7, "   "), "would be empty"),
        (
            lambda t: edit(drop(t, 3), 2, '{"name": "z"}'),
            "a tool call and its result are kept or deleted together",
        ),
        (lambda t: drop(drop(t, 1), 7), "must end with a user or tool message"),
    ],
)
def test_invalid_edits_are_refused_with_a_reason(change: Any, reason: str) -> None:
    msgs = convo()
    with pytest.raises(PartMirrorError, match=reason):
        parse(change(render(msgs)), msgs)


def test_reordering_is_refused() -> None:
    msgs = convo()
    t = render(msgs)
    b6 = t[t.index("[[CTX_BLOCK 6 ") : t.index("[[CTX_BLOCK 7 ")]
    with pytest.raises(PartMirrorError, match="out of order"):
        parse(b6 + t.replace(b6, ""), msgs)


def test_the_instructions_state_the_format_with_a_generic_example() -> None:
    text = instructions(PaperSettings(version=PAPER_V3))
    assert "[[CTX_BLOCK n kind=...]]" in text and "[[CTX_BLOCK note]]" in text
    assert "kind=tool_result tool=lookup" in text and "Summary:" in text  # the example
    assert "docthread" not in text and "journal" not in text  # nothing from the task


# ---------------------------------------------------------------------- sessions through the real runner


SUMMARISE = r"""python3 - <<'EOF'
import re
path = "/tmp/.live_ctx/LIVE_CTX_MAIN.txt"
parts = re.split(r"(?m)^(\[\[CTX_BLOCK [^\n]*\]\])$", open(path).read())
out = [parts[0]]
for i in range(1, len(parts), 2):
    head, body = parts[i], parts[i + 1]
    if "kind=tool_result" in head and "tool=read_journal" in head:
        body = "\nSummary: one journal page read; its details are no longer needed.\n\n"
    out.append(head + body)
open(path, "w").write("".join(out))
EOF"""
DROP_FIRST_PAIR = r"""python3 - <<'EOF'
import re
path = "/tmp/.live_ctx/LIVE_CTX_MAIN.txt"
text = open(path).read()
parts = re.split(r"(?m)^(\[\[CTX_BLOCK [^\n]*\]\])$", text)
heads = [parts[i] for i in range(1, len(parts), 2)]
first = next(h for h in heads if "kind=tool_call" in h)
out = [parts[0]]
for i in range(1, len(parts), 2):
    if parts[i] != first:
        out.append(parts[i] + parts[i + 1])
out.append("[[CTX_BLOCK note]]\nNote: the first page was routine.\n")
open(path, "w").write("".join(out))
EOF"""
BAD = r"""sed -i 's/^\[\[CTX_BLOCK 2 /[[CTX_BLOCK 999 /' /tmp/.live_ctx/LIVE_CTX_MAIN.txt"""


def tool_use(cid: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": cid, "name": name, "input": args}


class Provider:
    """An in-memory Anthropic endpoint answering from a script; it records every request body."""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script, self.bodies = list(script), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        content = self.script.pop(0)
        stop = "tool_use" if any(c["type"] == "tool_use" for c in content) else "end_turn"
        return httpx.Response(200, json={
            "id": f"msg_{len(self.bodies)}", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": len(request.content) // 4, "output_tokens": 50,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})  # fmt: skip


def session(tmp: Path, provider: Provider, **settings: Any) -> ArmSession:
    fx = instance("docthread-dev-1")
    model = live_model(ModelConfig(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
                       api_key="offline-placeholder")  # fmt: skip
    return ArmSession("paper_clm_shell", model, run_dir=tmp / "run", run_id="v3", ledger=Ledger.open(tmp / "l.json", 50.0),
                      price=PRICE, system_prompt=SYSTEM_PROMPT, task_tools=EvidenceService(fx).tools(),
                      model_settings=ModelConfig().model_settings(), conversation_id="v3", mode="live",
                      live_authorised=True, retry=ModelConfig().retry_policy(),
                      paper=PaperSettings(version=PAPER_V3, **settings))  # fmt: skip


def wire_problems(body: dict[str, Any]) -> list[str]:
    """Provider-level structure: roles alternate, and every tool_use is answered by a tool_result next."""
    msgs, out = body["messages"], []
    for i, m in enumerate(msgs):
        if i and m["role"] == msgs[i - 1]["role"]:
            out.append(f"two {m['role']} messages in a row at {i}")
        uses = [
            c["id"] for c in m["content"] if isinstance(c, dict) and c.get("type") == "tool_use"
        ]
        if uses:
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            got = [
                c["tool_use_id"]
                for c in (nxt or {}).get("content", [])
                if c.get("type") == "tool_result"
            ]
            if nxt is not None and sorted(uses) != sorted(got):
                out.append(f"tool_use {uses} answered by {got}")
    ids = {
        c["id"]
        for m in msgs
        for c in m["content"]
        if isinstance(c, dict) and c.get("type") == "tool_use"
    }
    for m in msgs:
        for c in m["content"]:
            if (
                isinstance(c, dict)
                and c.get("type") == "tool_result"
                and c["tool_use_id"] not in ids
            ):
                out.append(f"orphan tool_result {c['tool_use_id']}")
    return out


def text_of(body: dict[str, Any]) -> str:
    return json.dumps(body["messages"])


@pytest.mark.docker
def test_a_prose_summary_of_completed_exchanges_reaches_the_next_requests(tmp_path: Path) -> None:
    page = {"component": "moss-ocr", "hour": "11:00"}
    provider = Provider([
        [tool_use("r1", "read_journal", page | {"page": 1})],
        [tool_use("r2", "read_journal", page | {"page": 2})],
        [tool_use("b1", "bash", {"command": SUMMARISE})],
        [{"type": "text", "text": 'Done.\n```json\n{"rejecting_component": "moss-ocr", "first_rejection_time": "x"}\n```'}],
        [tool_use("b2", "bash", {"command": DROP_FIRST_PAIR})],
        [{"type": "text", "text": 'Second.\n```json\n{"faulty_change": "CHG-1"}\n```'}],
    ])  # fmt: skip
    s = session(tmp_path, provider)
    q = [user_message(x, TASK_V2) for x in questions(instance("docthread-dev-1"), TASK_V2)]
    assert s.run_turn(q[0]).ending == "completed"
    assert s.run_turn(q[1]).ending == "completed"
    after_edit = provider.bodies[3]  # the request after the summarising command
    results = [c for m in after_edit["messages"] for c in m["content"]
               if isinstance(c, dict) and c.get("type") == "tool_result"]  # fmt: skip
    by_id = {c["tool_use_id"]: json.dumps(c["content"]) for c in results}
    assert (
        "Summary: one journal page read" in by_id["r1"]
        and "Summary: one journal page read" in by_id["r2"]
    )
    assert "edit applied" in by_id["b1"]  # the running command's own result, with the receipt
    assert "seq    | time" not in text_of(
        after_edit
    )  # the page text itself is gone from the request
    # Turn 2 starts from the edited context; its own edit deletes the first pair and adds a note.
    turn2_first, after_edit2 = provider.bodies[4], provider.bodies[5]
    assert "Summary: one journal page read" in text_of(turn2_first)
    assert '"r1"' not in text_of(after_edit2) and "Note: the first page was routine." in text_of(
        after_edit2
    )
    for body in provider.bodies:  # every request the provider received is well formed
        assert wire_problems(body) == [], wire_problems(body)
    trace = [json.loads(line) for line in (tmp_path / "run/trace.jsonl").read_text().splitlines()]
    applied = [r for r in trace if r["type"] == "paper_edit_applied"]
    assert len(applied) == 2 and all(r["variant"] == PAPER_V3 for r in trace)


@pytest.mark.docker
def test_a_rejected_edit_changes_nothing_and_says_why(tmp_path: Path) -> None:
    page = {"component": "moss-ocr", "hour": "11:00", "page": 1}
    provider = Provider([
        [tool_use("r1", "read_journal", page)],
        [tool_use("b1", "bash", {"command": BAD})],
        [{"type": "text", "text": 'ok\n```json\n{"rejecting_component": "x", "first_rejection_time": "y"}\n```'}],
    ])  # fmt: skip
    s = session(tmp_path, provider)
    assert (
        s.run_turn(user_message(questions(instance("docthread-dev-1"), TASK_V2)[0], TASK_V2)).ending
        == "completed"
    )
    before, after = provider.bodies[1], provider.bodies[2]
    receipt = next(c for m in after["messages"] for c in m["content"]
                   if isinstance(c, dict) and c.get("type") == "tool_result" and c["tool_use_id"] == "b1")  # fmt: skip
    assert "edit rejected, nothing changed: there is no block 999" in json.dumps(receipt["content"])
    assert (
        after["messages"][: len(before["messages"])] == before["messages"]
    )  # the earlier context is unchanged
    assert wire_problems(after) == []


# ---------------------------------------------------------------------- rollback, feedback and the launcher


def unknown_block(text: str) -> str:
    return text.replace("[[CTX_BLOCK 1 ", "[[CTX_BLOCK 999 ", 1)


def small_v3(tmp: Path, script: list[Any], scripts: dict[str, Any], **settings: Any) -> Any:
    from .test_paper_shell import paper_agent

    agent, cap, seen = paper_agent(tmp, script, scripts,
                                   PaperSettings(**({"version": PAPER_V3, "budget_tokens": 4_048,
                                                    "reminder_before_tokens": 200} | settings)))  # fmt: skip
    records: list[dict[str, Any]] = []
    cap.on_record = records.append
    return agent, cap, seen, records


def test_a_rejected_edit_rolled_back_still_reports_its_reason_under_version_3(
    tmp_path: Path,
) -> None:
    import asyncio

    from .test_paper_shell import call

    big = "y" * 2_000
    script = [call("read", {"page": 1}, "r1"), call("read", {"page": 2}, "r2"), call("read", {"page": 3}, "r3"),
              call("bash", {"command": "bad"}, "b1"), ModelResponse(parts=[TextPart("answer")])]  # fmt: skip
    agent, _cap, seen, records = small_v3(tmp_path, script, {"bad": (unknown_block, big)})
    asyncio.run(agent.run("question"))
    [rollback] = [r for r in records if r["type"] == "paper_rollback"]
    assert rollback["units"] == 1 and rollback["carried_rejections"] == 1
    after = json.dumps([str(p) for m in seen[4] for p in m.parts])
    assert (
        "from the rolled-back step: edit rejected, nothing changed: there is no block 999" in after
    )
    assert big not in after and "b1" not in after


def test_steps_rebuilt_by_an_accepted_edit_stay_rollable_within_the_turn(tmp_path: Path) -> None:
    import asyncio

    from .test_paper_shell import call

    def summarise(text: str) -> str:
        return text.replace("page 2: " + "x" * 400, "Summary of page 2.")

    big = "y" * 2_600
    script = [call("read", {"page": 1}, "r1"), call("read", {"page": 2}, "r2"), call("bash", {"command": "sum"}, "b1"),
              call("bash", {"command": "big"}, "b2"), call("bash", {"command": "big"}, "b3"),
              ModelResponse(parts=[TextPart("answer")])]  # fmt: skip
    agent, cap, seen, records = small_v3(tmp_path, script, {"sum": (summarise, ""), "big": (None, big)},
                                         budget_tokens=3_700)  # fmt: skip
    asyncio.run(agent.run("question"))
    assert [r["type"] for r in records if r["type"] in ("paper_edit_applied", "paper_rollback")][:2] == [
        "paper_edit_applied", "paper_rollback"]  # fmt: skip
    assert any(
        "Summary of page 2." in json.dumps([str(p) for m in s for p in m.parts]) for s in seen[3:]
    )
    assert cap.state.rollbacks >= 1
    # one turn: every message, including those the accepted edit rebuilt, still counts as this turn's
    assert all(id(m) in cap.state.run_messages for m in cap.state.messages)


def test_eight_turns_run_through_the_launcher_with_version_3(tmp_path: Path) -> None:
    from clm_lib.p015 import launcher as L

    (tmp_path / "auth.json").write_text(json.dumps({"experiment": "015", "launch_id": "pv3",
                                                    "programme_ceiling_usd": 5.0, "block_stop_usd": 5.0,
                                                    "live": False}))  # fmt: skip
    spec = {"launch_id": "pv3", "instances": ["docthread-dev-1"], "arms": ["paper_clm_shell"], "questions": 8,
            "ledger": str(tmp_path / "l.json"), "ledger_ceiling_usd": 100.0, "task_version": TASK_V2,
            "authorisation": str(tmp_path / "auth.json"), "arm_settings": {"paper_clm_shell": {"version": PAPER_V3}},
            "scripted": {"policies": {"paper_clm_shell": "keep"}}}  # fmt: skip
    L.plan(spec, tmp_path / "L")
    assert L.run(tmp_path / "L") == L.EXIT_OK
    record = json.loads(
        (tmp_path / "L/runs/s01-docthread-dev-1-paper_clm_shell/r1/session.json").read_text()
    )
    assert record["launch_result"]["outcome"] == "complete" and record["score"]["points"] == 10
    assert (
        record["settings"]["variant"] == PAPER_V3
        and record["settings"]["mirror"] == "ctx-part-mirror/1"
    )
