"""Accounted dispatch for ``native-tools/1`` requests (programme 015). Opt-in.

This mirrors the existing ``Runner.call`` rules for the single-action protocol, which are
unchanged:

- before every attempt (first tries and retries alike) a cost bound is reserved in the ledger
  and persisted as pending; nothing is sent if it does not fit;
- each attempt is settled exactly once: provider-reported usage at the dated price; ``0`` only
  for provably unbilled failures (``billing == "none"``); the full reservation when the outcome
  is unknown (5xx, timeout, connection loss, interruption); a refusal from its returned usage;
- SDK retries are off, so runtime retries are the only retries, and each is its own attempt;
- reported input above the bound, or output above ``max_tokens``, stops with
  ``AccountingBoundViolated``.

Differences from the single-action path: the input bound also covers the tool definitions,
``tool_choice`` and ``thinking`` (``native_input_token_bound``); failed attempts' latency is
recorded too; usage is kept per attempt and per role (``main`` / ``summary``) so retries and
summaries are never folded into main-model figures.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .budget import (
    BOUND_OVERHEAD_TOKENS,
    BudgetExhausted,
    Ledger,
    ModelPrice,
    PriceUnavailable,
)
from .counters import SessionCounters
from .provider import ProviderError, Usage
from .transport import NativeProvider, NativeRequest, NativeResponse

NATIVE_BOUND_BASIS = (
    "utf8 bytes of system, messages, tools, tool_choice, thinking and output_config JSON plus "
    f"{BOUND_OVERHEAD_TOKENS} (assumption: <=1 token per byte; checked per call)"
)


class AccountingBoundViolated(RuntimeError):
    """Reported usage exceeded the reservation bound; reservations are unsafe."""


class CallFailed(RuntimeError):
    """A logical call ended without a usable response (all attempts failed, or a refusal)."""

    def __init__(self, error: ProviderError, attempts: int) -> None:
        super().__init__(f"{error} after {attempts} attempt(s)")
        self.error = error
        self.attempts = attempts


def native_input_token_bound(payload: dict[str, Any]) -> int:
    """Upper bound (same byte-based assumption as ``budget.input_token_bound``) on the input
    tokens of a native-tools payload. Tools and thinking settings are part of the input."""
    parts: list[str] = []
    system = payload.get("system", "")
    parts.append(system if isinstance(system, str) else json.dumps(system))
    for msg in payload.get("messages", []):
        content = msg.get("content", "")
        parts.append(content if isinstance(content, str) else json.dumps(content))
    for key in ("tools", "tool_choice", "thinking", "output_config"):
        if key in payload:
            parts.append(json.dumps(payload[key]))
    return sum(len(p.encode("utf-8")) for p in parts) + BOUND_OVERHEAD_TOKENS


@dataclass
class AttemptRecord:
    role: str
    call_index: int  # logical call number within the session, from 0
    attempt: int  # 0 for the first try, then 1, 2, ... for retries
    outcome: str  # "ok" | "error:<kind>" | "interrupted:<type>"
    retried: bool  # another attempt followed this one
    usage: dict[str, int | None] | None
    usage_source: str | None
    latency_s: float
    reserved_usd: float
    charged_usd: float
    cost_basis: str
    input_token_bound: int
    stop_reason: str | None = None
    request_id: str | None = None


_TOKEN_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


@dataclass
class NativeAccounting:
    attempts: list[AttemptRecord] = field(default_factory=list)
    calls: dict[str, int] = field(default_factory=dict)

    def by_role(self) -> dict[str, dict[str, float]]:
        """Totals per role. Tokens are summed from provider-reported usage of every attempt
        that returned usage (ok attempts and billed refusals), each attempt once."""
        out: dict[str, dict[str, float]] = {}
        for a in self.attempts:
            r = out.setdefault(
                a.role,
                {
                    "calls": 0,
                    "attempts": 0,
                    "retries": 0,
                    "failed_attempts": 0,
                    **dict.fromkeys(_TOKEN_FIELDS, 0),
                    "cache_creation_5m": 0,
                    "cache_creation_1h": 0,
                    "charged_usd": 0.0,
                    "assumed_usd": 0.0,
                    "latency_s": 0.0,
                },
            )
            r["attempts"] += 1
            r["calls"] += int(a.attempt == 0)
            r["retries"] += int(a.attempt > 0)
            r["failed_attempts"] += int(a.outcome != "ok")
            if a.usage:
                for k in (*_TOKEN_FIELDS, "cache_creation_5m", "cache_creation_1h"):
                    r[k] += a.usage.get(k) or 0
            r["charged_usd"] = round(r["charged_usd"] + a.charged_usd, 6)
            if a.cost_basis == "reservation_assumed":
                r["assumed_usd"] = round(r["assumed_usd"] + a.charged_usd, 6)
            r["latency_s"] = round(r["latency_s"] + a.latency_s, 3)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "bound_basis": NATIVE_BOUND_BASIS,
            "by_role": self.by_role(),
            "attempts": [asdict(a) for a in self.attempts],
        }


def dispatch(
    provider: NativeProvider,
    req: NativeRequest,
    *,
    ledger: Ledger,
    price: ModelPrice | None,
    run_id: str,
    accounting: NativeAccounting,
    counters: SessionCounters | None = None,
    retries: int = 2,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_attempt: Callable[[int, dict[str, Any], int, float], None] | None = None,
) -> NativeResponse:
    """Send one logical call, with up to ``retries`` retries of retryable errors.

    ``on_attempt(attempt, payload, bound, reserved_usd)`` is called after the reservation and
    before dispatch, for saving the exact request. Raises ``BudgetExhausted`` (nothing sent),
    ``CallFailed``, ``AccountingBoundViolated`` or the interrupting exception.
    """
    if price is None and not provider.is_scripted:
        raise PriceUnavailable(f"no dated price for {provider.model}; live calls are blocked")
    role = req.purpose
    call_index = accounting.calls.get(role, 0)
    accounting.calls[role] = call_index + 1
    for attempt in range(retries + 1):
        payload = provider.payload(req)
        bound = native_input_token_bound(payload)
        reserve = price.max_cost(bound, req.max_tokens, req.cache_ttl) if price else 0.0
        try:
            res = ledger.reserve(reserve, run_id, f"native:{role}", bound)
        except BudgetExhausted:
            # Nothing is sent. A retry that cannot be reserved ends the call as failed.
            if attempt > 0:
                accounting.attempts[-1].retried = False
                if counters is not None:
                    counters.call_abandoned(role, "budget_exhausted_before_retry")
            raise
        if on_attempt is not None:
            on_attempt(attempt, payload, bound, reserve)
        t0 = clock()

        def record(
            outcome: str,
            entry: dict[str, Any],
            usage: Usage | None,
            source: str | None,
            retried: bool,
            resp: NativeResponse | None = None,
            *,
            _attempt: int = attempt,
            _bound: int = bound,
            _reserve: float = reserve,
            _t0: float = t0,
        ) -> None:
            accounting.attempts.append(
                AttemptRecord(
                    role,
                    call_index,
                    _attempt,
                    outcome,
                    retried,
                    usage.to_dict() if usage else None,
                    source,
                    round(clock() - _t0, 3),
                    round(_reserve, 6),
                    entry["charged_usd"],
                    entry["cost_basis"],
                    _bound,
                    resp.stop_reason if resp else None,
                    resp.request_id if resp else None,
                )
            )
            if counters is not None:
                counters.attempt(role, _attempt, outcome == "ok", final=not retried)

        try:
            resp = provider.complete(req)
        except ProviderError as exc:
            billed: NativeResponse | None = getattr(exc, "response", None)
            if billed is not None and price is not None:
                actual: float | None = price.cost(billed.usage)
            elif exc.billing == "none" or (billed is not None and price is None):
                actual = 0.0
            else:
                actual = None
            entry = ledger.settle(
                res,
                actual_usd=actual,
                status=f"error:{exc.kind}",
                usage=billed.usage if billed else None,
            )
            retry = exc.retryable and exc.kind != "refusal" and attempt < retries
            record(
                f"error:{exc.kind}",
                entry,
                billed.usage if billed else None,
                billed.usage_source if billed else None,
                retry,
                billed,
            )
            if retry:
                sleep(min(30.0, 2.0 * 4**attempt))
                continue
            raise CallFailed(exc, attempt + 1) from exc
        except BaseException as exc:
            entry = ledger.settle(res, actual_usd=None, status=f"interrupted:{type(exc).__name__}")
            record(f"interrupted:{type(exc).__name__}", entry, None, None, False)
            raise
        cost = price.cost(resp.usage) if price else 0.0
        entry = ledger.settle(res, actual_usd=cost, status="ok", usage=resp.usage)
        record("ok", entry, resp.usage, resp.usage_source, False, resp)
        u = resp.usage
        reported = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
        if resp.usage_source == "provider" and (
            reported > bound or u.output_tokens > req.max_tokens
        ):
            raise AccountingBoundViolated(
                f"reported input {reported} (bound {bound}) / output {u.output_tokens} "
                f"(max {req.max_tokens}); reservations unsafe"
            )
        return resp
    raise AssertionError("unreachable")
