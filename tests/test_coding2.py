"""Experiment 005 coding task (invoice-gen/2): generation, evaluator validity and isolation.

These test the library (generator, reference, evaluator, runtime). Agent performance is measured
separately by the evaluator checks during live runs.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from typing import Any

import pytest

from clm_lib import coding2, invoice_ref2
from clm_lib.coding2 import config_at, schedule_for
from clm_lib.tasks import INSTANCES, generate

from .coding2_solutions import INDEPENDENT_SOURCE, faulty_core, independent_core, write_core_code
from .conftest import execute, working_block

NAMES = [n for n in INSTANCES if n.startswith("coding2")]
EVAL = [n for n in NAMES if "-eval-" in n]
ADVANCE = {"action": "advance"}
FINAL = {"action": "final", "answer": {"summary": "done"}}


def _independent() -> dict[str, Any]:
    ns: dict[str, Any] = {}
    exec(compile(INDEPENDENT_SOURCE, "independent", "exec"), ns)  # our own test code
    return ns


def _indep_call(ns: dict[str, Any], fn: str, args: list[Any], cfg: dict[str, int]) -> Any:
    try:
        if fn == "compute_invoice":
            return {"value": ns["invoice_for"](cfg, *args)}
        return {"value": ns["refund_for"](cfg, *args)}
    except ValueError:
        return {"raises": "ValueError"}


def _host_outcomes(
    checks: list[dict[str, Any]], cfg: dict[str, int], faults: frozenset[str]
) -> Any:
    """Simulated evaluator observations for a faulty reference (host-side, our own code)."""
    out = []
    for c in checks:
        obs = invoice_ref2.ref_call(c["fn"], c["args"], cfg, faults)
        out.append(
            {
                "id": c["id"],
                "observed": obs,
                "passed": obs == c["expect"],
                "stale": sorted(
                    m for m, old in c["stale"].items() if obs != c["expect"] and obs == old
                ),
            }
        )
    return coding2.EVAL_MARKER + json.dumps({"import_error": None, "results": out})


def test_generation_is_deterministic_with_distinct_structures() -> None:
    structures = set()
    for name in NAMES:
        a, b = generate(name), generate(name)
        assert a.fixture_sha256 == b.fixture_sha256 and a.truth.checks == b.truth.checks
        assert a.generator_version == "invoice-gen/2" and a.scorer_version == "invoice-checks/2"
        assert a.n_stages == (8 if name.startswith("coding2h-") else 6)
        if "-eval-" in name:
            structures.add((name.split("-")[0], json.dumps(a.truth.schedule)))
        cats = Counter(c["category"] for c in a.truth.checks)
        assert cats["retained"] and cats["replaced"] and cats["kept_part"] and cats["interaction"]
        assert sum(c["boundary"] for c in a.truth.checks) >= 8
        for c in a.truth.checks:  # replaced-rule checks always distinguish the old rule
            if c["category"] == "replaced":
                assert c["module"] in c["stale"]
            if c["category"] == "kept_part":
                assert c["module"] not in c["stale"]
    assert len(structures) == len(EVAL)
    # The harder variant is the base schedule plus two predefined stages.
    for key in coding2.BASE_SCHEDULES:
        assert schedule_for(f"coding2h-{key}")[:6] == schedule_for(f"coding2-{key}")


def test_schedules_respect_dependencies_and_partial_changes() -> None:
    for name in NAMES:
        sched = schedule_for(name)
        for k in range(1, len(sched) + 1):
            cfg = config_at(sched, k)
            if "STACK" in cfg:
                assert "TIER" in cfg and "BULK" in cfg
            if cfg.get("SHIP") == 2:
                assert "TAX" in cfg
            if "COUPON" in cfg:
                assert "TIER" in cfg
            for module, version in sched[k - 1]:
                if version == 2:  # a change always follows an earlier introduction
                    assert config_at(sched, k - 1).get(module) == 1
        final = config_at(sched, len(sched))
        assert set(final) >= {"ROUND", "VALIDATE", "BULK", "TIER", "STACK", "SHIP", "TAX"}


def test_stage_documents_and_visible_tests() -> None:
    inst = generate("coding2-eval-3")
    sched = inst.truth.schedule
    for k, files in enumerate(inst.stages, start=1):
        assert set(files) == {f"stage-{k}/REQUIREMENTS.md", "current-tests/test_invoice.py"}
        doc = files[f"stage-{k}/REQUIREMENTS.md"]
        for _module, version in sched[k - 1]:
            assert ("[CHANGED]" if version > 1 else "[NEW]") in doc
        tests = files["current-tests/test_invoice.py"]
        for module in config_at(sched, k):
            assert f"def test_{module.lower()}_" in tests
    # A partial change refers back to the stage that introduced the rule, without restating it.
    tier_change = next(k for k, ch in enumerate(sched, 1) if ("TIER", 2) in ch)
    doc = inst.stages[tier_change - 1][f"stage-{tier_change}/REQUIREMENTS.md"]
    assert "partly replaces the stage-1 rates" in doc and "2%" not in doc


def test_inputs_use_only_disclosed_fields_and_differ_from_visible_tests() -> None:
    for name in NAMES:
        inst = generate(name)
        visible = "".join(st["current-tests/test_invoice.py"] for st in inst.stages)
        for k, st in enumerate(inst.stages, start=1):
            cfg = config_at(inst.truth.schedule, k)
            if "COUPON" not in cfg:
                assert "coupon" not in st["current-tests/test_invoice.py"]
            if "REFUND" not in cfg:
                assert "compute_refund" not in st["current-tests/test_invoice.py"]
        for c in inst.truth.checks:
            assert repr(c["args"]) not in visible
            customer = c["args"][1]
            assert set(customer) <= {"tier", "region", "coupon"}
            if "COUPON" not in inst.truth.final_config:
                assert "coupon" not in customer
        for stage, checks in inst.truth.snapshot_checks.items():
            cfg = config_at(inst.truth.schedule, stage)
            for c in checks:
                assert c["module"] in cfg
                assert ("coupon" in c["args"][1]) <= ("COUPON" in cfg)


def test_reference_matches_independent_implementation() -> None:
    ns = _independent()
    n = 0
    for name in NAMES:
        inst = generate(name)
        sched = inst.truth.schedule
        for c in inst.truth.checks:
            assert _indep_call(ns, c["fn"], c["args"], inst.truth.final_config) == c["expect"], c
            n += 1
        rng = random.Random(name)
        for k in range(1, len(sched) + 1):  # every intermediate configuration, random inputs
            cfg = config_at(sched, k)
            for _ in range(150):
                module = rng.choice(list(cfg))
                fn, args, _ = coding2.module_case(module, rng, cfg)
                assert _indep_call(ns, fn, args, cfg) == invoice_ref2.ref_call(fn, args, cfg)
                n += 1
    assert n > 10000


def test_worked_example_by_hand() -> None:
    """One interacting example computed by hand from the requirement text."""
    cfg = {"ROUND": 1, "BULK": 2, "TIER": 2, "STACK": 1, "COUPON": 1, "SHIP": 2, "TAX": 2}
    lines = [
        {"sku": "A", "qty": 60, "unit_price": "1.25"},  # bulk: 75.00 x 0.90 = 67.50
        {"sku": "B", "qty": 2, "unit_price": "10.00"},  # 20.00, tier-eligible
    ]
    customer = {"tier": "gold", "region": "NZ", "coupon": "5.00"}
    # subtotal 87.50; tier 8% of 20.00 = 1.60; coupon 5.00; discount 6.60; 80.90 < 100 so
    # shipping 7.50, taxed: (80.90 + 7.50) x 15% = 13.26; total 80.90 + 7.50 + 13.26 = 101.66.
    assert invoice_ref2.ref_invoice(lines, customer, cfg) == {
        "subtotal": "87.50",
        "discount": "6.60",
        "shipping": "7.50",
        "tax": "13.26",
        "total": "101.66",
    }
    # Returning 20 units of A drops it below the bulk threshold (40 x 1.25 = 50.00, now
    # tier-eligible): kept 70.00 - 5.60 tier - 5.00 coupon = 59.40, tax 8.91 -> 68.31; the
    # original without shipping: 80.90 + 12.14 = 93.04; refund 93.04 - 68.31 = 24.73.
    cfg["REFUND"] = 1
    assert invoice_ref2.ref_refund(lines, customer, {"A": 20}, cfg) == "24.73"


FAULTS_EXPECTED = {
    # fault -> categories that must show a failure when the fault applies
    "partial:TIER": {"kept_part"},
    "partial:TAX": {"kept_part"},
    "partial:VALIDATE": {"kept_part"},
    "partial:COUPON": {"kept_part"},
    "order:SHIP_PRE_DISCOUNT": None,
    "order:TAX_PRE_DISCOUNT": None,
    "order:ROUND_LATE": None,
    "interact:REFUND_SHIP": None,
    "interact:REFUND_RETURNED": None,
    "interact:REFUND_NEG": None,
}


def _applies(fault: str, cfg: dict[str, int]) -> bool:
    kind, _, what = fault.partition(":")
    if kind == "partial":
        return cfg.get(what) == 2
    if what == "SHIP_PRE_DISCOUNT":
        return "SHIP" in cfg
    if what == "ROUND_LATE":
        return cfg.get("ROUND") == 1
    if what.startswith("REFUND"):
        return "REFUND" in cfg
    return True


def test_evaluator_detects_each_class_of_mistake_on_every_instance() -> None:
    """Forgotten rules, outdated rules, partial-change, calculation-order and interaction
    mistakes each fail at least one hidden check, in the expected category."""
    for name in NAMES:
        t = generate(name).truth
        final = t.final_config
        assert (
            coding2.score_coding(t, _host_outcomes(t.checks, final, frozenset()), True).outcome
            == "correct"
        )
        for module in final:
            if module == "ROUND" and final[module] == 1:
                continue
            cfg = {k: v for k, v in final.items() if k != module}  # forgotten rule
            s = coding2.score_coding(t, _host_outcomes(t.checks, cfg, frozenset()), True)
            assert not s.strict_success, (name, "forget", module)
            if t.categories[module] == "retained":
                assert module in s.retained_failed_modules and s.regressions == []
            if final[module] > 1:  # outdated rule kept
                old = {**final, module: final[module] - 1}
                s = coding2.score_coding(t, _host_outcomes(t.checks, old, frozenset()), True)
                assert s.by_category["replaced"]["stale"] >= 4 and s.stale_modules[module] >= 4
        for fault, cats in FAULTS_EXPECTED.items():
            if not _applies(fault, final):
                continue
            s = coding2.score_coding(t, _host_outcomes(t.checks, final, frozenset({fault})), True)
            assert not s.strict_success, (name, fault)
            for cat in cats or ():
                assert s.by_category[cat]["passed"] < s.by_category[cat]["total"], (name, fault)


def test_regression_requires_earlier_passing_snapshot() -> None:
    t = generate("coding2-eval-1").truth
    final = t.final_config
    retained = next(m for m, c in t.categories.items() if c == "retained" and m != "ROUND")
    broken = {k: v for k, v in final.items() if k != retained}
    final_out = _host_outcomes(t.checks, broken, frozenset())
    no_snap = coding2.score_coding(t, final_out, True)
    assert retained in no_snap.retained_failed_modules and no_snap.regressions == []
    snaps = {
        k: _host_outcomes(t.snapshot_checks[k], config_at(t.schedule, k), frozenset())
        for k in t.snapshot_checks
    }
    with_snap = coding2.score_coding(t, final_out, True, snapshot_outputs=snaps)
    assert with_snap.regressions == [retained]


# ------------------------------------------------------------- sandbox (Docker) tests
def _script(core: str, n_stages: int, extra: list[Any] | None = None) -> list[Any]:
    return [execute(write_core_code(core)), *(extra or []), *[ADVANCE] * (n_stages - 1), FINAL]


@pytest.mark.docker
def test_independent_solution_is_strictly_successful_in_sandbox(make_runner: Any) -> None:
    inst = generate("coding2h-eval-2")
    runner, _ = make_runner(_script(independent_core(inst.truth.final_config), 8))
    res = runner.run(inst, "clm")
    s = res.score
    assert res.status == "completed" and s.outcome == "correct" and s.strict_success
    assert s.passed == s.checks and s.stale == 0 and s.type_errors == 0
    score = json.loads((res.run_dir / "evaluator" / "score.json").read_text())
    assert score["scorer_version"] == "invoice-checks/2"
    assert (res.run_dir / "evaluator" / "submission" / "invoice" / "core.py").exists()
    # Snapshots were taken at each release (7) and evaluated; none is visible to the agent.
    assert sorted(s.snapshots) == [str(k) for k in range(1, 8)]
    assert not list((res.run_dir / "workspace").glob("**/snapshots"))


@pytest.mark.docker
@pytest.mark.parametrize(
    ("label", "change", "faults", "category"),
    [
        ("forgotten earlier rule", "drop:STACK", (), "retained"),
        ("outdated rule kept", "old:TIER", (), "replaced"),
        ("partial change over-applied", None, ("partial:TAX",), "kept_part"),
        ("wrong calculation order", None, ("order:TAX_PRE_DISCOUNT",), None),
        ("interaction mistake", None, ("interact:REFUND_RETURNED",), "final_stage"),
    ],
)
def test_faulty_solutions_are_detected_in_sandbox(
    make_runner: Any, label: str, change: str | None, faults: tuple[str, ...], category: str | None
) -> None:
    inst = generate("coding2h-eval-5")
    cfg = dict(inst.truth.final_config)
    if change:
        op, module = change.split(":")
        if op == "drop":
            cfg.pop(module)
        else:
            cfg[module] -= 1
    runner, _ = make_runner(_script(faulty_core(cfg, frozenset(faults)), 8))
    s = runner.run(inst, "clm").score
    assert s.outcome == "failed_checks" and not s.strict_success, label
    if category:
        assert s.by_category[category]["passed"] < s.by_category[category]["total"], label
    if change == "old:TIER":
        assert s.stale_modules.get("TIER", 0) >= 4


@pytest.mark.docker
def test_return_types_are_checked_exactly(make_runner: Any) -> None:
    inst = generate("coding2-dev-1")
    core = independent_core(inst.truth.final_config) + (
        "\n\n_exact = compute_invoice\n\n"
        "def compute_invoice(lines, customer):\n"
        "    return {k: Decimal(v) for k, v in _exact(lines, customer).items()}\n"
    )
    runner, _ = make_runner(_script(core, 6))
    s = runner.run(inst, "clm").score
    assert s.outcome == "failed_checks" and s.type_errors > 0
    # ValueError checks still pass: only returned values are affected.
    assert 0 < s.passed < s.checks


@pytest.mark.docker
def test_stage_visibility_and_evaluator_isolation(make_runner: Any) -> None:
    probe = (
        "import os\n"
        "fx = sorted(os.path.relpath(os.path.join(r, f), '/task/fixtures') for r, _, fs in os.walk('/task/fixtures') for f in fs)\n"
        "print('fixtures', fx)\n"
        "ev = [os.path.join(r, f) for r, _, fs in os.walk('/task') for f in fs if 'checks' in f or 'truth' in f or 'snapshot' in r]\n"
        "print('evaluator_files', ev)\n"
    )
    inst = generate("coding2-dev-2")
    script = [execute(probe), ADVANCE, execute(probe), ADVANCE, ADVANCE, ADVANCE, ADVANCE]
    script += [execute(probe), FINAL]
    runner, prov = make_runner(script)
    res = runner.run(inst, "clm")
    obs = [r.user for r in prov.requests]
    assert "fixtures ['current-tests/test_invoice.py', 'stage-1/REQUIREMENTS.md']" in obs[1]
    assert "'stage-2/REQUIREMENTS.md'" in obs[3] and "'stage-3/REQUIREMENTS.md'" not in obs[3]
    assert "'stage-6/REQUIREMENTS.md'" in obs[8]
    assert all("evaluator_files []" in o for o in (obs[1], obs[3], obs[8]))
    assert "Stage 2 of 6 requirements" in working_block(obs[2])
    for k in range(1, 7):
        doc = (res.run_dir / "fixtures" / f"stage-{k}" / "REQUIREMENTS.md").read_text()
        assert doc == inst.stages[k - 1][f"stage-{k}/REQUIREMENTS.md"]
    snaps = sorted(p.name for p in (res.run_dir / "evaluator" / "snapshots").iterdir())
    assert snaps == [f"stage-{k}" for k in range(1, 6)]
    # Snapshots hold the workspace (here still the seeded skeleton), never the context file.
    snap1 = res.run_dir / "evaluator" / "snapshots" / "stage-1"
    assert "NotImplementedError" in (snap1 / "invoice" / "core.py").read_text()
    assert not (snap1 / "context.json").exists()
