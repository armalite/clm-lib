"""`paper-clm-shell/2`: a rejected edit's reason is kept through the rollback of its step, as the README's "the
model is told why" requires; `paper-clm-shell/1` keeps its recorded behaviour. Synthetic data, a fake sandbox (no
command runs), scripted models; network blocked by conftest."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart

from clm_lib.p015.arm_errors import ArmContextLimit
from clm_lib.p015.paper.capability import PAPER_V2, PAPER_VERSION, PaperSettings

from .harness import dump
from .test_paper_shell import call, drop_pairs, ids, paper_agent

REASON = "a tool block must hold only result sections"
FEEDBACK = f"[context] from the rolled-back step: edit rejected, nothing changed: {REASON}."
BIG = "y" * 2_000  # a command output that takes the request over the small synthetic limit


def summarise_first_result(text: str) -> str:
    """The smoke-dev2 edit: a tool result replaced by a summary, without its `<<result id=…>>` header."""
    start = text.index("<<result id=r1 tool=read>>")
    end = text.index("<<end result>>", start)
    return text[:start] + "summary of page 1\n" + text[end:]


def run(
    tmp: Path, version: str, script: list[Any], scripts: dict[str, Any], **settings: Any
) -> tuple[list[list[ModelMessage]], list[dict[str, Any]], Any, BaseException | None]:
    settings = {"budget_tokens": 4_048, "reminder_before_tokens": 200, **settings}
    agent, cap, seen = paper_agent(tmp / version.replace("/", "-"), script, scripts,
                                   PaperSettings(version=version, **settings))  # fmt: skip
    records: list[dict[str, Any]] = []
    cap.on_record = records.append
    error = None
    try:
        asyncio.run(agent.run("question"))
    except ArmContextLimit as exc:
        error = exc
    return seen, records, cap, error


def text_of(request: list[ModelMessage]) -> str:
    return json.dumps([str(p) for m in request for p in m.parts])


def the_smoke_pattern() -> tuple[list[Any], dict[str, Any]]:
    script = [
        call("read", {"page": 1}, "r1"),
        call("read", {"page": 2}, "r2"),
        call("read", {"page": 3}, "r3"),
        call("bash", {"command": "bad"}, "b1"),  # malformed edit; its step is then over the limit
        call("bash", {"command": "good"}, "b2"),  # a correctly formatted edit
        ModelResponse(parts=[TextPart("answer")]),
    ]
    return script, {"bad": (summarise_first_result, BIG), "good": (drop_pairs, "")}


def requests(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r["type"] == "paper_request"]


def test_the_rejection_reason_survives_the_rollback_of_its_step(tmp_path: Path) -> None:
    script, scripts = the_smoke_pattern()
    seen, records, cap, error = run(tmp_path, PAPER_V2, script, scripts)
    assert error is None
    [rejected] = [r for r in records if r["type"] == "paper_edit_rejected"]
    assert rejected["reason"] == REASON
    [rollback] = [r for r in records if r["type"] == "paper_rollback"]
    assert rollback["units"] == 1 and rollback["carried_rejections"] == 1
    after = text_of(seen[4])  # the request after the rollback
    assert "your last 1 step(s) were rolled back" in after and FEEDBACK in after
    # Nothing else of the rolled-back step returns: not its call, its output or the edit's text.
    assert "b1" not in ids(seen[4]) and BIG not in after and "summary of page 1" not in after
    assert ids(seen[4]) == ["r1", "r2", "r3"]
    # A correctly formatted edit then fits and is accepted.
    applied = [r for r in records if r["type"] == "paper_edit_applied"]
    assert len(applied) == 1 and ids(seen[5]) == ["b2"] and cap.state.edits_accepted == 1
    # Given once: it is not kept, repeated or accumulated.
    assert sum(FEEDBACK in text_of(s) for s in seen) == 1
    assert cap.state.rejections == {} and FEEDBACK not in text_of(seen[5])
    assert all(r["variant"] == PAPER_V2 for r in records)
    assert (
        cap.describe()["variant"] == PAPER_V2 and cap.describe()["settings"]["version"] == PAPER_V2
    )


def test_version_1_keeps_its_recorded_behaviour_on_the_same_pattern(tmp_path: Path) -> None:
    script, scripts = the_smoke_pattern()
    seen, records, cap, _ = run(tmp_path, PAPER_VERSION, script, scripts)
    after = text_of(seen[4])
    assert (
        "your last 1 step(s) were rolled back" in after and "from the rolled-back step" not in after
    )
    [rollback] = [r for r in records if r["type"] == "paper_rollback"]
    assert "carried_rejections" not in rollback  # version 1's records are unchanged
    assert cap.state.rejections == {} and all(r["variant"] == PAPER_VERSION for r in records)
    assert PaperSettings().version == PAPER_VERSION  # still the default


def test_the_feedback_is_counted_in_the_next_requests_estimate(tmp_path: Path) -> None:
    script, scripts = the_smoke_pattern()
    _, v1, _, _ = run(tmp_path, PAPER_VERSION, script, scripts)
    _, v2, _, _ = run(tmp_path, PAPER_V2, *the_smoke_pattern())
    [r1], [r2] = ([r for r in v if r["type"] == "paper_rollback"] for v in (v1, v2))
    # The rollback measures the request with the feedback in it: its text (the scripted model's fixed 4
    # characters a token) plus the part's framing.
    added = r2["to_tokens"] - r1["to_tokens"]
    assert added >= len(FEEDBACK) / 4
    # The request actually sent carries it too (beside the rollback notice, in the same notice part).
    sent = requests(v2)[4]["estimated_tokens"] - requests(v1)[4]["estimated_tokens"]
    assert len(FEEDBACK) / 4 <= sent <= added


def test_the_feedback_is_subject_to_the_limit_like_any_other_content(tmp_path: Path) -> None:
    """When the rolled-back request fits only without the feedback, version 2 rolls back one step more (within the
    same allowance) rather than sending an over-limit request or dropping the reason."""
    script, scripts = the_smoke_pattern()
    _, v1, _, _ = run(tmp_path / "probe", PAPER_VERSION, script, scripts)
    [probe] = [r for r in v1 if r["type"] == "paper_rollback"]
    budget = 2_048 + probe["to_tokens"] + 2  # the limit sits just above the rolled-back request
    _, v1, _, _ = run(tmp_path, PAPER_VERSION, *the_smoke_pattern(), budget_tokens=budget)
    seen, v2, _, _ = run(tmp_path, PAPER_V2, *the_smoke_pattern(), budget_tokens=budget)
    [b1], [b2] = ([r for r in v if r["type"] == "paper_rollback"] for v in (v1, v2))
    assert b1["units"] == 1 and b2["units"] == 2 and b2["carried_rejections"] == 1
    assert b2["to_tokens"] <= budget - 2_048 and FEEDBACK in text_of(seen[4])
    assert ids(seen[4]) == ["r1", "r2"]  # the read before the edit went too; nothing was truncated


def test_a_rollback_without_a_rejected_edit_is_unchanged(tmp_path: Path) -> None:
    script = [
        call("read", {"page": 1}, "r1"),
        call("read", {"page": 2}, "r2"),
        call("bash", {"command": "big"}, "b1"),  # no edit: plain output over the limit
        ModelResponse(parts=[TextPart("answer")]),
    ]
    out = {}
    for version in (PAPER_VERSION, PAPER_V2):
        seen, records, _, _ = run(tmp_path, version, list(script), {"big": (None, BIG)})
        out[version] = (
            [dump(s) for s in seen],  # timestamps scrubbed
            [
                {k: v for k, v in r.items() if k not in ("variant", "carried_rejections")}
                for r in records
            ],
            [r.get("carried_rejections") for r in records if r["type"] == "paper_rollback"],
        )
    assert out[PAPER_VERSION][:2] == out[PAPER_V2][:2]  # the same requests and records
    assert out[PAPER_V2][2] == [0]


def test_rollback_allowance_and_the_terminal_rule_are_unchanged(tmp_path: Path) -> None:
    script = [
        call("read", {"page": 1}, "r1"),
        call("read", {"page": 2}, "r2"),
        call("read", {"page": 3}, "r3"),
        call("bash", {"command": "bad"}, "b1"),  # rolled back: the one rollback allowed
        call(
            "bash", {"command": "big"}, "b2"
        ),  # over the limit with none left: the final over-limit request
        call("bash", {"command": "big"}, "b3"),  # still over: the turn ends
        ModelResponse(parts=[TextPart("unreached")]),
    ]
    scripts = {"bad": (summarise_first_result, BIG), "big": (None, BIG)}
    summary = {}
    for version in (PAPER_VERSION, PAPER_V2):
        _, records, cap, error = run(tmp_path, version, list(script), scripts, max_rollbacks=1)
        assert isinstance(error, ArmContextLimit)
        summary[version] = (
            sum(r["units"] for r in records if r["type"] == "paper_rollback"),
            sum(1 for r in records if r["type"] == "paper_over_limit"),
            sum(1 for r in records if r["type"] == "context_limit"),
            cap.state.rollbacks,
        )
    assert summary[PAPER_VERSION] == summary[PAPER_V2] == (1, 1, 1, 1)


def test_an_unknown_version_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown paper arm version"):
        PaperSettings(version="paper-clm-shell/9")


def test_the_selected_version_reaches_the_contract_session_record_and_export(
    tmp_path: Path,
) -> None:
    from clm_lib.p015 import launcher as L
    from clm_lib.p015.export import export_session, verify_export

    (tmp_path / "auth.json").write_text(json.dumps({"experiment": "015", "launch_id": "pv2",
                                                    "programme_ceiling_usd": 5.0, "block_stop_usd": 5.0,
                                                    "live": False}))  # fmt: skip
    spec = {"launch_id": "pv2", "instances": ["docthread-dev-1"], "arms": ["paper_clm_shell"], "questions": 1,
            "ledger": str(tmp_path / "l.json"), "ledger_ceiling_usd": 100.0,
            "authorisation": str(tmp_path / "auth.json"),
            "arm_settings": {"paper_clm_shell": {"version": PAPER_V2}}}  # fmt: skip
    contract = L.plan(spec, tmp_path / "L")
    assert contract["arm_settings"]["paper_clm_shell"]["version"] == PAPER_V2
    assert L.run(tmp_path / "L") == L.EXIT_OK
    run_dir = tmp_path / "L/runs/s01-docthread-dev-1-paper_clm_shell/r1"
    session = json.loads((run_dir / "session.json").read_text())
    assert (
        session["settings"]["variant"] == PAPER_V2
        and session["settings"]["settings"]["version"] == PAPER_V2
    )
    dest = export_session(run_dir, tmp_path / "export")
    assert (
        verify_export(dest) == []
        and json.loads((dest / "session.json").read_text())["settings"]["variant"] == PAPER_V2
    )
