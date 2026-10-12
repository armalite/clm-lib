"""Experiment 009 (strategy 1): update-triggered context review (offline; Jev is faked)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clm_lib.budget import Ledger
from clm_lib.cli import condition_schedule, conditions_need_jev, parse_conditions
from clm_lib.config import Config, load_config
from clm_lib.jev import FakeAdvisor, JevError, JevPrice
from clm_lib.prompts import UPDATE_REVIEW_CLAUSE, UPDATE_REVIEW_JEV_CLAUSE, system_prompt
from clm_lib.provider import ScriptedProvider
from clm_lib.runner import Runner
from clm_lib.tasks import generate
from clm_lib.update_review import build_payload

from .conftest import TEST_PRICE, FakeExecutor, execute, working_ids

REPO = Path(__file__).resolve().parents[1]
PRICE = JevPrice("fake-jev", 0.042, 0.0, "test", "test")
ROUNDS_FINAL = {"action": "final", "answer": {"incidents": [], "unresolved": [], "summary": "x"}}
ADVANCE = {"action": "advance"}


def cfg_ur() -> Config:
    cfg = Config()
    cfg.limits.status_shows_revision = True
    return cfg


def make(
    tmp_path: Path, script: list[Any], cfg: Config, advisor: Any = None, executor: Any = None
) -> tuple[Runner, ScriptedProvider]:
    prov = ScriptedProvider(list(script))
    runner = Runner(
        cfg,
        prov,
        executor or FakeExecutor(),
        Ledger.open(tmp_path / "ledger.json", 100.0),
        TEST_PRICE,
        live=False,
        runs_dir=tmp_path / "runs",
        sleep=lambda s: None,
        jev=advisor,
        jev_price=PRICE if advisor else None,
    )
    return runner, prov


def full_script(task: str, before: list[Any] | None = None) -> list[Any]:
    n = generate(task).n_stages
    return [*(before or [execute("print(1)")]), *[ADVANCE] * (n - 1), ROUNDS_FINAL]


def events(run_dir: Path, kind: str) -> list[dict[str, Any]]:
    out = [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines()]
    return [e for e in out if e["event"] == kind]


def revisions(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((run_dir / "context").glob("rev-*.json"))]


def release_obs(run_dir: Path) -> list[str]:
    """The release observations, in order, from the final context and its history."""
    seen: dict[str, str] = {}
    for r in revisions(run_dir):
        for e in r["entries"]:
            if e["role"] == "observation" and e["body"].startswith("Stage "):
                seen.setdefault(e["id"], e["body"])
    return list(seen.values())


# ------------------------------------------------------------- historical behaviour, opt-in
def test_historical_configs_prompts_and_runs_are_unchanged(tmp_path: Path) -> None:
    for p in [*sorted((REPO / "configs").glob("exp*.toml")), REPO / "configs" / "default.toml"]:
        if p.stem == "exp009":
            continue
        c = load_config(p)
        assert c.update_review.enabled is False and c.update_review.advice is False, p.name
    kw: dict[str, Any] = dict(
        exec_timeout=30, output_cap=6000, pressure_pct=70, tail=4, max_entries=200, max_body=24000
    )
    for mode in ("clm", "clm_helpers", "clm_direct", "clm_reuse", "summary"):
        assert "UPDATE REVIEW" not in system_prompt(mode, task_kind="rounds", **kw)
    # Without the feature, a rounds run shows the historical release text only.
    runner, prov = make(tmp_path, full_script("rounds-dev-1"), cfg_ur())
    res = runner.run(generate("rounds-dev-1"), "clm_helpers")
    assert res.status == "completed" and "-upd" not in res.run_id
    assert all("UPDATE (" not in o for o in release_obs(res.run_dir))
    assert "UPDATE REVIEW" not in prov.requests[0].system
    assert res.metrics["update_review"]["mode"] == "off" and not events(
        res.run_dir, "update_review"
    )


def test_update_review_is_opt_in_and_validated(tmp_path: Path) -> None:
    runner, _ = make(tmp_path / "a", [ROUNDS_FINAL], cfg_ur())
    with pytest.raises(ValueError, match="no Jev advisor"):
        runner.run(generate("rounds-dev-1"), "clm_helpers", update_review="jev")
    with pytest.raises(ValueError, match="rounds tasks only"):
        runner.run(generate("dev"), "clm_helpers", update_review="control")
    with pytest.raises(ValueError, match="CLM modes"):
        runner.run(generate("rounds-dev-1"), "summary", update_review="control")
    adv = FakeAdvisor(lambda u, e: 0.5)
    runner, _ = make(tmp_path / "b", [ROUNDS_FINAL], cfg_ur(), adv)
    with pytest.raises(ValueError, match="exclusive"):
        runner.run(generate("rounds-dev-1"), "clm_helpers", jev=True, update_review="jev")
    with pytest.raises(ValueError, match="off, control or jev"):
        runner.run(generate("rounds-dev-1"), "clm_helpers", update_review="maybe")
    # The config default applies when the run does not override it.
    cfg = cfg_ur()
    cfg.update_review.enabled = True
    runner, prov = make(tmp_path / "c", full_script("rounds-dev-1"), cfg)
    res = runner.run(generate("rounds-dev-1"), "clm_helpers")
    assert "-upd-" in res.run_id and res.metrics["update_review"]["mode"] == "control"
    assert UPDATE_REVIEW_CLAUSE in prov.requests[0].system
    assert UPDATE_REVIEW_JEV_CLAUSE not in prov.requests[0].system


def test_payload_is_mechanical_board_and_change_lines_only() -> None:
    inst = generate("rounds-dev-1")
    payload, sources, cut = build_payload(inst.stages[7], 4000)
    assert sources == ["round-08/board.md", "round-08/changes.md"] and not cut
    board = inst.stages[7]["round-08/board.md"].splitlines()
    for line in payload.splitlines():
        path, num, text = line.split(":", 2)
        assert inst.stages[7][path].splitlines()[int(num) - 1].strip() == text.strip()
    assert board[0].startswith("#") and board[0] not in payload  # headings dropped
    assert "logs/" not in payload
    empty = [k for k, st in enumerate(inst.stages, 1) if not build_payload(st, 4000)[0]]
    assert empty == [7]  # round 7's board has no update lines and there is no change log
    short, _, cut = build_payload(inst.stages[7], 120)
    assert cut and short.endswith("the full files are released]") and len(short) < 200


# ------------------------------------------------------------- matched arms, advice only
def test_both_arms_get_identical_update_text_at_the_same_point(tmp_path: Path) -> None:
    task = "rounds-dev-1"
    adv = FakeAdvisor(lambda u, e: 0.8 if "board" in e else 0.1)
    runs = {}
    for arm, advisor in (("control", None), ("jev", adv)):
        runner, prov = make(tmp_path / arm, full_script(task), cfg_ur(), advisor)
        res = runner.run(generate(task), "clm_helpers", update_review=arm)
        assert res.status == "completed"
        runs[arm] = (res, prov)
    c_res, c_prov = runs["control"]
    j_res, j_prov = runs["jev"]
    assert "-upd-" in c_res.run_id and "-updjev-" in j_res.run_id
    c_obs, j_obs = release_obs(c_res.run_dir), release_obs(j_res.run_dir)
    assert len(c_obs) == len(j_obs) == generate(task).n_stages - 1
    for c, j in zip(c_obs, j_obs, strict=True):
        assert j.startswith(c)  # identical release + update text; advice only appended
        assert "Context review advice" not in c
    assert sum("Context review advice" in j for j in j_obs) == 8  # rounds 2-10 minus round 7
    c_recs = c_res.metrics["update_review"]["records"]
    j_recs = j_res.metrics["update_review"]["records"]
    assert [r["payload_sha256"] for r in c_recs] == [r["payload_sha256"] for r in j_recs]
    assert [r["step"] for r in c_recs] == [r["step"] for r in j_recs]
    assert [r["status"] for r in c_recs].count("no_update") == 1
    # The only system-prompt difference is the assisted arm's explanation of the advice.
    j_sys, c_sys = j_prov.requests[0].system, c_prov.requests[0].system
    assert (
        UPDATE_REVIEW_JEV_CLAUSE in j_sys and j_sys.replace(UPDATE_REVIEW_JEV_CLAUSE, "") == c_sys
    )
    # Jev scored every entry of the revision current at each release, before the next call.
    for r in j_recs:
        if r["status"] == "advised":
            rev = next(x for x in revisions(j_res.run_dir) if x["number"] == r["revision"])
            assert r["candidate_ids"] == [e["id"] for e in rev["entries"]]
            assert list(r["answers"]) == r["candidate_ids"]
    assert len(adv.calls) == 8 and all(c["state"]["update"] for c in adv.calls)
    assert [c["state"]["update"] for c in adv.calls] == [
        r["payload"] for r in j_recs if r["status"] == "advised"
    ]
    s = j_res.metrics["update_review"]
    assert s["updates_advised"] == 8 and s["jev_attempts"] == 8 and s["jev_input_tokens"] > 0
    assert s["advice_chars"] > 0 and j_res.metrics["jev"]["source"] == "update_review"
    assert j_res.metrics["cost"]["by_provider"]["typesafe"] > 0
    led = json.loads((tmp_path / "jev" / "ledger.json").read_text())
    assert [e["kind"] for e in led["entries"]].count("jev") == 8
    assert c_res.metrics["counts"]["provider_calls"] == j_res.metrics["counts"]["provider_calls"]


def test_advice_never_mutates_context(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.99)
    runner, _ = make(tmp_path, full_script("rounds-dev-1"), cfg_ur(), adv)
    res = runner.run(generate("rounds-dev-1"), "clm_helpers", update_review="jev")
    revs = {r["number"]: r for r in revisions(res.run_dir)}
    for r in res.metrics["update_review"]["records"]:
        before = revs[r["revision"]]["entries"]
        after = revs[r["revision"] + 1]["entries"]
        assert after[: len(before)] == before  # nothing removed or rewritten
        assert [e["id"] for e in after[len(before) :]] == [f"s{r['step']}.act", f"s{r['step']}.obs"]
    assert res.metrics["counts"]["edits_accepted_changed"] == 0


def test_jev_sees_only_current_context_and_released_lines(tmp_path: Path) -> None:
    class DeleteFirstObs(FakeExecutor):
        def run(self, code: str, workspace: Path, fixtures: Path) -> Any:
            if "DELETE" in code:
                d = json.loads((workspace / "context.json").read_text())
                d["entries"] = [e for e in d["entries"] if e["id"] != "s1.obs"]
                (workspace / "context.json").write_text(json.dumps(d))
            return super().run(code, workspace, fixtures)

    task = "rounds-dev-1"
    inst = generate(task)
    secret = "OBSERVATION-THE-AGENT-DELETED"
    adv = FakeAdvisor(lambda u, e: 0.3)
    script = full_script(task, [execute("print(1)"), execute("DELETE")])
    runner, _ = make(tmp_path, script, cfg_ur(), adv, DeleteFirstObs(outputs=[secret, "ok"]))
    res = runner.run(inst, "clm_helpers", update_review="jev")
    sent = json.dumps(adv.calls)
    assert secret not in sent  # deleted before the first release, so never a candidate
    first = res.metrics["update_review"]["records"][0]
    assert "s1.obs" not in first["candidate_ids"] and first["stage"] == 2
    rev1 = next(r for r in revisions(res.run_dir) if r["number"] == 1)
    assert any(secret in e["body"] for e in rev1["entries"])  # it was in context before
    for call in adv.calls:
        upd = call["state"]["update"]
        released = {p for line in upd.splitlines() for p in [line.split(":", 1)[0]]}
        assert all(p.startswith("round-") and "/logs/" not in p for p in released)
    for later in inst.stages[2:]:  # nothing from rounds after the second is in the first call
        for path, text in later.items():
            if path.endswith("board.md"):
                for line in text.splitlines()[2:]:
                    if line.strip():
                        assert line.strip() not in json.dumps(adv.calls[0])
    for incident in inst.truth.incidents:
        assert incident["cause"] not in json.dumps(adv.calls[0])  # not yet released
    assert "plan {" not in sent and inst.truth.notes not in sent


def test_failed_advice_leaves_the_agent_working_and_is_accounted(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.4)
    adv.fail_with = [JevError("http_529", "overloaded", retryable=True, billing="none")] * 2 + [
        JevError("timeout", "no response", retryable=True, billing="unknown")
    ] * 2
    cfg = cfg_ur()
    cfg.jev.retries = 1
    runner, _ = make(tmp_path, full_script("rounds-dev-1"), cfg, adv)
    res = runner.run(generate("rounds-dev-1"), "clm_helpers", update_review="jev")
    assert res.status == "completed"
    recs = res.metrics["update_review"]["records"]
    assert [r["status"] for r in recs[:2]] == ["advice_failed", "advice_failed"]
    assert recs[2]["status"] == "advised"
    obs = release_obs(res.run_dir)
    assert "Service problem: http_529" in obs[0] and "no answer" in obs[0]
    assert "Coverage: 0 of" in obs[1] and "Service problem: timeout" in obs[1]
    led = json.loads((tmp_path / "ledger.json").read_text())
    jev = [e for e in led["entries"] if e["kind"] == "jev"]
    assert [e["status"] for e in jev[:4]] == ["error:http_529"] * 2 + ["error:timeout"] * 2
    assert jev[0]["charged_usd"] == 0 and jev[2]["charged_usd"] > 0  # unknown outcome charged
    assert res.metrics["jev"]["errors"] == 4 and res.metrics["jev"]["retries"] == 2


def test_accepted_edits_after_advice_determine_the_next_request(tmp_path: Path) -> None:
    class KeepNote(FakeExecutor):
        def run(self, code: str, workspace: Path, fixtures: Path) -> Any:
            if "KEEP" in code:
                d = json.loads((workspace / "context.json").read_text())
                d["entries"] = [{"id": "n1", "role": "note", "body": "Thread B: CACHE_STAMPEDE"}]
                (workspace / "context.json").write_text(json.dumps(d))
            return super().run(code, workspace, fixtures)

    task = "rounds-dev-1"
    n = generate(task).n_stages
    adv = FakeAdvisor(lambda u, e: 0.9 if "CACHE_STAMPEDE" in e else 0.2)
    script = [execute("print(1)"), ADVANCE, execute("KEEP"), *[ADVANCE] * (n - 2), ROUNDS_FINAL]
    runner, prov = make(tmp_path, script, cfg_ur(), adv, KeepNote())
    res = runner.run(generate(task), "clm_helpers", update_review="jev")
    assert working_ids(prov.requests[3].user) == ["n1", "s3.act", "s3.obs", "s3.rcpt"]
    recs = res.metrics["update_review"]["records"]
    assert recs[0]["later_accepted_edits"][0]["step"] == 3
    assert set(recs[0]["candidates_changed"]) == {"s1.act", "s1.obs"}
    assert recs[1]["candidate_ids"][0] == "n1" and recs[1]["flagged"] == ["n1"]
    assert recs[1]["flagged_not_changed"] == ["n1"]  # observed as left unchanged


def test_candidate_cap_and_batching_are_explicit(tmp_path: Path) -> None:
    cfg = cfg_ur()
    cfg.update_review.max_candidates = 2
    cfg.update_review.max_batch_chars = 10
    adv = FakeAdvisor(lambda u, e: 0.5)
    task = "rounds-dev-1"
    script = [execute("print(1)"), execute("print(2)"), ADVANCE, ROUNDS_FINAL, ROUNDS_FINAL]
    runner, _ = make(tmp_path, script, cfg, adv)
    res = runner.run(generate(task), "clm_helpers", update_review="jev")
    rec = res.metrics["update_review"]["records"][0]
    assert rec["candidate_ids"] == ["s1.act", "s1.obs"]
    assert rec["not_assessed_ids"] == ["s2.act", "s2.obs"]  # listed, never silently dropped
    assert rec["batches"] == [1, 1] and len(adv.calls) == 2  # character limit splits batches
    obs = release_obs(res.run_dir)[0]
    assert "Not assessed (over the 2-entry limit)" in obs


# ------------------------------------------------------------- conditions and export
def test_condition_flags_and_schedule() -> None:
    conds = parse_conditions("clm_helpers+upd:on,clm_helpers+upd-jev:on")
    assert conds == [("clm_helpers+upd", True), ("clm_helpers+upd-jev", True)]
    assert conditions_need_jev("clm_helpers+upd:on,clm_helpers+upd-jev:on")
    assert not conditions_need_jev("clm_helpers+upd:on,clm_helpers:on")
    assert conditions_need_jev("clm_helpers+jev:on")
    with pytest.raises(ValueError, match="upd-jev"):
        parse_conditions("clm_helpers+updjev:on")
    rows = condition_schedule(["rounds-dev-1", "rounds-dev-2"], 1, conds)
    order = [(r["task"], r["mode"]) for r in rows]
    assert order == [
        ("rounds-dev-1", "clm_helpers+upd"),
        ("rounds-dev-1", "clm_helpers+upd-jev"),
        ("rounds-dev-2", "clm_helpers+upd-jev"),
        ("rounds-dev-2", "clm_helpers+upd"),
    ]


def test_update_review_evidence_survives_export(tmp_path: Path) -> None:
    from clm_lib.export import export_experiment

    adv = FakeAdvisor(lambda u, e: 0.6)
    runner, _ = make(tmp_path, full_script("rounds-dev-1"), cfg_ur(), adv)
    res = runner.run(generate("rounds-dev-1"), "clm_helpers", update_review="jev")
    exp = tmp_path / "exp"
    exp.mkdir()
    spec = {
        "id": "993-upd",
        "title": "t",
        "runs": [{"run_id": res.run_id, "role": "calibration", "phase": "pilot"}],
        "comparisons": [],
    }
    (exp / "experiment.json").write_text(json.dumps(spec))
    export_experiment(exp, tmp_path / "runs", tmp_path / "ledger.json", tmp_path / "results")
    out = tmp_path / "results" / "experiments" / "993-upd"
    row = json.loads((out / "metrics.json").read_text())[0]
    assert row["update_review"] == "jev" and row["ur_updates_advised"] == 8
    assert row["ur_jev_attempts"] == 8 and row["cost_typesafe_usd"] > 0
    assert row["ur_models"] == '["fake-jev"]' and row["ur_candidates"] > 0
    assert (out / "artifacts" / "runs" / "calibration" / res.run_id / "jev" / "0001.json").exists()
    assert "update review with Jev advice" in (out / "report.md").read_text()
