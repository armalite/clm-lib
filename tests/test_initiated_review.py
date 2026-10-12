"""Experiment 010: Claude-initiated context review (offline; Jev is faked)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clm_lib.cli import conditions_need_jev, parse_conditions
from clm_lib.config import Config, load_config
from clm_lib.jev import FakeAdvisor, JevError
from clm_lib.prompts import INITIATED_REVIEW_CLAUSE, INITIATED_REVIEW_JEV_CLAUSE, system_prompt
from clm_lib.runner import ROUNDS_ACTION_SCHEMA, parse_action
from clm_lib.tasks import generate

from .conftest import FakeExecutor, execute, working_ids
from .test_update_review import ADVANCE, ROUNDS_FINAL, make, revisions

REPO = Path(__file__).resolve().parents[1]
FINAL = {
    "action": "final",
    "answer": {"root_cause": "x", "required_value": "0", "remedy": "x", "evidence_refs": []},
}


def cfg_ir() -> Config:
    cfg = Config()
    cfg.limits.status_shows_revision = True
    return cfg


def review(info: str = "Thread B root cause revised: CERT_EXPIRED replaces DB_POOL") -> dict:
    return {"action": "begin_review", "new_information": info, "thought": "board changed"}


class NoteExecutor(FakeExecutor):
    """``NOTE:<id>:<body>`` code replaces the context with one note; ``DROP:<id>`` removes it."""

    def run(self, code: str, workspace: Path, fixtures: Path) -> Any:
        d = json.loads((workspace / "context.json").read_text())
        if code.startswith("NOTE:"):
            _, eid, body = code.split(":", 2)
            d["entries"] = [{"id": eid, "role": "note", "body": body}]
        elif code.startswith("ADD:"):
            _, eid, body = code.split(":", 2)
            d["entries"].append({"id": eid, "role": "note", "body": body})
        elif code.startswith("DROP:"):
            d["entries"] = [e for e in d["entries"] if e["id"] != code[5:]]
        (workspace / "context.json").write_text(json.dumps(d))
        return super().run(code, workspace, fixtures)


def review_obs(run_dir: Path) -> list[str]:
    seen: dict[str, str] = {}
    for r in revisions(run_dir):
        for e in r["entries"]:
            if e["role"] == "observation" and e["body"].startswith("Context review"):
                seen.setdefault(e["id"], e["body"])
    return list(seen.values())


# ------------------------------------------------------------- historical behaviour, opt-in
def test_historical_configs_prompts_and_schema_are_unchanged(tmp_path: Path) -> None:
    for p in [*sorted((REPO / "configs").glob("exp*.toml")), REPO / "configs" / "default.toml"]:
        if p.stem >= "exp010":
            continue
        c = load_config(p)
        assert c.initiated_review.enabled is False and c.initiated_review.advice is False, p.name
    kw: dict[str, Any] = dict(
        exec_timeout=30, output_cap=6000, pressure_pct=70, tail=4, max_entries=200, max_body=24000
    )
    for mode in ("clm", "clm_helpers", "clm_direct", "clm_reuse", "summary"):
        assert "begin_review" not in system_prompt(mode, task_kind="rounds", **kw)
    assert "begin_review" not in json.dumps(ROUNDS_ACTION_SCHEMA)
    assert parse_action(json.dumps(review()), 1000)[0] is None  # not an action unless enabled
    assert parse_action(json.dumps(review()), 1000, allow_begin_review=True)[0]
    # Without the feature, begin_review is refused (repair) and nothing is recorded.
    runner, prov = make(tmp_path, [execute("print(1)"), review(), FINAL, FINAL], cfg_ir())
    res = runner.run(generate("dev"), "clm_helpers")
    assert res.metrics["counts"]["repair_calls"] == 1 and "-rev" not in res.run_id
    assert "begin_review" not in prov.requests[0].system
    assert res.metrics["initiated_review"]["mode"] == "off"


def test_initiated_review_is_opt_in_and_validated(tmp_path: Path) -> None:
    runner, _ = make(tmp_path / "a", [FINAL], cfg_ir())
    with pytest.raises(ValueError, match="no Jev advisor"):
        runner.run(generate("dev"), "clm_helpers", initiated_review="jev")
    with pytest.raises(ValueError, match="CLM modes"):
        runner.run(generate("dev"), "summary", initiated_review="control")
    with pytest.raises(ValueError, match="off, control or jev"):
        runner.run(generate("dev"), "clm_helpers", initiated_review="always")
    adv = FakeAdvisor(lambda u, e: 0.5)
    runner, _ = make(tmp_path / "b", [FINAL], cfg_ir(), adv)
    with pytest.raises(ValueError, match="exclusive"):
        runner.run(
            generate("rounds-dev-1"), "clm_helpers", update_review="jev", initiated_review="jev"
        )
    with pytest.raises(ValueError, match="exclusive"):
        runner.run(generate("dev"), "clm_helpers", jev=True, initiated_review="jev")
    cfg = cfg_ir()
    cfg.initiated_review.enabled = True
    runner, prov = make(tmp_path / "c", [FINAL], cfg)
    res = runner.run(generate("dev"), "clm_helpers")
    assert "-rev-" in res.run_id and res.metrics["initiated_review"]["mode"] == "control"
    assert INITIATED_REVIEW_CLAUSE in prov.requests[0].system
    assert INITIATED_REVIEW_JEV_CLAUSE not in prov.requests[0].system


# ------------------------------------------------------------- matched conditions
def test_conditions_differ_only_by_advice_and_advice_precedes_the_edit(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.9 if "DB_POOL" in e else 0.1)
    script = [
        execute("NOTE:n1:Thread B suspected DB_POOL_EXHAUSTED"),
        execute("print(2)"),
        review(),
        execute("NOTE:n1:Thread B cause CERT_EXPIRED (revised)"),
        FINAL,
    ]
    out = {}
    for arm, advisor in (("control", None), ("jev", adv)):
        runner, prov = make(tmp_path / arm, script, cfg_ir(), advisor, NoteExecutor())
        res = runner.run(generate("dev"), "clm_helpers", initiated_review=arm)
        assert res.status == "completed"
        out[arm] = (res, prov)
    (c_res, c_prov), (j_res, j_prov) = out["control"], out["jev"]
    assert "-rev-" in c_res.run_id and "-revjev-" in j_res.run_id
    c_sys, j_sys = c_prov.requests[0].system, j_prov.requests[0].system
    assert j_sys.replace(INITIATED_REVIEW_JEV_CLAUSE, "") == c_sys
    (c_obs,), (j_obs,) = review_obs(c_res.run_dir), review_obs(j_res.run_dir)
    assert j_obs.startswith(c_obs) and "Context review advice" not in c_obs
    assert "n1: 0.90" in j_obs and "s2.obs: 0.10" in j_obs
    # The review is one model turn; the advice is in the very next request, before the edit.
    assert "Context review advice" in j_prov.requests[3].user
    assert "Context review advice" not in j_prov.requests[2].user
    assert working_ids(j_prov.requests[4].user)[0] == "n1"
    assert "CERT_EXPIRED (revised)" in j_prov.requests[4].user
    for res in (c_res, j_res):
        rec = res.metrics["initiated_review"]["records"][0]
        assert rec["step"] == 3 and rec["edited_next_step"]
        assert rec["candidates_changed"] == ["n1", "s1.obs", "s2.obs"]
        assert res.metrics["counts"]["provider_calls"] == 5
    jrec = j_res.metrics["initiated_review"]["records"][0]
    assert jrec["flagged_then_changed"] == ["n1"] and jrec["status"] == "advised"
    assert len(adv.calls) == 1 and adv.calls[0]["state"]["update"] == review()["new_information"]
    assert c_res.metrics["initiated_review"]["jev_attempts"] == 0
    assert j_res.metrics["jev"]["source"] == "initiated_review"
    assert j_res.metrics["cost"]["by_provider"]["typesafe"] > 0


def test_candidates_are_current_structural_entries_only(tmp_path: Path) -> None:
    secret = "DELETED-NOTE-TEXT"
    adv = FakeAdvisor(lambda u, e: 0.4)
    script = [
        execute(f"NOTE:n1:{secret}"),
        execute("ADD:n2:Thread A ongoing"),
        execute("DROP:n1"),
        review(),
        FINAL,
    ]
    runner, _ = make(tmp_path, script, cfg_ir(), adv, NoteExecutor())
    res = runner.run(generate("dev"), "clm_helpers", initiated_review="jev")
    rec = res.metrics["initiated_review"]["records"][0]
    rev = next(r for r in revisions(res.run_dir) if r["number"] == rec["revision"])
    ids = {e["id"]: e for e in rev["entries"]}
    assert "n1" not in ids and secret not in json.dumps(adv.calls)
    assert rec["candidate_ids"] == [
        i for i, e in ids.items() if e["role"] in ("note", "observation")
    ]
    assert set(rec["excluded_ids"]) == {
        i for i, e in ids.items() if e["role"] in ("assistant", "receipt")
    }
    assert all(x.endswith((".act", ".rcpt")) for x in rec["excluded_ids"]) and rec["excluded_ids"]
    sent = [q["instructions"]["entry"] for q in adv.calls[0]["questions"].values()]
    assert sent == [ids[i]["body"] for i in rec["candidate_ids"]]
    obs = review_obs(res.run_dir)[0]
    assert f"context revision {rec['revision']}" in obs and "runtime step or receipt" in obs


def test_rejections_cost_no_jev_call_and_limit_applies(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.5)
    cfg = cfg_ir()
    cfg.initiated_review.max_reviews = 1
    script = [execute("print(1)"), review("   "), review("x" * 3000), review(), review(), FINAL]
    runner, _ = make(tmp_path, script, cfg, adv)
    res = runner.run(generate("dev"), "clm_helpers", initiated_review="jev")
    ir = res.metrics["initiated_review"]
    assert ir["reviews"] == 1 and ir["reviews_rejected"] == 3 and len(adv.calls) == 1
    errs = [r["error"] for r in ir["rejections"]]
    assert "empty" in errs[0] and "at most 2000" in errs[1] and "limit reached" in errs[2]
    led = json.loads((tmp_path / "ledger.json").read_text())
    assert [e["kind"] for e in led["entries"]].count("jev") == 1


def test_jev_sees_no_future_or_hidden_information(tmp_path: Path) -> None:
    inst = generate("rounds-dev-1")
    adv = FakeAdvisor(lambda u, e: 0.3)
    script = [
        execute("print(open('/task/fixtures/round-01/board.md').read())"),
        review("round 1 board shows no threads"),
        *[ADVANCE] * (inst.n_stages - 1),
        ROUNDS_FINAL,
    ]
    runner, _ = make(tmp_path, script, cfg_ir(), adv)
    res = runner.run(inst, "clm_helpers", initiated_review="jev")
    sent = json.dumps(adv.calls)
    for later in inst.stages[1:]:
        for path, text in later.items():
            if path.endswith("board.md"):
                for line in text.splitlines()[2:]:
                    if line.strip():
                        assert line.strip() not in sent
    for incident in inst.truth.incidents:
        assert incident["cause"] not in sent
    rec = res.metrics["initiated_review"]["records"][0]
    assert rec["stage"] == 1 and res.metrics["initiated_review"]["reviews_before_last_stage"] == 1


def test_service_failures_leave_the_agent_working_and_are_accounted(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.6)
    adv.fail_with = [JevError("http_529", "overloaded", retryable=True, billing="none")] * 2 + [
        JevError("timeout", "no response", retryable=True, billing="unknown")
    ]
    cfg = cfg_ir()
    cfg.jev.retries = 1
    script = [execute("print(1)"), review(), review("second"), FINAL]
    runner, _ = make(tmp_path, script, cfg, adv)
    res = runner.run(generate("dev"), "clm_helpers", initiated_review="jev")
    assert res.status == "completed"
    recs = res.metrics["initiated_review"]["records"]
    assert [r["status"] for r in recs] == ["advice_failed", "advised"]
    assert recs[0]["attempts"] == 2 and recs[1]["attempts"] == 2
    obs = review_obs(res.run_dir)
    assert "Service problem: http_529" in obs[0] and "no answer" in obs[0]
    led = json.loads((tmp_path / "ledger.json").read_text())
    jev = [e for e in led["entries"] if e["kind"] == "jev"]
    assert [e["status"] for e in jev] == ["error:http_529", "error:http_529", "error:timeout", "ok"]
    assert jev[0]["charged_usd"] == 0 and jev[2]["charged_usd"] > 0
    assert res.metrics["jev"]["errors"] == 3 and res.metrics["jev"]["retries"] == 2


def test_advice_residence_and_stale_labelling(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.7)
    script = [
        execute("NOTE:n1:Thread A ongoing"),
        review("Thread A resolved"),
        execute("print(3)"),
        execute("NOTE:n1:Thread A resolved"),  # removes the review observation too
        review("Thread B mitigated"),
        FINAL,
    ]
    runner, _ = make(tmp_path, script, cfg_ir(), adv, NoteExecutor())
    res = runner.run(generate("dev"), "clm_helpers", initiated_review="jev")
    r1, r2 = res.metrics["initiated_review"]["records"]
    assert r1["observation_requests_carried"] == 2  # requests 3 and 4; the step-4 edit drops it
    assert r2["revision"] > r1["revision"] and r2["candidate_ids"] == ["n1", "s4.obs"]
    o1, o2 = review_obs(res.run_dir)
    assert f"revision {r1['revision']};" in o1 and f"revision {r2['revision']};" in o2
    assert r1["later_accepted_edits"][-1]["step"] == 4 and "n1" in r1["candidates_changed"]


# ------------------------------------------------------------- conditions and export
def test_condition_flags() -> None:
    conds = parse_conditions("clm_helpers+rev:on,clm_helpers+rev-jev:on")
    assert conds == [("clm_helpers+rev", True), ("clm_helpers+rev-jev", True)]
    assert conditions_need_jev("clm_helpers+rev:on,clm_helpers+rev-jev:on")
    assert not conditions_need_jev("clm_helpers+rev:on,clm_helpers+upd:on")
    with pytest.raises(ValueError, match="rev-jev"):
        parse_conditions("clm_helpers+revjev:on")


def test_initiated_review_evidence_survives_export(tmp_path: Path) -> None:
    from clm_lib.export import export_experiment

    adv = FakeAdvisor(lambda u, e: 0.6)
    runner, _ = make(tmp_path, [execute("print(1)"), review(), FINAL], cfg_ir(), adv)
    res = runner.run(generate("dev"), "clm_helpers", initiated_review="jev")
    exp = tmp_path / "exp"
    exp.mkdir()
    spec = {
        "id": "992-rev",
        "title": "t",
        "runs": [{"run_id": res.run_id, "role": "calibration", "phase": "calibration"}],
        "comparisons": [],
    }
    (exp / "experiment.json").write_text(json.dumps(spec))
    export_experiment(exp, tmp_path / "runs", tmp_path / "ledger.json", tmp_path / "results")
    out = tmp_path / "results" / "experiments" / "992-rev"
    row = json.loads((out / "metrics.json").read_text())[0]
    assert row["initiated_review"] == "jev" and row["ir_reviews"] == 1
    assert row["ir_jev_attempts"] == 1 and row["cost_typesafe_usd"] > 0
    assert (out / "artifacts" / "runs" / "calibration" / res.run_id / "jev" / "0001.json").exists()
    assert "initiated review with Jev advice" in (out / "report.md").read_text()
