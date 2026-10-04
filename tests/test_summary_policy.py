"""Experiment 003 baseline: token-tail/1 summary policy, with fixed-tail/1 preserved."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from clm_lib.baseline import FIXED_TAIL, TOKEN_TAIL, SummaryPolicy
from clm_lib.config import Config, load_config
from clm_lib.context import Entry
from clm_lib.prompts import SUMMARY_SYSTEM, SUMMARY_SYSTEM_TOKEN_TAIL, system_prompt
from clm_lib.tasks import generate

from .conftest import FINAL, TEST_PRICE, FakeExecutor, execute, working_ids

LARGE = 4200  # chars; at a 4,000-token budget this observation sinks fixed-tail/1


def config(policy: str, budget: int = 4000) -> Config:
    cfg = Config()
    return replace(
        cfg,
        baseline=replace(cfg.baseline, policy=policy),
        limits=replace(cfg.limits, context_budget_tokens=budget),
    )


class Responder:
    def __init__(self, n: int = 6, summaries: list[str] | None = None) -> None:
        self.actions = [execute(f"s{i}") for i in range(n)] + [FINAL]
        self.summaries = summaries or []
        self.summary_requests: list[Any] = []

    def __call__(self, req: Any) -> Any:
        if req.purpose == "summary":
            self.summary_requests.append(req)
            return self.summaries.pop(0) if self.summaries else "SUMMARY: pool=12 at logs/a.log:42"
        return self.actions.pop(0)


def outputs() -> list[str]:
    return ["a" * 500, "b" * 500, "B" * LARGE, "c" * 300, "d" * 300, "e" * 300]


def events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines()]


def test_large_recent_observation_is_compacted_not_kept_verbatim(make_runner: Any) -> None:
    resp = Responder()
    runner, _ = make_runner(
        [resp] * 40, executor=FakeExecutor(outputs=outputs()), config=config(TOKEN_TAIL)
    )
    res = runner.run(generate("dev"), "summary")
    assert res.status == "completed"
    applied = next(e for e in events(res.run_dir) if e["event"] == "summary_applied")
    assert "s3.obs" in applied["replaced"]  # the oversized recent entry went into compaction
    assert applied["policy"] == TOKEN_TAIL
    assert applied["est_request_tokens"] <= 0.7 * 4000  # room left below the pressure point
    assert res.metrics["counts"]["summaries_applied"] == 1


def test_same_case_overflows_under_the_original_policy(make_runner: Any) -> None:
    resp = Responder()
    runner, _ = make_runner(
        [resp] * 40, executor=FakeExecutor(outputs=outputs()), config=config(FIXED_TAIL)
    )
    res = runner.run(generate("dev"), "summary")
    assert res.status == "context_overflow"  # the failure mode token-tail/1 is meant to remove
    assert res.metrics["counts"]["summary_calls"] == 2


def test_request_after_summarisation_is_summary_plus_token_tail(make_runner: Any) -> None:
    resp = Responder()
    outs = ["a" * 500, "b" * 500, "c" * 2600, "d" * 300, "e" * 300, "f" * 300]
    runner, prov = make_runner(
        [resp] * 40, executor=FakeExecutor(outputs=outs), config=config(TOKEN_TAIL, 4000)
    )
    res = runner.run(generate("dev"), "summary")
    applied = next(e for e in events(res.run_dir) if e["event"] == "summary_applied")
    i = next(k for k, r in enumerate(prov.requests) if r.purpose == "summary")
    after = working_ids(prov.requests[i + 1].user)
    assert after == ["sum1", *applied["kept_tail"]]
    assert not set(applied["replaced"]) & set(after)
    assert "SUMMARY: pool=12" in prov.requests[i + 1].user
    summary_req = prov.requests[i]
    assert summary_req.system == SUMMARY_SYSTEM_TOKEN_TAIL
    assert f"at most {applied['requested_max_chars']} characters" in summary_req.user
    assert all(f'"id": "{x}"' in summary_req.user for x in applied["replaced"])
    assert len(applied["kept_tail"]) >= 1  # a small recent tail survives verbatim


def test_summary_attempts_and_retries_are_accounted(make_runner: Any, tmp_path: Path) -> None:
    resp = Responder(summaries=["S" * 9000, "SUMMARY: short"])  # first attempt too large
    runner, prov = make_runner(
        [resp] * 40,
        executor=FakeExecutor(outputs=outputs()),
        price=TEST_PRICE,
        ceiling=5.0,
        config=config(TOKEN_TAIL),
    )
    res = runner.run(generate("dev"), "summary")
    c = res.metrics["counts"]
    ev = events(res.run_dir)
    assert c["summary_calls"] == 2 and c["summaries_applied"] == 1
    too_large = [e for e in ev if e["event"] == "summary_too_large"]
    assert len(too_large) == 1 and too_large[0]["attempt"] == 0
    applied = next(e for e in ev if e["event"] == "summary_applied")
    assert (
        applied["attempt"] == 1
        and applied["requested_max_chars"] < too_large[0]["requested_max_chars"]
    )
    ledger = json.loads((tmp_path / "ledger.json").read_text())["entries"]
    assert sum(1 for e in ledger if e["kind"] == "summary") == 2
    assert len(ledger) == c["provider_calls"] == len(prov.requests)


def test_split_bounds_the_tail_by_tokens() -> None:
    entries = (
        Entry("a", "observation", "x" * 300),
        Entry("b", "observation", "x" * 3000),
        Entry("c", "assistant", "x" * 300),
        Entry("d", "observation", "x" * 300),
    )
    tokens = {"a": 100, "b": 1000, "c": 100, "d": 100}
    pol = SummaryPolicy(policy=TOKEN_TAIL, tail_tokens=400, newest_tokens=2000)
    older, tail = pol.split(entries, lambda e: tokens[e.id])
    assert [e.id for e in tail] == ["c", "d"] and [e.id for e in older] == ["a", "b"]
    # A large newest entry is kept alone if it fits the newest-entry cap...
    newest = (*entries[:3], Entry("y", "observation", "x" * 4000))
    older, tail = pol.split(newest, lambda e: tokens.get(e.id, 1500))
    assert [e.id for e in tail] == ["y"] and [e.id for e in older] == ["a", "b", "c"]
    # ...and compacted too if it exceeds even that, rather than blocking everything.
    big_last = (*entries[:3], Entry("z", "observation", "x" * 9000))
    older, tail = pol.split(big_last, lambda e: tokens.get(e.id, 3000))
    assert tail == [] and older[-1].id == "z"
    # fixed-tail/1 keeps four entries regardless of size.
    older, tail = SummaryPolicy().split(entries)
    assert [e.id for e in tail] == ["a", "b", "c", "d"] and older == []


def test_original_policy_is_default_and_unchanged(make_runner: Any) -> None:
    cfg = Config()
    assert cfg.baseline.policy == FIXED_TAIL
    assert load_config(Path("configs/default.toml")).baseline.policy == FIXED_TAIL
    assert load_config(Path("configs/exp003.toml")).baseline.policy == TOKEN_TAIL
    pol = SummaryPolicy()
    assert pol.policy == FIXED_TAIL
    assert [pol.char_limit(0, 99999), pol.char_limit(1, 99999)] == [3000, 1500]
    assert pol.request("t", [], 0, 100).system == SUMMARY_SYSTEM
    kw: dict[str, Any] = dict(
        exec_timeout=30, output_cap=6000, pressure_pct=70, tail=4, max_entries=200, max_body=24000
    )
    original = system_prompt("summary", **kw)
    assert "keeping the newest 4 entries verbatim" in original
    assert system_prompt("summary", **kw, summary_policy=FIXED_TAIL) == original
    assert system_prompt("clm", **kw) == system_prompt("clm", **kw, summary_policy=TOKEN_TAIL)
    with pytest.raises(ValueError, match="unknown summary policy"):
        SummaryPolicy(policy="nope")
    # Provenance: the policy is recorded for summary runs.
    runner, _ = make_runner([execute("a"), FINAL], executor=FakeExecutor())
    res = runner.run(generate("dev"), "summary")
    assert json.loads((res.run_dir / "run.json").read_text())["summary_policy"] == FIXED_TAIL
    assert res.metrics["summary_policy"] == FIXED_TAIL
