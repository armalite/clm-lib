"""The exposure report (`p015-exposure/1`): read-only, checked against the recorded smoke evidence (when the results
repository is present beside this checkout), units kept apart, and absent measurements kept distinct from zero."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from clm_lib.p015 import exposure as X

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO.parent / "clm-lib-test-results/experiments/015-public-clm-comparison"
DEV2, DEV1 = RESULTS / "smoke-dev2-export", RESULTS / "smoke-dev1-export"
needs_results = pytest.mark.skipif(
    not DEV2.exists(), reason="the results repository is not beside this checkout"
)


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


@needs_results
def test_smoke_dev2_totals_match_the_record_and_the_export_is_untouched(tmp_path: Path) -> None:
    before = tree_digest(DEV2)
    assert (
        X.main(
            [str(DEV2), "--json", str(tmp_path / "r.json"), "--markdown", str(tmp_path / "r.md")]
        )
        == 0
    )
    assert tree_digest(DEV2) == before
    g, p, s = json.loads((tmp_path / "r.json").read_text())["sessions"]
    assert [x["calls"]["attempts"] for x in (g, p, s)] == [27, 20, 26]
    assert [x["cost"]["confirmed_usd"] for x in (g, p, s)] == [1.02431, 0.222977, 0.444595]
    assert [x["tokens"]["peak_main_input_tokens"] for x in (g, p, s)] == [24_422, 29_806, 20_685]
    assert [x["tools"]["delivered_chars"] for x in (g, p, s)] == [122_436, 56_866, 111_341]
    assert [x["tools"]["repeated_calls"] for x in (g, p, s)] == [7, 0, 6]
    assert [(x["turns"]["planned"], x["turns"]["attempted"], x["turns"]["completed"]) for x in (g, p, s)] == [
        (8, 8, 8), (8, 4, 3), (8, 8, 8)]  # fmt: skip
    assert (g["management"]["edits_accepted"], g["management"]["held_outputs"],
            g["management"]["held_output_refetches"], g["notices"]) == (4, 1, 1, {"pressure": 2})  # fmt: skip
    assert (p["management"]["rollbacks"], p["management"]["edits_rejected"], p["notices"]) == (
        6, 1, {"reminder": 1, "rollback": 6, "over_limit": 1})  # fmt: skip
    assert (s["management"]["summaries"], s["calls"]["summary_logical"]) == (3, 3)
    assert [x["time"]["wall_s"] for x in (g, p, s)] == [118.0, 100.0, 108.0]
    assert p["ending"] == "context_limit" and p["arm_version"] == "paper-clm-shell/1"
    assert g["arm_version"] == "guarded-clm 0.2.0.dev1"
    for x in (g, p, s):  # per-turn figures add up to the session's
        assert (
            sum(t["attempts"]["main"] + t["attempts"]["summary"] for t in x["per_turn"])
            == x["calls"]["attempts"]
        )
        assert (
            sum(t["tool_delivered_chars"] for t in x["per_turn"]) == x["tools"]["delivered_chars"]
        )
        assert round(sum(t["charged_usd"] for t in x["per_turn"]), 6) == x["cost"]["total_usd"]
    assert [t["attempts"]["summary"] for t in s["per_turn"]] == [0, 0, 0, 3, 0, 0, 0, 0]


@needs_results
def test_smoke_dev1_failures_retries_and_assumed_cost_are_reported_apart(tmp_path: Path) -> None:
    g, p = X.report(DEV1)["sessions"]
    assert (g["calls"]["attempts"], g["calls"]["failed"], g["calls"]["retries"]) == (23, 7, 7)
    assert (g["cost"]["confirmed_usd"], g["cost"]["assumed_usd"]) == (0.455897, 1.371392)
    assert [t["retries"] for t in g["per_turn"]] == [0, 1, 1, 1, 1, 1, 1, 1]
    assert (p["outcome"], p["calls"]["refused"], p["turns"]["attempted"], p["turns"]["completed"]) == (
        "stopped", 1, 1, 0)  # fmt: skip


@needs_results
def test_absent_measurements_are_not_zero() -> None:
    g, p, s = X.report(DEV2)["sessions"]
    assert s["management"]["edits_accepted"] is None and s["management"]["held_outputs"] is None
    assert g["management"]["summaries"] is None and p["management"]["held_outputs"] is None
    assert p["management"]["carried_rejections"] is None  # version 1 records no such field
    assert "n/a" in X.markdown(X.report(DEV2))


@needs_results
def test_one_session_directory_and_the_refusal_to_write_into_an_export(tmp_path: Path) -> None:
    one = next((DEV2 / "sessions").iterdir())
    rep = X.report(one)
    assert (
        len(rep["sessions"]) == 1 and rep["sessions"][0]["turns"]["planned"] is None
    )  # not in a session record
    assert X.main([str(DEV2), "--json", str(DEV2 / "report.json")]) == 2
    assert not (DEV2 / "report.json").exists()
