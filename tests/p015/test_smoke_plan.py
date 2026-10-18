"""The proposed first live development smoke (`configs/p015/smoke-dev1.json`): complete, pinned to the accepted
library, with exactly the chosen settings; it plans offline into a temporary directory and is refused before any
session without an authorisation. Network blocked (conftest); temporary ledger; no provider request is made."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from clm_lib.budget import Ledger
from clm_lib.p015 import launcher as L
from clm_lib.p015.docthread.fixture import DEV_INSTANCES
from clm_lib.p015.live import ModelConfig
from clm_lib.p015.paper.capability import PaperSettings
from clm_lib.p015.session import GuardedSettings
from clm_lib.p015.summary.capability import SummarySettings

REPO = Path(__file__).resolve().parents[2]
SMOKE = json.loads((REPO / "configs/p015/smoke-dev1.json").read_text())


def test_the_smoke_spec_is_complete_pinned_and_small() -> None:
    assert L.live_spec_problems(SMOKE) == [] and L.library_pin_problems(SMOKE) == []
    assert SMOKE["mode"] == "live" and SMOKE["questions"] == 8
    assert SMOKE["instances"] == ["docthread-dev-1"] and set(SMOKE["instances"]) <= set(
        DEV_INSTANCES
    )
    assert L.balanced_order(SMOKE["instances"], SMOKE["arms"]) == [
        ("docthread-dev-1", "guarded_clm"), ("docthread-dev-1", "paper_clm_shell"),
        ("docthread-dev-1", "token_tail_p015")]  # fmt: skip
    assert SMOKE["library"]["guarded_clm"]["revision"] == "3f184734362c4c9e24d7f5e2a2d6de5fa18153dd"


def test_the_chosen_settings_are_exactly_those_proposed() -> None:
    model = ModelConfig(**SMOKE["model"])
    assert model.model_settings() == {
        "max_tokens": 8192, "anthropic_effort": "high", "anthropic_cache": "5m",
        "anthropic_cache_tool_definitions": "5m", "anthropic_cache_instructions": "5m",
    }  # fmt: skip
    assert (
        model.model == "claude-sonnet-5" and model.thinking == "adaptive" and model.max_retries == 2
    )
    s = SMOKE["arm_settings"]
    guarded = GuardedSettings(**s["guarded_clm"])
    assert (guarded.budget_tokens, guarded.max_steps, guarded.cap) == (32_000, 60, 144)
    paper = PaperSettings(**s["paper_clm_shell"])
    assert (paper.limit, paper.max_steps, paper.cap) == (29_952, 60, 144)
    assert (paper.profile, paper.reminders, paper.rollback, paper.rollback_limit) == (
        "paper",
        "single",
        "last-step",
        6,
    )
    summary = SummarySettings(**s["token_tail_p015"])
    assert (summary.budget_tokens, summary.hard, summary.max_steps, summary.cap, summary.summary_call_cap) == (
        32_000, 31_000, 60, 144, 24)  # fmt: skip


def test_the_smoke_plans_offline_and_is_refused_without_authorisation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    Ledger.open(
        tmp_path / "ledger.json", 100.0
    )  # a temporary ledger; the active one is never touched
    spec = copy.deepcopy(SMOKE) | {"ledger": str(tmp_path / "ledger.json"),
                                   "authorisation": str(tmp_path / "missing-authorisation.json")}  # fmt: skip
    L.plan(spec, tmp_path / "L")
    contract = json.loads((tmp_path / "L/contract.json").read_text())
    for key in (
        "model",
        "arm_settings",
        "paper_rules",
        "retry_policy",
        "library",
        "questions",
    ):
        assert contract[key] == spec[key], key
    assert [s["session_id"] for s in contract["sessions"]] == [
        "s01-docthread-dev-1-guarded_clm", "s02-docthread-dev-1-paper_clm_shell",
        "s03-docthread-dev-1-token_tail_p015"]  # fmt: skip
    assert L.run(tmp_path / "L") == L.EXIT_SPEND  # no authorisation: refused before any session
    assert not list((tmp_path / "L/runs").iterdir())
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["entries"] == [] and ledger["pending"] == []
