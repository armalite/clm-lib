"""Experiment 010 v2: review required before context edits (offline; Jev is faked)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clm_lib.cli import condition_schedule, conditions_need_jev, parse_conditions
from clm_lib.config import load_config
from clm_lib.context import Entry
from clm_lib.jev import FakeAdvisor, JevError
from clm_lib.prompts import (
    INITIATED_REVIEW_CLAUSE,
    INITIATED_REVIEW_V2_CLAUSE,
    INITIATED_REVIEW_V2_JEV_CLAUSE,
)
from clm_lib.runner import REVIEW_REQUIRED, _Run, parse_action
from clm_lib.tasks import generate

from .conftest import execute, working_ids
from .test_initiated_review import FINAL, NoteExecutor, cfg_ir, review_obs
from .test_update_review import ADVANCE, ROUNDS_FINAL, make, revisions

REPO = Path(__file__).resolve().parents[1]


def upd(info: str = "Thread B root cause revised to CERT_EXPIRED") -> dict[str, Any]:
    return {"action": "begin_review", "purpose": "update", "new_information": info}


COMPACT = {"action": "begin_review", "purpose": "compaction"}


def note(body: str, eid: str = "n1") -> dict[str, Any]:
    return execute(f"NOTE:{eid}:{body}")


def run_pre(
    tmp_path: Path, script: list[Any], arm: str = "control", advisor: Any = None, task: str = "dev"
) -> Any:
    runner, prov = make(tmp_path, script, cfg_ir(), advisor, NoteExecutor())
    res = runner.run(generate(task), "clm_helpers", initiated_review=arm, review_required=True)
    return res, prov


def receipts(run_dir: Path) -> list[str]:
    seen: dict[str, str] = {}
    for r in revisions(run_dir):
        for e in r["entries"]:
            if e["role"] == "receipt":
                seen.setdefault(e["id"], e["body"])
    return list(seen.values())


# ------------------------------------------------------------- historical behaviour, opt-in
def test_historical_configs_and_v1_are_unchanged(tmp_path: Path) -> None:
    for p in [*sorted((REPO / "configs").glob("exp*.toml")), REPO / "configs" / "default.toml"]:
        if p.stem == "exp010v2":
            continue
        assert load_config(p).initiated_review.required is False, p.name
    # v1 (review optional) keeps its clause, schema and direct editing.
    runner, prov = make(tmp_path, [note("direct"), FINAL], cfg_ir(), None, NoteExecutor())
    res = runner.run(generate("dev"), "clm_helpers", initiated_review="control")
    assert "-rev-" in res.run_id and INITIATED_REVIEW_CLAUSE in prov.requests[0].system
    assert INITIATED_REVIEW_V2_CLAUSE not in prov.requests[0].system
    assert res.metrics["counts"]["edits_accepted_changed"] == 1
    assert "required" not in res.metrics["initiated_review"]
    v1 = {"action": "begin_review", "new_information": "x"}
    assert parse_action(json.dumps(v1), 1000, allow_begin_review=True)[0]
    assert (
        parse_action(json.dumps(v1), 1000, allow_begin_review=True, begin_review_v2=True)[0] is None
    )
    with pytest.raises(ValueError, match="needs initiated review"):
        runner.run(generate("dev"), "clm_helpers", review_required=True)


def test_condition_a_keeps_direct_editing(tmp_path: Path) -> None:
    runner, prov = make(tmp_path, [note("direct"), FINAL], cfg_ir(), None, NoteExecutor())
    res = runner.run(generate("dev"), "clm_helpers")
    assert res.metrics["counts"]["edits_accepted_changed"] == 1
    assert "begin_review" not in prov.requests[0].system
    assert res.metrics["initiated_review"]["mode"] == "off"


# ------------------------------------------------------------- the gate
def test_edits_cannot_bypass_the_review(tmp_path: Path) -> None:
    script = [note("first"), upd(), note("second"), note("third"), FINAL]
    res, prov = run_pre(tmp_path, script)
    assert "-pre-" in res.run_id and INITIATED_REVIEW_V2_CLAUSE in prov.requests[0].system
    ir = res.metrics["initiated_review"]
    assert ir["version"] == "initiated-review/2" and ir["required"] is True
    assert ir["edits_blocked"] == 2 and ir["blocked_edit_steps"] == [1, 4]
    assert ir["accepted_edits_authorised"] == 1 and ir["accepted_edits_unauthorised"] == 0
    assert res.metrics["counts"]["edits_accepted_changed"] == 1
    rc = receipts(res.run_dir)
    assert rc[0].startswith("context.json edit rejected at step 1") and REVIEW_REQUIRED in rc[0]
    assert "accepted at step 3" in rc[1] and REVIEW_REQUIRED in rc[2]
    # The blocked step-1 write never reached the context; the authorised step-3 write did.
    assert "n1" not in working_ids(prov.requests[2].user)  # the blocked write never landed
    assert working_ids(prov.requests[3].user)[0] == "n1"
    assert '"id": "n1", "role": "note", "body": "second"' in prov.requests[3].user
    assert '"body": "third"' not in prov.requests[4].user
    assert (res.run_dir / "context" / "rejected-step-001.txt").exists()
    assert ir["authorisation_outcomes"] == {"consumed": 1}
    rec = ir["records"][0]
    assert rec["authorisation_outcome"] == "consumed" and rec["authorised_edit_step"] == 3
    # Status line tells Claude whether an authorisation exists.
    assert "context edit authorisation: none" in prov.requests[0].user
    assert "available from the review at step 2" in prov.requests[2].user


def test_runtime_appends_keep_and_advance_expires_the_authorisation(tmp_path: Path) -> None:
    n = generate("rounds-dev-1").n_stages
    script = [
        execute("print(1)"),
        upd("round 1 board read"),
        execute("print('reading logs')"),  # runtime appends only
        note("after logs"),  # still authorised
        upd("second"),
        ADVANCE,  # expires the unused authorisation
        note("after release"),  # blocked
        *[ADVANCE] * (n - 2),
        ROUNDS_FINAL,
    ]
    res, _ = run_pre(tmp_path, script, task="rounds-dev-1")
    ir = res.metrics["initiated_review"]
    outs = [(a["review_step"], a["outcome"]) for a in ir["authorisations"]]
    assert outs == [(2, "consumed"), (5, "expired")]
    assert ir["blocked_edit_steps"] == [7] and ir["accepted_edits_authorised"] == 1


def test_replacement_noop_and_unchanged_writes(tmp_path: Path) -> None:
    script = [upd("a"), upd("b"), execute("print(1)"), FINAL]
    res, _ = run_pre(tmp_path, script)
    ir = res.metrics["initiated_review"]
    assert [a["outcome"] for a in ir["authorisations"]] == ["replaced", "unused"]
    assert res.metrics["counts"]["edits_accepted_changed"] == 0 and ir["edits_blocked"] == 0

    class SameWrite(NoteExecutor):
        def run(self, code: str, workspace: Path, fixtures: Path) -> Any:
            if code == "SAME":
                d = json.loads((workspace / "context.json").read_text())
                (workspace / "context.json").write_text(json.dumps(d, indent=2))
            return super().run(code, workspace, fixtures)

    runner, _ = make(tmp_path / "s", [execute("SAME"), FINAL], cfg_ir(), None, SameWrite())
    res = runner.run(
        generate("dev"), "clm_helpers", initiated_review="control", review_required=True
    )
    assert res.metrics["initiated_review"]["edits_blocked"] == 0  # no content change, no gate
    assert res.metrics["counts"]["edits_unchanged_write"] == 1


def test_invalidation_and_recovery_exemption(tmp_path: Path) -> None:
    runner, _ = make(tmp_path, [FINAL], cfg_ir(), None)
    r = _Run(
        runner, generate("dev"), "clm_helpers", "unit-run", "", False, False, "off", "control", True
    )
    r.store.append([Entry("n1", "note", "now")], "runtime", 1)
    r.edit_auth = {"step": 1, "revision": 0, "purpose": "update", "bodies": {"n1": "before"}}
    assert "no longer matches" in (r._authorise_edit(()) or "")
    assert r.edit_auth is None and r.auth_log[-1]["outcome"] == "invalidated"
    r.recovery_active = True  # OVER LIMIT: the shrinking edit must happen in this step
    assert r._authorise_edit(()) is None and r.recovery_exempt_steps == [0]
    r.recovery_active = False
    assert REVIEW_REQUIRED in (r._authorise_edit(()) or "")


# ------------------------------------------------------------- advice: before the edit, narrow
def test_b_and_c_differ_only_by_advice_which_precedes_the_edit(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.9 if "DB_POOL" in e else 0.1)
    script = [
        execute("print(1)"),
        upd("start"),
        note("Thread B DB_POOL"),
        upd(),
        note("Thread B CERT"),
        FINAL,
    ]
    out = {}
    for arm, advisor in (("control", None), ("jev", adv)):
        res, prov = run_pre(tmp_path / arm, script, arm, advisor)
        out[arm] = (res, prov)
    (b, bp), (c, cp) = out["control"], out["jev"]
    assert "-prejev-" in c.run_id
    assert (
        cp.requests[0].system.replace(INITIATED_REVIEW_V2_JEV_CLAUSE, "") == bp.requests[0].system
    )
    b_obs, c_obs = review_obs(b.run_dir), review_obs(c.run_dir)
    assert len(b_obs) == len(c_obs) == 2
    for x, y in zip(b_obs, c_obs, strict=True):
        assert y.startswith(x) and "Context review advice" not in x
    assert "n1: 0.90" in c_obs[1]
    # Advice is in the request that decides the edit (step 5), before the edit is made.
    assert "n1: 0.90" in cp.requests[4].user and "Thread B CERT" not in cp.requests[4].user
    assert "Thread B CERT" in cp.requests[5].user
    for res in (b, c):
        assert res.metrics["counts"]["edits_accepted_changed"] == 2
        assert res.metrics["counts"]["provider_calls"] == 6
    rec = c.metrics["initiated_review"]["records"][1]
    assert rec["flagged_then_changed"] == ["n1"] and rec["authorised_edit_step"] == 5
    assert len(adv.calls) == 2 and adv.calls[1]["state"]["update"] == upd()["new_information"]
    assert c.metrics["cost"]["by_provider"]["typesafe"] > 0 and b.metrics["jev"]["attempts"] == 0
    # Advice never edits: the review step only appends its own act/obs.
    revs = {r["number"]: r for r in revisions(c.run_dir)}
    before = revs[rec["revision"]]["entries"]
    after = revs[rec["revision"] + 1]["entries"]
    assert after[: len(before)] == before and [e["id"] for e in after[len(before) :]] == [
        "s4.act",
        "s4.obs",
    ]


def test_compaction_is_not_applicable_and_still_authorises(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.5)
    res, _ = run_pre(tmp_path, [execute("print(1)"), COMPACT, note("compacted"), FINAL], "jev", adv)
    ir = res.metrics["initiated_review"]
    assert adv.calls == [] and ir["reviews_not_applicable"] == 1 and ir["reviews_compaction"] == 1
    assert ir["records"][0]["status"] == "not_applicable" and ir["accepted_edits_authorised"] == 1
    assert "not applicable (compaction review" in review_obs(res.run_dir)[0]


def test_invalid_reviews_issue_nothing(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.5)
    bad_compaction = {**COMPACT, "new_information": "something"}
    script = [upd("  "), bad_compaction, upd("x" * 3000), note("blocked"), FINAL]
    res, _ = run_pre(tmp_path, script, "jev", adv)
    ir = res.metrics["initiated_review"]
    assert ir["reviews"] == 0 and ir["reviews_rejected"] == 3 and adv.calls == []
    assert ir["edits_blocked"] == 1 and ir["authorisations"] == []
    errs = [x["error"] for x in ir["rejections"]]
    assert "needs the new information" in errs[0] and "takes no new information" in errs[1]
    assert "at most 2000" in errs[2]


def test_service_errors_keep_the_workflow_and_are_accounted(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.6)
    adv.fail_with = [JevError("http_529", "overloaded", retryable=True, billing="none")] * 2
    cfg = cfg_ir()
    cfg.jev.retries = 1
    runner, _ = make(
        tmp_path, [execute("print(1)"), upd(), note("edited"), FINAL], cfg, adv, NoteExecutor()
    )
    res = runner.run(generate("dev"), "clm_helpers", initiated_review="jev", review_required=True)
    ir = res.metrics["initiated_review"]
    assert ir["records"][0]["status"] == "advice_failed" and ir["accepted_edits_authorised"] == 1
    assert "Service problem: http_529" in review_obs(res.run_dir)[0]
    led = json.loads((tmp_path / "ledger.json").read_text())
    assert [e["status"] for e in led["entries"] if e["kind"] == "jev"] == ["error:http_529"] * 2


def test_jev_sees_only_current_released_information(tmp_path: Path) -> None:
    inst = generate("rounds-dev-1")
    adv = FakeAdvisor(lambda u, e: 0.3)
    secret = "DELETED-NOTE"
    script = [
        execute("print(1)"),
        COMPACT,
        note(secret),
        COMPACT,
        note("replaced"),  # secret note removed by an authorised edit
        upd("round 1 board read"),
        *[ADVANCE] * (inst.n_stages - 1),
        ROUNDS_FINAL,
    ]
    res, _ = run_pre(tmp_path, script, "jev", adv, task="rounds-dev-1")
    sent = json.dumps(adv.calls)
    assert any(secret in json.dumps(r["entries"]) for r in revisions(res.run_dir))  # it existed
    assert len(adv.calls) == 1 and secret not in sent
    for later in inst.stages[1:]:
        for path, text in later.items():
            if path.endswith("board.md"):
                for line in text.splitlines()[2:]:
                    if line.strip():
                        assert line.strip() not in sent
    for incident in inst.truth.incidents:
        assert incident["cause"] not in sent
    assert "api_key" not in sent.lower() and "bearer" not in sent.lower()


# ------------------------------------------------------------- conditions and export
def test_three_condition_flags_and_schedule() -> None:
    conds = parse_conditions("clm_helpers:on,clm_helpers+pre:on,clm_helpers+pre-jev:on")
    assert [c[0] for c in conds] == ["clm_helpers", "clm_helpers+pre", "clm_helpers+pre-jev"]
    assert conditions_need_jev("clm_helpers:on,clm_helpers+pre:on,clm_helpers+pre-jev:on")
    assert not conditions_need_jev("clm_helpers:on,clm_helpers+pre:on")
    tasks = [f"rounds-eval-{i}" for i in range(9, 13)]
    rows = condition_schedule(tasks, 3, conds)
    assert len(rows) == 36
    for pos in range(3):
        counts = {c[0]: 0 for c in conds}
        for r in rows:
            if r["position"] == pos:
                counts[r["mode"]] += 1
        assert set(counts.values()) == {4}  # each condition 4 times in each position


def test_v1_and_v2_export_separately(tmp_path: Path) -> None:
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
        tmp_path, [execute("print(1)"), upd(), note("e"), FINAL], cfg_ir(), adv, NoteExecutor()
    )
    v2 = r2.run(generate("dev"), "clm_helpers", initiated_review="jev", review_required=True)
    exp = tmp_path / "exp"
    exp.mkdir()
    spec = {
        "id": "991-pre",
        "title": "t",
        "runs": [
            {"run_id": v1.run_id, "role": "calibration", "phase": "calibration"},
            {"run_id": v2.run_id, "role": "calibration", "phase": "v2-calibration", "group": "v2"},
        ],
        "comparisons": [],
    }
    (exp / "experiment.json").write_text(json.dumps(spec))
    export_experiment(exp, tmp_path / "runs", tmp_path / "ledger.json", tmp_path / "results")
    out = tmp_path / "results" / "experiments" / "991-pre"
    assert (out / "artifacts" / "runs" / "calibration" / v1.run_id / "summary.json").exists()
    assert (out / "artifacts" / "runs" / "v2" / "calibration" / v2.run_id / "summary.json").exists()
    assert not (out / "artifacts" / "runs" / "calibration" / v2.run_id).exists()
    rows = {r["run_id"]: r for r in json.loads((out / "metrics.json").read_text())}
    assert rows[v1.run_id]["phase"] == "calibration" and "ir_version" not in rows[v1.run_id]
    assert (
        rows[v2.run_id]["phase"] == "v2-calibration"
        and rows[v2.run_id]["ir_version"] == "initiated-review/2"
    )
    assert rows[v2.run_id]["ir_edits_authorised"] == 1
    manifest = json.loads((out / "manifest.json").read_text())
    paths = {r["run_id"]: r["artifact_path"] for r in manifest["runs"]}
    assert paths[v2.run_id] == f"artifacts/runs/v2/calibration/{v2.run_id}"
    assert "required pre-edit review with Jev advice" in (out / "report.md").read_text()
