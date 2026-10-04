"""Dated prices, conservative cost bounds and the persistent spend ledger."""

from __future__ import annotations

import json
import math
import os
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .provider import Usage

# Conservative lower bound on characters per token used for cost reservations.
RESERVATION_CHARS_PER_TOKEN = 2.0


class BudgetExhausted(RuntimeError):
    """The next call's reserved cost does not fit in the remaining budget."""


class PriceUnavailable(RuntimeError):
    """No dated price is configured for the model; cost cannot be bounded."""


@dataclass(frozen=True)
class ModelPrice:
    model: str
    input: float  # USD per million tokens
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float
    source: str
    retrieved: str

    def cost(self, usage: Usage) -> float:
        m = 1_000_000
        return (
            usage.input_tokens * self.input / m
            + usage.cache_creation_input_tokens * self.cache_write_5m / m
            + usage.cache_read_input_tokens * self.cache_read / m
            + usage.output_tokens * self.output / m
        )

    def max_cost(self, input_tokens_upper: int, max_output_tokens: int) -> float:
        # Assume every input token is billed at the highest input-side rate.
        worst_in = max(self.input, self.cache_write_5m)
        return (input_tokens_upper * worst_in + max_output_tokens * self.output) / 1_000_000


def load_prices(path: Path) -> dict[str, ModelPrice]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    source = data.get("source", "")
    retrieved = data.get("retrieved", "")
    out: dict[str, ModelPrice] = {}
    for model, p in data.get("models", {}).items():
        out[model] = ModelPrice(
            model=model,
            input=float(p["input"]),
            cache_write_5m=float(p["cache_write_5m"]),
            cache_write_1h=float(p["cache_write_1h"]),
            cache_read=float(p["cache_read"]),
            output=float(p["output"]),
            source=p.get("source", source),
            retrieved=p.get("retrieved", retrieved),
        )
    return out


def reservation_input_tokens(request_chars: int) -> int:
    return math.ceil(request_chars / RESERVATION_CHARS_PER_TOKEN) + 64


@dataclass
class Reservation:
    id: int
    amount: float
    run_id: str
    kind: str


@dataclass
class Ledger:
    """Append-only spend ledger persisted as JSON across CLI invocations.

    ``ceiling_usd`` is fixed when the ledger is created. Callers may only lower
    the effective ceiling (``limit_usd``); nothing here raises it.
    """

    path: Path
    ceiling_usd: float
    entries: list[dict[str, Any]] = field(default_factory=list)
    _open: dict[int, Reservation] = field(default_factory=dict)
    limit_usd: float | None = None  # optional lower cap for this process
    start_spent_usd: float = 0.0
    _next_id: int = 0

    @classmethod
    def open(cls, path: Path, ceiling_usd: float, limit_usd: float | None = None) -> Ledger:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            ceiling = min(float(data["ceiling_usd"]), ceiling_usd)
            led = cls(path, ceiling, data.get("entries", []))
        else:
            led = cls(path, ceiling_usd)
            led._save()
        led.limit_usd = limit_usd
        led.start_spent_usd = led.spent_usd
        return led

    @property
    def spent_usd(self) -> float:
        return sum(float(e.get("charged_usd", 0.0)) for e in self.entries)

    @property
    def reserved_open_usd(self) -> float:
        return sum(r.amount for r in self._open.values())

    @property
    def effective_ceiling(self) -> float:
        if self.limit_usd is None:
            return self.ceiling_usd
        return min(self.ceiling_usd, self.start_spent_usd + self.limit_usd)

    @property
    def remaining_usd(self) -> float:
        return self.effective_ceiling - self.spent_usd - self.reserved_open_usd

    def reserve(self, amount: float, run_id: str, kind: str) -> Reservation:
        if amount > self.remaining_usd + 1e-12:
            raise BudgetExhausted(
                f"next call needs a ${amount:.4f} reservation; remaining ${self.remaining_usd:.4f} "
                f"of ceiling ${self.effective_ceiling:.2f} (spent ${self.spent_usd:.4f})"
            )
        self._next_id += 1
        res = Reservation(self._next_id, amount, run_id, kind)
        self._open[res.id] = res
        return res

    def settle(
        self,
        res: Reservation,
        *,
        actual_usd: float | None,
        status: str,
        usage: Usage | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Close a reservation.

        ``actual_usd`` is the cost computed from provider-reported usage. If it is
        None (outcome unknown, e.g. a timeout), the full reservation is charged.
        """
        self._open.pop(res.id, None)
        charged = res.amount if actual_usd is None else actual_usd
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_id": res.run_id,
            "kind": res.kind,
            "status": status,
            "reserved_usd": round(res.amount, 6),
            "actual_usd": None if actual_usd is None else round(actual_usd, 6),
            "charged_usd": round(charged, 6),
            "cost_basis": "provider_usage" if actual_usd is not None else "reservation_assumed",
            "usage": usage.to_dict() if usage else None,
            "note": note,
        }
        self.entries.append(entry)
        self._save()
        return entry

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        doc = {
            "ceiling_usd": self.ceiling_usd,
            "note": "Charged = provider-usage cost, or the full reservation when the outcome is unknown.",
            "entries": self.entries,
        }
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, float] = {}
        for e in self.entries:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0.0) + float(e["charged_usd"])
        return {
            "ceiling_usd": self.ceiling_usd,
            "spent_usd": round(self.spent_usd, 6),
            "remaining_usd": round(self.effective_ceiling - self.spent_usd, 6),
            "calls": len(self.entries),
            "by_kind": {k: round(v, 6) for k, v in by_kind.items()},
            "unknown_outcome_calls": sum(
                1 for e in self.entries if e["cost_basis"] == "reservation_assumed"
            ),
        }


def price_dict(p: ModelPrice) -> dict[str, Any]:
    return asdict(p)
