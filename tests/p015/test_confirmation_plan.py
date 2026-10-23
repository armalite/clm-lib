"""The proposed confirmation configuration (`configs/p015/proposed-confirmation.json`): complete, pinned, with the
smoke-dev2 shared settings, `paper-clm-shell/2`, the predeclared fresh instances and the balanced order. It is only
planned into a temporary directory; no fixture is generated and nothing is run. Network blocked (conftest)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clm_lib.budget import Ledger
from clm_lib.p015 import launcher as L
from clm_lib.p015.paper.capability import PaperSettings

from . import harness as h

REPO = Path(__file__).resolve().parents[2]
CONFIRM = json.loads((REPO / "configs/p015/proposed-confirmation.json").read_text())
SMOKE = json.loads((REPO / "configs/p015/smoke-dev2.json").read_text())


def test_the_confirmation_spec_is_complete_and_pinned() -> None:
    """confirm1 ran with guarded-clm 0.2.0.dev1 and the library's default request layout. The file is kept as it
    ran: it pins that library, so the launcher refuses it against any other, and it predates the explicit request
    options that new live plans state."""
    assert CONFIRM["library"]["guarded_clm"] == h.HISTORICAL_GUARDED_PIN
    assert L.live_spec_problems(CONFIRM) == h.GUARDED_REQUEST_UNSET
    assert L.library_pin_problems(CONFIRM) == h.pin_problems_against_loaded(
        h.HISTORICAL_GUARDED_PIN
    )
    assert L.version_problems(CONFIRM) == []
    assert (CONFIRM["task_version"], CONFIRM["turn_endings"]) == (
        "docthread-task/2",
        "p015-turn-endings/1",
    )
    assert (
        PaperSettings(**CONFIRM["arm_settings"]["paper_clm_shell"]).version == "paper-clm-shell/2"
    )


def test_only_the_identity_instances_and_paper_version_differ_from_smoke_dev2() -> None:
    changed = {k for k in CONFIRM.keys() | SMOKE.keys() if CONFIRM.get(k) != SMOKE.get(k)}
    assert changed == {
        "note",
        "launch_id",
        "authorisation",
        "instances",
        "arm_settings",
        "settings_status",
    }
    paper = dict(CONFIRM["arm_settings"]["paper_clm_shell"])
    assert paper.pop("version") == "paper-clm-shell/2"
    assert {**CONFIRM["arm_settings"], "paper_clm_shell": paper} == SMOKE["arm_settings"]
    for key in ("model", "retry_policy", "paper_rules", "library", "task_version", "turn_endings", "questions",
                "arms", "ledger"):  # fmt: skip
        assert CONFIRM[key] == SMOKE[key], key


def test_the_instances_follow_the_predeclared_rule() -> None:
    """The predeclared names and mechanisms, each with the first seed of 7400 + k + 6j that passed the mechanical
    checks at the freeze (2026-10-19 manifest: k = 1 took 7407 and k = 6 took 7496; the others their first seed)."""
    mechanisms = ["lease", "memory", "pages", "lease", "memory", "pages"]
    assert [(i["name"], i["mechanism"]) for i in CONFIRM["instances"]] == [
        (f"docthread-confirm-{k}", m) for k, m in zip(range(1, 7), mechanisms, strict=True)
    ]
    assert all(
        (i["seed"] - 7400 - k) % 6 == 0 and i["seed"] > 7400
        for k, i in enumerate(CONFIRM["instances"], 1)
    )
    assert [i["seed"] for i in CONFIRM["instances"]] == [7407, 7402, 7403, 7404, 7405, 7496]


def test_it_plans_18_balanced_sessions_without_generating_a_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("planning must not generate an instance")

    monkeypatch.setattr(L, "generate", refuse)
    Ledger.open(
        tmp_path / "ledger.json", 100.0
    )  # a temporary ledger; the active one is never touched
    spec = h.as_current(CONFIRM) | {"ledger": str(tmp_path / "ledger.json"),
                                    "authorisation": str(tmp_path / "missing.json")}  # fmt: skip
    contract = L.plan(spec, tmp_path / "L")
    assert len(contract["sessions"]) == 18
    order = [(s["instance"], s["arm"]) for s in contract["sessions"]]
    assert order == L.balanced_order([i["name"] for i in CONFIRM["instances"]], CONFIRM["arms"])
    positions = {
        arm: [i % 3 for i, (_, a) in enumerate(order) if a == arm] for arm in CONFIRM["arms"]
    }
    assert all(
        sorted(p) == [0, 0, 1, 1, 2, 2] for p in positions.values()
    )  # each position twice per arm
    assert contract["arm_settings"]["paper_clm_shell"]["version"] == "paper-clm-shell/2"
    assert (contract["task_version"], contract["turn_endings"], contract["truncation"]) == (
        "docthread-task/2", "p015-turn-endings/1", "p015-truncation/1")  # fmt: skip
