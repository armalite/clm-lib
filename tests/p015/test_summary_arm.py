"""The summary arm (`token-tail-p015/1`): what is kept from clm-lib's `token-tail/1`, the summariser boundary and its
accounting, the request after a summary, the failure endings, and scripted docthread sessions (network blocked)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from clm_lib.baseline import TOKEN_TAIL, SummaryPolicy
from clm_lib.budget import Ledger, ModelPrice
from clm_lib.config import BaselineConfig, Limits
from clm_lib.context import Entry
from clm_lib.p015.accounting import AccountedModel, SpendRefused
from clm_lib.p015.docthread.arms_offline import ArmPlan, run
from clm_lib.p015.docthread.fixture import instance, mentions
from clm_lib.p015.export import export_session, verify_export
from clm_lib.p015.records import read_jsonl
from clm_lib.p015.summary.capability import (
    SUMMARY_SYSTEM_P015,
    SummarySettings,
    TokenTailSummary,
    summary_user,
)
from clm_lib.prompts import summary_user as historical_summary_user

PRICE = ModelPrice("scripted", 2.0, 2.5, 4.0, 0.2, 10.0, "offline placeholder", "offline")


def test_token_tail_parameters_and_summariser_layout_are_kept() -> None:
    s, bl, lim = SummarySettings(), BaselineConfig(), Limits()
    assert (s.pressure_ratio, s.recovery_reserve_tokens) == (
        lim.pressure_ratio,
        lim.recovery_reserve_tokens,
    )
    assert (s.tail_ratio, s.newest_ratio, s.target_ratio) == (
        bl.tail_ratio,
        bl.newest_ratio,
        bl.target_ratio,
    )
    assert (s.max_chars, s.min_chars, s.retries) == (
        bl.summary_max_chars,
        bl.summary_min_chars,
        bl.summary_retries,
    )
    b = s.budget_tokens
    assert s.policy() == SummaryPolicy(
        max_chars=3000, retries=1, policy=TOKEN_TAIL, tail_tokens=int(0.15 * b), newest_tokens=int(0.25 * b),
        target_tokens=int(0.5 * b), min_chars=600,
    )  # fmt: skip
    entries = [Entry("sum1", "summary", "earlier <facts>"), Entry("m1", "assistant", "call")]
    assert summary_user("the task", entries, 900) == historical_summary_user(
        "the task", entries, 900
    )
    assert "file:line" not in SUMMARY_SYSTEM_P015 and "local files" not in SUMMARY_SYSTEM_P015


@pytest.fixture(scope="module")
def sessions(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("summary")
    plans = {
        "ordinary": ArmPlan("token_tail_p015", policy="none"),
        "fixed": ArmPlan("token_tail_p015", "docthread-dev-2", "none", synthetic_usage=False),
        "faulty": ArmPlan("token_tail_p015", policy="none", label="stress", summary_faults={1: "too_long"}),
        "overflow": ArmPlan("token_tail_p015", policy="none", label="stress", summary_faults={1: "too_long", 2: "empty"}),
        "capped": ArmPlan("token_tail_p015", policy="none", label="stress", summary_faults={1: "too_long"},
                          summary=SummarySettings(summary_call_cap=1)),
        "summariser_failure": ArmPlan("token_tail_p015", policy="none", label="failure", fail_at_attempt=9),
        "refusal": ArmPlan("token_tail_p015", policy="none", refuse_after_turn=5, label="failure"),
    }  # fmt: skip
    return {k: (run(p, out, k), out / k) for k, p in plans.items()}


def test_sessions_complete_with_summaries(sessions: dict[str, Any]) -> None:
    for name in ("ordinary", "fixed"):
        result, _ = sessions[name]
        assert result["endings"] == ["completed"] * 8 and result["score"]["points"] == 10, name
        m = result["measurements"]
        assert m["summaries_applied"] >= 1 and m["pressure_requests"] >= 1, name
        assert m["peak_estimate_before_management"] >= 0.70 * 32_000 > m["peak_estimated_tokens"], (
            name
        )


def test_summariser_sees_only_agent_visible_material(sessions: dict[str, Any]) -> None:
    _, run_dir = sessions["ordinary"]
    fx = instance("docthread-dev-1")
    attempts = read_jsonl(run_dir / "attempts.jsonl")
    seen = ""
    for a in attempts:
        if a["event"] != "started":
            continue
        text = json.dumps(a["messages"], ensure_ascii=False)
        if a["category"] == "summary":
            parts = a["messages"][0]["parts"]
            assert parts[0]["content"] == SUMMARY_SYSTEM_P015 and len(a["messages"]) == 1
            assert a["parameters"]["function_tools"] == []  # no tools, no instructions
            user = parts[1]["content"]
            assert user.startswith("<task>\n") and "<transcript_to_summarise>" in user
            for (
                name,
                value,
            ) in fx.facts.items():  # every fact it is given was already in an agent request
                if mentions(user, value):
                    assert mentions(seen, value), name
        else:
            seen += text


def test_summary_replaces_older_history_in_the_next_request(sessions: dict[str, Any]) -> None:
    result, run_dir = sessions["ordinary"]
    attempts = read_jsonl(run_dir / "attempts.jsonl")
    trace = read_jsonl(run_dir / "trace.jsonl")
    applied = [r for r in trace if r["type"] == "summary_applied"]
    summaries = [a for a in attempts if a["event"] == "finished" and a["category"] == "summary"]
    main = [a for a in attempts if a["event"] == "started" and a["category"] == "main"]
    assert len(applied) == len(summaries) == 2
    for s in summaries:
        text = s["response"]["parts"][0]["content"]
        after = next(a for a in main if a["attempt"] > s["attempt"])
        before = [a for a in main if a["attempt"] < s["attempt"]][-1]
        first = after["messages"][0]["parts"]
        assert first[-1][
            "content"
        ] == f"<summary_of_earlier_work>\n{text}\n</summary_of_earlier_work>" or (
            first[-2]["content"] == f"<summary_of_earlier_work>\n{text}\n</summary_of_earlier_work>"
            and first[-1]["content"].startswith("The question you are working on now:")
        )

        def calls(a: dict[str, Any]) -> list[str]:
            return [
                p["tool_call_id"]
                for m in a["messages"]
                for p in m["parts"]
                if p["part_kind"] == "tool-call"
            ]

        dropped = [c for c in calls(before) if c not in calls(after)]
        assert dropped  # the summarised calls are gone ...
        assert all(
            c not in calls(a) for c in dropped for a in main if a["attempt"] > s["attempt"]
        )  # ... for good
        assert len(after["messages"]) < len(before["messages"])
    assert (
        result["measurements"]["refetches"] >= 1
    )  # summaries carry calls and answers, not tool-output facts


def test_summariser_calls_are_accounted_apart(sessions: dict[str, Any], tmp_path: Path) -> None:
    result, run_dir = sessions["ordinary"]
    attempts = read_jsonl(run_dir / "attempts.jsonl")
    finished = [a for a in attempts if a["event"] == "finished"]
    numbers = [a["attempt"] for a in finished]
    assert numbers == list(range(len(numbers)))  # one numbering across both wrappers
    by = result["measurements"]["attempts_by_category"]
    assert (
        by["summary"]["attempts"] == 2
        and by["main"]["attempts"] == result["measurements"]["requests"]
    )
    assert by["summary"]["input_tokens"] > 0 and by["summary"]["charged_usd"] > 0
    ledger = json.loads((run_dir.parent / "ordinary-ledger.json").read_text())
    kinds = {e["kind"] for e in ledger["entries"]}
    assert kinds == {"p015:main", "p015:summary"}
    turns = read_jsonl(run_dir / "turns.jsonl")
    assert sum(t["counters"]["summary_calls"] for t in turns) == 2
    assert all(
        t["counters"]["steps"] == t["counters"]["calls"] for t in turns
    )  # summaries are not steps
    dest = export_session(run_dir, tmp_path / "export")
    assert verify_export(dest) == []
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["arm"] == "token_tail_p015" and manifest["attempts"]["by_category"] == {
        "main": 38,
        "summary": 2,
    }
    assert manifest["attempts"]["charged_usd_by_basis"].keys() == {"provider_usage"}


def test_summary_faults_retry_overflow_and_cap(sessions: dict[str, Any]) -> None:
    faulty = sessions["faulty"][0]
    assert faulty["endings"] == ["completed"] * 8
    trace = read_jsonl(sessions["faulty"][1] / "trace.jsonl")
    first = [r for r in trace if r["type"] in ("summary_too_large", "summary_applied")][:2]
    assert [r["type"] for r in first] == ["summary_too_large", "summary_applied"]
    assert (
        first[1]["summary_attempt"] == 1
        and first[1]["requested_max_chars"] < first[0]["requested_max_chars"]
    )
    overflow = sessions["overflow"]
    assert overflow[0]["endings"] == ["context_limit"]
    kinds = [
        r["type"]
        for r in read_jsonl(overflow[1] / "trace.jsonl")
        if r["type"].startswith(("summary_", "context"))
    ]
    assert kinds[-3:] == ["summary_too_large", "summary_empty", "context_limit"]
    assert sessions["capped"][0]["endings"] == ["summary_call_cap"]


def test_summariser_failure_and_refusal_end_explicitly(sessions: dict[str, Any]) -> None:
    result, run_dir = sessions["summariser_failure"]
    assert result["endings"] == ["provider_failure"]
    last = [a for a in read_jsonl(run_dir / "attempts.jsonl") if a["event"] == "finished"][-1]
    assert last["category"] == "summary" and last["billing"] == "unknown"
    assert (
        last["charged_usd"] == last["reserved_usd"]
    )  # outcome unknown: charged at the reservation
    refusal = sessions["refusal"][0]
    assert refusal["endings"] == ["completed"] * 5 + ["spend_refused"]


def test_a_refused_summariser_call_ends_as_spend_refused(tmp_path: Path) -> None:
    def agent_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[ToolCallPart("big", {}, "c1")])

    ledger = Ledger.open(tmp_path / "ledger.json", 100.0)
    summariser = AccountedModel(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("s")])), ledger=ledger,
                                price=PRICE, run_id="t", mode="live", live_authorised=False)  # fmt: skip
    cap = TokenTailSummary(summariser, SummarySettings(budget_tokens=4_000))

    def big() -> str:
        return "x" * 4_000

    agent: Agent[None, str] = Agent(FunctionModel(agent_model), tools=[big], capabilities=[cap])
    with pytest.raises(SpendRefused) as exc:
        asyncio.run(agent.run("go"))
    assert exc.value.kind == "not_authorised"
