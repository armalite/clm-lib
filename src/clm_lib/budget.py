"""Dated prices, conservative cost bounds and the persistent spend ledger."""

from __future__ import annotations

import json
import os
import tomllib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .provider import Usage

# Input-token bound used for cost reservations. This is an assumption-based bound,
# not a provider guarantee: it assumes the tokenizer emits at most one token per
# UTF-8 byte of request text, plus a fixed allowance for message framing and any
# hidden structured-output scaffolding. Every live call checks the provider-reported
# input tokens against it (see Runner) and stops if the bound is ever exceeded.
BOUND_BASIS = "utf8_bytes_plus_overhead (assumption: <=1 token per byte; checked per call)"
BOUND_OVERHEAD_TOKENS = 2048


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


def input_token_bound(payload: dict[str, Any]) -> int:
    """Upper bound (under BOUND_BASIS) on input tokens for a Messages payload."""
    parts: list[str] = []
    system = payload.get("system", "")
    parts.append(system if isinstance(system, str) else json.dumps(system))
    for msg in payload.get("messages", []):
        content = msg.get("content", "")
        parts.append(content if isinstance(content, str) else json.dumps(content))
    if "output_config" in payload:
        parts.append(json.dumps(payload["output_config"]))
    text_bytes = sum(len(p.encode("utf-8")) for p in parts)
    return text_bytes + BOUND_OVERHEAD_TOKENS


class LedgerBusy(RuntimeError):
    """Another live process holds pending reservations in the same ledger.

    This PID check is a guard against an obvious mistake, not a concurrency lock: two
    processes opening the ledger before either reserves are not detected, and writes
    are last-writer-wins. Live commands must be run one at a time.
    """


@dataclass
class Reservation:
    id: str
    amount: float
    run_id: str
    kind: str
    input_token_bound: int = 0


@dataclass
class Ledger:
    """Append-only spend ledger persisted as JSON across CLI invocations.

    ``ceiling_usd`` is fixed when the ledger is created. Callers may only lower
    the effective ceiling (``limit_usd``); nothing here raises it.
    """

    path: Path
    ceiling_usd: float
    entries: list[dict[str, Any]] = field(default_factory=list)
    _open: dict[str, Reservation] = field(default_factory=dict)
    limit_usd: float | None = None  # optional lower cap for this process
    start_spent_usd: float = 0.0

    @classmethod
    def open(cls, path: Path, ceiling_usd: float, limit_usd: float | None = None) -> Ledger:
        """Open (or create) the ledger.

        Pending reservations persisted by an earlier process were dispatched (or
        about to be) when that process stopped, so their spend is unknown. They are
        resolved conservatively by charging the full reservation.
        """
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            ceiling = min(float(data["ceiling_usd"]), ceiling_usd)
            led = cls(path, ceiling, data.get("entries", []))
            pending = data.get("pending", [])
            live = [p for p in pending if _pid_alive(int(p.get("pid", -1)))]
            if live:
                raise LedgerBusy(
                    f"{len(live)} pending reservation(s) held by running pid(s) "
                    f"{sorted({p['pid'] for p in live})}; run one live command at a time"
                )
            for p in pending:
                led.entries.append(
                    {
                        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                        "run_id": p["run_id"],
                        "kind": p["kind"],
                        "status": "unresolved_after_restart",
                        "reserved_usd": p["reserved_usd"],
                        "actual_usd": None,
                        "charged_usd": p["reserved_usd"],
                        "cost_basis": "reservation_assumed",
                        "usage": None,
                        "note": f"pending since {p['ts']} (pid {p.get('pid')}); outcome unknown",
                    }
                )
            if pending:
                led._save()
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

    def reserve(
        self, amount: float, run_id: str, kind: str, input_token_bound: int = 0
    ) -> Reservation:
        """Reserve ``amount`` and persist it as pending *before* the caller dispatches."""
        if amount > self.remaining_usd + 1e-12:
            raise BudgetExhausted(
                f"next call needs a ${amount:.4f} reservation; remaining ${self.remaining_usd:.4f} "
                f"of ceiling ${self.effective_ceiling:.2f} (spent ${self.spent_usd:.4f})"
            )
        res = Reservation(uuid.uuid4().hex, amount, run_id, kind, input_token_bound)
        self._open[res.id] = res
        self._save()
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
            "input_token_bound": res.input_token_bound,
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
            "reservation_bound": BOUND_BASIS,
            "entries": self.entries,
            "pending": [
                {
                    "id": r.id,
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "pid": os.getpid(),
                    "run_id": r.run_id,
                    "kind": r.kind,
                    "reserved_usd": round(r.amount, 6),
                }
                for r in self._open.values()
            ],
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


def _pid_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
