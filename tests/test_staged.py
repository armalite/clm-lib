"""Experiment 002: staged task progression, evidence visibility, scoring and timing."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from clm_lib.config import Config
from clm_lib.tasks import INSTANCES, STAGED_EVAL, generate, score_answer

from .conftest import FakeExecutor, execute, working_block

STAGED = [n for n in INSTANCES if n.startswith("staged-")]
ADVANCE = {"action": "advance"}


def staged_final(inst: Any, **over: Any) -> dict[str, Any]:
    g = inst.truth.evidence_groups
    ans = {
        "root_cause": "DB_POOL_EXHAUSTED: pool too small",
        "required_value": inst.truth.value_variants[0],
        "remedy": "RAISE_DB_POOL_LIMIT: raise it",
        "evidence_refs": [f"{p}:{n}" for p, n in (g["cause"][0], g["value"][0], g["origin"][0])],
    }
    ans.update(over)
    return {"action": "final", "answer": ans}


def test_stages_are_disjoint_deterministic_and_evidence_is_staged() -> None:
    hashes = set()
    for name in STAGED:
        a, b = generate(name), generate(name)
        assert a.fixture_sha256 == b.fixture_sha256 and a.n_stages == 3
        hashes.add(a.fixture_sha256)
        for k, files in enumerate(a.stages, start=1):
            assert all(p.startswith(f"stage-{k}/") for p in files)
        union: dict[str, str] = {}
        for files in a.stages:
            union.update(files)
        assert union == a.files
        g = a.truth.evidence_groups
        assert g["origin"][0][0].startswith("stage-1/") and g["value"][0][0].startswith("stage-3/")
        assert all(p.startswith(("stage-2/", "stage-3/")) for p, _ in g["cause"])
        origin_text = a.files[g["origin"][0][0]].splitlines()[g["origin"][0][1] - 1]
        assert "db.pool.max_size" in origin_text and a.truth.stale_variants[0] in origin_text
        assert f"max_size={a.truth.value_variants[0]}" not in a.prompt
        # Stage-1 files alone contain neither the final value record nor the later updates.
        stage1 = "".join(a.stages[0].values())
        value_line = a.files[g["value"][0][0]].splitlines()[g["value"][0][1] - 1]
        assert value_line not in stage1 and "Stage 2 of 3" not in stage1
    assert len(hashes) == len(STAGED)
    assert len({generate(n).truth.value_variants[0] for n in STAGED_EVAL} | {"x"}) >= 2


def test_staged_scoring_outcomes() -> None:
    inst = generate("staged-dev-1")
    t, f = inst.truth, inst.files
    ok = score_answer(staged_final(inst)["answer"], t, f)
    assert ok.strict_success and ok.groups_covered == {"cause": True, "value": True, "origin": True}
    for stale in t.stale_variants:  # build default and repo config default
        assert (
            score_answer(staged_final(inst, required_value=stale)["answer"], t, f).outcome
            == "stale_value"
        )
    hyp = score_answer(
        staged_final(inst, root_cause="CLIENT_TIMEOUT_TOO_LOW: timeouts")["answer"], t, f
    )
    assert hyp.outcome == "stale_hypothesis" and not hyp.cause_ok
    g = t.evidence_groups
    no_origin = staged_final(
        inst, evidence_refs=[f"{p}:{n}" for p, n in (g["cause"][0], g["value"][0])]
    )
    s = score_answer(no_origin["answer"], t, f)
    assert s.outcome == "unsupported" and s.groups_covered["origin"] is False


@pytest.mark.docker
def test_future_stages_and_truth_are_not_visible_early(make_runner: Any) -> None:
    inst = generate("staged-dev-1")
    g = inst.truth.evidence_groups
    value_line = inst.files[g["value"][0][0]].splitlines()[g["value"][0][1] - 1]
    probe = (
        "import os\n"
        "print('dirs', sorted(os.listdir('/task/fixtures')))\n"
        "blob = ''.join(open(os.path.join(r, f)).read() for r, _, fs in os.walk('/task/fixtures') for f in fs)\n"
        f"print('value_record_visible', {value_line!r} in blob)\n"
        "print('truth_files', [f for r, _, fs in os.walk('/task') for f in fs if 'truth' in f])\n"
    )
    script = [
        execute(probe),
        staged_final(inst),  # premature: must not be accepted at stage 1
        ADVANCE,
        execute(probe),
        ADVANCE,
        execute(probe),
        ADVANCE,  # no-op: everything already released
        staged_final(inst),
    ]
    runner, prov = make_runner(script)
    res = runner.run(inst, "clm")
    assert res.status == "completed" and res.score.strict_success
    obs = [r.user for r in prov.requests]
    assert "dirs ['stage-1']" in obs[1] and "value_record_visible False" in obs[1]
    assert "Final answer not accepted: 1 of 3" in obs[2]
    assert "Stage 2 of 3 released" in obs[3] and "Stage 2 of 3 (09:30" in obs[3]
    assert "dirs ['stage-1', 'stage-2']" in obs[4] and "value_record_visible False" in obs[4]
    assert (
        "dirs ['stage-1', 'stage-2', 'stage-3']" in obs[6] and "value_record_visible True" in obs[6]
    )
    assert "No further evidence" in obs[7]
    assert all("truth_files []" in o for o in (obs[1], obs[4], obs[6]))
    m = res.metrics["management"]
    assert m["stages_released"] == 3 and m["premature_finals"] == 1
    assert [t["stage"] for t in m["timeline"]] == [1, 1, 1, 2, 2, 3, 3, 3]
    # Earlier stage files are unchanged after later releases.
    for rel, text in inst.stages[0].items():
        assert (res.run_dir / "fixtures" / rel).read_text() == text


def test_advance_is_rejected_for_unstaged_tasks(make_runner: Any) -> None:
    runner, _ = make_runner([ADVANCE, ADVANCE], executor=FakeExecutor())
    res = runner.run(generate("dev"), "clm")
    assert res.status == "failed_protocol"


def test_management_timing_is_recorded(make_runner: Any, cfg: Config) -> None:
    inst = generate("staged-dev-2")
    small = replace(cfg, limits=replace(cfg.limits, context_budget_tokens=4200, max_calls=30))

    class Responder:
        def __init__(self) -> None:
            self.actions = [execute("a"), execute("b"), ADVANCE, execute("c"), execute("d"),
                            ADVANCE, execute("e"), ADVANCE, staged_final(inst)]  # fmt: skip

        def __call__(self, req: Any) -> Any:
            return (
                "SUMMARY: pool facts at stage-1 release notes"
                if req.purpose == "summary"
                else self.actions.pop(0)
            )

    resp = Responder()
    ex = FakeExecutor(outputs=["z" * 700 for _ in range(10)])
    runner, prov = make_runner([resp] * 40, executor=ex, config=small)
    res = runner.run(inst, "summary")
    m = res.metrics["management"]
    assert res.status == "completed", res.stop_reason
    assert m["summary_steps"], "expected at least one summary under the small budget"
    first = m["first_management_step"]
    assert first == m["summary_steps"][0]
    assert m["action_steps_after_first_management"] == sum(
        1 for t in m["timeline"] if t["step"] >= first
    )
    assert m["first_pressure_step"] is not None and m["first_pressure_step"] <= first
    assert m["first_management_stage"] in (1, 2, 3)
    assert json.dumps(m)  # serialisable
    assert "SUMMARY: pool facts" in working_block(prov.requests[-1].user)
