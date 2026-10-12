"""Experiment 008: optional host-mediated Jev context review (offline; Jev is faked)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clm_lib import jev as jevmod
from clm_lib.budget import Ledger
from clm_lib.cli import condition_schedule, parse_conditions
from clm_lib.config import Config, load_config
from clm_lib.jev import FakeAdvisor, JevError, JevPrice, TypeSafeAdvisor, build_request
from clm_lib.jev_diag import load_cases, run_diagnostic
from clm_lib.prompts import JEV_CLAUSE, system_prompt
from clm_lib.provider import ScriptedProvider
from clm_lib.runner import ROUNDS_ACTION_SCHEMA, Runner, parse_action
from clm_lib.tasks import generate

from .conftest import TEST_PRICE, FakeExecutor, execute, working_ids

REPO = Path(__file__).resolve().parents[1]
CASES = REPO / "experiments" / "008-jev-context-review" / "diagnostic-cases-v1.json"
PRICE = JevPrice("fake-jev", 0.042, 0.0, "test", "test")
FINAL = {
    "action": "final",
    "answer": {"root_cause": "x", "required_value": "0", "remedy": "x", "evidence_refs": []},
}


def cfg_jev(enabled: bool = True) -> Config:
    cfg = Config()
    cfg.jev.enabled = enabled
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


def review(
    ids: list[str], revision: int, update: str = "the db pool limit was raised"
) -> dict[str, Any]:
    return {"action": "review_context", "revision": revision, "entry_ids": ids, "update": update}


def events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines()]


def revisions(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((run_dir / "context").glob("rev-*.json"))]


# ------------------------------------------------------------- historical behaviour, opt-in
def test_historical_configs_and_prompts_are_unchanged() -> None:
    # Configs written before experiment 008 (later experiments set their own flags).
    historical = [p for p in sorted((REPO / "configs").glob("exp*.toml")) if p.stem < "exp008"]
    for p in [*historical, REPO / "configs" / "default.toml"]:
        c = load_config(p)
        assert c.jev.enabled is False and c.limits.status_shows_revision is False, p.name
    # Experiment 008 shows the revision to both arms; Jev is switched on per run, never by default.
    c = load_config(REPO / "configs" / "exp008.toml")
    assert c.jev.enabled is False and c.limits.status_shows_revision is True
    kw: dict[str, Any] = dict(
        exec_timeout=30, output_cap=6000, pressure_pct=70, tail=4, max_entries=200, max_body=24000
    )
    assert "review_context" not in system_prompt("clm_helpers", **kw)
    assert "review_context" not in json.dumps(ROUNDS_ACTION_SCHEMA)
    assert "review_context" in JEV_CLAUSE and "must" not in JEV_CLAUSE.lower()


def test_review_action_is_opt_in(tmp_path: Path) -> None:
    ok, _ = parse_action(json.dumps(review(["s1.obs"], 1)), 1000, False, allow_review=True)
    assert ok
    bad, err = parse_action(json.dumps(review(["s1.obs"], 1)), 1000, False, allow_review=False)
    assert bad is None and "execute" in err
    both = {**review(["a"], 1), "update_entry_id": "a"}
    assert parse_action(json.dumps(both), 1000, allow_review=True)[0] is None
    # Without an advisor, Jev cannot be enabled; without Jev, the action is rejected.
    runner, _ = make(tmp_path / "a", [FINAL], cfg_jev(True))
    with pytest.raises(ValueError, match="advisor"):
        runner.run(generate("dev"), "clm_helpers")
    runner, prov = make(
        tmp_path / "b", [execute("print(1)"), review(["s1.obs"], 2), FINAL, FINAL], cfg_jev(False)
    )
    res = runner.run(generate("dev"), "clm_helpers")
    assert "review_context" not in prov.requests[0].system
    assert res.metrics["counts"]["repair_calls"] == 1 and res.metrics["jev"]["enabled"] is False
    with pytest.raises(ValueError, match="CLM modes"):
        make(tmp_path / "c", [FINAL], cfg_jev(True), FakeAdvisor(lambda u, e: 0.5))[0].run(
            generate("dev"), "summary"
        )


# ------------------------------------------------------------- advice only
def test_advice_never_mutates_context_and_records_everything(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.9 if "pool" in e.lower() else 0.1)
    script = [execute("print(1)"), review(["s1.act", "s1.obs"], 1), FINAL]
    runner, prov = make(
        tmp_path, script, cfg_jev(), adv, FakeExecutor(outputs=["db pool wait 900ms"])
    )
    res = runner.run(generate("dev"), "clm_helpers")
    assert res.status == "completed" and "-jev-" in res.run_id
    assert (
        "review_context" in prov.requests[0].system and "context revision" in prov.requests[0].user
    )
    revs = revisions(res.run_dir)
    before = next(r for r in revs if r["number"] == 1)["entries"]
    after = revs[-1]["entries"]
    assert after[: len(before)] == before  # nothing removed or rewritten
    assert [e["id"] for e in after[len(before) :]] == ["s2.act", "s2.obs"]
    advice = after[-1]["body"]
    assert "s1.obs: 0.90" in advice and "Coverage: 2 of 2" in advice and "unchanged" in advice
    # The request carried only the update and the listed entries' exact text from revision 2.
    body = adv.calls[0]
    assert body["state"] == {"update": "the db pool limit was raised"}
    assert [q["instructions"]["entry"] for q in body["questions"].values()] == [
        e["body"] for e in before
    ]
    assert all(q["instructions"]["question"] == jevmod.QUESTION for q in body["questions"].values())
    j = res.metrics["jev"]
    assert j["reviews"] == 1 and j["attempts"] == 1 and j["errors"] == 0
    rec = j["review_records"][0]
    assert rec["answers"] == {"s1.act": 0.1, "s1.obs": 0.9} and rec["flagged"] == ["s1.obs"]
    saved = json.loads((res.run_dir / "jev" / "0001.json").read_text())
    assert saved["request"] == body and saved["model"] == "fake-jev"
    led = json.loads((tmp_path / "ledger.json").read_text())
    kinds = [e["kind"] for e in led["entries"]]
    assert kinds.count("jev") == 1 and kinds.count("action") == 3
    assert res.metrics["cost"]["by_provider"]["typesafe"] > 0
    assert res.metrics["counts"]["provider_calls"] == 3  # the review used a main-model turn


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        (review(["s1.obs"], 99), "does not exist"),
        (review(["nope"], 1), "not in revision"),
        (review(["s1.obs", "s1.obs"], 1), "duplicates"),
        (review(["s1.obs"], 1, update="   "), "empty"),
        (review(["s1.obs"], 1, update="x" * 5000), "at most"),
        (
            {
                "action": "review_context",
                "revision": 1,
                "entry_ids": ["s1.obs"],
                "update_entry_id": "zz",
            },
            "update_entry_id",
        ),
    ],
)
def test_invalid_reviews_are_refused_without_calling_jev(
    tmp_path: Path, action: dict[str, Any], reason: str
) -> None:
    adv = FakeAdvisor(lambda u, e: 0.5)
    runner, _ = make(tmp_path, [execute("print(1)"), action, FINAL], cfg_jev(), adv)
    res = runner.run(generate("dev"), "clm_helpers")
    assert res.status == "completed" and adv.calls == []
    assert res.metrics["jev"]["reviews_rejected"] == 1
    obs = revisions(res.run_dir)[-1]["entries"][-1]["body"]
    assert obs.startswith("Context review not performed") and reason in obs


def test_update_by_entry_reference_and_review_limit(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.7)
    cfg = cfg_jev()
    cfg.jev.max_reviews = 1
    ref = {
        "action": "review_context",
        "revision": 1,
        "entry_ids": ["s1.act"],
        "update_entry_id": "s1.obs",
    }
    runner, _ = make(
        tmp_path,
        [execute("print('NEW FACT')"), ref, {**ref, "revision": 2}, FINAL],
        cfg,
        adv,
        FakeExecutor(outputs=["NEW FACT"]),
    )
    res = runner.run(generate("dev"), "clm_helpers")
    assert "NEW FACT" in adv.calls[0]["state"]["update"] and len(adv.calls) == 1
    assert res.metrics["jev"]["reviews"] == 1 and res.metrics["jev"]["reviews_rejected"] == 1
    assert "review limit reached" in res.metrics["jev"]["review_records"][1]["error"]


def test_service_failures_and_malformed_answers_leave_the_agent_working(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: None)  # no usable answers
    adv.fail_with = [JevError("http_529", "overloaded", retryable=True, billing="none")] * 2
    cfg = cfg_jev()
    cfg.jev.retries = 1
    script = [execute("print(1)"), review(["s1.obs"], 1), review(["s1.obs"], 2), FINAL]
    runner, _ = make(tmp_path, script, cfg, adv)
    res = runner.run(generate("dev"), "clm_helpers")
    assert res.status == "completed"
    j = res.metrics["jev"]
    assert j["attempts"] == 3 and j["errors"] == 2 and j["retries"] == 1
    obs = [e["body"] for e in revisions(res.run_dir)[-1]["entries"] if e["role"] == "observation"]
    assert any("Service problem" in o and "0 of 1" in o for o in obs)
    assert any("no answer" in o for o in obs)  # malformed answers are reported, not hidden
    led = json.loads((tmp_path / "ledger.json").read_text())
    assert [e["status"] for e in led["entries"] if e["kind"] == "jev"] == [
        "error:http_529",
        "error:http_529",
        "ok",
    ]


class DeleteObsExecutor(FakeExecutor):
    """Code containing DELETE removes s1.obs from context.json (an accepted model edit)."""

    def run(self, code: str, workspace: Path, fixtures: Path) -> Any:
        if "DELETE" in code:
            d = json.loads((workspace / "context.json").read_text())
            d["entries"] = [e for e in d["entries"] if e["id"] != "s1.obs"]
            (workspace / "context.json").write_text(json.dumps(d))
        return super().run(code, workspace, fixtures)


def shown_revision(user: str) -> int:
    line = next(x for x in user.splitlines() if x.startswith("context revision "))
    return int(line.split()[-1])


SECRET = "DELETED-OBSERVATION-TEXT"
# Step 1 (rev 1) records SECRET only as s1.obs; step 2 deletes s1.obs (rev 2) and the runtime
# appends the step (rev 3), the revision the model is shown at step 3.
BEFORE_REVIEW: list[Any] = [execute("print(1)"), execute("DELETE")]


def deleting_executor() -> FakeExecutor:
    return DeleteObsExecutor(outputs=[SECRET, "ok"])


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        (review(["s1.obs"], 1), "revision 1 is no longer current"),
        (
            {
                "action": "review_context",
                "revision": 1,
                "entry_ids": ["s1.act"],
                "update_entry_id": "s1.obs",
            },
            "revision 1 is no longer current",
        ),
        (review(["s1.obs"], 3), "not in revision 3"),
        (
            {
                "action": "review_context",
                "revision": 3,
                "entry_ids": ["s1.act"],
                "update_entry_id": "s1.obs",
            },
            "update_entry_id 's1.obs' is not in revision 3",
        ),
    ],
)
def test_deleted_entries_never_reach_jev(
    tmp_path: Path, action: dict[str, Any], reason: str
) -> None:
    """Candidates and update_entry_id resolve only from the current revision, so an entry the
    agent deleted cannot be sent to Jev by naming an older revision or by its id."""
    adv = FakeAdvisor(lambda u, e: 0.5)
    runner, prov = make(
        tmp_path, [*BEFORE_REVIEW, action, FINAL], cfg_jev(), adv, deleting_executor()
    )
    res = runner.run(generate("dev"), "clm_helpers")
    rev1 = next(r for r in revisions(res.run_dir) if r["number"] == 1)
    assert SECRET in {e["id"]: e["body"] for e in rev1["entries"]}["s1.obs"]  # it existed
    assert shown_revision(prov.requests[2].user) == 3
    assert "s1.obs" not in working_ids(prov.requests[2].user)
    assert res.status == "completed" and adv.calls == []  # no Jev request
    led = json.loads((tmp_path / "ledger.json").read_text())
    assert [e["kind"] for e in led["entries"]].count("jev") == 0  # no paid reservation
    j = res.metrics["jev"]
    assert j["reviews_rejected"] == 1 and j["reviews"] == 0 and j["attempts"] == 0
    obs = revisions(res.run_dir)[-1]["entries"][-1]["body"]
    assert obs.startswith("Context review not performed") and reason in obs
    assert SECRET not in obs and not (res.run_dir / "jev").exists()


def test_current_revision_review_uses_the_revision_shown_to_the_model(tmp_path: Path) -> None:
    adv = FakeAdvisor(lambda u, e: 0.8)
    script = [*BEFORE_REVIEW, review(["s1.act", "s2.obs"], 3), FINAL]
    runner, prov = make(tmp_path, script, cfg_jev(), adv, deleting_executor())
    res = runner.run(generate("dev"), "clm_helpers")
    assert [shown_revision(r.user) for r in prov.requests] == [0, 1, 3, 4]
    assert res.status == "completed" and len(adv.calls) == 1
    current = next(r for r in revisions(res.run_dir) if r["number"] == 3)["entries"]
    by_id = {e["id"]: e["body"] for e in current}
    sent = [q["instructions"]["entry"] for q in adv.calls[0]["questions"].values()]
    assert sent == [by_id["s1.act"], by_id["s2.obs"]] and SECRET not in json.dumps(adv.calls)
    rec = res.metrics["jev"]["review_records"][0]
    assert rec["status"] == "ok" and rec["revision"] == 3
    obs = revisions(res.run_dir)[-1]["entries"][-1]["body"]
    assert "revision 3;" in obs and "s2.obs: 0.80" in obs


def test_requests_hold_only_agent_visible_text(tmp_path: Path) -> None:
    from clm_lib.config import RequestConfig

    inst = generate("rounds-dev-1")
    future = inst.files["round-02/board.md"]
    adv = FakeAdvisor(lambda u, e: 0.2)
    cfg = cfg_jev()
    cfg.request = RequestConfig(layout="blocks/1", run_isolation="run-tag/1")
    script = [
        execute("print(open('/task/fixtures/round-01/board.md').read())"),
        review(["s1.obs"], 1, "board shows no threads yet"),
    ]
    script += [{"action": "advance"}] * (inst.n_stages - 1)
    script += [{"action": "final", "answer": {"incidents": [], "unresolved": [], "summary": "x"}}]
    runner, _ = make(tmp_path, script, cfg, adv)
    res = runner.run(inst, "clm_helpers")
    text = json.dumps(adv.calls)
    assert future.splitlines()[2] not in text  # no future round content
    for inc in inst.truth.incidents:
        assert inc["cause"] not in text  # no hidden truth
    assert res.metrics["jev"]["reviews"] == 1


def test_edits_after_advice_still_determine_the_next_request(tmp_path: Path) -> None:
    class KeepOneExecutor(FakeExecutor):
        def run(self, code: str, workspace: Path, fixtures: Path) -> Any:
            if "KEEP" in code:
                d = json.loads((workspace / "context.json").read_text())
                d["entries"] = [{"id": "n1", "role": "note", "body": "kept after advice"}]
                (workspace / "context.json").write_text(json.dumps(d))
            return super().run(code, workspace, fixtures)

    adv = FakeAdvisor(lambda u, e: 0.95)
    script = [execute("print('old fact')"), review(["s1.obs"], 1), execute("KEEP"), FINAL]
    runner, prov = make(tmp_path, script, cfg_jev(), adv, KeepOneExecutor())
    res = runner.run(generate("dev"), "clm_helpers")
    assert working_ids(prov.requests[3].user) == ["n1", "s3.act", "s3.obs", "s3.rcpt"]
    rec = res.metrics["jev"]["review_records"][0]
    assert rec["flagged_then_changed"] == ["s1.obs"] and rec["later_accepted_edit_steps"] == [3]


# ------------------------------------------------------------- HTTP client
def test_typesafe_client_parses_and_classifies_without_hidden_retries() -> None:
    seen: list[tuple[str, dict[str, str], bytes]] = []
    replies = [
        (
            200,
            json.dumps(
                {
                    "model": "jev-1.13.0",
                    "answers": {"e0": {"type": "noul", "noul": 0.81}},
                    "usage": {"input_tokens": 120, "output_tokens": 1},
                }
            ).encode(),
        ),
        (529, b"overloaded"),
        (401, b"no"),
        (200, b"not json"),
    ]

    def transport(
        url: str, headers: dict[str, str], data: bytes, timeout: float
    ) -> tuple[int, bytes]:
        seen.append((url, headers, data))
        return replies.pop(0)

    adv = TypeSafeAdvisor("sk-test-not-real", transport=transport)
    body = build_request("u", [jevmod.ReviewItem("a", "entry text")], adv.model)
    resp, _ = adv.call(body)
    assert (
        resp.model == "jev-1.13.0"
        and resp.probabilities == {"e0": 0.81}
        and resp.input_tokens == 120
    )
    assert seen[0][1]["Authorization"] == "Bearer sk-test-not-real"
    assert json.loads(seen[0][2]) == body and seen[0][0] == "https://api.typesafe.ai/v1/systemone"
    with pytest.raises(JevError) as e1:
        adv.call(body)
    assert e1.value.retryable and len(seen) == 2  # one attempt per call: no internal retries
    with pytest.raises(JevError) as e2:
        adv.call(body)
    assert e2.value.kind == "auth" and not e2.value.retryable
    with pytest.raises(JevError, match="malformed"):
        adv.call(body)
    assert "sk-test" not in repr(adv)
    assert PRICE.cost(1_000_000, 50) == pytest.approx(0.042)


# ------------------------------------------------------------- diagnostic, schedule, export
def test_diagnostic_cases_and_scoring(tmp_path: Path) -> None:
    doc, frozen = load_cases(CASES, "frozen")
    _, dev = load_cases(CASES, "dev")
    assert len(frozen) == 24 and len(dev) == 6 and doc["reporting_threshold"] == 0.5
    assert not {c["update_id"] for c in dev} & {c["update_id"] for c in frozen}
    kinds = {c["kind"] for c in frozen}
    assert {
        "partial_change",
        "partial_unchanged",
        "historical",
        "ambiguous",
        "multi_fact",
        "unrelated",
    } <= kinds
    labels = {c["entry"]: c["label"] for c in frozen}
    adv = FakeAdvisor(
        lambda u, e: {"affected": 0.9, "unaffected": 0.1, "ambiguous": 0.5}[labels[e]]
    )
    out = run_diagnostic(
        adv,
        PRICE,
        Ledger.open(tmp_path / "l.json", 100.0),
        CASES,
        "frozen",
        tmp_path / "diag",
        "fake-jev",
    )
    s = json.loads((out / "summary.json").read_text())["score"]
    assert s["recall"] == 1.0 and s["precision"] == 1.0 and len(s["ambiguous"]) == 2
    assert s["requests"] == len({c["update_id"] for c in frozen}) == len(adv.calls)


def test_two_condition_parsing_and_balanced_schedule() -> None:
    conds = parse_conditions("clm_helpers:on,clm_helpers+jev:on")
    assert conds == [("clm_helpers", True), ("clm_helpers+jev", True)]
    pilot = condition_schedule(["rounds-dev-1", "rounds-dev-2"], 1, conds)
    assert [r["mode"] for r in pilot] == [
        "clm_helpers",
        "clm_helpers+jev",
        "clm_helpers+jev",
        "clm_helpers",
    ]
    big = condition_schedule([f"t{i}" for i in range(4)], 3, conds)
    for t in range(4):
        firsts = [r["mode"] for r in big if r["task"] == f"t{t}" and r["position"] == 0]
        assert len(set(firsts[:2])) == 2  # alternates across repetitions of an instance
    with pytest.raises(ValueError):
        parse_conditions("clm_helpers+x:on,clm:on")


def test_jev_evidence_survives_export(tmp_path: Path) -> None:
    from clm_lib.export import export_experiment

    adv = FakeAdvisor(lambda u, e: 0.6)
    runner, _ = make(tmp_path, [execute("print(1)"), review(["s1.obs"], 1), FINAL], cfg_jev(), adv)
    res = runner.run(generate("dev"), "clm_helpers")
    diag = run_diagnostic(
        adv, PRICE, runner.ledger, CASES, "dev", tmp_path / "runs" / "jev-diagnostic", "fake-jev"
    )
    exp = tmp_path / "exp"
    exp.mkdir()
    spec = {
        "id": "994-jev",
        "title": "t",
        "runs": [{"run_id": res.run_id, "role": "calibration", "phase": "pilot"}],
        "comparisons": [],
        "diagnostics": [diag.name],
    }
    (exp / "experiment.json").write_text(json.dumps(spec))
    export_experiment(exp, tmp_path / "runs", tmp_path / "ledger.json", tmp_path / "results")
    out = tmp_path / "results" / "experiments" / "994-jev"
    row = json.loads((out / "metrics.json").read_text())[0]
    assert row["jev_enabled"] and row["jev_reviews"] == 1 and row["cost_typesafe_usd"] > 0
    assert (out / "artifacts" / "runs" / "calibration" / res.run_id / "jev" / "0001.json").exists()
    assert (out / "artifacts" / "diagnostics" / diag.name / "summary.json").exists()
    assert "Jev review" in (out / "report.md").read_text()
