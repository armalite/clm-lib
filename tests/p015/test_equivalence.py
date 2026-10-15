"""The same synthetic conversations, run directly through guarded-clm (A) and through GuardedSession (B), must give
identical transcripts: every request the model saw (messages, instructions, tool definitions, settings, usage),
every capability trace record, and every turn's output, history or error."""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

import pytest

from clm_lib.budget import Ledger

from . import harness as h

EXPECTED_ENDINGS = {
    "read_and_answer_scripted": ["completed"],
    "read_and_answer": ["completed"],
    "edits": ["completed", "completed"],
    "multiple_calls": ["completed"],
    "guard_burst": ["completed"],
    "pressure_rearm_reset": ["completed", "completed"],
    "answers_retained": ["completed", "completed", "completed"],
    "step_limit": ["step_limit"],
    "call_cap": ["call_cap"],
    "tool_failure": [
        "unclassified_error"
    ],  # a framework (unbridged) tool raised; see test_tools for bridged ones
    "provider_failure": ["provider_failure"],
}


def run_both(name: str, tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], Any]:
    case = h.CASES[name]
    direct = h.run_direct(case)
    session_run, session = h.run_session(
        case, tmp_path / "run", Ledger.open(tmp_path / "ledger.json", 100.0)
    )
    return direct, session_run, session


@pytest.mark.parametrize("name", sorted(h.CASES))
def test_session_matches_direct_library(name: str, tmp_path: Path) -> None:
    direct, via_session, _ = run_both(name, tmp_path)
    assert direct == h.without_endings(via_session)
    assert [r["ending"] for r in via_session["runs"]] == EXPECTED_ENDINGS[name]


def kinds(transcript: dict[str, Any]) -> collections.Counter[str]:
    return collections.Counter(r["type"] for r in transcript["records"])


def status_lines(transcript: dict[str, Any], call: int) -> list[str]:
    parts = transcript["calls"][call]["messages"][0]["parts"]
    prompt = next(p["content"] for p in parts if p.get("part_kind") == "user-prompt")
    return prompt.split("<runtime_status>\n")[1].split("\n</runtime_status>")[0].splitlines()


def test_the_cases_exercise_what_they_claim(tmp_path: Path) -> None:
    """Guards against a vacuous comparison: each case shows its behaviour in the (identical) transcripts."""
    t = {name: h.run_direct(case) for name, case in h.CASES.items()}
    edits = [r for r in t["edits"]["records"] if r["type"] == "lite_edit"]
    assert [r["status"] for r in edits] == [
        "accepted",
        "accepted",
        "unchanged_write",
        "rejected",
        "accepted",
        "rejected",
        "rejected",
    ]
    assert kinds(t["edits"])["lite_protected_refused"] == 2
    assert kinds(t["multiple_calls"])["lite_parallel_calls"] == 2
    g = kinds(t["guard_burst"])
    assert g["lite_held_back"] >= 1 and g["lite_refetch"] >= 1 and g["lite_edit"] == 1
    p = [
        r["type"]
        for r in t["pressure_rearm_reset"]["records"]
        if r["type"] in ("lite_over_limit", "context_limit")
    ]
    assert (
        p == ["lite_over_limit"] * 3
    )  # twice in turn 1 (re-armed in between), once at turn 2's start (reset)
    assert any(line.startswith("PRESSURE") for line in status_lines(t["pressure_rearm_reset"], 3))
    last = t["answers_retained"]["calls"][-1]["messages"][0]["parts"]
    prompt = next(p["content"] for p in last if p.get("part_kind") == "user-prompt")
    assert (
        '"id": "t1.answer"' in prompt
        and '"id": "t2.answer"' in prompt
        and '"id": "t2.request"' in prompt
    )
    assert t["step_limit"]["runs"][-1]["error"].startswith("StepLimitError")
    assert t["call_cap"]["runs"][-1]["error"].startswith("UsageLimitExceeded")
    assert t["tool_failure"]["runs"][-1]["error"] == "RuntimeError: pager backend unavailable"
    assert t["provider_failure"]["runs"][-1]["error"].startswith("ModelHTTPError: status_code: 503")
    # The scripted path uses fixed-ratio estimates; the provider-like path calibrates on the reported usage.
    assert t["read_and_answer_scripted"]["calls"][0]["reported_input_tokens"] is None
    assert t["read_and_answer"]["calls"][0]["reported_input_tokens"] > 0
    assert (
        status_lines(t["read_and_answer_scripted"], 1)[1]
        != status_lines(t["read_and_answer"], 1)[1]
    )


def test_session_state_persists_across_turns(tmp_path: Path) -> None:
    _, via_session, session = run_both("answers_retained", tmp_path)
    assert session.turns == 3 and session.history is not None
    # One agent and one conversation for the session; each turn's model-visible history is the previous result's.
    turn3 = via_session["runs"][2]["all_messages"]
    assert (
        sum(1 for m in turn3 if m["kind"] == "request") == 1
    )  # a single rebuilt request, as the library leaves it
