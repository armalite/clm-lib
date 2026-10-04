"""Spec §8.1 test 8a: fixtures are deterministic and the scorer separates outcomes."""

from __future__ import annotations

from typing import Any

from clm_lib.tasks import HELDOUT, INSTANCES, TaskInstance, generate, score_answer

# Experiment 001 task family; the staged family (experiment 002) is tested in test_staged.py.
SINGLE = [n for n in INSTANCES if not n.startswith(("staged-", "coding"))]


def refs(inst: TaskInstance) -> list[str]:
    g = inst.truth.evidence_groups
    return [f"{p}:{n}" for p, n in (g["cause"][0], g["value"][0])]


def answer(inst: TaskInstance, **over: Any) -> dict[str, Any]:
    base = {
        "root_cause": f"{inst.truth.cause}: explanation",
        "required_value": inst.truth.value_variants[0],
        "remedy": f"{inst.truth.remedies[0]}: do it",
        "evidence_refs": refs(inst),
    }
    base.update(over)
    return base


def test_generation_is_deterministic_and_heldout_differs() -> None:
    hashes = {}
    for name in SINGLE:
        a, b = generate(name), generate(name)
        assert a.fixture_sha256 == b.fixture_sha256
        hashes[name] = a.fixture_sha256
    assert len(set(hashes.values())) == len(SINGLE)
    scenarios = {generate(n).spec.scenario for n in HELDOUT}
    assert generate("dev").spec.scenario not in scenarios
    values = {generate(n).truth.value_variants[0] for n in SINGLE}
    assert len(values) == len(SINGLE)


def test_fixtures_exceed_budget_but_lines_fit_output_cap() -> None:
    for name in SINGLE:
        inst = generate(name)
        assert sum(len(t) for t in inst.files.values()) > 8000 * 3 * 5
        assert max(len(line) for t in inst.files.values() for line in t.splitlines()) < 400
        assert inst.truth.value_variants[0] not in inst.prompt
        for group in inst.truth.evidence_groups.values():
            for path, line in group:
                assert 1 <= line <= inst.files[path].count("\n")


def test_scorer_outcomes() -> None:
    for name in SINGLE:
        inst = generate(name)
        t, f = inst.truth, inst.files
        ok = score_answer(answer(inst), t, f)
        assert ok.strict_success and ok.outcome == "correct"
        assert ok.components == {"cause": 1.0, "remedy": 1.0, "value": 1.0, "evidence": 1.0}
        unsupported = score_answer(answer(inst, evidence_refs=["ops/oncall-notes.md:5"]), t, f)
        assert unsupported.outcome == "unsupported" and not unsupported.strict_success
        stale = score_answer(answer(inst, required_value=t.stale_variants[0]), t, f)
        assert stale.outcome == "stale_value" and stale.value_status == "stale"
        wrong = score_answer(answer(inst, root_cause="DISK_FULL: no"), t, f)
        assert wrong.outcome == "incorrect" and not wrong.cause_ok
        assert score_answer(None, t, f).outcome == "no_answer"


def test_reference_parsing() -> None:
    inst = generate("dev")
    (cp, cn), (vp, vn) = (
        inst.truth.evidence_groups["cause"][0],
        inst.truth.evidence_groups["value"][0],
    )
    s = score_answer(
        answer(
            inst, evidence_refs=[f"/task/fixtures/{cp}:{cn}", f"{vp}:{max(1, vn - 2)}-{vn + 1}"]
        ),
        inst.truth,
        inst.files,
    )
    assert s.strict_success and s.valid_refs == 2
    bad = score_answer(
        answer(inst, evidence_refs=[f"{cp}:1-500", "nope.log:3", "garbage"]), inst.truth, inst.files
    )
    assert bad.valid_refs == 0 and len(bad.invalid_refs) == 3 and not bad.strict_success


def test_value_normalisation_variants() -> None:
    inst = generate("heldout-1")
    serial = inst.truth.value_variants[0]
    s = score_answer(
        answer(inst, required_value=f"`{serial.lower().replace(':', '')}`"), inst.truth, inst.files
    )
    assert s.value_status == "exact"


def test_setting_equals_value_form_is_accepted_by_score_v2_only() -> None:
    inst = generate("heldout-2")
    v, stale = inst.truth.value_variants[0], inst.truth.stale_variants[0]
    ans = answer(inst, required_value=f"clients.pricing-core.timeout_ms={v}")
    assert score_answer(ans, inst.truth, inst.files, version="score/1").value_status == "wrong"
    assert score_answer(ans, inst.truth, inst.files).value_status == "exact"
    stale_ans = answer(inst, required_value=f"clients.x.timeout_ms={stale}")
    assert score_answer(stale_ans, inst.truth, inst.files).value_status == "stale"
    serial = generate("heldout-1").truth.value_variants[0]  # colon-separated serials unaffected
    h1 = generate("heldout-1")
    assert (
        score_answer(answer(h1, required_value=serial), h1.truth, h1.files).value_status == "exact"
    )
