"""Experiment 007: rounds task generation and scoring, condition instructions, the separate
summary-call allowance, helper evidence tiers and the three-condition schedule."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from clm_lib import rounds
from clm_lib.budget import Ledger
from clm_lib.cli import ORDERS_3, condition_schedule, parse_conditions
from clm_lib.config import Config, RequestConfig, load_config
from clm_lib.helper_audit import audit_run, classify_function
from clm_lib.prompts import CLM_CLAUSE, CLM_INSTRUCTIONS, system_prompt
from clm_lib.provider import ModelRequest, ScriptedProvider
from clm_lib.runner import Runner, parse_action
from clm_lib.tasks import INSTANCES, generate

from .conftest import TEST_PRICE, FakeExecutor, execute

REPO = Path(__file__).resolve().parents[1]
NAMES = [n for n in INSTANCES if n.startswith("rounds-")]
ADVANCE = {"action": "advance"}
KW: dict[str, Any] = dict(
    exec_timeout=30, output_cap=6000, pressure_pct=70, tail=4, max_entries=200, max_body=24000
)


def correct_answer(truth: rounds.RoundsTruth) -> dict[str, Any]:
    incidents = []
    for inc in truth.incidents:
        refs = [f"{rows[0][0]}:{rows[0][1]}" for rows in inc["evidence_groups"].values()]
        incidents.append(
            {
                "cause": inc["cause"],
                "services": list(inc["services"]),
                "status": inc["status"],
                "evidence_refs": refs,
            }
        )
    return {"incidents": incidents, "unresolved": list(truth.unresolved), "summary": "x"}


def final(answer: dict[str, Any]) -> dict[str, Any]:
    return {"action": "final", "answer": answer}


# ------------------------------------------------------------- generation
def test_generation_is_deterministic_and_instances_vary_meaningfully() -> None:
    seen = []
    for name in NAMES:
        a, b = generate(name), generate(name)
        assert a.fixture_sha256 == b.fixture_sha256 and a.kind == "rounds"
        assert 10 <= a.n_stages <= 12 and a.scorer_version == "rounds-score/1"
        t = a.truth
        seen.append(
            (
                tuple(i["cause"] for i in t.incidents),
                tuple(tuple(i["services"]) for i in t.incidents),
                tuple(t.unresolved),
                t.notes.split(";")[0],
            )
        )
    for i, x in enumerate(seen):  # causes, services, follow-ups and event timings all differ
        for y in seen[i + 1 :]:
            assert x[0] != y[0] or x[1] != y[1]
            assert x[3] != y[3]
    assert {generate(n).n_stages for n in NAMES} == {10, 11, 12}


def test_every_required_answer_is_established_by_the_evidence() -> None:
    for name in NAMES:
        inst = generate(name)
        t, files = inst.truth, inst.files
        n = inst.n_stages
        for inc in t.incidents:
            for group, rows in inc["evidence_groups"].items():
                assert rows, (name, inc["thread"], group)
                for path, line in rows:
                    text = files[path].splitlines()[line - 1]
                    if group == "status":
                        assert "Status: ongoing" in text or inc["status"] in text
                    if group == "origin":
                        assert "APPLIED" in text and "build" in text
        a, b, c = t.incidents
        boards = "".join(files[f"round-{k:02d}/board.md"] for k in range(1, n + 1))
        assert f"(code {a['cause']})" in boards and "Thread A status: resolved" in boards
        assert "revised: " in boards and f"({b['cause']})" in boards
        assert f"supersedes the earlier suspicion of {t.superseded_causes[0]}" in boards
        assert (
            f"now also include {b['services'][0]}" in boards
            or f"now also include {b['services'][1]}" in boards
        )
        origin_path, origin_line = c["evidence_groups"]["origin"][0]
        origin = files[origin_path].splitlines()[origin_line - 1]
        cause_path, cause_line = c["evidence_groups"]["cause"][0]
        cause_text = files[cause_path].splitlines()[cause_line - 1]
        key_value = origin.split("sets `")[1].rstrip("`")
        key, value = key_value.split("=")
        assert key in cause_text and value in cause_text  # later evidence links to round 1/2
        for code in t.unresolved:
            assert f"Follow-up OPEN {code}" in boards and f"Follow-up CLOSED {code}" not in boards
        for code in t.closed_follow_ups:
            assert f"Follow-up CLOSED {code}" in boards
        for code in t.false_alarm_causes:
            assert "false alarm" in boards and f"({code})" in boards
        # The log format changes part-way (key=value text, then JSON lines).
        first_log = next(p for p in files if p.startswith("round-01/logs/"))
        last_log = next(p for p in files if p.startswith(f"round-{n:02d}/logs/"))
        assert not files[first_log].startswith("{") and files[last_log].startswith("{")


def test_prompt_states_the_contract_without_answers() -> None:
    inst = generate("rounds-eval-1")
    p = inst.prompt
    assert '"incidents"' in p and '"unresolved"' in p and "evidence_refs" in p
    for inc in inst.truth.incidents:
        for s in inc["services"]:
            assert s not in p  # no instance facts in the task text


# ------------------------------------------------------------- scoring
def _variant(good: dict[str, Any], edit: Any, inst: Any) -> rounds.RoundsScore:
    a = copy.deepcopy(good)
    edit(a)
    return rounds.score_rounds(a, inst.truth, inst.files)


def test_correct_answer_is_strict_and_faulty_answers_fail_specifically() -> None:
    for name in NAMES:
        inst = generate(name)
        t = inst.truth
        good = correct_answer(t)
        s = rounds.score_rounds(good, t, inst.files)
        assert s.strict_success and s.outcome == "correct" and s.passed == s.checks
        sup, origin = t.superseded_causes[0], t.incidents[2]["evidence_groups"]["origin"][0]
        origin_ref = f"{origin[0]}:{origin[1]}"
        closed = t.closed_follow_ups[0] if t.closed_follow_ups else None
        fa = t.false_alarm_causes[0] if t.false_alarm_causes else None
        cases: list[tuple[str, Any]] = [
            ("superseded cause", lambda a, sup=sup: a["incidents"][1].update(cause=sup)),
            ("stale status", lambda a: a["incidents"][0].update(status="mitigated")),
            ("partial update missed", lambda a: a["incidents"][1]["services"].pop()),
            ("missing incident", lambda a: a["incidents"].pop(2)),
            (
                "early fact not cited",
                lambda a, origin_ref=origin_ref: a["incidents"][2].update(
                    evidence_refs=[r for r in a["incidents"][2]["evidence_refs"] if r != origin_ref]
                ),
            ),
            (
                "invalid ref",
                lambda a: a["incidents"][0].update(evidence_refs=["round-99/board.md:1"]),
            ),
            ("open follow-up missing", lambda a: a["unresolved"].pop()),
        ]
        if closed:
            cases.append(
                (
                    "closed follow-up reported",
                    lambda a, closed=closed: a["unresolved"].append(closed),
                )
            )
        if fa:
            cases.append(
                (
                    "false alarm reported",
                    lambda a, fa=fa: a["incidents"].append(
                        {"cause": fa, "services": [], "status": "resolved", "evidence_refs": []}
                    ),
                )
            )
        out = {label: _variant(good, edit, inst) for label, edit in cases}
        assert not any(sc.strict_success for sc in out.values()), name
        assert out["superseded cause"].by_category["causes"]["stale"] == 1
        assert out["superseded cause"].outcome == "stale"
        assert out["stale status"].by_category["status"]["stale"] == 1
        assert out["partial update missed"].by_category["services"]["passed"] == 2
        assert out["missing incident"].missing_causes == ["BAD_CONFIG_ROLLOUT"]
        assert out["early fact not cited"].details[2]["evidence_groups"]["origin"] is False
        assert out["invalid ref"].invalid_refs == ["round-99/board.md:1"]
        assert out["open follow-up missing"].by_category["unresolved"]["passed"] == 0
        if closed:
            assert out["closed follow-up reported"].by_category["unresolved"]["stale"] == 1
        if fa:
            assert out["false alarm reported"].details[-1]["false_alarm_reported"] == [fa]
        s = rounds.score_rounds(None, t, inst.files)
        assert not s.strict_success and s.outcome == "no_answer" and s.passed == 0


def test_rounds_final_answer_parsing() -> None:
    ok, err = parse_action(
        json.dumps(final({"incidents": [], "unresolved": [], "summary": ""})),
        100,
        True,
        rounds.ROUNDS_FIELDS,
    )
    assert ok and not err
    bad, err = parse_action(
        json.dumps(final({"incidents": {}, "unresolved": [], "summary": ""})),
        100,
        True,
        rounds.ROUNDS_FIELDS,
    )
    assert bad is None and "incidents" in err


# ------------------------------------------------------------- instructions
def test_condition_instructions_differ_only_in_their_clause() -> None:
    direct = system_prompt("clm_direct", **KW, task_kind="rounds")
    helpers = system_prompt("clm_helpers", **KW, task_kind="rounds")
    assert direct.replace(CLM_CLAUSE["clm_direct"], "") == helpers.replace(
        CLM_CLAUSE["clm_helpers"], ""
    )
    assert (
        "reusable helper\n  modules" not in direct and "reusable helper\n  modules" not in helpers
    )
    assert "do not save" in direct and "optional" in CLM_CLAUSE["clm_helpers"]
    # The original CLM text and earlier prompts are unchanged.
    assert "reusable helper\n  modules" in CLM_INSTRUCTIONS
    assert (
        hashlib.sha256(system_prompt("clm", **KW).encode())
        .hexdigest()
        .startswith("fa474b0d1ea967a4")
    )
    assert (
        hashlib.sha256(system_prompt("summary", **KW).encode())
        .hexdigest()
        .startswith("84134cfb02d2d02e")
    )


def test_historical_configs_keep_summary_calls_in_max_calls() -> None:
    for name in ("default", "exp003", "exp004", "exp005", "exp006"):
        lim = load_config(REPO / "configs" / f"{name}.toml").limits
        assert lim.summary_calls_in_max_calls is True


# ------------------------------------------------------------- summary allowance
def _summary_cfg(separate: bool) -> Config:
    cfg = Config()
    cfg.baseline.policy = "token-tail/1"
    cfg.limits.context_budget_tokens = 4000
    cfg.limits.max_calls = 6
    cfg.limits.summary_calls_in_max_calls = not separate
    cfg.limits.max_summary_calls = 10
    return cfg


def test_separate_summary_allowance_keeps_task_calls_available(tmp_path: Path) -> None:
    def step(req: ModelRequest) -> Any:
        return "summary text " * 60 if req.purpose == "summary" else execute("print(1)")

    results = {}
    for separate in (False, True):
        cfg = _summary_cfg(separate)
        runner = Runner(
            cfg,
            ScriptedProvider([step] * 40),
            FakeExecutor(outputs=["y" * 2400] * 40),
            Ledger.open(tmp_path / f"l{separate}.json", 100.0),
            TEST_PRICE,
            live=False,
            runs_dir=tmp_path / f"runs{separate}",
            sleep=lambda s: None,
        )
        res = runner.run(generate("dev"), "summary")
        c = res.metrics["counts"]
        results[separate] = c
        assert res.status == "max_calls"
        assert c["summary_calls"] >= 1
    assert results[False]["action_calls"] + results[False]["summary_calls"] <= 6
    assert results[True]["action_calls"] == 6  # every task call kept; summaries counted apart


# ------------------------------------------------------------- helper evidence
def _cfg() -> Config:
    cfg = Config()
    cfg.request = RequestConfig(layout="blocks/1", prompt_caching=False, run_isolation="run-tag/1")
    return cfg


FILTER_HELPER = (
    "import json\n"
    "def compact(keep_last=2):\n"
    "    d = json.load(open('context.json'))\n"
    "    es = d['entries']\n"
    "    notes = [e for e in es if e['role'] == 'note']\n"
    "    rest = [e for e in es if e['role'] != 'note'][-keep_last:]\n"
    "    d['entries'] = notes + rest\n"
    "    json.dump(d, open('context.json', 'w'))\n"
)
WRAPPER_HELPER = (
    "import json\n"
    "def save(text):\n"
    "    json.dump({'format': 'clm-context/v1', 'entries': [{'id': 'n', 'role': 'note', 'body': text}]}, open('context.json', 'w'))\n"
)


@pytest.mark.docker
@pytest.mark.parametrize(
    ("helper", "substantive"), [(FILTER_HELPER, True), (WRAPPER_HELPER, False)]
)
def test_helper_tiers_distinguish_repeated_substantive_logic(
    tmp_path: Path, helper: str, substantive: bool
) -> None:
    from clm_lib.executor import DockerExecutor

    call = (
        "import cm\ncm.compact()\nprint('ok')\n"
        if substantive
        else "import cm\ncm.save('facts so far')\nprint('ok')\n"
    )
    script = [
        execute(f"open('cm.py', 'w').write({helper!r})\nprint('written')\n"),
        execute("print('pad ' * 50)"),
        execute(call),
        execute("print('more ' * 50)"),
        execute(call),
        {
            "action": "final",
            "answer": {
                "root_cause": "x",
                "required_value": "0",
                "remedy": "x",
                "evidence_refs": [],
            },
        },
    ]
    runner = Runner(
        _cfg(),
        ScriptedProvider(script),
        DockerExecutor(),
        Ledger.open(tmp_path / "l.json", 100.0),
        TEST_PRICE,
        live=False,
        runs_dir=tmp_path / "runs",
        sleep=lambda s: None,
    )
    res = runner.run(generate("dev"), "clm_helpers")
    a = audit_run(res.run_dir)
    f = a["files"]["cm.py"]
    assert (
        a["tiers"]["created"] and a["tiers"]["executed"] and a["tiers"]["repeated_accepted_edits"]
    )
    assert f["helper_written_accepted_edit_steps"] == [3, 5] and f[
        "next_request_confirmed_steps"
    ] == [3, 5]
    assert a["tiers"]["substantive_repeated"] is substantive
    kinds = {w["kind"] for w in f["writers"].values()}
    assert kinds == (
        {"selection/filtering"} if substantive else {"wrapper (writes caller-supplied content)"}
    )
    assert [r["step"] for r in a["reuse_violations"]] == [3, 5]  # reuse of saved code (fine in C)


@pytest.mark.docker
def test_direct_condition_reuse_is_flagged_but_within_step_functions_are_not(
    tmp_path: Path,
) -> None:
    from clm_lib.executor import DockerExecutor

    inline = (
        "import json\n"
        "def trim(d):\n    d['entries'] = d['entries'][-2:]\n    return d\n"
        "json.dump(trim(json.load(open('context.json'))), open('context.json', 'w'))\nprint('ok')\n"
    )
    script = [
        execute(inline),
        execute("open('notes.md', 'w').write('facts: none yet')\nprint('ok')"),
        execute(f"open('cm.py', 'w').write({FILTER_HELPER!r})\nprint('saved')"),
        execute("exec(open('cm.py').read())\ncompact()\nprint('ok')"),
        {
            "action": "final",
            "answer": {
                "root_cause": "x",
                "required_value": "0",
                "remedy": "x",
                "evidence_refs": [],
            },
        },
    ]
    runner = Runner(
        _cfg(),
        ScriptedProvider(script),
        DockerExecutor(),
        Ledger.open(tmp_path / "l.json", 100.0),
        TEST_PRICE,
        live=False,
        runs_dir=tmp_path / "runs",
        sleep=lambda s: None,
    )
    res = runner.run(generate("dev"), "clm_direct")
    a = audit_run(res.run_dir)
    assert set(a["files"]) == {"cm.py"}  # notes and within-step functions are not helpers
    assert a["reuse_violations"] == [
        {"step": 4, "file": "cm.py", "evidence": ["read and exec/compile in code"]}
    ]


def test_classifier_on_plain_sources() -> None:
    assert classify_function(FILTER_HELPER, "compact")["kind"] == "selection/filtering"
    assert classify_function(WRAPPER_HELPER, "save")["kind"].startswith("wrapper")


# ------------------------------------------------------------- three-condition schedule
def test_three_condition_schedule_balance() -> None:
    conds = parse_conditions("summary:on,clm_direct:on,clm_helpers:on")
    six = condition_schedule(["a", "b", "c"], 2, conds)
    counts: dict[tuple[str, int], int] = {}
    for r in six:
        counts[(r["mode"], r["position"])] = counts.get((r["mode"], r["position"]), 0) + 1
    assert set(counts.values()) == {2} and len(counts) == 9  # exact over six blocks
    eight = condition_schedule(["a", "b", "c", "d"], 2, conds)
    assert len(eight) == 24 and len({r["block"] for r in eight}) == 8
    per_pos: dict[tuple[str, int], int] = {}
    for r in eight:
        per_pos[(r["mode"], r["position"])] = per_pos.get((r["mode"], r["position"]), 0) + 1
    assert set(per_pos.values()) <= {2, 3, 4}  # eight blocks cannot be balanced exactly
    assert len(set(ORDERS_3)) == 6


@pytest.mark.docker
def test_rounds_release_visibility_and_end_to_end_scoring(tmp_path: Path) -> None:
    from clm_lib.executor import DockerExecutor

    inst = generate("rounds-dev-1")
    probe = (
        "import os\n"
        "print(sorted(d for d in os.listdir('/task/fixtures')))\n"
        "print('hidden', [f for r, _, fs in os.walk('/task') for f in fs if 'truth' in f])\n"
    )
    n = inst.n_stages
    script = [execute(probe), ADVANCE, execute(probe)] + [ADVANCE] * (n - 2)
    script += [final(correct_answer(inst.truth))]
    runner = Runner(
        _cfg(),
        ScriptedProvider(script),
        DockerExecutor(),
        Ledger.open(tmp_path / "l.json", 100.0),
        TEST_PRICE,
        live=False,
        runs_dir=tmp_path / "runs",
        sleep=lambda s: None,
    )
    res = runner.run(inst, "clm_direct")
    obs = [r.user for r in runner.provider.requests]  # type: ignore[attr-defined]
    assert "['round-01']" in obs[1] and "hidden []" in obs[1]
    assert "['round-01', 'round-02']" in obs[3]
    assert "Round 2 of" in obs[2]
    assert res.status == "completed" and res.score.strict_success
    score = json.loads((res.run_dir / "evaluator" / "score.json").read_text())
    assert score["scorer_version"] == "rounds-score/1"
    from clm_lib.prompts import PROMPT_VERSION

    assert res.metrics["prompt_version"] == PROMPT_VERSION


@pytest.mark.parametrize("fixed", [False, True])
def test_spill_reaches_an_observation_followed_by_a_receipt(tmp_path: Path, fixed: bool) -> None:
    """Calibration defect: a CLM step that edits context and prints a large observation."""
    cfg = Config()
    cfg.limits.context_budget_tokens = 4000
    cfg.limits.spill_past_receipt = fixed
    big = "z" * 9000
    edit = (
        "import json\nd = json.load(open('context.json'))\nd['entries'] = []\n"
        "json.dump(d, open('context.json', 'w'))\n"
    )
    writes = {1: {}}
    outputs = [big]

    class EditingExecutor(FakeExecutor):
        def run(self, code: str, workspace: Path, fixtures: Path) -> Any:
            (workspace / "context.json").write_text(
                json.dumps(
                    {
                        "format": "clm-context/v1",
                        "entries": [{"id": "n1", "role": "note", "body": "kept"}],
                    }
                )
            )
            return super().run(code, workspace, fixtures)

    runner = Runner(
        cfg,
        ScriptedProvider(
            [
                execute(edit),
                {
                    "action": "final",
                    "answer": {
                        "root_cause": "x",
                        "required_value": "0",
                        "remedy": "x",
                        "evidence_refs": [],
                    },
                },
            ]
        ),
        EditingExecutor(outputs=outputs, writes=writes),
        Ledger.open(tmp_path / "l.json", 100.0),
        TEST_PRICE,
        live=False,
        runs_dir=tmp_path / "runs",
        sleep=lambda s: None,
    )
    cfg.limits.output_cap_chars = 9000
    res = runner.run(generate("dev"), "clm")
    if fixed:
        assert res.status == "completed" and res.metrics["counts"]["spills"] == 1
    else:
        assert res.metrics["counts"]["spills"] == 0


# ------------------------------------------------------------- revised experiment 007
def test_reuse_condition_differs_only_in_its_clause_and_task_text_is_shared() -> None:
    direct = system_prompt("clm_direct", **KW, task_kind="rounds")
    reuse = system_prompt("clm_reuse", **KW, task_kind="rounds")
    assert direct.replace(CLM_CLAUSE["clm_direct"], "") == reuse.replace(
        CLM_CLAUSE["clm_reuse"], ""
    )
    assert "perform working-context edits through a reusable Python" in reuse
    assert "do not need to edit on every step" in CLM_CLAUSE["clm_reuse"]
    # The clm_helpers (initial phase) text is unchanged by the revision.
    assert "This is optional." in CLM_CLAUSE["clm_helpers"]
    # Task text and released evidence do not depend on the condition.
    assert generate("rounds-dev-1").prompt == generate("rounds-dev-1").prompt


def test_revised_config_fixes_spill_and_historical_config_does_not() -> None:
    old = load_config(REPO / "configs" / "exp007.toml")
    new = load_config(REPO / "configs" / "exp007r.toml")
    assert old.limits.spill_past_receipt is False and new.limits.spill_past_receipt is True
    assert old.limits.recheck_after_notice is False and new.limits.recheck_after_notice is True
    a, b = old.to_dict(), new.to_dict()
    for d in (a, b):
        d["limits"].pop("spill_past_receipt")
        d["limits"].pop("recheck_after_notice")
        d["budget"].pop("ceiling_usd")
    assert a == b  # nothing else differs


NOTE_HELPER = (
    "import json\n"
    "def note(text, nid='n1'):\n"
    "    d = json.load(open('context.json'))\n"
    "    d['entries'] = [{'id': nid, 'role': 'note', 'body': text}]\n"
    "    json.dump(d, open('context.json', 'w'))\n"
)


@pytest.mark.docker
def test_reuse_adherence_and_categories(tmp_path: Path) -> None:
    from clm_lib.executor import DockerExecutor

    inline = (
        "import json\nd = json.load(open('context.json'))\nd['entries'] = d['entries'][-1:]\n"
        "json.dump(d, open('context.json', 'w'))\nprint('inline')\n"
    )
    script = [
        execute(f"open('ctx.py', 'w').write({NOTE_HELPER!r})\nprint('saved')"),
        execute("print('pad ' * 50)"),
        execute("import ctx\nctx.note('facts A')\nprint('ok')"),
        execute(inline),
        execute("import ctx\nctx.note('facts B')\nprint('ok')"),
        {
            "action": "final",
            "answer": {
                "root_cause": "x",
                "required_value": "0",
                "remedy": "x",
                "evidence_refs": [],
            },
        },
    ]
    runner = Runner(
        _cfg(),
        ScriptedProvider(script),
        DockerExecutor(),
        Ledger.open(tmp_path / "l.json", 100.0),
        TEST_PRICE,
        live=False,
        runs_dir=tmp_path / "runs",
        sleep=lambda s: None,
    )
    res = runner.run(generate("dev"), "clm_reuse")
    a = audit_run(res.run_dir)
    assert a["accepted_edit_steps"] == [3, 4, 5]
    assert a["helper_assisted_edit_steps"] == [3, 5] and a["helper_attributed_edit_steps"] == [3, 5]
    assert a["adherence"] == "partial" and a["tiers"]["repeated_execution"]
    assert a["categories"] == ["writes or replaces supplied note content"]
    assert a["files"]["ctx.py"]["functions"] == {"note": "writes or replaces supplied note content"}


def test_export_keeps_phases_distinct(make_runner: Any, tmp_path: Path) -> None:
    from clm_lib.export import export_experiment

    ids = []
    for _ in range(2):
        runner, _ = make_runner([execute("a"), FINAL_INCIDENT], executor=FakeExecutor())
        ids.append(runner.run(generate("dev"), "clm").run_id)
    exp = tmp_path / "exp"
    exp.mkdir()
    spec = {
        "id": "995-phases",
        "title": "t",
        "runs": [
            {"run_id": ids[0], "role": "calibration", "phase": "initial-calibration"},
            {"run_id": ids[1], "role": "calibration", "phase": "revised-calibration"},
        ],
        "comparisons": [],
    }
    (exp / "experiment.json").write_text(json.dumps(spec))
    runs_root = tmp_path / "runs"
    export_experiment(exp, runs_root, tmp_path / "ledger.json", tmp_path / "results")
    out = tmp_path / "results" / "experiments" / "995-phases"
    rows = json.loads((out / "metrics.json").read_text())
    assert [r["phase"] for r in rows] == ["initial-calibration", "revised-calibration"]
    manifest = json.loads((out / "manifest.json").read_text())
    assert [r["phase"] for r in manifest["runs"]] == ["initial-calibration", "revised-calibration"]
    report = (out / "report.md").read_text()
    assert "[initial-calibration]" in report and "[revised-calibration]" in report


FINAL_INCIDENT = {
    "action": "final",
    "answer": {"root_cause": "x", "required_value": "0", "remedy": "x", "evidence_refs": []},
}


@pytest.mark.parametrize("fixed", [False, True])
def test_pressure_notice_cannot_end_a_run_without_spill_or_recovery(
    tmp_path: Path, fixed: bool, monkeypatch: Any
) -> None:
    """Revised-calibration defect: a request just under the hard limit plus the PRESSURE notice."""
    from clm_lib import runner as runner_mod

    original = runner_mod._Run.status_text

    def long_notice(self: Any, est: int, pressure: bool, recovery: bool) -> str:
        text = original(self, est, pressure, recovery)
        return text + ("\n" + "notice " * 400 if pressure and not recovery else "")

    monkeypatch.setattr(runner_mod._Run, "status_text", long_notice)
    cfg = Config()
    cfg.limits.context_budget_tokens = 4000
    cfg.limits.output_cap_chars = 9000
    cfg.limits.recheck_after_notice = fixed
    runner = Runner(
        cfg,
        ScriptedProvider([execute("print(1)"), FINAL_INCIDENT, FINAL_INCIDENT]),
        FakeExecutor(outputs=["w" * 3000]),  # request ~2,900 tokens: above pressure, below hard
        Ledger.open(tmp_path / "l.json", 100.0),
        TEST_PRICE,
        live=False,
        runs_dir=tmp_path / "runs",
        sleep=lambda s: None,
    )
    res = runner.run(generate("dev"), "clm")
    if fixed:
        assert res.status == "completed"
        assert res.metrics["counts"]["spills"] + res.metrics["counts"]["recovery_requests"] >= 1
    else:
        assert res.status == "context_overflow" and "exceeds limit" in res.metrics["stop_reason"]
