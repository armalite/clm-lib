"""Optional host-mediated context review by TypeSafe Jev (experiment 008).

The main model may request advice with a ``review_context`` action. The runtime (never the
sandbox: it has no network or credentials) sends one fixed yes/no question per submitted
context entry to the TypeSafe System One API and returns the probabilities as an observation.
The advice never edits, removes or retains entries; the main model decides what to do.

API (https://docs.typesafe.ai/api, retrieved 2026-10-12): ``POST {base_url}`` with
``Authorization: Bearer <TYPESAFE_API_KEY>``; body ``{"state", "model", "questions"}``; each
``noul`` question has ``instructions`` and optional ``criteria`` (``true``/``false``); the response
has ``model`` (the versioned id that answered), ``answers[key].noul`` (probability of yes, 0..1)
and ``usage.input_tokens`` / ``usage.output_tokens``. Question keys are not sent to the model.
Errors: 401, 422, 429 (rate limit), 529 (overloaded). The official SDK retries by default; this
client uses plain HTTP and performs only the retries configured here, each one recorded.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

QUESTION_VERSION = "jev-review/1"
# The fixed question (one noul per entry). Phrased so that a high value means "yes".
QUESTION = (
    "Does this update require reconsidering a current assertion or planned action in this entry?"
)
CRITERIA = {
    "true": (
        "Yes if the update explicitly replaces or contradicts something the entry currently asserts "
        "or plans, or changes part of it (for example a cause, status, value, requirement or next "
        "step), even when other parts of the entry remain valid. Reconsidering does not mean the "
        "whole entry is wrong or should be deleted."
    ),
    "false": (
        "No if the entry is unrelated to the update, if the update only adds information without "
        "changing what the entry asserts or plans, or if the entry describes a historical fact or "
        "a failed approach that is still correctly described as historical after the update."
    ),
}


@dataclass(frozen=True)
class ReviewItem:
    entry_id: str
    body: str


def build_questions(items: list[ReviewItem]) -> dict[str, Any]:
    """One noul question per entry; keys are local indexes (not sent to the model)."""
    return {
        f"e{i}": {
            "type": "noul",
            "instructions": {"question": QUESTION, "entry": item.body},
            "criteria": CRITERIA,
        }
        for i, item in enumerate(items)
    }


def build_request(update: str, items: list[ReviewItem], model: str) -> dict[str, Any]:
    """The exact request body: the update is the state; entries go into their questions."""
    return {"state": {"update": update}, "model": model, "questions": build_questions(items)}


class JevError(Exception):
    """A Jev call failed. ``billing``: "none" (rejected before evaluation) or "unknown"."""

    def __init__(self, kind: str, message: str, *, retryable: bool, billing: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.retryable = retryable
        self.billing = billing


@dataclass
class JevResponse:
    model: str
    probabilities: dict[str, float | None]  # question key -> probability of yes (None: missing)
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any]


def parse_response(body: Any, keys: list[str]) -> JevResponse:
    """Validate a response; missing or out-of-range answers become None (coverage is reported)."""
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        raise JevError(
            "malformed", "response has no answers object", retryable=False, billing="unknown"
        )
    model = body.get("model")
    raw_usage = body.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    probs: dict[str, float | None] = {}
    for k in keys:
        a = body["answers"].get(k)
        v = a.get("noul") if isinstance(a, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0:
            probs[k] = float(v)
        else:
            probs[k] = None
    return JevResponse(
        model=str(model) if isinstance(model, str) else "unknown",
        probabilities=probs,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        raw=body,
    )


Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def urllib_transport(
    url: str, headers: dict[str, str], data: bytes, timeout: float
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@dataclass
class TypeSafeAdvisor:
    """Plain-HTTP TypeSafe client. The key is read by the caller from the host environment and
    held only here; it is never written to traces, requests saved on disk or the sandbox."""

    api_key: str = field(repr=False)
    model: str = "jev-1.13.0"
    base_url: str = "https://api.typesafe.ai/v1/systemone"
    timeout_s: float = 15.0
    transport: Transport = field(default=urllib_transport, repr=False)
    name: str = "typesafe"

    def call(self, body: dict[str, Any]) -> tuple[JevResponse, float]:
        """One attempt (no internal retries). Returns (response, latency_s)."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        t0 = time.monotonic()
        try:
            status, raw = self.transport(
                self.base_url, headers, json.dumps(body).encode(), self.timeout_s
            )
        except TimeoutError as exc:
            raise JevError(
                "timeout", "request timed out", retryable=True, billing="unknown"
            ) from exc
        except OSError as exc:
            raise JevError(
                "connection", type(exc).__name__, retryable=True, billing="unknown"
            ) from exc
        latency = time.monotonic() - t0
        if status == 401:
            raise JevError("auth", "unauthorized", retryable=False, billing="none")
        if status == 422:
            raise JevError(
                "bad_request", raw[:300].decode("utf-8", "replace"), retryable=False, billing="none"
            )
        if status in (429, 529) or status >= 500:
            raise JevError(
                f"http_{status}",
                "overloaded or rate limited",
                retryable=True,
                billing="none" if status in (429, 529) else "unknown",
            )
        if status != 200:
            raise JevError(
                f"http_{status}",
                raw[:300].decode("utf-8", "replace"),
                retryable=False,
                billing="unknown",
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JevError(
                "malformed", "response is not JSON", retryable=False, billing="unknown"
            ) from exc
        return parse_response(parsed, list(body["questions"])), latency


@dataclass
class FakeAdvisor:
    """Test double: ``answer(update, entry_body) -> probability or None``; may raise JevError."""

    answer: Callable[[str, str], float | None]
    model: str = "fake-jev"
    name: str = "fake"
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_with: list[JevError] = field(default_factory=list)

    def call(self, body: dict[str, Any]) -> tuple[JevResponse, float]:
        self.calls.append(body)
        if self.fail_with:
            raise self.fail_with.pop(0)
        update = body["state"]["update"]
        answers = {}
        for k, q in body["questions"].items():
            p = self.answer(update, q["instructions"]["entry"])
            answers[k] = {"type": "noul", "noul": p} if p is not None else {"type": "noul"}
        resp = {
            "model": self.model,
            "answers": answers,
            "usage": {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": len(answers)},
        }
        return parse_response(resp, list(body["questions"])), 0.01


@dataclass(frozen=True)
class JevPrice:
    model: str
    input: float  # USD per million input tokens
    output: float
    source: str
    retrieved: str

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input + output_tokens * self.output) / 1_000_000

    def max_cost(self, body: dict[str, Any]) -> float:
        # Conservative: at most one token per UTF-8 byte of the request, plus a fixed allowance.
        return (len(json.dumps(body).encode()) + 2048) * max(self.input, self.output) / 1_000_000


def load_jev_price(path: Any, model: str) -> JevPrice | None:
    import tomllib
    from pathlib import Path

    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    p = (data.get("typesafe") or {}).get(model)
    if not p:
        return None
    return JevPrice(
        model, float(p["input"]), float(p["output"]), p.get("source", ""), p.get("retrieved", "")
    )
