"""Financial accounting at the Pydantic AI model boundary (decision G3).

`AccountedModel` wraps the real model. Every `request()` call is one provider attempt:

1. refused before dispatch (`SpendRefused`) when live use isn't authorised, when a live model has no dated price,
   when a live request has no `max_tokens` (its cost can't be bounded), or when the ledger reservation doesn't fit;
2. reserved in the ledger, persisted as pending, before dispatch;
3. settled exactly once: provider-reported usage at the dated price; 0 only for provably unbilled failures (HTTP 4xx
   and 429); the full reservation when the outcome is unknown (5xx, connection loss, any other error, interruption).

`WrapperModel` forwards `system`, `profile` and request preparation, so the capability sees the same model identity
(its real-usage versus fixed-ratio estimate choice is unchanged), and the request passed on is exactly the one
Pydantic AI built. Pydantic AI does not retry model requests. Retries happen only here, by `RetryPolicy`
(`p015-retry/1`; none by default), each as its own reserved attempt, and live provider clients have the SDK's own
retries disabled (`live.py`), so that every attempt passes through this wrapper. A spend gate, when given, is
rechecked before every attempt (`spend.py`).

Usage: Pydantic AI's `RequestUsage.input_tokens` already includes `cache_read_tokens` and `cache_write_tokens`
(its documented convention; Anthropic's raw figures are normalised on extraction), so the uncached input is
`input_tokens - cache_read_tokens - cache_write_tokens`. Cached tokens are never added a second time.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage
from pydantic_core import to_jsonable_python

from ..budget import BOUND_OVERHEAD_TOKENS, BudgetExhausted, Ledger, ModelPrice
from ..provider import Usage

Mode = Literal["scripted", "live"]
ACCOUNTING_VERSION = "p015-accounting/1"


class SpendRefused(RuntimeError):
    """A provider attempt was refused before dispatch for a financial or authorisation reason. It is never a
    context-management outcome (decision G7)."""

    def __init__(self, reason: str, kind: str) -> None:
        super().__init__(reason)
        self.kind = kind  # "not_authorised" | "no_price" | "unbounded_output" | "budget_exhausted" | "not_scripted"


class LaunchStopped(RuntimeError):
    """A stop was requested (launcher `stop --now`, or a signal) and is honoured before the next provider attempt.
    Nothing is dispatched; the session ends as `stopped`."""


class SpendGate(Protocol):
    """Rechecked before every provider attempt (see `clm_lib.p015.spend`). Raises `SpendRefused` or
    `LaunchStopped`; returns normally to let the attempt reserve and dispatch."""

    def check(self, amount_usd: float, category: str) -> None: ...


RETRY_VERSION = "p015-retry/1"


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retries of one logical call (`p015-retry/1`), the same for every arm. Each retry is a separate,
    reserved and settled attempt, rechecked by the spend gate. Only HTTP 429, 500, 502, 503, 504 and 529 and
    connection failures are retried; anything else ends the call. The SDK's own retries must be off (`live.py`)."""

    max_retries: int = (
        0  # 0: no retries (the default offline, so scripted failures end the session)
    )
    statuses: tuple[int, ...] = (429, 500, 502, 503, 504, 529)
    base_delay_s: float = 2.0
    factor: float = 4.0
    max_delay_s: float = 60.0

    def retryable(self, exc: BaseException) -> bool:
        if isinstance(exc, ModelHTTPError):
            return exc.status_code in self.statuses
        return isinstance(exc, ModelAPIError)  # a connection failure or timeout, no status

    def delay(self, exc: BaseException, retry: int) -> float:
        after = None
        if isinstance(exc, ModelHTTPError):
            headers = getattr(exc, "headers", None) or {}
            raw = {k.lower(): v for k, v in dict(headers).items()}.get("retry-after")
            try:
                after = float(raw) if raw is not None else None
            except ValueError:
                after = None
        planned = self.base_delay_s * self.factor**retry
        return min(self.max_delay_s, max(planned, after or 0.0))


class AccountingBoundViolated(RuntimeError):
    """Reported usage exceeded the reservation bound; reservations are unsafe (a harness defect, not a result)."""


class AttemptCounter:
    """Attempt numbers, shared by the wrappers of one session (the agent's and a summariser's), so that every
    provider attempt of the session has a distinct number."""

    def __init__(self) -> None:
        self.value = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            index = self.value
            self.value += 1
            return index


class AttemptSink(Protocol):
    def attempt_started(self, entry: dict[str, Any]) -> None: ...

    def attempt_finished(self, entry: dict[str, Any]) -> None: ...


