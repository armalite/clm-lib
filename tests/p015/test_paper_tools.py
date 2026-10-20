"""`paper-clm-tools/1`: the structured-tools arm (`context_read`, `context_apply`). Its operations on synthetic
messages; whole sessions through the real `ArmSession` and Pydantic AI's Anthropic adapter over an in-memory HTTP
transport, so that the provider request bodies themselves are checked; pressure and recovery across turns; and the
launcher with export. No network; a placeholder key; temporary ledgers; synthetic responses."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from clm_lib.budget import Ledger
from clm_lib.p015.arms import ArmSession
from clm_lib.p015.docthread.fixture import instance
from clm_lib.p015.docthread.task import SYSTEM_PROMPT, TASK_V2, questions, user_message
from clm_lib.p015.docthread.tools import EvidenceService
from clm_lib.p015.live import ModelConfig, live_model
from clm_lib.p015.papertools import (
    ACCEPTED,
    EDIT_TOOL,
    NOTE,
    OVERFLOW,
    TOOLS_VERSION,
    EditRefused,
    PaperToolsContext,
    ToolsSettings,
    apply_edit,
    context_view,
    instructions,
    page_of,
    render_view,
    undo_latest,
)
from clm_lib.p015.session import ContinuationUnsupported
from clm_lib.tooling import ToolOutcome, ToolSpec

from .test_paper_parts import PRICE, text_of, tool_use, wire_problems


def convo() -> list[ModelMessage]:
    """A pinned first request, two completed tool exchanges, an answer, then a later question."""
    return [
        ModelRequest(parts=[SystemPromptPart("sys"), UserPromptPart("The task.")]),
        ModelResponse(parts=[TextPart("Looking."), ToolCallPart("lookup", {"name": "a"}, "c1")]),
        ModelRequest(parts=[ToolReturnPart("lookup", "a: " + "x" * 2000, "c1")]),
        ModelResponse(parts=[ToolCallPart("lookup", {"name": "b"}, "c2")]),
        ModelRequest(parts=[ToolReturnPart("lookup", "b: " + "y" * 2000, "c2")]),
        ModelResponse(parts=[TextPart("Answer one.")]),
        ModelRequest(parts=[UserPromptPart("Question two?")]),
    ]


# ---------------------------------------------------------------------- the block view and the operations


def test_blocks_are_message_parts_after_the_pinned_ones() -> None:
    view = context_view(convo())
    assert [(b.number, b.kind, b.label, b.call_id) for b in view] == [
        (1, "assistant_text", "assistant", None), (2, "tool_call", "tool call lookup", "c1"),
        (3, "tool_result", "tool result lookup", "c1"), (4, "tool_call", "tool call lookup", "c2"),
        (5, "tool_result", "tool result lookup", "c2"), (6, "assistant_text", "assistant", None),
        (7, "user", "user", None)]  # fmt: skip
    assert [b.current for b in view] == [False] * 5 + [True, True]
    text = render_view(view, 2, 3)
    assert text.startswith("Blocks 2-3 of 7.\n=== block 2 | tool call lookup ===\n")
    assert "The task." not in render_view(view) and "sys" not in render_view(
        view
    )  # pinned, not shown


def test_a_tool_result_replaced_by_a_summary_stays_the_native_result_of_its_call() -> None:
    msgs = convo()
    out = apply_edit(msgs, [{"op": "replace_block", "block": 3, "text": "Summary: a is fine."}],
                    in_place=False)  # fmt: skip
    result = out[2].parts[0]
    assert isinstance(result, ToolReturnPart) and result.content == "Summary: a is fine."
    assert (result.tool_call_id, result.tool_name) == ("c1", "lookup")
    assert out[1] is msgs[1] and out[3:-1] == msgs[3:-1]  # untouched messages are the same objects
    assert out[-1] is not msgs[-1] and msgs[-1].parts == out[-1].parts  # the final request: a copy


def test_deleting_one_half_of_a_pair_deletes_both_and_merges_the_neighbours() -> None:
    msgs = convo()
    out = apply_edit(msgs, [{"op": "delete", "blocks": [5]}], in_place=False)  # c2's result
    calls = [p.tool_call_id for m in out for p in m.parts if isinstance(p, ToolCallPart)]
    assert calls == ["c1"]
    # The emptied reply and request are gone; c1's result request and the answer stay in order.
    assert [type(m).__name__ for m in out] == ["ModelRequest", "ModelResponse", "ModelRequest",
                                               "ModelResponse", "ModelRequest"]  # fmt: skip
    assert out[3].parts[0].content == "Answer one."  # type: ignore[union-attr]


def test_replace_text_substitute_and_insert() -> None:
    msgs = convo()
    out = apply_edit(msgs, [
        {"op": "replace_text", "block": 1, "old": "Looking.", "new": "Checking a."},
        {"op": "substitute", "pattern": "x{10,}", "replacement": "[x run]", "blocks": [3]},
        {"op": "insert", "after": 3, "role": "notes", "text": "a looked fine."},
    ], in_place=False)  # fmt: skip
    assert out[1].parts[0].content == "Checking a."  # type: ignore[union-attr]
    request = out[2].parts
    assert isinstance(request[0], ToolReturnPart) and request[0].content == "a: [x run]"
    assert (
        isinstance(request[1], UserPromptPart) and request[1].content == "[notes]\na looked fine."
    )
    assert [b.label for b in context_view(out)][
        3
    ] == "notes"  # the role the model gave is the block's label


def test_a_whole_history_can_be_replaced_by_one_note() -> None:
    msgs = convo()
    out = apply_edit(msgs, [{"op": "delete", "blocks": [1, 2, 4, 6]},
                           {"op": "insert", "after": 0, "role": "notes", "text": "a and b looked up; answered."}],
                    in_place=False)  # fmt: skip
    assert len(out) == 1 and isinstance(out[0], ModelRequest)
    assert [type(p).__name__ for p in out[0].parts] == ["SystemPromptPart", "UserPromptPart", "UserPromptPart",
                                                        "UserPromptPart"]  # fmt: skip
    assert out[0].parts[2].content.startswith("[notes]")  # type: ignore[union-attr]


def test_an_accepted_set_changes_the_final_request_in_place_only_when_asked() -> None:
    msgs = convo()
    final = msgs[-1]
    ops = [{"op": "insert", "after": 7, "role": "notes", "text": "later"}]
    apply_edit(msgs, ops, in_place=False)
    assert len(final.parts) == 1
    out = apply_edit(msgs, ops, in_place=True)
    assert out[-1] is final and len(final.parts) == 2


@pytest.mark.parametrize(
    ("ops", "reason"),
    [
        ([], "The operation list is empty."),
        (
            [{"op": "replace_block", "block": 99, "text": "t"}],
            "Block 99 does not exist; the context has 7 blocks.",
        ),
        (
            [{"op": "replace_text", "block": 3, "old": "x", "new": "z"}],
            "must occur exactly once in block 3",
        ),
        (
            [{"op": "replace_block", "block": 3, "text": "  "}],
            "Block 3 would have no text left; delete it instead.",
        ),
        (
            [{"op": "replace_block", "block": 1, "text": "Looking."}],
            "The operations would change nothing.",
        ),
        ([{"op": "rewrite", "block": 1}], "Unknown operation 'rewrite'"),
        (
            [{"op": "replace_block", "block": 2, "text": "not json"}],
            "must remain a JSON object",
        ),
        (
            [{"op": "replace_block", "block": 3, "text": "t"}, {"op": "delete", "blocks": [2]}],
            "goes with a deleted call or result, so it cannot be changed",
        ),
        (
            [{"op": "delete", "blocks": [3]}, {"op": "replace_block", "block": 3, "text": "t"}],
            "cannot be both deleted and changed",
        ),
        (
            [{"op": "insert", "after": 1, "role": "two words", "text": "t"}],
            "A role is a short name",
        ),
        (
            [{"op": "substitute", "pattern": "(", "replacement": ""}],
            "not a valid regular expression",
        ),
        ([{"op": "delete", "blocks": [7]}], "Nothing would be left to send."),
    ],
)
def test_invalid_operation_sets_are_refused_with_a_reason_and_change_nothing(
    ops: list[dict[str, Any]], reason: str
) -> None:
    msgs = convo()
    before = [dataclasses.replace(m) for m in msgs]
    with pytest.raises(EditRefused, match=None) as err:
        apply_edit(msgs, ops, in_place=True)
    assert reason in str(err.value)
    assert msgs == before


def test_the_current_steps_calls_cannot_be_deleted() -> None:
    msgs = [*convo()[:-1], ModelResponse(parts=[ToolCallPart(EDIT_TOOL, {"ops": []}, "e1")]),
            ModelRequest(parts=[UserPromptPart(f"{NOTE} (results of this step)")])]  # fmt: skip
    with pytest.raises(EditRefused, match="is a tool call of the current step"):
        apply_edit(msgs, [{"op": "delete", "blocks": [7]}], in_place=False)


def test_paging_and_rollback() -> None:
    text = "".join(str(i % 10) for i in range(250))
    assert page_of(text, 1, 200).startswith("[page 1 of 3; continue with page=2]\n0123")
    assert page_of(text, 3, 200).startswith("[page 3 of 3]\n")
    assert page_of(text, 4, 200) == "There is no page 4; this view has 3."
    msgs = convo()[:5]  # ends with c2's result
    out, names = undo_latest(msgs) or ([], [])
    assert names == ['lookup({"name": "b"})']
    assert not any(
        isinstance(p, ToolCallPart) and p.tool_call_id == "c2" for m in out for p in m.parts
    )
    assert isinstance(out[-1], ModelRequest)
    assert undo_latest([convo()[0]]) is None  # nothing to undo


def test_the_instructions_describe_the_mechanism_and_settings_only() -> None:
    text = instructions(ToolsSettings())
    assert (
        "29,952 tokens" in text
        and "6 times in this conversation" in text
        and "8,000 characters" in text
    )
    for op in ("replace_text", "replace_block", "delete", "substitute", "insert"):
        assert f'"op": "{op}"' in text
    assert "summary" not in text.lower()  # no advice on what to keep
    assert "the turn ends" in instructions(ToolsSettings(max_retries=0))
    with pytest.raises(ValueError, match="unknown paper_clm_tools version"):
        ToolsSettings(version="paper-clm-tools/0")


def test_a_staged_edit_that_no_longer_applies_is_reported_and_changes_nothing() -> None:
    cap = PaperToolsContext()
    cap.state.staged = [{"op": "delete", "blocks": [42]}]
    msgs = convo()
    out, applied = cap.commit(msgs)
    assert not applied and out is msgs and len(msgs) == 7
    assert msgs[-1].parts[-1].content == (  # type: ignore[union-attr]
        f"{NOTE} Your staged edit did not take effect: Block 42 does not exist; the context has 7 blocks."
    )


# ---------------------------------------------------------------------- sessions over the provider wire


class Provider:
    """An in-memory Anthropic endpoint; each script entry is content, or a function of the request body."""

    def __init__(self, script: list[Any]) -> None:
        self.script, self.bodies = list(script), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        entry = self.script.pop(0)
        content = entry(body) if callable(entry) else entry
        stop = "tool_use" if any(c["type"] == "tool_use" for c in content) else "end_turn"
        return httpx.Response(200, json={
            "id": f"msg_{len(self.bodies)}", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": len(request.content) // 4, "output_tokens": 50,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})  # fmt: skip


def wire_session(tmp: Path, provider: Provider, **settings: Any) -> ArmSession:
    fx = instance("docthread-dev-1")
    model = live_model(ModelConfig(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
                       api_key="offline-placeholder")  # fmt: skip
    return ArmSession("paper_clm_tools", model, run_dir=tmp / "run", run_id="pt", ledger=Ledger.open(tmp / "l.json", 50.0),
                      price=PRICE, system_prompt=SYSTEM_PROMPT, task_tools=EvidenceService(fx).tools(),
                      model_settings=ModelConfig().model_settings(), conversation_id="pt", mode="live",
                      live_authorised=True, retry=ModelConfig().retry_policy(),
                      tools=ToolsSettings(**settings))  # fmt: skip


def results_by_id(body: dict[str, Any]) -> dict[str, str]:
    """Each tool_result's text, by call id."""

    def text(content: Any) -> str:
        return content if isinstance(content, str) else "".join(x.get("text", "") for x in content)

    return {c["tool_use_id"]: text(c["content"]) for m in body["messages"] for c in m["content"]
            if isinstance(c, dict) and c.get("type") == "tool_result"}  # fmt: skip


