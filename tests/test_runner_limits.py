"""Spec §8.1 tests 6-7: summaries, overflow recovery, stop conditions, accounting."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from clm_lib.budget import Ledger
from clm_lib.config import Config
from clm_lib.provider import AnthropicProvider, ModelRequest, ProviderError, ScriptedProvider
from clm_lib.runner import Runner
from clm_lib.tasks import generate

from .conftest import FINAL, TEST_PRICE, FakeExecutor, execute, working_ids


def small_budget(cfg: Config, budget: int, **kw: Any) -> Config:
    return replace(cfg, limits=replace(cfg.limits, context_budget_tokens=budget, **kw))


class Responder:
    """Answers summary requests with ``summary`` and action requests from a queue."""

    def __init__(
        self, actions: list[dict[str, Any]], summary: str = "SUMMARY: pool=12 at logs/a.log:42"
    ) -> None:
        self.actions = list(actions)
        self.summary = summary
        self.summary_requests: list[ModelRequest] = []

    def __call__(self, req: ModelRequest) -> str | dict[str, Any]:
        if req.purpose == "summary":
            self.summary_requests.append(req)
            return self.summary
        return self.actions.pop(0)

    def script(self, n: int = 40) -> list[Any]:
        return [self] * n


def test_summary_replaces_older_entries_and_keeps_tail(make_runner: Any, cfg: Config) -> None:
    resp = Responder([execute(f"step {i}") for i in range(6)] + [FINAL])
    ex = FakeExecutor(outputs=[f"out{i} " + "z" * 900 for i in range(6)])
    runner, prov = make_runner(resp.script(), executor=ex, config=small_budget(cfg, 3600))
    res = runner.run(generate("dev"), "summary")
    assert res.status == "completed"
    assert res.metrics["counts"]["summaries_applied"] >= 1
    assert res.metrics["counts"]["summary_calls"] == len(resp.summary_requests) >= 1
    first_sum = next(i for i, r in enumerate(prov.requests) if r.purpose == "summary")
    events = [json.loads(x) for x in (res.run_dir / "events.jsonl").read_text().splitlines()]
    applied = next(e for e in events if e["event"] == "summary_applied")
    after = working_ids(prov.requests[first_sum + 1].user)
    assert len(applied["kept_tail"]) == 4
    assert after == ["sum1", *applied["kept_tail"]]  # declared tail kept verbatim
    assert not set(applied["replaced"]) & set(after)  # older section replaced, not appended
    summarised = prov.requests[first_sum].user
    assert all(f'"id": "{i}"' in summarised for i in applied["replaced"])
    assert "SUMMARY: pool=12" in prov.requests[first_sum + 1].user
    assert "<task>" in prov.requests[first_sum].user  # summariser sees the task


def test_summary_overflow_after_bounded_retry(make_runner: Any, cfg: Config) -> None:
    resp = Responder([execute(f"s{i}") for i in range(6)] + [FINAL], summary="S" * 30000)
    ex = FakeExecutor(outputs=["z" * 1400 for _ in range(6)])
    runner, _ = make_runner(resp.script(), executor=ex, config=small_budget(cfg, 3600))
    res = runner.run(generate("dev"), "summary")
    assert res.status == "context_overflow"
    assert res.metrics["counts"]["summary_calls"] == 2  # one attempt + one bounded retry
    assert res.metrics["counts"]["summary_overflows"] == 1
    assert res.score.outcome == "no_answer"


def test_clm_recovery_request_then_overflow(make_runner: Any, cfg: Config) -> None:
    actions = [execute(f"s{i}") for i in range(10)] + [FINAL]
    ex = FakeExecutor(outputs=["z" * 1400 for _ in range(10)])
    runner, prov = make_runner(actions, executor=ex, config=small_budget(cfg, 3600))
    res = runner.run(generate("dev"), "clm")
    assert res.status == "context_overflow"
    assert res.metrics["counts"]["recovery_requests"] == 1
    assert "OVER LIMIT" in prov.requests[-1].user
    assert any("PRESSURE" in r.user for r in prov.requests)


def test_clm_recovery_succeeds_when_model_shrinks_context(make_runner: Any, cfg: Config) -> None:
    actions = [execute(f"s{i}") for i in range(10)] + [FINAL]
    ex = FakeExecutor(outputs=["z" * 1400 for _ in range(10)])
    runner, prov = make_runner(actions, executor=ex, config=small_budget(cfg, 3600))

    # Shrink the context on whichever execution follows the recovery request.
    original_run = ex.run

    def run(code: str, workspace: Any, fixtures: Any) -> Any:
        if "OVER LIMIT" in prov.requests[-1].user:
            empty = {
                "format": "clm-context/v1",
                "entries": [{"id": "n", "role": "note", "body": "kept"}],
            }
            (workspace / "context.json").write_text(json.dumps(empty))
        return original_run(code, workspace, fixtures)

    ex.run = run  # type: ignore[method-assign]
    res = runner.run(generate("dev"), "clm")
    assert res.status == "completed"
    assert res.metrics["counts"]["recovery_requests"] >= 1
    assert res.metrics["counts"]["edits_accepted_changed"] >= 1


def test_spill_moves_oversized_latest_observation(make_runner: Any, cfg: Config) -> None:
    ex = FakeExecutor(outputs=["small", "B" * 5900])
    runner, prov = make_runner(
        [execute("a"), execute("b"), FINAL], executor=ex, config=small_budget(cfg, 3600)
    )
    res = runner.run(generate("dev"), "summary")
    assert res.metrics["counts"]["spills"] == 1
    assert "moved to /task/workspace/spill/s2.obs.txt" in prov.requests[2].user
    assert (
        (res.run_dir / "workspace" / "spill" / "s2.obs.txt").read_text().startswith("exit_code=0")
    )


def test_max_calls_stop(make_runner: Any, cfg: Config) -> None:
    config = replace(cfg, limits=replace(cfg.limits, max_calls=3))
    runner, prov = make_runner([execute("x")] * 10, executor=FakeExecutor(), config=config)
    res = runner.run(generate("dev"), "clm")
    assert res.status == "max_calls"
    assert len(prov.requests) == 3 and res.metrics["counts"]["provider_calls"] == 3


def test_all_call_types_and_retries_are_accounted(make_runner: Any, cfg: Config) -> None:
    resp = Responder([execute("a"), execute("b"), execute("c"), FINAL])
    script: list[Any] = ["this is prose, not JSON", *resp.script()]
    ex = FakeExecutor(
        outputs=["z" * 1400] * 3,
        writes={2: {"context.json": "{broken"}},  # ignored in summary mode: not authoritative there
    )
    runner, prov = make_runner(
        script, executor=ex, price=TEST_PRICE, ceiling=5.0, config=small_budget(cfg, 3000)
    )
    prov.fail_with = {
        2: ProviderError("rate_limit", "429", retryable=True, billing="none"),
        3: ProviderError("timeout", "timed out", retryable=True, billing="unknown"),
    }
    res = runner.run(generate("dev"), "summary")
    c = res.metrics["counts"]
    ledger = json.loads((runner.ledger.path).read_text())["entries"]
    assert len(ledger) == c["provider_calls"] == len(prov.requests)
    kinds = {e["kind"] for e in ledger}
    assert {"action", "repair"} <= kinds
    assert c["repair_calls"] == 1 and c["api_retries"] == 2 and c["provider_errors"] == 2
    statuses = [e["status"] for e in ledger]
    assert "error:rate_limit" in statuses and "error:timeout" in statuses
    timeout = next(e for e in ledger if e["status"] == "error:timeout")
    assert (
        timeout["cost_basis"] == "reservation_assumed"
        and timeout["charged_usd"] == timeout["reserved_usd"]
    )
    rate = next(e for e in ledger if e["status"] == "error:rate_limit")
    assert rate["charged_usd"] == 0.0
    total = sum(e["charged_usd"] for e in ledger)
    assert res.metrics["cost"]["incurred_usd"] == pytest.approx(total, abs=1e-5)
    assert res.metrics["cost"]["of_which_assumed_from_reservation_usd"] == pytest.approx(
        timeout["charged_usd"]
    )


def test_rejected_edits_counted_in_clm(make_runner: Any) -> None:
    ex = FakeExecutor(writes={1: {"context.json": "{broken"}})
    runner, _ = make_runner([execute("a"), FINAL], executor=ex)
    res = runner.run(generate("dev"), "clm")
    assert (
        res.metrics["counts"]["edits_rejected"] == 1 and res.metrics["counts"]["edit_attempts"] == 1
    )


def test_no_call_starts_after_budget_exhaustion(make_runner: Any, tmp_path: Any) -> None:
    script = [execute("a"), execute("b"), FINAL]
    probe, _ = make_runner(list(script), executor=FakeExecutor(), price=TEST_PRICE, ceiling=5.0)
    pr = probe.run(generate("dev"), "clm")
    first = json.loads((pr.run_dir / "requests" / "0001.json").read_text())["meta"]["reserved_usd"]
    (tmp_path / "ledger.json").unlink()
    runner, prov = make_runner(
        list(script), executor=FakeExecutor(), price=TEST_PRICE, ceiling=first + 0.001
    )
    res = runner.run(generate("dev"), "clm")
    assert res.status == "budget_exhausted"
    assert len(prov.requests) == 1
    events = (res.run_dir / "events.jsonl").read_text()
    assert '"budget_stop"' in events
    assert runner.ledger.spent_usd <= first + 0.001


def test_ledger_ceiling_never_increases(tmp_path: Any) -> None:
    led = Ledger.open(tmp_path / "l.json", 2.0)
    assert Ledger.open(tmp_path / "l.json", 50.0).ceiling_usd == 2.0
    assert Ledger.open(tmp_path / "l.json", 1.0).ceiling_usd == 1.0
    assert led.ceiling_usd == 2.0


def test_scripted_provider_cannot_be_used_live_and_real_needs_opt_in(
    cfg: Config, tmp_path: Any
) -> None:
    led = Ledger.open(tmp_path / "l.json", 1.0)
    with pytest.raises(ValueError, match="scripted provider cannot be used for a live run"):
        Runner(cfg, ScriptedProvider([]), FakeExecutor(), led, TEST_PRICE, live=True)
    with pytest.raises(ValueError, match="requires live=True"):
        Runner(
            cfg,
            AnthropicProvider(model="claude-opus-5-5"),
            FakeExecutor(),
            led,
            TEST_PRICE,
            live=False,
        )
    with pytest.raises(ValueError, match="no dated price"):
        Runner(cfg, AnthropicProvider(model="unknown"), FakeExecutor(), led, None, live=True)