def split_usage(usage: RequestUsage) -> Usage:
    """Pydantic AI usage to clm-lib's ledger usage (uncached input separate from cache reads and writes)."""
    uncached = usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens
    details = usage.details or {}
    writes_1h = details.get(
        "cache_creation.ephemeral_1h_input_tokens", details.get("ephemeral_1h_input_tokens")
    )
    return Usage(
        input_tokens=max(uncached, 0),
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_tokens,
        cache_creation_input_tokens=usage.cache_write_tokens,
        cache_creation_1h=int(writes_1h) if isinstance(writes_1h, int) else None,
    )


def request_input_bound(messages: list[ModelMessage], params: ModelRequestParameters) -> int:
    """An upper bound on input tokens (same assumption as `budget.input_token_bound`: at most one token per UTF-8
    byte, plus a fixed overhead): the messages as sent, the instructions and the tool definitions."""
    size = len(ModelMessagesTypeAdapter.dump_json(messages))
    for message in messages:
        instructions = getattr(message, "instructions", None)
        if instructions:
            size += len(instructions.encode("utf-8"))
    tools = [
        {"name": t.name, "description": t.description, "parameters": t.parameters_json_schema}
        for t in [*params.function_tools, *params.output_tools]
    ]
    size += len(json.dumps(tools).encode("utf-8"))
    return size + BOUND_OVERHEAD_TOKENS


def classify_failure(exc: BaseException) -> tuple[str, str]:
    """(error kind, billing): billing is "none" (provably unbilled) or "unknown" (charged at the reservation)."""
    if isinstance(exc, ModelHTTPError):
        unbilled = 400 <= exc.status_code < 500
        return f"http_{exc.status_code}", "none" if unbilled else "unknown"
    return type(exc).__name__, "unknown"