def uses(body: dict[str, Any]) -> list[str]:
    return [c["id"] for m in body["messages"] for c in m["content"]
            if isinstance(c, dict) and c.get("type") == "tool_use"]  # fmt: skip


def answer(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": f'{text}\n```json\n{{"rejecting_component": "x"}}\n```'}]


PAGE = {"component": "moss-ocr", "hour": "11:00"}
Q = [user_message(x, TASK_V2) for x in questions(instance("docthread-dev-1"), TASK_V2)]


def test_replacement_and_deletion_reach_the_next_requests_as_valid_provider_messages(
    tmp_path: Path,
) -> None:
    provider = Provider([
        [tool_use("r1", "read_journal", PAGE | {"page": 1})],
        [tool_use("r2", "read_journal", PAGE | {"page": 2})],
        # blocks: 1 call r1, 2 result r1, 3 call r2, 4 result r2, 5 this call
        [tool_use("a1", EDIT_TOOL, {"ops": [{"op": "replace_block", "block": 2, "text": "Summary: page 1 read."},
                                            {"op": "delete", "blocks": [3]}]})],
        answer("First."),
        # turn 2 blocks: 1 call r1, 2 result r1, 3 call a1, 4 receipt a1, 5 answer, 6 question, 7 this call
        [tool_use("a2", EDIT_TOOL, {"ops": [{"op": "delete", "blocks": [1]},
                                            {"op": "insert", "after": 5, "role": "notes",
                                             "text": "Page 1 was routine."}]})],
        answer("Second."),
    ])  # fmt: skip
    s = wire_session(tmp_path, provider)
    assert s.run_turn(Q[0]).ending == "completed"
    assert (s.capability.state.steps, s.capability.state.free_steps) == (
        3,
        1,
    )  # the accepted edit was free
    assert s.run_turn(Q[1]).ending == "completed"
    page_text = results_by_id(provider.bodies[1])["r1"]
    after = provider.bodies[3]  # the request after the first edit's step
    got = results_by_id(after)
    assert "Summary: page 1 read." in got["r1"] and page_text not in text_of(after)
    assert "r2" not in uses(after) and "r2" not in got  # the deleted pair is gone, call and result
    assert got["a1"].startswith(
        f"{ACCEPTED} 2 operation(s) on block(s) 2, 3; your context will be about"
    )
    assert "[context: about" in got["a1"]  # the readout on the receipt, in the request as sent
    assert "Summary: page 1 read." in text_of(
        provider.bodies[4]
    )  # turn 2 starts from the edited context
    final = provider.bodies[5]
    assert "r1" not in uses(final) and "r1" not in results_by_id(final)
    assert "[notes]\\nPage 1 was routine." in text_of(final)
    for body in provider.bodies:  # every request the provider received is well formed
        assert wire_problems(body) == [], wire_problems(body)
    trace = [json.loads(line) for line in (tmp_path / "run/trace.jsonl").read_text().splitlines()]
    commits = [r for r in trace if r["type"] == "tools_commit"]
    assert len(commits) == 2 and all(r["chars_after"] < r["chars_before"] for r in commits)
    assert all(r["variant"] == TOOLS_VERSION for r in trace)
    meta = json.loads((tmp_path / "run/session.json").read_text())
    assert (
        meta["framework_tools"] == ["context_read", EDIT_TOOL]
        and meta["settings"]["variant"] == TOOLS_VERSION
    )


def test_a_rejected_edit_preserves_the_context_and_is_a_task_step(tmp_path: Path) -> None:
    provider = Provider([
        [tool_use("r1", "read_journal", PAGE | {"page": 1})],
        [tool_use("a1", EDIT_TOOL, {"ops": [{"op": "replace_text", "block": 2, "old": "no such text", "new": "x"}]})],
        answer("Done."),
    ])  # fmt: skip
    s = wire_session(tmp_path, provider)
    assert s.run_turn(Q[0]).ending == "completed"
    before, after = provider.bodies[1], provider.bodies[2]
    assert after["messages"][: len(before["messages"])] == before["messages"]  # nothing changed
    assert results_by_id(after)["a1"].startswith(
        "REJECTED: The text to replace must occur exactly once in block 2. Your context is unchanged."
    )
    assert (s.capability.state.steps, s.capability.state.free_steps) == (3, 0)
    assert wire_problems(after) == []


def test_a_second_edit_in_one_step_is_refused_and_the_step_is_not_free(tmp_path: Path) -> None:
    provider = Provider([
        [tool_use("r1", "read_journal", PAGE | {"page": 1})],
        [tool_use("a1", EDIT_TOOL, {"ops": [{"op": "replace_block", "block": 2, "text": "Short."}]}),
         tool_use("a2", EDIT_TOOL, {"ops": [{"op": "insert", "after": 0, "role": "notes", "text": "n"}]})],
        answer("Done."),
    ])  # fmt: skip
    s = wire_session(tmp_path, provider)
    assert s.run_turn(Q[0]).ending == "completed"
    got = results_by_id(provider.bodies[2])
    receipts = sorted(v[:8] for k, v in got.items() if k in ("a1", "a2"))
    assert receipts == ["Accepted", "REJECTED"]
    assert any("already has a staged edit" in v for v in got.values())
    assert (s.capability.state.steps, s.capability.state.free_steps) == (3, 0)
    assert wire_problems(provider.bodies[2]) == []


def test_context_read_pages_the_current_view(tmp_path: Path) -> None:
    provider = Provider([
        [tool_use("r1", "read_journal", PAGE | {"page": 1})],
        [tool_use("v1", "context_read", {"from_block": 2, "to_block": 2})],
        answer("Done."),
    ])  # fmt: skip
    s = wire_session(tmp_path, provider, max_output_chars=600)
    assert s.run_turn(Q[0]).ending == "completed"
    shown = results_by_id(provider.bodies[2])["v1"]
    assert shown.startswith("[page 1 of ") and "=== block 2 | tool result read_journal ===" in shown
    assert s.capability.state.steps == 3  # a read is a task step


# ---------------------------------------------------------------------- pressure and recovery across turns


LOOKUP = ToolSpec("lookup", "Returns one record.", {"type": "object", "properties": {"key": {"type": "string"}},
                                                    "required": ["key"]}, "task", "none")  # fmt: skip


class Lookup:
    spec = LOOKUP

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(f"record {arguments['key']}: " + "z" * 5_000)


class Pressured:
    """Five lookups per turn; after a rollback note, one edit deleting every earlier call; on a final-turn note, the
    answer."""

    def __init__(self) -> None:
        self.turn, self.done, self.seen = 0, 0, []  # type: ignore[var-annotated]

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        self.seen.append(list(messages))
        notes = " ".join(str(p.content) for p in messages[-1].parts
                         if isinstance(p, UserPromptPart) and str(p.content).startswith(NOTE))  # fmt: skip
        questions_seen = sum(1 for m in messages for p in m.parts if isinstance(p, UserPromptPart)
                             and str(p.content).startswith("Q"))  # fmt: skip
        if questions_seen != self.turn:
            self.turn, self.done = questions_seen, 0
        if "no retries left" in notes:
            return ModelResponse(parts=[TextPart(f"final answer {self.turn}")])
        if OVERFLOW in notes:
            calls = [b.number for b in context_view(messages) if b.kind == "tool_call"]
            return ModelResponse(parts=[ToolCallPart(EDIT_TOOL, {"ops": [{"op": "delete", "blocks": calls}]},
                                                     f"e{self.turn}")])  # fmt: skip
        if self.done < 5:
            self.done += 1
            return ModelResponse(parts=[ToolCallPart("lookup", {"key": f"t{self.turn}k{self.done}"},
                                                     f"t{self.turn}k{self.done}")])  # fmt: skip
        return ModelResponse(parts=[TextPart(f"answer {self.turn}")])


def scripted_session(
    tmp: Path, respond: Callable[..., ModelResponse], **settings: Any
) -> ArmSession:
    return ArmSession("paper_clm_tools", FunctionModel(respond), run_dir=tmp / "run", run_id="pr",
                      ledger=Ledger.open(tmp / "l.json", 50.0), price=PRICE, system_prompt="sys",
                      task_tools=[Lookup()], conversation_id="pr", tools=ToolsSettings(**settings))  # fmt: skip


def test_pressure_rolls_back_to_the_margin_then_recovers_then_gets_a_final_turn_then_ends(
    tmp_path: Path,
) -> None:
    script = Pressured()
    s = scripted_session(tmp_path, script.respond, budget_tokens=8_048, max_retries=1)
    limit = s.capability.settings.limit
    endings = [s.run_turn(f"Q{n}: what is in the records?").ending for n in (1, 2, 3)]
    assert endings == ["completed", "completed", "context_limit"]
    trace = [json.loads(line) for line in (tmp_path / "run/trace.jsonl").read_text().splitlines()]
    rollbacks = [r for r in trace if r["type"] == "tools_rollback"]
    assert [(r["retry"], r["final_turn"]) for r in rollbacks] == [(1, False), (1, True)]
    assert all(
        r["tokens_after"] <= limit - 2_048 and r["steps_undone"] >= 1 for r in rollbacks
    )  # to the margin
    assert [r["type"] for r in trace if r["type"] in ("tools_commit", "context_limit")] == ["tools_commit",
                                                                                         "context_limit"]  # fmt: skip
    assert any(r["type"] == "tools_reminder" for r in trace)
    # Turn 1: the rollback note asks for room; the edit deletes every earlier call, and the next request shows it.
    flat = [" ".join(str(p) for m in seen for p in m.parts) for seen in script.seen]
    note_at = next(i for i, t in enumerate(flat) if "retry 1 of 1" in t)
    assert "t1k1" in flat[note_at] and "t1k1" not in flat[note_at + 1]
    assert OVERFLOW in flat[note_at + 1]  # the rollback note is pinned: the deletion kept it
    # Turn 2: the retries are used, so the model is told this is its final turn, and it answers.
    assert any("There are no retries left: this is your last turn" in t for t in flat)
    # Every request sent stayed within the limit.
    sent = [r["estimated_tokens"] for r in trace if r["type"] == "tools_request"]
    assert sent and max(sent) <= limit
    turns = [json.loads(line) for line in (tmp_path / "run/turns.jsonl").read_text().splitlines()]
    assert (
        turns[-1]["counters"]["final_turn"] is True and turns[-1]["counters"]["retries_used"] == 1
    )
    assert (
        turns[0]["counters"]["free_steps"] == 1
        and turns[0]["counters"]["edits_committed_total"] == 1
    )


def test_with_no_retries_the_first_over_limit_request_ends_the_turn(tmp_path: Path) -> None:
    script = Pressured()
    s = scripted_session(tmp_path, script.respond, budget_tokens=8_048, max_retries=0)
    assert s.run_turn("Q1: what is in the records?").ending == "context_limit"
    with pytest.raises(ContinuationUnsupported, match="not part of paper-clm-tools/1"):
        s.continue_after_limit()


def test_the_step_limit_counts_task_steps_and_gives_the_finalize_notice(tmp_path: Path) -> None:
    script = Pressured()
    s = scripted_session(tmp_path, script.respond, max_steps=4)
    assert s.run_turn("Q1: what is in the records?").ending == "step_limit"
    last = " ".join(
        str(p.content) for p in script.seen[-1][-1].parts if isinstance(p, UserPromptPart)
    )
    assert f"{NOTE} 1 step(s) remain: finish now with your final answer." in last


# ---------------------------------------------------------------------- the launcher, accounting and export


def tools_spec(tmp: Path, launch_id: str, **settings: Any) -> dict[str, Any]:
    (tmp / "auth.json").write_text(json.dumps({"experiment": "015", "launch_id": launch_id,
                                               "programme_ceiling_usd": 5.0, "block_stop_usd": 5.0,
                                               "live": False}))  # fmt: skip
    return {"launch_id": launch_id, "instances": ["docthread-dev-1"], "arms": ["paper_clm_tools"], "questions": 8,
            "ledger": str(tmp / "l.json"), "ledger_ceiling_usd": 100.0, "task_version": TASK_V2,
            "authorisation": str(tmp / "auth.json"), "arm_settings": {"paper_clm_tools": settings}}  # fmt: skip


@pytest.mark.parametrize("policy", ["tools_keep", "tools_tidy"])
def test_eight_turns_run_through_the_launcher_and_export(tmp_path: Path, policy: str) -> None:
    from clm_lib.p015 import exposure
    from clm_lib.p015 import launcher as L
    from clm_lib.p015.export import verify_export

    spec = tools_spec(tmp_path, "pt", version=TOOLS_VERSION, budget_tokens=12_000)
    spec["scripted"] = {"policies": {"paper_clm_tools": policy}}
    L.plan(spec, tmp_path / "L")
    assert L.run(tmp_path / "L") == L.EXIT_OK
    run = tmp_path / "L/runs/s01-docthread-dev-1-paper_clm_tools/r1"
    record = json.loads((run / "session.json").read_text())
    assert record["launch_result"]["outcome"] == "complete" and record["score"]["points"] == 10
    assert record["settings"]["variant"] == TOOLS_VERSION and record["settings"]["limit"] == 9_952
    trace = [json.loads(line) for line in (run / "trace.jsonl").read_text().splitlines()]
    assert (
        sum(r["type"] == "tools_commit" for r in trace) >= 1
    )  # the scripted investigator's edits applied
    attempts = [json.loads(line) for line in (run / "attempts.jsonl").read_text().splitlines()]
    finished = [a for a in attempts if a["event"] == "finished"]
    assert finished and all(a["category"] == "main" for a in finished)
    assert record["launch_result"]["attempts"] == len(finished)
    manifest = L.export_launch(tmp_path / "L", tmp_path / "export")
    [exported] = manifest["runs"]
    assert exported["verify_problems"] == [] and not exported["partial"]
    assert verify_export(tmp_path / "export/sessions" / exported["exported"]) == []
    rep = exposure.report(tmp_path / "export")
    [session] = rep["sessions"]
    management = session["management"]
    assert management["edits_committed"] == sum(r["type"] == "tools_commit" for r in trace) >= 1
    assert management["held_outputs"] is None


def test_a_live_plan_must_state_the_tools_settings_and_the_default_arm_set_is_unchanged(
    tmp_path: Path,
) -> None:
    from clm_lib.p015 import launcher as L

    spec = tools_spec(tmp_path, "pl")
    spec["mode"] = "live"
    problems = L.live_spec_problems(spec)
    for key in ("version", "budget_tokens", "max_retries", "readouts"):
        assert any(p.startswith(f"arm_settings.paper_clm_tools.{key} is not set") for p in problems)
    assert "paper_clm_tools" not in L.ARMS and "paper_clm_tools" in L.KNOWN_ARMS
    spec = tools_spec(tmp_path, "pc")
    spec["turn_endings"] = "p015-turn-endings/2"
    with pytest.raises(
        ValueError, match="not supported by paper_clm_tools: continuing after a limit-ended turn"
    ):
        L.plan(spec, tmp_path / "pc")


def test_the_proposed_live_validation_is_complete_unauthorised_and_rehearses_offline(
    tmp_path: Path,
) -> None:
    from clm_lib.p015 import launcher as L

    repo = Path(__file__).resolve().parents[2]
    spec = json.loads((repo / "configs/p015/proposed-tools-dev1.json").read_text())
    assert L.live_spec_problems(spec) == [] and L.version_problems(spec) == []
    assert spec["mode"] == "live" and spec["arms"] == ["paper_clm_tools"]
    assert spec["settings_status"]["authorisation"] == "not given"
    assert ToolsSettings(**spec["arm_settings"]["paper_clm_tools"]) == ToolsSettings(call_cap=144)
    # The same plan, scripted: it plans, runs eight turns and completes, with no provider and no real ledger.
    auth = {"experiment": "015", "launch_id": spec["launch_id"], "programme_ceiling_usd": 5.0, "block_stop_usd": 5.0,
            "live": False}  # fmt: skip
    (tmp_path / "auth.json").write_text(json.dumps(auth))
    rehearsal = spec | {"mode": "scripted", "ledger": str(tmp_path / "l.json"), "ledger_ceiling_usd": 100.0,
                        "authorisation": str(tmp_path / "auth.json")}  # fmt: skip
    L.plan(rehearsal, tmp_path / "L")
    assert L.run(tmp_path / "L") == L.EXIT_OK
    [row] = L.status(tmp_path / "L")["sessions"]
    assert row["outcome"] == "complete"
