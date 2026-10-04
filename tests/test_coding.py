"""Experiment 004 coding task: generation, stage visibility, evaluator isolation and scoring.

These test the library (task generator, runtime, evaluator). Agent performance is measured
separately by the evaluator checks during live runs.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

import pytest

from clm_lib import coding
from clm_lib.prompts import system_prompt
from clm_lib.runner import CODING_FIELDS, parse_action
from clm_lib.tasks import INSTANCES, generate

from .coding_solutions import RUN_VISIBLE_TESTS, write_solution_code
from .conftest import execute, working_block

CODING = [n for n in INSTANCES if n.startswith("coding-")]
ADVANCE = {"action": "advance"}
FINAL = {"action": "final", "answer": {"summary": "done"}}


def test_generation_is_deterministic_and_instances_differ_in_structure() -> None:
    structures = set()
    for name in CODING:
        a, b = generate(name), generate(name)
        assert a.fixture_sha256 == b.fixture_sha256 and a.kind == "coding" and a.n_stages == 4
        structures.add(json.dumps(a.truth.schedule))
        cats = Counter(a.truth.categories.values())
        assert cats["retained"] >= 1 and cats["replaced"] >= 1  # unchanged and superseded rules
        final = coding.config_at(a.truth.schedule, 4)
        assert {c["module"] for c in a.truth.checks} == set(final)  # only disclosed rules
        for c in a.truth.checks:
            if c["category"] == "replaced":
                assert c["stale"] is not None and c["stale"] != c["expect"]
        for k, files in enumerate(a.stages, start=1):
            assert set(files) == {f"stage-{k}/REQUIREMENTS.md", "current-tests/test_invoice.py"}
            cfg = coding.config_at(a.truth.schedule, k)
            tests = files["current-tests/test_invoice.py"]
            assert all(f"def test_{m.lower()}_" in tests for m in cfg)  # every rule in force
        later = "".join(a.stages[1]["stage-2/REQUIREMENTS.md"])
        assert "Only the changes are listed" in later
    assert len(structures) == len(CODING)


def test_visible_test_inputs_differ_from_evaluator_inputs() -> None:
    inst = generate("coding-eval-1")
    visible = inst.stages[-1]["current-tests/test_invoice.py"]
    for c in inst.truth.checks:
        assert repr(c["args"]) not in visible


def test_coding_final_answer_and_prompts() -> None:
    ok, err = parse_action(
        '{"action": "final", "answer": {"summary": "x"}}', 1000, True, CODING_FIELDS
    )
    assert ok and not err
    bad, err = parse_action(
        '{"action": "final", "answer": {"root_cause": "x"}}', 1000, True, CODING_FIELDS
    )
    assert bad is None and "summary" in err
    kw: dict[str, Any] = dict(
        exec_timeout=30, output_cap=6000, pressure_pct=70, tail=4, max_entries=200, max_body=24000
    )
    # Incident prompts are byte-identical to those recorded in experiment 002.
    assert (
        hashlib.sha256(system_prompt("summary", **kw).encode())
        .hexdigest()
        .startswith("84134cfb02d2d02e")
    )
    assert (
        hashlib.sha256(system_prompt("clm", **kw).encode())
        .hexdigest()
        .startswith("fa474b0d1ea967a4")
    )
    coding_prompt = system_prompt("clm", **kw, task_kind="coding")
    assert "software engineering agent" in coding_prompt and '"summary"' in coding_prompt
    assert "root_cause" not in coding_prompt


def _final_cfg(name: str) -> dict[str, int]:
    return coding.config_at(generate(name).truth.schedule, 4)


def _run(make_runner: Any, name: str, cfg: dict[str, int], extra: list[Any] | None = None) -> Any:
    script = [execute(write_solution_code(cfg)), *(extra or []), ADVANCE, ADVANCE, ADVANCE, FINAL]
    runner, prov = make_runner(script)
    return runner.run(generate(name), "clm"), prov


@pytest.mark.docker
def test_known_correct_solution_is_strictly_successful(make_runner: Any) -> None:
    res, _ = _run(make_runner, "coding-eval-2", _final_cfg("coding-eval-2"))
    s = res.score
    assert res.status == "completed" and s.outcome == "correct" and s.strict_success
    assert s.passed == s.checks and s.stale == 0
    assert (res.run_dir / "evaluator" / "submission" / "invoice" / "core.py").exists()
    assert not (res.run_dir / "evaluator" / "_eval_work").exists()


@pytest.mark.docker
def test_regressed_solution_fails_retained_checks(make_runner: Any) -> None:
    name = "coding-eval-2"
    inst = generate(name)
    retained = next(m for m, c in inst.truth.categories.items() if c == "retained" and m != "ROUND")
    cfg = {k: v for k, v in _final_cfg(name).items() if k != retained}  # rule dropped
    s = _run(make_runner, name, cfg)[0].score
    assert s.outcome == "failed_checks" and not s.strict_success
    assert s.by_category["retained"]["passed"] < s.by_category["retained"]["total"]
    assert s.by_category["retained"]["stale"] == 0


@pytest.mark.docker
def test_stale_rule_solution_is_detected(make_runner: Any) -> None:
    name = "coding-eval-1"
    inst = generate(name)
    replaced = next(m for m, c in inst.truth.categories.items() if c == "replaced")
    cfg = {**_final_cfg(name), replaced: 1}  # old rule left in place
    s = _run(make_runner, name, cfg)[0].score
    assert not s.strict_success
    module_checks = [c["id"] for c in inst.truth.checks if c["module"] == replaced]
    stale_ids = {r["id"] for r in s.results if r["stale"]}
    assert set(module_checks) <= stale_ids  # every check of that rule shows the old behaviour
    assert s.by_category["replaced"]["stale"] >= len(module_checks)


@pytest.mark.docker
def test_unsubmitted_correct_code_is_not_strict(make_runner: Any) -> None:
    runner, _ = make_runner(
        [execute(write_solution_code(_final_cfg("coding-eval-3"))), ADVANCE, ADVANCE, ADVANCE]
    )
    res = runner.run(generate("coding-eval-3"), "clm")  # script ends: no final answer
    assert res.score.outcome == "correct_unsubmitted" and not res.score.strict_success


@pytest.mark.docker
def test_stage_visibility_and_evaluator_isolation(make_runner: Any) -> None:
    probe = (
        "import os\n"
        "fx = sorted(os.path.relpath(os.path.join(r, f), '/task/fixtures') for r, _, fs in os.walk('/task/fixtures') for f in fs)\n"
        "print('fixtures', fx)\n"
        "ev = [os.path.join(r, f) for r, _, fs in os.walk('/task') for f in fs if 'checks' in f or 'truth' in f]\n"
        "print('evaluator_files', ev)\n"
        "try:\n    open('/task/fixtures/current-tests/test_invoice.py', 'a').write('x')\n    print('visible_tests_writable True')\n"
        "except OSError:\n    print('visible_tests_writable False')\n"
        "print('tests_head', open('/task/fixtures/current-tests/test_invoice.py').read()[:60])\n"
    )
    inst = generate("coding-dev-1")
    script = [execute(probe), ADVANCE, execute(probe), ADVANCE, ADVANCE, execute(probe), FINAL]
    runner, prov = make_runner(script)
    res = runner.run(inst, "clm")
    obs = [r.user for r in prov.requests]
    assert "fixtures ['current-tests/test_invoice.py', 'stage-1/REQUIREMENTS.md']" in obs[1]
    assert "'stage-2/REQUIREMENTS.md'" in obs[3] and "'stage-3/REQUIREMENTS.md'" not in obs[3]
    assert "'stage-4/REQUIREMENTS.md'" in obs[6]
    assert all(
        "evaluator_files []" in o and "visible_tests_writable False" in o
        for o in (obs[1], obs[3], obs[6])
    )
    assert "after stage 1" in working_block(obs[1]) and "after stage 4" in working_block(obs[6])
    # The released requirement text is also added to the working context on advance.
    assert "Stage 2 of 4 requirements" in working_block(obs[2])
    # Earlier stage documents never change; the visible tests reflect the final stage.
    for k in range(1, 5):
        doc = (res.run_dir / "fixtures" / f"stage-{k}" / "REQUIREMENTS.md").read_text()
        assert doc == inst.stages[k - 1][f"stage-{k}/REQUIREMENTS.md"]
    final_tests = (res.run_dir / "fixtures" / "current-tests" / "test_invoice.py").read_text()
    assert final_tests == inst.stages[3]["current-tests/test_invoice.py"]


@pytest.mark.docker
def test_visible_tests_run_in_the_sandbox(make_runner: Any) -> None:
    inst = generate("coding-dev-2")
    stage1 = coding.config_at(inst.truth.schedule, 1)
    runner, prov = make_runner(
        [execute(write_solution_code(stage1)), execute(RUN_VISIBLE_TESTS), FINAL]
    )
    runner.run(inst, "clm")
    out = working_block(prov.requests[2].user)
    assert "rc 0" in out and "OK" in out
