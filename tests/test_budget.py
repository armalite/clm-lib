"""Ledger persistence across interruption/restart, the reservation bound and billing classes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clm_lib.budget import BOUND_OVERHEAD_TOKENS, Ledger, LedgerBusy, input_token_bound
from clm_lib.provider import AnthropicProvider, ModelRequest, ModelResponse, ScriptedProvider, Usage
from clm_lib.tasks import generate

from .conftest import FINAL, TEST_PRICE, FakeExecutor, execute


def test_pending_reservation_is_persisted_before_dispatch(tmp_path: Path) -> None:
    led = Ledger.open(tmp_path / "l.json", 1.0)
    res = led.reserve(0.25, "run-x", "action")
    on_disk = json.loads((tmp_path / "l.json").read_text())
    assert [p["id"] for p in on_disk["pending"]] == [res.id]
    assert on_disk["pending"][0]["reserved_usd"] == 0.25
    led.settle(res, actual_usd=0.01, status="ok")
    on_disk = json.loads((tmp_path / "l.json").read_text())
    assert on_disk["pending"] == [] and on_disk["entries"][-1]["charged_usd"] == 0.01


def test_killed_process_reservation_is_charged_on_restart(tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    code = (
        "import os, sys\nfrom pathlib import Path\nfrom clm_lib.budget import Ledger\n"
        f"led = Ledger.open(Path({str(path)!r}), 1.0)\n"
        "led.reserve(0.3, 'run-killed', 'action')\n"
        "os._exit(9)  # simulated crash after dispatch, before settlement\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
    assert proc.returncode == 9
    led = Ledger.open(path, 1.0)
    entry = led.entries[-1]
    assert entry["status"] == "unresolved_after_restart"
    assert entry["charged_usd"] == 0.3 and entry["cost_basis"] == "reservation_assumed"
    assert led.spent_usd == pytest.approx(0.3) and led.remaining_usd == pytest.approx(0.7)
    assert json.loads(path.read_text())["pending"] == []
    # Reopening again must not double-charge.
    assert Ledger.open(path, 1.0).spent_usd == pytest.approx(0.3)


def test_live_pending_from_another_process_blocks_open(tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        doc = {
            "ceiling_usd": 1.0,
            "entries": [],
            "pending": [
                {
                    "id": "a",
                    "ts": "t",
                    "pid": sleeper.pid,
                    "run_id": "r",
                    "kind": "action",
                    "reserved_usd": 0.1,
                }
            ],
        }
        path.write_text(json.dumps(doc))
        with pytest.raises(LedgerBusy):
            Ledger.open(path, 1.0)
    finally:
        sleeper.kill()
        sleeper.wait()


def test_interrupt_during_provider_call_charges_reservation(
    make_runner: Any, tmp_path: Path
) -> None:
    def boom(req: ModelRequest) -> str:
        raise KeyboardInterrupt

    runner, _ = make_runner(
        [execute("a"), boom], executor=FakeExecutor(), price=TEST_PRICE, ceiling=5.0
    )
    with pytest.raises(KeyboardInterrupt):
        runner.run(generate("dev"), "clm")
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    last = ledger["entries"][-1]
    assert last["status"] == "interrupted:KeyboardInterrupt"
    assert (
        last["cost_basis"] == "reservation_assumed" and last["charged_usd"] == last["reserved_usd"]
    )
    assert ledger["pending"] == []
    summary = json.loads(next((tmp_path / "runs").glob("*/summary.json")).read_text())
    assert summary["status"] == "interrupted"
    assert summary["cost"]["of_which_assumed_from_reservation_usd"] == pytest.approx(
        last["charged_usd"]
    )


def test_bound_covers_payload_bytes() -> None:
    prov = AnthropicProvider(model="claude-opus-5-5")
    req = ModelRequest("sys é", "user ✓ " * 100, 2048, "action", {"type": "object"})
    payload = prov.payload(req)
    text_bytes = len("sys é".encode()) + len(("user ✓ " * 100).encode())
    assert input_token_bound(payload) >= text_bytes + BOUND_OVERHEAD_TOKENS


class OverReportingProvider(ScriptedProvider):
    def complete(self, req: ModelRequest) -> ModelResponse:
        resp = super().complete(req)
        resp.usage = Usage(input_tokens=10_000_000, output_tokens=10)
        resp.usage_source = "provider"
        return resp


def test_run_stops_when_reported_usage_exceeds_bound(cfg: Any, tmp_path: Path) -> None:
    from clm_lib.runner import Runner

    prov = OverReportingProvider([execute("a"), FINAL])
    led = Ledger.open(tmp_path / "l.json", 100.0)
    runner = Runner(
        cfg, prov, FakeExecutor(), led, TEST_PRICE, live=False, runs_dir=tmp_path / "runs"
    )
    res = runner.run(generate("dev"), "clm")
    assert res.status == "accounting_bound_violated"
    assert res.metrics["valid_for_comparison"] is False
    assert len(prov.requests) == 1


def _raising_provider(exc: Exception) -> AnthropicProvider:
    def create(**kwargs: Any) -> Any:
        raise exc

    prov = AnthropicProvider(model="claude-opus-5-5")
    prov._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return prov


def test_billing_classification() -> None:
    import anthropic
    import httpx2

    from clm_lib.provider import ProviderError

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    cases = [
        (
            anthropic.InternalServerError(
                "x", response=httpx2.Response(500, request=req), body=None
            ),
            "unknown",
        ),
        (
            anthropic.RateLimitError("x", response=httpx2.Response(429, request=req), body=None),
            "none",
        ),
        (
            anthropic.BadRequestError("x", response=httpx2.Response(400, request=req), body=None),
            "none",
        ),
        (anthropic.APITimeoutError(request=req), "unknown"),
        (RuntimeError("something odd after send"), "unknown"),
        (RuntimeError("credential chain: token expired"), "none"),
    ]
    for exc, billing in cases:
        with pytest.raises(ProviderError) as info:
            _raising_provider(exc).complete(ModelRequest("s", "u", 10, "action"))
        assert info.value.billing == billing, (type(exc).__name__, info.value)


def test_compare_halts_on_accounting_bound_violation(cfg: Any, tmp_path: Path) -> None:
    from clm_lib.cli import run_matrix
    from clm_lib.runner import Runner

    prov = OverReportingProvider([FINAL] * 20)
    led = Ledger.open(tmp_path / "l.json", 100.0)
    runner = Runner(
        cfg, prov, FakeExecutor(), led, TEST_PRICE, live=False, runs_dir=tmp_path / "runs"
    )
    out = tmp_path / "cmp.json"
    cells, code = run_matrix(runner, cfg, ["heldout-1", "heldout-2"], 2, out, "t")
    assert code == 4
    assert len(prov.requests) == 1  # nothing dispatched after the violation
    assert cells[0]["status"] == "accounting_bound_violated"
    assert len(cells) == 8 and all(c["status"] == "missing" for c in cells[1:])
    assert all("accounting assumption failed" in c["reason"] for c in cells[1:])
    rec = json.loads(out.read_text())
    assert rec["halted"] and rec["frozen"]["prompt_version"] and "code_state" in rec["frozen"]
