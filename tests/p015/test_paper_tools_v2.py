"""`paper-clm-tools/2`: the thinking rule for rebuilt replies, regression-tested on the request shape the provider
refused in dev-tools1 (clm-lib-test-results 015 `dev-tools1-record.md`). Synthetic signed thinking; the real
`ArmSession` and Pydantic AI's Anthropic adapter over an in-memory HTTP transport, so the request bodies the adapter
emits are checked. No network; a placeholder key; temporary ledgers. Local tests cannot show that the provider
accepts these requests."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from clm_lib.p015.arm_errors import ArmRequestIncompatible
from clm_lib.p015.papertools import (
    EDIT_TOOL,
    TOOLS_V2,
    TOOLS_VERSION,
    PaperToolsContext,
    ThinkingRule,
    ToolsSettings,
    TurnReply,
    apply_edit,
    context_view,
    turn_problem,
)

from .test_paper_parts import text_of, tool_use, wire_problems
from .test_paper_tools import PAGE, Provider, Q, results_by_id, uses, wire_session


def think(sig: str) -> ThinkingPart:
    return ThinkingPart("", signature=sig, provider_name="anthropic")


def turn_rule(msgs: list[ModelMessage], *sigs: str) -> ThinkingRule:
    """The rule as the arm builds it: the identities of the thinking parts returned in the turn in progress."""
    return ThinkingRule(frozenset(id(p) for m in msgs for p in m.parts
                                  if isinstance(p, ThinkingPart) and p.signature in sigs))  # fmt: skip


def observed_shape() -> list[ModelMessage]:
    """dev-tools1's history before its second edit, in miniature: an earlier turn whose first reply holds thinking,
    text, thinking and a read, then two more reads with thinking, an answer, and the current turn's edit call."""
    return [
        ModelRequest(parts=[SystemPromptPart("sys"), UserPromptPart("Question one?")]),
        ModelResponse(
            parts=[
                think("sA"),
                TextPart("Looking."),
                think("sB"),
                ToolCallPart("read", {"p": 6}, "c6"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart("read", "page 6 " + "x" * 500, "c6")]),
        ModelResponse(parts=[think("sC"), ToolCallPart("read", {"p": 8}, "c8")]),
        ModelRequest(parts=[ToolReturnPart("read", "page 8 " + "y" * 500, "c8")]),
        ModelResponse(parts=[think("sD"), ToolCallPart("read", {"p": 9}, "c9")]),
        ModelRequest(parts=[ToolReturnPart("read", "page 9 " + "z" * 500, "c9")]),
        ModelResponse(parts=[TextPart("Answer one.")]),
        ModelRequest(parts=[UserPromptPart("Question two?")]),
        ModelResponse(parts=[think("sE"), ToolCallPart(EDIT_TOOL, {"ops": []}, "e1")]),
        ModelRequest(parts=[ToolReturnPart(EDIT_TOOL, "Accepted: ...", "e1")]),
    ]


def second_edit(msgs: list[ModelMessage]) -> list[dict[str, Any]]:
    """The second dev-tools1 edit: pages 6 and 8 (calls and results) and the thinking block between them."""
    view = context_view(msgs)
    want = [
        b.number
        for b in view
        if b.call_id in ("c6", "c8") or (b.kind == "other" and b.where == (3, 0))
    ]
    assert len(want) == 5
    return [{"op": "delete", "blocks": want}]


def thinking_of(message: ModelMessage) -> list[str | None]:
    return [p.signature for p in message.parts if isinstance(p, ThinkingPart)]


# ---------------------------------------------------------------------- the rule, on synthetic messages


def test_version_1_reproduces_the_refused_shape() -> None:
    msgs = observed_shape()
    out = apply_edit(msgs, second_edit(msgs), in_place=False)
    merged = out[1]
    assert [p.part_kind for p in merged.parts] == [
        "thinking",
        "text",
        "thinking",
        "thinking",
        "tool-call",
    ]
    # content.3 is reply 3's thinking, now directly after reply 1's: a sequence the model never generated
    assert thinking_of(merged) == ["sA", "sB", "sD"]


def test_version_2_omits_thinking_from_the_merged_earlier_reply_only() -> None:
    msgs = observed_shape()
    rule = turn_rule(msgs, "sE")
    out = apply_edit(msgs, second_edit(msgs), in_place=False, thinking=rule)
    merged = out[1]
    assert [p.part_kind for p in merged.parts] == ["text", "tool-call"] and rule.omitted == 3
    calls = [p.tool_call_id for m in out for p in m.parts if isinstance(p, ToolCallPart)]
    assert calls == ["c9", "e1"]  # the deletion still takes effect
    assert (
        out[3] is msgs[7] and out[5] is msgs[9]
    )  # replies echoed verbatim keep their objects, thinking included
    assert thinking_of(out[5]) == ["sE"]


def test_a_verbatim_earlier_reply_keeps_its_thinking() -> None:
    msgs = observed_shape()
    rule = turn_rule(msgs, "sE")
    note = [
        {"op": "insert", "after": 13, "role": "notes", "text": "pages 6 to 9 read"}
    ]  # touches no reply
    out = apply_edit(msgs, note, in_place=False, thinking=rule)
    assert rule.omitted == 0 and [thinking_of(m) for m in out if isinstance(m, ModelResponse)][:3] == [
        ["sA", "sB"], ["sC"], ["sD"]]  # fmt: skip


def test_an_altered_earlier_reply_loses_its_thinking_without_a_merge() -> None:
    msgs = observed_shape()
    rule = turn_rule(msgs, "sE")
    text = next(b.number for b in context_view(msgs) if b.text == "Looking.")
    out = apply_edit(msgs, [{"op": "replace_block", "block": text, "text": "Reading."}], in_place=False,
                     thinking=rule)  # fmt: skip
    assert thinking_of(out[1]) == [] and rule.omitted == 2 and thinking_of(out[3]) == ["sC"]


def test_a_reply_holding_thinking_of_the_turn_in_progress_keeps_it() -> None:
    """Within a tool-use turn thinking must be passed back; the rule does not strip it there (a live uncertainty)."""
    msgs = observed_shape()
    rule = turn_rule(msgs, "sA", "sB", "sC", "sD", "sE")  # as if all of it were this turn's
    out = apply_edit(msgs, second_edit(msgs), in_place=False, thinking=rule)
    assert thinking_of(out[1]) == ["sA", "sB", "sD"] and rule.omitted == 0


def test_deleting_a_read_between_replies_merges_them_and_omits_both_thinking_blocks() -> None:
    msgs = observed_shape()
    rule = turn_rule(msgs, "sE")
    ops = [{"op": "delete", "blocks": [b.number for b in context_view(msgs) if b.call_id == "c8"]}]
    out = apply_edit(msgs, ops, in_place=False, thinking=rule)
    # reply 3 keeps only sC and its result request empties, so replies 3 and 5 merge: [sC, sD, call c9]
    assert [p.part_kind for p in out[3].parts] == ["tool-call"] and rule.omitted == 2
    assert thinking_of(out[1]) == ["sA", "sB"]  # reply 1 is untouched and keeps its thinking
    assert alternates(out)


def test_a_reply_left_with_only_thinking_is_dropped_and_its_requests_merge() -> None:
    msgs = observed_shape()
    msgs[4] = ModelRequest(
        parts=[ToolReturnPart("read", "page 8", "c8"), UserPromptPart("[notes]\nkeep")]
    )
    rule = turn_rule(msgs, "sE")
    ops = [{"op": "delete", "blocks": [b.number for b in context_view(msgs) if b.call_id == "c8"]}]
    out = apply_edit(msgs, ops, in_place=False, thinking=rule)
    assert rule.omitted == 1 and not any("sC" in thinking_of(m) for m in out)
    merged = out[2]  # page 6's result and the note, now one request
    assert [p.part_kind for p in merged.parts] == ["tool-return", "user-prompt"]
    assert alternates(out)


def alternates(messages: list[ModelMessage]) -> bool:
    return all(
        isinstance(m, ModelRequest) != isinstance(n, ModelRequest)
        for m, n in itertools.pairwise(messages)
    )


def test_the_version_is_explicit_and_version_1_is_the_default() -> None:
    assert ToolsSettings().version == TOOLS_VERSION == "paper-clm-tools/1"
    assert ToolsSettings(version=TOOLS_V2).version == "paper-clm-tools/2"
    with pytest.raises(ValueError, match="unknown paper_clm_tools version"):
        ToolsSettings(version="paper-clm-tools/3")


# ---------------------------------------------------------------------- the request the adapter emits


def thinking_block(sig: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": sig}


def answer(text: str, *sigs: str) -> list[dict[str, Any]]:
    return [
        *map(thinking_block, sigs),
        {"type": "text", "text": f'{text}\n```json\n{{"rejecting_component": "x"}}\n```'},
    ]


def script() -> list[Any]:
    # Turn 1: the observed reply shapes. Turn 2: the second dev-tools1 edit, then an answer. Turn 3: an answer.
    # Turn 2's blocks: 1 sA, 2 text, 3 sB, 4 call r6, 5 result r6, 6 sC, 7 call r8, 8 result r8, 9 sD, 10 call r9,
    # 11 result r9, 12 answer, 13 question two, 14 sE, 15 this call.
    return [
        [thinking_block("sigA"), {"type": "text", "text": "Looking."}, thinking_block("sigB"),
         tool_use("r6", "read_journal", PAGE | {"page": 6})],
        [thinking_block("sigC"), tool_use("r8", "read_journal", PAGE | {"page": 8})],
        [thinking_block("sigD"), tool_use("r9", "read_journal", PAGE | {"page": 9})],
        answer("First."),
        [thinking_block("sigE"), tool_use("e1", EDIT_TOOL, {"ops": [{"op": "delete", "blocks": [4, 5, 6, 7, 8]}]})],
        answer("Second.", "sigF"),
        answer("Third."),
    ]  # fmt: skip


def assistant_turns(body: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return [m["content"] for m in body["messages"] if m["role"] == "assistant"]


def thinking_problems(body: dict[str, Any], originals: list[list[dict[str, Any]]]) -> list[str]:
    """Every assistant message that carries thinking must be one provider response's content, as returned."""
    out = []
    for i, content in enumerate(assistant_turns(body)):
        sigs = [c["signature"] for c in content if c.get("type") == "thinking"]
        if not sigs:
            continue
        source = [o for o in originals if any(c.get("signature") == sigs[0] for c in o)]
        if len(source) != 1 or [c.get("type") for c in content] != [c["type"] for c in source[0]]:
            out.append(
                f"assistant message {i} carries thinking but is not a response as returned: {sigs}"
            )
        elif [c.get("signature") for c in content if c.get("type") == "thinking"] != [
                c["signature"] for c in source[0] if c["type"] == "thinking"]:  # fmt: skip
            out.append(
                f"assistant message {i}: thinking differs from the response that produced it"
            )
    return out


def run(tmp: Path, version: str) -> Provider:
    provider = Provider(script())
    s = wire_session(tmp, provider, version=version)
    for q in Q[:3]:
        assert s.run_turn(q).ending == "completed"
    return provider


def test_version_1_emits_the_request_shape_the_provider_refused(tmp_path: Path) -> None:
    provider = run(tmp_path, TOOLS_VERSION)
    after = provider.bodies[5]  # the request carrying the committed deletion
    first = assistant_turns(after)[0]
    assert [c["type"] for c in first] == ["thinking", "text", "thinking", "thinking", "tool_use"]
    assert (
        first[3]["signature"] == "sigD"
    )  # messages.1.content.3: another response's thinking, merged in
    assert thinking_problems(after, script()) != []


def test_version_2_emits_valid_requests_through_a_later_turn(tmp_path: Path) -> None:
    provider = run(tmp_path, TOOLS_V2)
    originals = script()
    before, after, later = provider.bodies[4], provider.bodies[5], provider.bodies[6]
    # the intended deletion takes effect: pages 6 and 8, calls and results, are gone from the next request
    assert {"r6", "r8"} <= set(uses(before)) and not {"r6", "r8"} & set(uses(after))
    assert not {"r6", "r8"} & set(results_by_id(after))
    page6 = results_by_id(before)["r6"].split("\n[context:")[0]
    assert page6 not in text_of(after)
    # the merged earlier reply is sent without thinking; the turn's own reply is sent as returned
    first = assistant_turns(after)[0]
    assert [c["type"] for c in first] == ["text", "tool_use"] and first[1]["id"] == "r9"
    current = next(c for c in assistant_turns(after) if any(b.get("id") == "e1" for b in c))
    assert current == originals[4]
    for body in provider.bodies[5:]:
        assert thinking_problems(body, originals) == [], thinking_problems(body, originals)
        assert wire_problems(body) == [], wire_problems(body)
    # the next turn rebuilds the same history: the earlier reply still without thinking, the others as returned
    assert assistant_turns(later)[0] == first
    assert originals[5] in [c for c in assistant_turns(later)]  # turn 2's answer, with its thinking
    trace = [json.loads(line) for line in (tmp_path / "run/trace.jsonl").read_text().splitlines()]
    [commit] = [r for r in trace if r["type"] == "tools_commit"]
    assert commit["thinking_omitted"] == 3 and all(r["variant"] == TOOLS_V2 for r in trace)
    meta = json.loads((tmp_path / "run/session.json").read_text())
    assert meta["settings"]["variant"] == TOOLS_V2


# ---------------------------------------------------------------------- the turn in progress


def turn_replies(msgs: list[ModelMessage], *sigs: str) -> list[TurnReply]:
    return [TurnReply(None, tuple(m.parts)) for m in msgs
            if isinstance(m, ModelResponse) and any(p.signature in sigs for p in m.parts if isinstance(p, ThinkingPart))]  # fmt: skip


def current_turn_shape() -> list[ModelMessage]:
    """An earlier turn, then the turn in progress: two reads with thinking, then the edit call."""
    return [
        ModelRequest(parts=[SystemPromptPart("sys"), UserPromptPart("Question one?")]),
        ModelResponse(parts=[TextPart("Answer one.")]),
        ModelRequest(parts=[UserPromptPart("Question two?")]),
        ModelResponse(parts=[think("t2"), ToolCallPart("read", {"p": 2}, "r2")]),
        ModelRequest(parts=[ToolReturnPart("read", "page 2 " + "x" * 500, "r2")]),
        ModelResponse(parts=[think("t3"), ToolCallPart("read", {"p": 3}, "r3")]),
        ModelRequest(parts=[ToolReturnPart("read", "page 3 " + "y" * 500, "r3")]),
        ModelResponse(parts=[think("t4"), ToolCallPart(EDIT_TOOL, {"ops": []}, "e1")]),
        ModelRequest(parts=[UserPromptPart("[context manager] (results of this step)")]),
    ]


def test_turn_problem_requires_each_thinking_reply_of_the_turn_as_returned() -> None:
    msgs = current_turn_shape()
    replies = turn_replies(msgs, "t2", "t3", "t4")
    assert turn_problem(msgs, replies) is None
    view = context_view(msgs)
    r2 = [b.number for b in view if b.call_id == "r2"]
    merged = apply_edit(msgs, [{"op": "delete", "blocks": r2}], in_place=False, thinking=turn_rule(msgs, "t2", "t3", "t4"))  # fmt: skip
    assert turn_problem(merged, replies) == [0, 1]  # t2's reply altered and merged into t3's
    summary = [{"op": "replace_block", "block": next(b.number for b in view if b.kind == "tool_result" and b.call_id == "r2"),
                "text": "Summary: page 2 read."}]  # fmt: skip
    assert (
        turn_problem(apply_edit(msgs, summary, in_place=False), replies) is None
    )  # results may change
    undone = [m for m in msgs if m is not msgs[7]]  # the newest reply removed: a trailing run
    assert turn_problem(undone, replies, rollback=True) is None and turn_problem(
        undone, replies
    ) == [2]
    gap = [m for i, m in enumerate(msgs) if i not in (3, 4)]  # an earlier reply removed: a gap
    assert turn_problem(gap, replies, rollback=True) == [0]


def tools_cap(msgs: list[ModelMessage], *sigs: str) -> PaperToolsContext:
    cap = PaperToolsContext(ToolsSettings(version=TOOLS_V2))
    cap.state.turn_replies = turn_replies(msgs, *sigs)
    return cap


def test_an_incompatible_staged_set_is_refused_at_the_commit_too() -> None:
    msgs = current_turn_shape()
    cap = tools_cap(msgs, "t2", "t3", "t4")
    cap.state.staged = [
        {"op": "delete", "blocks": [b.number for b in context_view(msgs) if b.call_id == "r2"]}
    ]
    before = [list(m.parts) for m in msgs]
    out, applied = cap.commit(msgs)
    assert not applied and out is msgs and [list(m.parts) for m in msgs][:-1] == before[:-1]
    note = msgs[-1].parts[-1].content  # type: ignore[union-attr]
    assert note.startswith(
        "[context manager] Your staged edit did not take effect: This would change a reply"
    )


def test_rollback_that_would_break_a_thinking_reply_stops_before_dispatch() -> None:
    msgs = current_turn_shape()
    cap = tools_cap(msgs, "t2", "t3", "t4")
    msgs[3] = ModelResponse(
        parts=[*msgs[3].parts, TextPart("extra")]
    )  # a reply no longer as returned
    with pytest.raises(ArmRequestIncompatible, match="nothing was dispatched"):
        cap.recover(msgs, 0)
    from clm_lib.p015 import launcher as L

    assert L.classify_outcome(["completed", "request_incompatible"], 8) == "harness_defect"


# ---------------------------------------------------------------------- the turn in progress, over the wire


def current_turn_script() -> list[Any]:
    # Turn 2 blocks: 1 t1, 2 text, 3 call r1, 4 result r1, 5 answer, 6 question two, 7 t2, 8 call r2, 9 result r2,
    # 10 t3, 11 call r3, 12 result r3, 13 t4, 14 call e1, 15 result e1, 16 t5, 17 call e2.
    # Turn 3 blocks: ... 18 result e2, 19 t6, 20 answer, 21 question three, 22 t7, 23 call e3.
    return [
        [thinking_block("t1"), {"type": "text", "text": "Looking."}, tool_use("r1", "read_journal", PAGE | {"page": 1})],
        answer("First."),
        [thinking_block("t2"), tool_use("r2", "read_journal", PAGE | {"page": 2})],
        [thinking_block("t3"), tool_use("r3", "read_journal", PAGE | {"page": 3})],
        [thinking_block("t4"), tool_use("e1", EDIT_TOOL, {"ops": [{"op": "delete", "blocks": [8]}]})],
        [thinking_block("t5"), tool_use("e2", EDIT_TOOL, {"ops": [
            {"op": "replace_block", "block": 12, "text": "Summary: page 3 read, no rejections."}]})],
        answer("Second.", "t6"),
        [thinking_block("t7"), tool_use("e3", EDIT_TOOL, {"ops": [{"op": "delete", "blocks": [8]}]})],
        answer("Third."),
    ]  # fmt: skip


def test_the_turn_in_progress_refuses_a_merge_allows_a_summary_and_the_next_turn_deletes(
    tmp_path: Path,
) -> None:
    provider = Provider(current_turn_script())
    s = wire_session(tmp_path, provider, version=TOOLS_V2)
    originals = current_turn_script()
    assert s.run_turn(Q[0]).ending == "completed"
    assert s.run_turn(Q[1]).ending == "completed"
    turn2 = s.capability.state
    assert (turn2.steps, turn2.free_steps) == (
        4,
        1,
    )  # the refused edit is a task step; the accepted one is free
    assert s.run_turn(Q[2]).ending == "completed"
    b = provider.bodies
    # 1. The current-turn deletion is refused at staging: nothing changes, and the reason says what to do instead.
    refused = results_by_id(b[5])["e1"]
    assert refused.startswith(
        "REJECTED: This would change a reply you made earlier in this turn (blocks 7, 8, 10, 11)."
    )
    assert "replace_block with a summary" in refused and refused.split("\n[context:")[0].endswith(
        "unchanged."
    )
    assert b[5]["messages"][: len(b[4]["messages"])] == b[4]["messages"]
    # 2. A useful reduction in the same turn is accepted: the page-3 result becomes a summary, replies untouched.
    page3 = results_by_id(b[4])["r3"].split("\n[context:")[0]
    assert results_by_id(b[6])[
        "r3"
    ] == "Summary: page 3 read, no rejections." and page3 not in text_of(b[6])
    # 3. In the next turn the same deletion is an earlier-turn edit and is applied, without the merged thinking.
    after = b[8]
    assert "r2" not in uses(after) and "r2" not in results_by_id(after)
    sigs = [
        c.get("signature") for m in assistant_turns(after) for c in m if c.get("type") == "thinking"
    ]
    # replies t2 and t3 merge once read 2 is gone, so both lose their thinking (earlier turn); the rest are intact
    assert not {"t2", "t3"} & set(sigs) and {"t1", "t4", "t5", "t6", "t7"} <= set(sigs)
    for i, body in enumerate(
        b
    ):  # every request sent keeps each thinking reply as returned, and pairs every call
        assert thinking_problems(body, originals) == [], (i, thinking_problems(body, originals))
        assert wire_problems(body) == [], (i, wire_problems(body))
    trace = [json.loads(line) for line in (tmp_path / "run/trace.jsonl").read_text().splitlines()]
    assert [r.get("compatibility") for r in trace if r["type"] == "tools_edit" and r["status"] == "rejected"] == [
        "current_turn_thinking"]  # fmt: skip
    assert [r["thinking_omitted"] for r in trace if r["type"] == "tools_commit"] == [0, 2]
    assert not [r for r in trace if r["type"] == "request_incompatible"]


def rollback_script() -> list[Any]:
    return [
        [thinking_block("u1"), tool_use("p1", "read_journal", PAGE | {"page": 1})],
        [thinking_block("u2"), tool_use("p2", "read_journal", PAGE | {"page": 2})],
        [thinking_block("u3"), tool_use("p3", "read_journal", PAGE | {"page": 3})],
        answer("After the rollback.", "u4"),
        answer("Next turn."),
    ]


def test_rollback_in_the_turn_removes_only_the_newest_replies_and_later_requests_stay_valid(
    tmp_path: Path,
) -> None:
    # A first run with room measures the request sizes; the second puts the limit just under the request that
    # carries page 3, so that request is rolled back (reserve 200: the margin and the reminder point).
    probe = Provider(rollback_script())
    wire_session(tmp_path / "probe", probe, version=TOOLS_V2).run_turn(Q[0])
    sizes = [json.loads(x)["estimated_tokens"] for x in (tmp_path / "probe/run/trace.jsonl").read_text().splitlines()
             if json.loads(x)["type"] == "tools_request"]  # fmt: skip
    provider = Provider(rollback_script())
    s = wire_session(
        tmp_path / "live",
        provider,
        version=TOOLS_V2,
        budget_tokens=sizes[3] - 1 + 200,
        reserve_tokens=200,
    )
    assert s.run_turn(Q[0]).ending == "completed" and s.run_turn(Q[1]).ending == "completed"
    trace = [json.loads(x) for x in (tmp_path / "live/run/trace.jsonl").read_text().splitlines()]
    [rollback] = [r for r in trace if r["type"] == "tools_rollback"]
    assert rollback["steps_undone"] >= 1 and not [
        r for r in trace if r["type"] == "request_incompatible"
    ]
    sent = provider.bodies[3]  # the request after the rollback
    assert "p3" not in uses(sent) and "Context limit" in text_of(sent)
    for body in provider.bodies:
        assert thinking_problems(body, rollback_script()) == [] and wire_problems(body) == []