@dataclass(init=False)
class AccountedModel(WrapperModel):
    """The real model, with a ledger reservation and settlement around every provider attempt."""

    def __init__(
        self,
        wrapped: Model,
        *,
        ledger: Ledger,
        price: ModelPrice | None,
        run_id: str,
        sink: AttemptSink | None = None,
        mode: Mode = "scripted",
        live_authorised: bool = False,
        category: str = "main",
        counter: AttemptCounter | None = None,
        gate: SpendGate | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(wrapped)
        self.ledger = ledger
        self.price = price
        self.run_id = run_id
        self.sink = sink
        self.mode = mode
        self.live_authorised = live_authorised
        self.category = category  # "main" for the agent's calls; "summary" for a summariser's
        self.counter = counter or AttemptCounter()
        self.attempts = 0  # this wrapper's attempts
        self.logical_calls = 0  # this wrapper's logical calls (each may have several attempts)
        self.gate = gate
        self.retry = retry or RetryPolicy()
        self.sleep = sleep or asyncio.sleep

    def _refuse_if_not_allowed(self, max_tokens: int | None) -> None:
        scripted_model = isinstance(self.wrapped, FunctionModel | TestModel)
        if self.mode == "scripted":
            if not scripted_model:
                raise SpendRefused(
                    "a non-scripted model needs mode='live' and an authorisation", "not_scripted"
                )
            return
        if not self.live_authorised:
            raise SpendRefused("live provider requests are not authorised", "not_authorised")
        if self.price is None:
            raise SpendRefused(
                f"no dated price for {self.wrapped.model_name}; cost cannot be bounded", "no_price"
            )
        if max_tokens is None:
            raise SpendRefused(
                "a live request needs max_tokens so its cost can be bounded", "unbounded_output"
            )

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        """One logical call: an attempt, and up to `retry.max_retries` further attempts after a retryable failure."""
        logical = self.logical_calls
        self.logical_calls += 1
        retry = 0
        while True:
            try:
                return await self._attempt(
                    messages, model_settings, model_request_parameters, logical, retry
                )
            except (SpendRefused, LaunchStopped, AccountingBoundViolated):
                raise
            except Exception as exc:
                if retry >= self.retry.max_retries or not self.retry.retryable(exc):
                    raise
                await self.sleep(self.retry.delay(exc, retry))
                retry += 1

    async def _attempt(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        logical: int,
        retry: int,
    ) -> ModelResponse:
        max_tokens = (model_settings or {}).get("max_tokens")
        index = self.counter.next()
        self.attempts += 1
        entry: dict[str, Any] = {
            "attempt": index,
            "category": self.category,
            "mode": self.mode,
            "logical_call": logical,
            "retry": retry,
        }
        try:
            self._refuse_if_not_allowed(max_tokens)
        except SpendRefused as exc:
            entry.update(outcome="refused", refusal=exc.kind, reason=str(exc))
            if self.sink is not None:
                self.sink.attempt_finished(entry)
            raise
        bound = request_input_bound(messages, model_request_parameters)
        reserve = self.price.max_cost(bound, max_tokens or 0) if self.price is not None else 0.0
        if self.gate is not None:
            try:
                self.gate.check(reserve, self.category)
            except SpendRefused as exc:
                entry.update(
                    outcome="refused",
                    refusal=exc.kind,
                    reason=str(exc),
                    reserve_usd=round(reserve, 6),
                )
                if self.sink is not None:
                    self.sink.attempt_finished(entry)
                raise
            except LaunchStopped as exc:
                entry.update(outcome="refused", refusal="stopped", reason=str(exc))
                if self.sink is not None:
                    self.sink.attempt_finished(entry)
                raise
        try:
            reservation = self.ledger.reserve(reserve, self.run_id, f"p015:{self.category}", bound)
        except BudgetExhausted as exc:
            entry.update(
                outcome="refused",
                refusal="budget_exhausted",
                reason=str(exc),
                reserve_usd=round(reserve, 6),
            )
            if self.sink is not None:
                self.sink.attempt_finished(entry)
            raise SpendRefused(str(exc), "budget_exhausted") from exc
        entry.update(input_token_bound=bound, max_tokens=max_tokens, reserved_usd=round(reserve, 6))
        if self.sink is not None:
            self.sink.attempt_started(
                {
                    **entry,
                    "messages": to_jsonable_python(messages),
                    "settings": to_jsonable_python(model_settings),
                    "parameters": to_jsonable_python(model_request_parameters),
                }
            )
        start = time.monotonic()
        try:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
        except Exception as exc:
            latency = round(time.monotonic() - start, 3)
            kind, billing = classify_failure(exc)
            settled = self.ledger.settle(
                reservation, actual_usd=0.0 if billing == "none" else None, status=f"error:{kind}"
            )
            entry.update(
                outcome="error", error=kind, billing=billing, latency_s=latency, **_settled(settled)
            )
            if self.sink is not None:
                self.sink.attempt_finished(entry)
            raise
        except BaseException as exc:  # cancellation or interruption after dispatch: outcome unknown
            settled = self.ledger.settle(
                reservation, actual_usd=None, status=f"interrupted:{type(exc).__name__}"
            )
            entry.update(outcome="interrupted", error=type(exc).__name__, **_settled(settled))
            if self.sink is not None:
                self.sink.attempt_finished(entry)
            raise
        latency = round(time.monotonic() - start, 3)
        usage = split_usage(response.usage)
        cost = self.price.cost(usage) if self.price is not None else 0.0
        settled = self.ledger.settle(reservation, actual_usd=cost, status="ok", usage=usage)
        entry.update(
            outcome="ok",
            latency_s=latency,
            reported={
                "input_tokens_total": response.usage.input_tokens,
                "input_tokens_uncached": usage.input_tokens,
                "cache_read_tokens": usage.cache_read_input_tokens,
                "cache_write_tokens": usage.cache_creation_input_tokens,
                "output_tokens": usage.output_tokens,
            },
            # Which model class produced the usage; a FunctionModel's usage may itself be synthetic provider-like
            # figures (whether the capability calibrates on it depends on the model's `system`, not on this label).
            usage_source="scripted-model"
            if isinstance(self.wrapped, FunctionModel | TestModel)
            else "provider-model",
            response=to_jsonable_python(response),
            **_settled(settled),
        )
        if self.sink is not None:
            self.sink.attempt_finished(entry)
        if self.mode == "live" and (
            response.usage.input_tokens > bound
            or (max_tokens is not None and usage.output_tokens > max_tokens)
        ):
            raise AccountingBoundViolated(
                f"reported input {response.usage.input_tokens} (bound {bound}) / output {usage.output_tokens}"
            )
        return response

    @asynccontextmanager
    async def request_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamedResponse]:
        raise NotImplementedError("streaming is not accounted on the p015 path")
        yield  # pragma: no cover

    async def count_tokens(self, *args: Any, **kwargs: Any) -> RequestUsage:
        raise RuntimeError(
            "token counting is a provider call and is not permitted on the p015 path"
        )


def _settled(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "charged_usd": entry["charged_usd"],
        "cost_basis": entry["cost_basis"],
        "ledger_status": entry["status"],
    }
