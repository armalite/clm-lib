"""guarded-clm's own public transcript scenarios, run with every model wrapped in clm-lib's AccountedModel, must
still match the library's stored transcripts exactly: the accounting boundary is transparent to the capability.

Needs the guarded-clm checkout's tests (an editable install from a sibling checkout); skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import guarded_clm
import pytest

from clm_lib.budget import Ledger
from clm_lib.p015.accounting import AccountedModel

ROOT = Path(guarded_clm.__file__).resolve().parents[2]
SCENARIOS = ROOT / "tests" / "golden" / "scenarios.py"
DATA = ROOT / "tests" / "golden" / "data"

if not SCENARIOS.exists():
    pytest.skip(
        "guarded-clm's scenario tests are not available (not an editable checkout)",
        allow_module_level=True,
    )

spec = importlib.util.spec_from_file_location("guarded_clm_scenarios", SCENARIOS)
assert spec is not None and spec.loader is not None
sc = importlib.util.module_from_spec(spec)
sys.modules["guarded_clm_scenarios"] = sc
spec.loader.exec_module(sc)

from . import harness as h  # noqa: E402


def factory(settings: Any, on_record: Any) -> Any:
    return sc.Stack(
        capabilities=[
            h.GuardedSettings(
                budget_tokens=settings.budget_tokens,
                max_steps=settings.max_steps,
                call_cap=settings.call_cap,
                end_over_budget=settings.end_over_budget,
                max_output_chars=settings.max_output_chars,
                max_file_bytes=settings.max_file_bytes,
                max_entries=settings.max_entries,
                max_body_chars=settings.max_body_chars,
                max_total_chars=settings.max_total_chars,
            ).capability(on_record)
        ]
    )


@pytest.mark.parametrize("name", sorted(sc.SCENARIOS))
def test_library_scenario_through_the_accounting_boundary(
    name: str, tmp_path: Path, monkeypatch: Any
) -> None:
    ledger = Ledger.open(tmp_path / "ledger.json", 100.0)
    real_agent = sc.Agent

    def accounted_agent(model: Any, *args: Any, **kwargs: Any) -> Any:
        return real_agent(
            AccountedModel(model, ledger=ledger, price=h.PRICE, run_id=name), *args, **kwargs
        )

    monkeypatch.setattr(sc, "Agent", accounted_agent)
    actual = json.loads(json.dumps(sc.SCENARIOS[name](factory)))
    stored = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))["transcript"]
    assert actual == stored
    assert len(ledger.entries) == len(actual["calls"])  # one settled attempt per model request
