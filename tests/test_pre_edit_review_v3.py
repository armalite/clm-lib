"""Experiment 010 v3: required pre-edit review with a runtime-supplied update (offline)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from clm_lib.cli import conditions_need_jev, parse_conditions
from clm_lib.config import load_config
from clm_lib.jev import FakeAdvisor, JevError
from clm_lib.prompts import (
    INITIATED_REVIEW_V2_CLAUSE,
    INITIATED_REVIEW_V3_CLAUSE,
    INITIATED_REVIEW_V3_JEV_CLAUSE,
)
from clm_lib.tasks import generate
from clm_lib.update_review import build_payload

from .conftest import execute
from .test_initiated_review import FINAL, NoteExecutor, cfg_ir, review_obs
from .test_update_review import ADVANCE, ROUNDS_FINAL, make, revisions

REPO = Path(__file__).resolve().parents[1]
TASK = "rounds-dev-1"
REVIEW = {"action": "begin_review"}
COMPACT = {"action": "begin_review", "purpose": "compaction"}


def note(body: str, eid: str = "n1") -> dict[str, Any]:
    return execute(f"NOTE:{eid}:{body}")


def tail(task: str, released: int) -> list[Any]:
    """Advance through the remaining rounds and finish."""
    return [*[ADVANCE] * (generate(task).n_stages - released), ROUNDS_FINAL]


def run3(tmp: Path, script: list[Any], arm: str = "jev", adv: Any = None, cfg: Any = None) -> Any:
    runner, prov = make(tmp, script, cfg or cfg_ir(), adv, NoteExecutor())
    res = runner.run(
        generate(TASK),
        "clm_helpers",
        initiated_review=arm,
        review_required=True,
        review_update_source="latest-round",
    )
    return res, prov


def payload(round_no: int) -> str:
    return build_payload(generate(TASK).stages[round_no - 1], 4000)[0]


# ------------------------------------------------------------- historical behaviour, opt-in
def test_historical_configs_v2_prompts_and_validation(tmp_path: Path) -> None:
    for p in [*sorted((REPO / "configs").glob("exp*.toml")), REPO / "configs" / "default.toml"]:
        if p.stem == "exp010v3":
            continue
        assert load_config(p).initiated_review.update_source == "stated", p.name
    # v2 runs still use the v2 clause and the stated-information rule.
    runner, prov = make(tmp_path / "v2", [REVIEW, FINAL, FINAL], cfg_ir(), None, NoteExecutor())
    res = runner.run(
        generate("dev"), "clm_helpers", initiated_review="control", review_required=True
    )
    assert INITIATED_REVIEW_V2_CLAUSE in prov.requests[0].system
    assert INITIATED_REVIEW_V3_CLAUSE not in prov.requests[0].system
    assert res.metrics["counts"]["repair_calls"] == 1  # v2 needs a purpose
    runner, _ = make(tmp_path / "x", [FINAL], cfg_ir(), None)
    with pytest.raises(ValueError, match="needs a required review on rounds"):
        runner.run(
            generate("dev"),
            "clm_helpers",
            initiated_review="control",
            review_required=True,
            review_update_source="latest-round",
        )
    with pytest.raises(ValueError, match="needs a required review on rounds"):
        runner.run(
            generate(TASK),
            "clm_helpers",
            initiated_review="control",
            review_update_source="latest-round",
        )
    with pytest.raises(ValueError, match="stated or latest-round"):
        runner.run(
            generate(TASK),
            "clm_helpers",
            initiated_review="control",
            review_required=True,
            review_update_source="hidden",
        )


# ------------------------------------------------------------- the correction
def test_compaction_label_does_not_skip_jev(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.2)
    script = [execute("print(1)"), ADVANCE, COMPACT, note("compacted"), *tail(TASK, 2)]
    res, _ = run3(tmp_path, script, "jev", adv)
    ir = res.metrics["initiated_review"]
    rec = ir["records"][0]
    assert ir["version"] == "initiated-review/3" and rec["purpose"] == "compaction"
    assert rec["status"] == "advised" and rec["applicable"] and rec["update_round"] == 2
    assert len(adv.calls) == 1 and adv.calls[0]["state"]["update"] == payload(2)
    assert rec["payload_sha256"] == hashlib.sha256(payload(2).encode()).hexdigest()
    assert ir["reviews_applicable"] == 1 and ir["reviews_by_label"] == {"compaction": 1}


def test_c_advises_before_the_edit_and_b_gets_the_same_material(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.9 if "pool" in e else 0.1)
    script = [
        note("ignored: blocked"),
        REVIEW,
        note("db pool suspected"),
        ADVANCE,
        REVIEW,
        note("revised"),
        *tail(TASK, 2),
    ]
    out = {}
    for arm, advisor in (("control", None), ("jev", adv)):
        out[arm] = run3(tmp_path / arm, script, arm, advisor)
    (b, bp), (c, cp) = out["control"], out["jev"]
    assert "-pre3-" in b.run_id and "-pre3jev-" in c.run_id
    assert (
        cp.requests[0].system.replace(INITIATED_REVIEW_V3_JEV_CLAUSE, "") == bp.requests[0].system
    )
    bo, co = review_obs(b.run_dir), review_obs(c.run_dir)
    assert len(bo) == len(co) == 2
    for x, y in zip(bo, co, strict=True):
        assert y.startswith(x) and "Context review advice" not in x
        assert "runtime-supplied, round-files/1" in x
    # The payload is named, not quoted, so B and C see nothing A cannot read from the files.
    for line in payload(2).splitlines():
        assert line.split(": ", 1)[1] not in bo[1]
    # Advice is in the request that decides the step-6 edit, before the edit lands.
    assert "n1: 0.90" in cp.requests[5].user and '"body": "revised"' not in cp.requests[5].user
    assert '"body": "revised"' in cp.requests[6].user
    for res in (b, c):
        ir = res.metrics["initiated_review"]
        assert ir["edits_blocked"] == 1 and ir["accepted_edits_authorised"] == 2
        assert ir["accepted_edits_unauthorised"] == 0
    assert b.metrics["jev"]["attempts"] == 0 and len(adv.calls) == 2
    assert [x["state"]["update"] for x in adv.calls] == [payload(1), payload(2)]
    rec = c.metrics["initiated_review"]["records"][1]
    assert rec["flagged_then_changed"] == ["n1"] and rec["authorised_edit_step"] == 6
    # Advice never edits: the review step appends only its own entries.
    revs = {r["number"]: r for r in revisions(c.run_dir)}
    before, after = revs[rec["revision"]]["entries"], revs[rec["revision"] + 1]["entries"]
    assert after[: len(before)] == before and [e["id"] for e in after[len(before) :]] == [
        "s5.act",
        "s5.obs",
    ]


def test_explicit_empty_inputs(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.5)
    # A review before any entry exists has no candidates.
    script = [REVIEW, execute("print(1)"), *[ADVANCE] * 6, REVIEW, *tail(TASK, 7)]
    res, _ = run3(tmp_path, script, "jev", adv)
    r1, r2 = res.metrics["initiated_review"]["records"]
    assert r1["status"] == "not_applicable" and r1["not_applicable_reason"] == "no_candidates"
    assert r2["update_round"] == 7 and payload(7) == ""
    assert r2["status"] == "not_applicable" and r2["not_applicable_reason"] == "empty_update"
    assert adv.calls == []
    obs = review_obs(res.run_dir)
    assert "no entries to assess" in obs[0] and "round 7 released no incident-board" in obs[1]
    na = res.metrics["initiated_review"]["reviews_not_applicable_by_reason"]
    assert na == {"empty_update": 1, "no_candidates": 1}


def test_repeat_reviews_of_one_round_are_each_paid(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.3)
    script = [execute("print(1)"), REVIEW, REVIEW, note("x"), *tail(TASK, 1)]
    res, _ = run3(tmp_path, script, "jev", adv)
    recs = res.metrics["initiated_review"]["records"]
    assert [r["round_review_index"] for r in recs] == [0, 1] and len(adv.calls) == 2
    assert res.metrics["initiated_review"]["reviews_repeat_round"] == 1
    led = json.loads((tmp_path / "ledger.json").read_text())
    assert [e["kind"] for e in led["entries"]].count("jev") == 2


def test_no_deleted_or_future_information_reaches_jev(tmp_path: Path) -> None:
    inst = generate(TASK)
    adv = FakeAdvisor(lambda u, e: 0.4)
    secret = "DELETED-NOTE"
    script = [REVIEW, note(secret), REVIEW, note("replaced"), REVIEW, *tail(TASK, 1)]
    res, _ = run3(tmp_path, script, "jev", adv)
    sent = json.dumps(adv.calls)
    assert any(secret in json.dumps(r["entries"]) for r in revisions(res.run_dir))
    last = adv.calls[-1]
    assert secret not in json.dumps(last) and last["state"]["update"] == payload(1)
    for later in inst.stages[1:]:
        for path, text in later.items():
            if path.endswith("board.md"):
                for line in text.splitlines()[2:]:
                    if line.strip():
                        assert line.strip() not in sent
    for incident in inst.truth.incidents:
        assert incident["cause"] not in sent
    assert "api_key" not in sent.lower()


def test_failures_are_recorded_and_the_workflow_continues(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.6)
    adv.fail_with = [JevError("http_529", "overloaded", retryable=True, billing="none")] * 2
    cfg = cfg_ir()
    cfg.jev.retries = 1
    res, _ = run3(
        tmp_path, [execute("print(1)"), REVIEW, note("e"), *tail(TASK, 1)], "jev", adv, cfg
    )
    rec = res.metrics["initiated_review"]["records"][0]
    assert rec["status"] == "advice_failed" and rec["attempts"] == 2
    assert res.metrics["initiated_review"]["accepted_edits_authorised"] == 1
    assert "Service problem: http_529" in review_obs(res.run_dir)[0]
    led = json.loads((tmp_path / "ledger.json").read_text())
    assert [e["status"] for e in led["entries"] if e["kind"] == "jev"] == ["error:http_529"] * 2


def test_labels_and_notes_are_optional_and_recorded_only(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.5)
    labelled = {"action": "begin_review", "purpose": "update", "new_information": "my note"}
    res, _ = run3(tmp_path, [execute("print(1)"), labelled, REVIEW, *tail(TASK, 1)], "jev", adv)
    recs = res.metrics["initiated_review"]["records"]
    assert [r["purpose"] for r in recs] == ["update", None] and res.metrics["initiated_review"][
        "reviews_rejected"
    ] == 0
    assert [c["state"]["update"] for c in adv.calls] == [payload(1), payload(1)]  # note never used


# ------------------------------------------------------------- A, flags and export
def test_condition_a_and_flags() -> None:
    conds = parse_conditions("clm_helpers:on,clm_helpers+pre3:on,clm_helpers+pre3-jev:on")
    assert [c[0] for c in conds] == ["clm_helpers", "clm_helpers+pre3", "clm_helpers+pre3-jev"]
    assert conditions_need_jev("clm_helpers+pre3-jev:on")
    assert not conditions_need_jev("clm_helpers:on,clm_helpers+pre3:on")


def test_v1_v2_v3_export_separately(tmp_path: Path) -> None:
    from clm_lib.export import export_experiment

    adv = FakeAdvisor(lambda u, e: 0.6)
    r1, _ = make(
        tmp_path,
        [execute("print(1)"), {"action": "begin_review", "new_information": "x"}, FINAL],
        cfg_ir(),
        adv,
    )
    v1 = r1.run(generate("dev"), "clm_helpers", initiated_review="jev")
    r2, _ = make(
        tmp_path,
        [execute("print(1)"), {"action": "begin_review", "purpose": "compaction"}, FINAL],
        cfg_ir(),
        adv,
    )
    v2 = r2.run(generate("dev"), "clm_helpers", initiated_review="jev", review_required=True)
    v3, _ = run3(tmp_path, [execute("print(1)"), REVIEW, note("e"), *tail(TASK, 1)], "jev", adv)
    exp = tmp_path / "exp"
    exp.mkdir()
    spec = {
        "id": "990-v3",
        "title": "t",
        "runs": [
            {"run_id": v1.run_id, "role": "calibration", "phase": "calibration"},
            {"run_id": v2.run_id, "role": "calibration", "phase": "v2-calibration", "group": "v2"},
            {"run_id": v3.run_id, "role": "calibration", "phase": "v3-calibration", "group": "v3"},
        ],
        "comparisons": [],
    }
    (exp / "experiment.json").write_text(json.dumps(spec))
    export_experiment(exp, tmp_path / "runs", tmp_path / "ledger.json", tmp_path / "results")
    out = tmp_path / "results" / "experiments" / "990-v3" / "artifacts" / "runs"
    assert (out / "calibration" / v1.run_id).is_dir()
    assert (out / "v2" / "calibration" / v2.run_id).is_dir()
    assert (out / "v3" / "calibration" / v3.run_id).is_dir()
    rows = {r["run_id"]: r for r in json.loads((out.parents[1] / "metrics.json").read_text())}
    assert (
        rows[v3.run_id]["ir_update_source"] == "latest-round"
        and rows[v3.run_id]["ir_reviews_applicable"] == 1
    )
    assert (
        "ir_update_source" not in rows[v2.run_id]
        and rows[v2.run_id]["ir_version"] == "initiated-review/2"
    )
    assert (
        "required pre-edit review (runtime update) with Jev advice"
        in (out.parents[1] / "report.md").read_text()
    )
