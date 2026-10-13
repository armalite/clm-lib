"""W2 export task (export-gen/1, experiment 011 pilot): generation, scorer validity and isolation.

These test the library (generator, reference, evaluator, runtime). Agent outcomes are measured
separately by the evaluator during live runs; the faulty solutions here are offline scorer
validation only and never enter an agent run.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from clm_lib import w2
from clm_lib.tasks import INSTANCES, generate
from clm_lib.w2 import config_at, ref_export

from .conftest import execute, working_block
from .w2_solutions import INDEPENDENT_SOURCE, faulty_core, independent_core, write_core_code

DEV = ["w2-dev-1", "w2-dev-2"]
ALL = [n for n in INSTANCES if n.startswith("w2-")]
ADVANCE = {"action": "advance"}
FINAL = {"action": "final", "answer": {"summary": "done"}}
DISCLOSES = {
    "STATUS": "include_archived",
    "CONF": "purpose",
    "SINCE": "since",
    "LIMIT": "max_records",
}


def _independent() -> dict[str, Any]:
    ns: dict[str, Any] = {}
    exec(compile(INDEPENDENT_SOURCE, "independent", "exec"), ns)  # our own test code
    return ns


def _indep_observe(ns: dict[str, Any], c: dict[str, Any], cfg: dict[str, int]) -> dict[str, Any]:
    try:
        text = ns["export_for"](cfg, *json.loads(json.dumps(c["args"])))
    except PermissionError:
        return {"raises": "PermissionError"}
    if c["mode"] == "csv":
        return {"text": text}
    parsed = json.loads(text)
    return {"json": {k: parsed.get(k) for k in c["keys"]}}


def _host_outcomes(
    checks: list[dict[str, Any]], cfg: dict[str, int], faults: frozenset[str]
) -> str:
    """Simulated evaluator observations for a faulty reference (host-side, our own code)."""
    out = []
    for c in checks:
        try:
            raw = ref_export(*json.loads(json.dumps(c["args"])), cfg, faults)
        except PermissionError:
            raw, obs = None, {"raises": "PermissionError"}
        else:
            if c["mode"] == "csv":
                obs = {"text": raw}
            else:
                try:
                    parsed = json.loads(raw)
                    obs = {"json": {k: parsed.get(k) for k in c["keys"]}}
                except ValueError:
                    obs = {"not_json_object": raw[:300]}
        passed = obs == c["expect"]
        stale = sorted(
            m
            for m, old in c["stale"].items()
            if not passed and (obs == old or ("text" in old and raw == old["text"]))
        )
        out.append({"id": c["id"], "observed": obs, "passed": passed, "stale": stale})
    return w2.EVAL_MARKER + json.dumps({"import_error": None, "results": out})


def test_generation_is_deterministic_and_has_the_w2_ingredients() -> None:
    for name in ALL:
        a, b = generate(name), generate(name)
        assert a.files == b.files and a.truth.checks == b.truth.checks
    for name in DEV:
        sched = w2.schedule_for(name)
        assert len(sched) == 5
        intro = w2.introduced_at(sched)
        stages = {m: k for k, ch in enumerate(sched, 1) for m, v in ch if v == 2}
        # CSV first, JSON at stage 3; the obligation is announced before the format change.
        assert config_at(sched, 2)["FORMAT"] == 1 and stages["FORMAT"] == 3
        assert intro["OBLIG"] < stages["FORMAT"]
        # An exception and a later partial change of another earlier rule.
        assert "CONF" in config_at(sched, 2)
        partial = [m for m in stages if m != "FORMAT"]
        assert len(partial) == 1 and stages[partial[0]] == 4
        t = generate(name)
        doc3 = t.stages[2]["stage-3/REQUIREMENTS.md"]
        assert "exported_by" not in doc3  # the obligation is not restated when JSON arrives
        assert "exported_by" in t.stages[1]["stage-2/REQUIREMENTS.md"]
        cats = {c["category"] for c in t.truth.checks}
        assert cats == set(w2.CATEGORIES)
    assert generate("w2-dev-1").truth.schedule != generate("w2-dev-2").truth.schedule


def test_reference_matches_independent_implementation() -> None:
    ns = _independent()
    for name in ALL:
        t = generate(name).truth
        for c in t.checks:
            assert _indep_observe(ns, c, t.final_config) == c["expect"], (name, c["id"])
        for stage, checks in t.snapshot_checks.items():
            cfg = config_at(t.schedule, stage)
            for c in checks:
                assert _indep_observe(ns, c, cfg) == c["expect"], (name, c["id"])


def test_checks_use_only_disclosed_options_and_differ_from_visible_tests() -> None:
    for name in ALL:
        inst = generate(name)
        t = inst.truth
        visible = "".join(st["current-tests/test_stage.py"] for st in inst.stages)
        groups = [(t.final_config, t.checks)] + [
            (config_at(t.schedule, k), cs) for k, cs in t.snapshot_checks.items()
        ]
        for cfg, checks in groups:
            allowed = {opt for m, opt in DISCLOSES.items() if m in cfg}
            for c in checks:
                assert set(c["args"][2]) <= allowed, (name, c["id"])
                assert repr(c["args"]) not in visible
        ids = [c["id"] for c in t.checks]
        assert len(ids) == len(set(ids))


def test_visible_tests_cover_only_the_latest_stage_and_pass_on_the_reference() -> None:
    ns = _independent()
    for name in DEV:
        inst = generate(name)
        for k, st in enumerate(inst.stages, start=1):
            src = st["current-tests/test_stage.py"]
            mods = {m.lower() for m, _ in inst.truth.schedule[k - 1]}
            named = {line.split("_")[1] for line in src.splitlines() if "def test_" in line}
            assert named <= mods | {"nothing"}, (name, k, named)
            # The tests pass against the independent implementation under the stage-k rules.
            cfg = config_at(inst.truth.schedule, k)
            mod = type(json)("exporter")
            mod.export_records = lambda r, u, p, cfg=cfg: ns["export_for"](cfg, r, u, p)  # type: ignore[attr-defined]
            scope: dict[str, Any] = {"__name__": "visible"}
            import sys
            import unittest

            sys.modules["exporter"] = mod
            try:
                exec(compile(src, "visible", "exec"), scope)
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(scope["TestStage"])
                result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
                assert result.wasSuccessful(), (name, k, result.failures[:1])
            finally:
                del sys.modules["exporter"]


FAULTS = {
    "partial:PERM": ("kept_part",),
    "partial:PERM_REPLACE": ("kept_part",),
    "partial:STATUS": ("kept_part",),
    "exception:CONF_NONE": ("exception",),
    "exception:CONF_ADMIN": ("exception",),
    "order:LIMIT_FIRST": (),
}


def test_evaluator_detects_each_class_of_mistake() -> None:
    """Forgotten rules, an unchanged format (missed change), outdated partial rules, a lost
    exception, a lost deferred obligation and over-applied partial changes each fail the
    expected category; the correct reference passes everything."""
    for name in DEV:
        t = generate(name).truth
        final = t.final_config
        ok = w2.score_export(t, _host_outcomes(t.checks, final, frozenset()), True)
        assert ok.outcome == "correct" and ok.strict_success
        for module in final:
            if module == "FORMAT":
                continue
            cfg = {k: v for k, v in final.items() if k != module}
            s = w2.score_export(t, _host_outcomes(t.checks, cfg, frozenset()), True)
            assert not s.strict_success, (name, "forget", module)
            cat = t.categories[module]
            assert s.by_category[cat]["passed"] < s.by_category[cat]["total"], (name, module)
            if cat in ("retained", "exception"):
                assert module in s.retained_failed_modules
        for module, version in final.items():
            if version > 1:
                old = {**final, module: version - 1}
                s = w2.score_export(t, _host_outcomes(t.checks, old, frozenset()), True)
                assert s.stale_modules.get(module, 0) >= 3, (name, module, s.stale_modules)
        csv = {**final, "FORMAT": 1}
        s = w2.score_export(t, _host_outcomes(t.checks, csv, frozenset()), True)
        raises = sum("raises" in c["expect"] for c in t.checks)  # format-independent checks
        assert s.passed == raises and s.stale_modules["FORMAT"] >= 30
        for fault, cats in FAULTS.items():
            module = fault.split(":")[1].split("_")[0]
            if fault.startswith("partial") and final.get(module) != 2:
                continue
            s = w2.score_export(t, _host_outcomes(t.checks, final, frozenset({fault})), True)
            assert not s.strict_success, (name, fault)
            for cat in cats:
                assert s.by_category[cat]["passed"] < s.by_category[cat]["total"], (name, fault)
        # The deferred obligation fails only its own checks.
        no_oblig = {k: v for k, v in final.items() if k != "OBLIG"}
        s = w2.score_export(t, _host_outcomes(t.checks, no_oblig, frozenset()), True)
        failed = {c for c, v in s.by_category.items() if v["passed"] < v["total"]}
        assert failed == {"deferred"}


def test_regression_requires_an_earlier_passing_snapshot() -> None:
    t = generate("w2-dev-1").truth
    broken = {k: v for k, v in t.final_config.items() if k != "STATUS"}
    out = _host_outcomes(t.checks, broken, frozenset())
    assert w2.score_export(t, out, True).regressions == []
    snaps = {
        k: _host_outcomes(cs, config_at(t.schedule, k), frozenset())
        for k, cs in t.snapshot_checks.items()
    }
    # Dropping STATUS also changes CONF and SINCE results (archived records reappear).
    assert "STATUS" in w2.score_export(t, out, True, snapshot_outputs=snaps).regressions


# ------------------------------------------------------------- sandbox (Docker) tests
def _script(core: str, n: int) -> list[Any]:
    return [execute(write_core_code(core)), *[ADVANCE] * (n - 1), FINAL]


@pytest.mark.docker
@pytest.mark.parametrize("name", DEV)
def test_independent_solution_is_strictly_successful_in_sandbox(
    make_runner: Any, name: str
) -> None:
    inst = generate(name)
    runner, _ = make_runner(_script(independent_core(inst.truth.final_config), 5))
    res = runner.run(inst, "clm")
    s = res.score
    assert res.status == "completed" and s.outcome == "correct" and s.strict_success
    assert s.passed == s.checks and s.stale == 0
    score = json.loads((res.run_dir / "evaluator" / "score.json").read_text())
    assert score["scorer_version"] == "export-checks/1"
    assert sorted(s.snapshots) == ["1", "2", "3", "4"]


@pytest.mark.docker
@pytest.mark.parametrize(
    ("label", "change", "faults", "category"),
    [
        ("format change missed (still CSV)", "csv", (), "replaced"),
        ("deferred obligation lost", "drop:OBLIG", (), "deferred"),
        ("exception lost", None, ("exception:CONF_NONE",), "exception"),
        ("unaffected rule dropped", "drop:SINCE", (), "retained"),
        ("partial change over-applied", None, ("partial:PERM_REPLACE",), "kept_part"),
    ],
)
def test_faulty_solutions_are_detected_in_sandbox(
    make_runner: Any, label: str, change: str | None, faults: tuple[str, ...], category: str
) -> None:
    inst = generate("w2-dev-1")
    cfg = dict(inst.truth.final_config)
    if change == "csv":
        cfg["FORMAT"] = 1
    elif change:
        cfg.pop(change.split(":")[1])
    runner, _ = make_runner(_script(faulty_core(cfg, frozenset(faults)), 5))
    s = runner.run(inst, "clm").score
    assert s.outcome == "failed_checks" and not s.strict_success, label
    assert s.by_category[category]["passed"] < s.by_category[category]["total"], label
    if change == "csv":
        assert s.stale_modules.get("FORMAT", 0) >= 30


@pytest.mark.docker
def test_stage_visibility_and_evaluator_isolation(make_runner: Any) -> None:
    probe = (
        "import os\n"
        "fx = sorted(os.path.relpath(os.path.join(r, f), '/task/fixtures') for r, _, fs in os.walk('/task/fixtures') for f in fs)\n"
        "print('fixtures', fx)\n"
        "ev = [os.path.join(r, f) for r, _, fs in os.walk('/task') for f in fs if 'checks' in f or 'truth' in f or 'snapshot' in r]\n"
        "print('evaluator_files', ev)\n"
    )
    inst = generate("w2-dev-2")
    script = [execute(probe), ADVANCE, execute(probe), ADVANCE, ADVANCE, ADVANCE]
    script += [execute(probe), FINAL]
    runner, prov = make_runner(script)
    res = runner.run(inst, "clm")
    obs = [r.user for r in prov.requests]
    assert "fixtures ['current-tests/test_stage.py', 'stage-1/REQUIREMENTS.md']" in obs[1]
    assert "'stage-2/REQUIREMENTS.md'" in obs[3] and "'stage-3/REQUIREMENTS.md'" not in obs[3]
    assert "'stage-5/REQUIREMENTS.md'" in obs[7]
    assert all("evaluator_files []" in o for o in (obs[1], obs[3], obs[7]))
    assert "Stage 2 of 5 requirements" in working_block(obs[2])
    snaps = sorted(p.name for p in (res.run_dir / "evaluator" / "snapshots").iterdir())
    assert snaps == [f"stage-{k}" for k in range(1, 5)]
