"""session-counters/1 and accounted native dispatch (programme 015), scripted providers only.

Ledgers here are temporary test ledgers under tmp_path; the active ledger is never opened.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clm_lib.budget import BudgetExhausted, Ledger, PriceUnavailable
from clm_lib.counters import CALL_DEFINITIONS, PROPOSED_LIMITS, SessionCounters
from clm_lib.native_call import (
    AccountingBoundViolated,
    CallFailed,
    NativeAccounting,
    dispatch,
    native_input_token_bound,
)
from clm_lib.provider import ProviderError
from clm_lib.tooling import ToolOutcome, ToolRegistry, ToolSpec
from clm_lib.transport import (
    AnthropicNativeProvider,
    NativeRequest,
    ScriptedNativeProvider,
    interpret,
    not_executed_result,
    outcome_result,
    results_message,
    validate_history,
)

from .conftest import TEST_PRICE

SEARCH = ToolSpec(
    "search",
    "Search.",
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "task",
    "none",
)
APPLY = ToolSpec(
    "context_apply",
    "Edit the context.",
    {"type": "object", "properties": {"edit": {"type": "string"}}, "required": ["edit"]},
    "context",
    "context",
)
REG = ToolRegistry((SEARCH, APPLY))
USAGE = {
    "input_tokens": 100,
    "cache_creation_input_tokens": 40,
    "cache_read_input_tokens": 900,
    "output_tokens": 50,
    "cache_creation_5m": 40,
    "cache_creation_1h": 0,
}


def req(purpose: str = "main", messages: list[dict[str, Any]] | None = None) -> NativeRequest:
    msgs = messages or [{"role": "user", "content": "task"}]
    return NativeRequest(("system",), tuple(msgs), REG.to_api(), 512, purpose)  # type: ignore[arg-type]


def step(
    content: list[dict[str, Any]], stop: str, usage: dict[str, int] | None = None
) -> dict[str, Any]:
    return {"content": content, "stop_reason": stop, "usage": usage or USAGE}


def ledger(tmp_path: Path, ceiling: float = 5.0) -> Ledger:
    return Ledger.open(tmp_path / "ledger.json", ceiling)


def run(
    provider: Any,
    led: Ledger,
    acc: NativeAccounting,
    counters: SessionCounters,
    r: NativeRequest,
    **kw: Any,
) -> Any:
    return dispatch(
        provider,
        r,
        ledger=led,
        price=TEST_PRICE,
        run_id="t",
        accounting=acc,
        counters=counters,
        sleep=lambda s: None,
        **kw,
    )


# ------------------------------------------------------------- counters
def test_counter_increments_are_explicit() -> None:
    c = SessionCounters()
    with pytest.raises(ValueError, match="before the first user turn"):
        c.step("task")
    c.user_turn()
    c.attempt("main", 0, ok=False, final=False)  # failed first try, retried
    c.attempt("main", 1, ok=True, final=True)
    c.response("tool_calls", {"task": 2, "context": 1})
    c.tool_executed("task", ok=True)
    c.tool_executed("task", ok=False)  # ran, but the tool returned an error
    c.tool_not_executed("context", "policy:one_call_per_response")
    c.step("task")
    c.step("editing")
    c.edit("accepted")
    c.edit("rejected")
    c.edit("unchanged")
    c.user_turn()
    c.attempt("summary", 0, ok=True, final=True)
    c.attempt("main", 0, ok=False, final=True)  # failed with no retry: a failed call
    t = c.to_dict()["totals"]
    assert t["user_turns"] == 2
    assert (t["main_calls"], t["main_attempts"], t["main_retries"], t["main_failed_calls"]) == (
        2,
        3,
        1,
        1,
    )
    assert (t["summary_calls"], t["summary_attempts"], t["summary_retries"]) == (1, 1, 0)
    assert (
        t["tool_calls_requested"],
        t["tool_calls_executed"],
        t["tool_calls_failed"],
        t["tool_calls_not_executed"],
    ) == (3, 2, 1, 1)
    assert (t["task_steps"], t["editing_steps"]) == (1, 1)
    assert (t["edits_accepted"], t["edits_rejected"], t["edits_unchanged"], t["edit_attempts"]) == (
        1,
        1,
        1,
        3,
    )
    assert c.get("main_attempts", turn=1) == 2 and c.get("main_attempts", turn=2) == 1
    assert c.get("summary_calls", turn=1) == 0
    assert c.call_totals() == {
        "main_calls": 2,
        "main_attempts": 3,
        "main_and_summary_calls": 3,
        "all_attempts": 4,
    }
    assert c.consistency_problems() == []
    assert c.to_dict()["tool_calls_not_executed_by_reason"] == {"policy:one_call_per_response": 1}
    c.response("tool_calls", {"task": 1})
    assert c.consistency_problems() == ["4 tool calls requested but 3 executed or declined"]
    with pytest.raises(ValueError):
        c.step("free")  # type: ignore[arg-type]


def test_proposed_limits_are_recorded_not_enforced() -> None:
    names = {p.name: p for p in PROPOSED_LIMITS}
    assert names["task_steps_per_user_turn"].value == 60
    assert names["session_call_cap"].value == 144 == 2 * 60 + 24
    assert all("not enforced" in p.status for p in PROPOSED_LIMITS)
    assert "PENDING" in names["session_call_cap"].counts
    assert set(CALL_DEFINITIONS) == {
        "main_calls",
        "main_attempts",
        "main_and_summary_calls",
        "all_attempts",
    }
    c = SessionCounters()
    c.user_turn()
    for _ in range(61):
        c.step("task")  # nothing stops at 60: enforcement is an arm decision for later
    assert c.get("task_steps") == 61


# ------------------------------------------------------------- dispatch and accounting
def test_tool_loop_round_trip_accounts_every_attempt(tmp_path: Path) -> None:
    calls = [
        {"type": "tool_use", "id": "t1", "name": "search", "input": {"query": "a"}},
        {"type": "tool_use", "id": "t2", "name": "context_apply", "input": {"edit": "x"}},
    ]
    p = ScriptedNativeProvider(
        [
            step([{"type": "thinking", "thinking": "", "signature": "s"}, *calls], "tool_use"),
            step([{"type": "text", "text": "done"}], "end_turn"),
        ]
    )
    led, acc, c = ledger(tmp_path), NativeAccounting(), SessionCounters()
    c.user_turn()
    history: list[dict[str, Any]] = [{"role": "user", "content": "task"}]
    r1 = run(p, led, acc, c, req(messages=history))
    it = interpret(r1, REG)
    c.response(it.kind, {"task": 1, "context": 1})
    results = [outcome_result(r1.tool_calls[0], ToolOutcome("2 hits"))]
    c.tool_executed("task", ok=True)
    results.append(not_executed_result(r1.tool_calls[1], "arm declines context edits here"))
    c.tool_not_executed("context", "policy")
    history += [r1.assistant_message(), results_message(r1.tool_calls, results)]
    assert validate_history(history) == []
    r2 = run(p, led, acc, c, req(messages=history))
    c.response(interpret(r2, REG).kind, {})
    # The second request carried the first response's blocks unchanged, thinking included.
    sent = p.payloads[1]["messages"]
    assert sent[1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "s"}
    assert [b["tool_use_id"] for b in sent[2]["content"]] == ["t1", "t2"]
    roles = acc.by_role()["main"]
    assert (roles["calls"], roles["attempts"], roles["retries"]) == (2, 2, 0)
    assert roles["input_tokens"] == 200 and roles["cache_read_input_tokens"] == 1800
    assert roles["cache_creation_input_tokens"] == 80 and roles["output_tokens"] == 100
    expected = 2 * (100 * 4 + 40 * 5 + 900 * 0.2 + 50 * 20) / 1e6
    assert roles["charged_usd"] == pytest.approx(expected, abs=1e-6)
    assert led.spent_usd == pytest.approx(expected, abs=1e-6)
    assert [e["kind"] for e in led.entries] == ["native:main", "native:main"]
    assert c.get("responses_tool_calls") == 1 and c.get("responses_completion") == 1
    assert c.consistency_problems() == []


def test_retries_are_separate_attempts_and_never_double_counted(tmp_path: Path) -> None:
    p = ScriptedNativeProvider(
        [step([{"type": "text", "text": "ok"}], "end_turn")],
        fail_with={
            0: ProviderError("rate_limit", "429", retryable=True, billing="none"),
            1: ProviderError("server", "500", retryable=True, billing="unknown"),
        },
    )
    led, acc, c = ledger(tmp_path), NativeAccounting(), SessionCounters()
    c.user_turn()
    seen: list[int] = []
    run(p, led, acc, c, req(), on_attempt=lambda a, payload, bound, res: seen.append(a))
    assert seen == [0, 1, 2]
    outcomes = [(a.attempt, a.outcome, a.retried, a.cost_basis) for a in acc.attempts]
    assert outcomes == [
        (0, "error:rate_limit", True, "provider_usage"),
        (1, "error:server", True, "reservation_assumed"),
        (2, "ok", False, "provider_usage"),
    ]
    m = acc.by_role()["main"]
    assert (m["calls"], m["attempts"], m["retries"], m["failed_attempts"]) == (1, 3, 2, 2)
    # Only the successful attempt returned usage; tokens are counted once.
    assert m["input_tokens"] == 100 and m["output_tokens"] == 50
    reserved_server = acc.attempts[1].reserved_usd
    assert acc.attempts[0].charged_usd == 0.0 and acc.attempts[1].charged_usd == reserved_server
    assert m["assumed_usd"] == pytest.approx(reserved_server)
    assert led.spent_usd == pytest.approx(sum(a.charged_usd for a in acc.attempts))
    assert len(led.entries) == 3 and not led._open
    t = c.to_dict()["totals"]
    assert (t["main_calls"], t["main_attempts"], t["main_retries"], t["main_failed_calls"]) == (
        1,
        3,
        2,
        0,
    )


def test_summaries_are_accounted_apart_from_main_calls(tmp_path: Path) -> None:
    p = ScriptedNativeProvider(
        [
            step([{"type": "text", "text": "main"}], "end_turn"),
            step([{"type": "text", "text": "summary"}], "end_turn"),
        ]
    )
    led, acc, c = ledger(tmp_path), NativeAccounting(), SessionCounters()
    c.user_turn()
    run(p, led, acc, c, req("main"))
    run(p, led, acc, c, req("summary"))
    roles = acc.by_role()
    assert roles["main"]["calls"] == 1 and roles["summary"]["calls"] == 1
    assert [e["kind"] for e in led.entries] == ["native:main", "native:summary"]
    assert c.get("main_calls") == 1 and c.get("summary_calls") == 1
    assert c.call_totals()["main_calls"] == 1 and c.call_totals()["all_attempts"] == 2


def test_exhausted_retries_and_non_retryable_errors_fail_explicitly(tmp_path: Path) -> None:
    err = ProviderError("timeout", "t", retryable=True, billing="unknown")
    p = ScriptedNativeProvider([], fail_with={0: err, 1: err, 2: err})
    led, acc, c = ledger(tmp_path), NativeAccounting(), SessionCounters()
    c.user_turn()
    with pytest.raises(CallFailed) as ei:
        run(p, led, acc, c, req())
    assert ei.value.attempts == 3 and ei.value.error.kind == "timeout"
    assert [a.retried for a in acc.attempts] == [True, True, False]
    assert c.get("main_failed_calls") == 1 and c.get("main_attempts") == 3
    bad = ScriptedNativeProvider(
        [], fail_with={0: ProviderError("bad_request", "400", retryable=False, billing="none")}
    )
    acc2 = NativeAccounting()
    with pytest.raises(CallFailed):
        run(bad, led, acc2, c, req())
    assert len(acc2.attempts) == 1 and acc2.attempts[0].charged_usd == 0.0


def test_refusal_is_settled_from_its_usage_and_not_retried(tmp_path: Path) -> None:
    p = ScriptedNativeProvider(
        [step([{"type": "text", "text": ""}], "refusal", {"input_tokens": 10, "output_tokens": 2})]
    )
    led, acc, c = ledger(tmp_path), NativeAccounting(), SessionCounters()
    c.user_turn()
    with pytest.raises(CallFailed) as ei:
        run(p, led, acc, c, req())
    assert ei.value.error.kind == "refusal"
    (a,) = acc.attempts
    assert a.outcome == "error:refusal" and a.cost_basis == "provider_usage"
    assert a.charged_usd == pytest.approx((10 * 4 + 2 * 20) / 1e6)
    assert a.usage is not None and a.usage["output_tokens"] == 2


def test_interruption_charges_the_full_reservation(tmp_path: Path) -> None:
    def boom(_: NativeRequest) -> dict[str, Any]:
        raise KeyboardInterrupt

    p = ScriptedNativeProvider([boom])
    led, acc, c = ledger(tmp_path), NativeAccounting(), SessionCounters()
    c.user_turn()
    with pytest.raises(KeyboardInterrupt):
        run(p, led, acc, c, req())
    (a,) = acc.attempts
    assert a.outcome == "interrupted:KeyboardInterrupt" and a.charged_usd == a.reserved_usd > 0
    assert led.entries[-1]["status"] == "interrupted:KeyboardInterrupt"


def test_budget_is_checked_before_dispatch(tmp_path: Path) -> None:
    p = ScriptedNativeProvider([step([{"type": "text", "text": "x"}], "end_turn")])
    led, acc, c = ledger(tmp_path, ceiling=0.000001), NativeAccounting(), SessionCounters()
    c.user_turn()
    with pytest.raises(BudgetExhausted):
        run(p, led, acc, c, req())
    assert p.requests == [] and acc.attempts == [] and led.entries == []


def test_bound_includes_tools_and_violations_stop(tmp_path: Path) -> None:
    r = req()
    with_tools = AnthropicNativeProvider("m").payload(r)
    without = dict(with_tools)
    without.pop("tools")
    assert native_input_token_bound(with_tools) > native_input_token_bound(without)
    huge = {"input_tokens": 10**6, "output_tokens": 1}
    p = ScriptedNativeProvider(
        [{**step([{"type": "text", "text": "x"}], "end_turn", huge), "usage_source": "provider"}]
    )
    led, acc, c = ledger(tmp_path), NativeAccounting(), SessionCounters()
    c.user_turn()
    with pytest.raises(AccountingBoundViolated):
        run(p, led, acc, c, r)
    assert acc.attempts[0].outcome == "ok" and led.entries[-1]["status"] == "ok"


def test_live_dispatch_without_a_price_is_blocked(tmp_path: Path) -> None:
    p = AnthropicNativeProvider("claude-sonnet-5")
    with pytest.raises(PriceUnavailable):
        dispatch(
            p, req(), ledger=ledger(tmp_path), price=None, run_id="t", accounting=NativeAccounting()
        )
    assert p._client is None  # no client was even constructed


def test_retry_that_cannot_be_reserved_ends_as_a_failed_call(tmp_path: Path) -> None:
    p = ScriptedNativeProvider(
        [], fail_with={0: ProviderError("server", "500", retryable=True, billing="unknown")}
    )
    led, acc, c = ledger(tmp_path), NativeAccounting(), SessionCounters()
    c.user_turn()
    first_reserve = TEST_PRICE.max_cost(native_input_token_bound(p.payload(req())), 512, None)
    # Room for exactly one attempt: the unknown-outcome failure is charged its reservation.
    led.limit_usd = first_reserve * 1.5
    with pytest.raises(BudgetExhausted):
        run(p, led, acc, c, req())
    assert len(p.requests) == 1 and len(acc.attempts) == 1
    assert acc.attempts[0].retried is False
    assert c.get("main_failed_calls") == 1 and c.get("main_attempts") == 1
    assert c.consistency_problems() == []
