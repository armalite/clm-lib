"""Versioned session counters ``session-counters/1`` (programme 015). Opt-in.

The existing runner's ``_Counters`` are unchanged. There, a "call" is one provider *attempt*:
``provider_calls`` and ``limits.max_calls`` count actions, repairs, summaries and API retries
alike (summaries are excluded only when ``limits.summary_calls_in_max_calls`` is false). These
counters keep logical calls, attempts and retries apart, so that whichever definition a later
decision adopts can be read off without re-deriving it.

Every count is derived from an append-only event list. Increments (session totals and the
current user turn's totals both change):

=====================================  =====================================================
Event (method)                          Increments
=====================================  =====================================================
``user_turn()``                         ``user_turns`` +1; opens a new per-turn bucket
``attempt(role, attempt, ok, final)``   ``{role}_attempts`` +1 for every dispatched attempt,
                                        whatever its outcome; ``{role}_calls`` +1 when
                                        ``attempt == 0`` (one logical call); ``{role}_retries``
                                        +1 when ``attempt > 0``; ``{role}_failed_calls`` +1 when
                                        the attempt failed and no retry follows (``final``)
``call_abandoned(role, why)``           ``{role}_failed_calls`` +1 for a call whose earlier attempt
                                        was recorded as followed by a retry, when the retry is
                                        never dispatched (for example its reservation does not
                                        fit the budget); no attempt is counted
``response(kind, requested)``           ``responses_{kind}`` +1; ``tool_calls_requested`` +n
                                        for the calls the model emitted (also in malformed
                                        responses), by tool category
``tool_executed(category, ok)``         ``tool_calls_executed`` +1 when the call was run,
                                        including runs that failed; ``tool_calls_failed`` +1
                                        additionally when the run returned an error
``tool_not_executed(category, why)``    ``tool_calls_not_executed`` +1 (invalid call, malformed
                                        response, or declined by the arm's policy), by reason
``step(kind)``                          ``task_steps`` or ``editing_steps`` +1. Which steps are
                                        editing steps is an arm policy, supplied by the caller;
                                        no free-editing rule is built in
``edit(outcome)``                       ``edits_accepted`` / ``edits_rejected`` /
                                        ``edits_unchanged`` +1 (``edit_attempts`` is their sum)
=====================================  =====================================================

``role`` is ``"main"`` (the task model) or ``"summary"`` (the summary baseline's summariser).
Events other than ``user_turn`` before the first user turn raise, so nothing is uncounted.

The proposed limits are recorded in ``PROPOSED_LIMITS`` and are **not enforced** here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

COUNTERS_VERSION = "session-counters/1"
Role = Literal["main", "summary"]
StepKind = Literal["task", "editing"]
EditOutcome = Literal["accepted", "rejected", "unchanged"]
ResponseKind = Literal["tool_calls", "completion", "malformed"]
ROLES = ("main", "summary")

FIELDS = (
    "user_turns",
    "main_calls",
    "main_attempts",
    "main_retries",
    "main_failed_calls",
    "summary_calls",
    "summary_attempts",
    "summary_retries",
    "summary_failed_calls",
    "responses_tool_calls",
    "responses_completion",
    "responses_malformed",
    "tool_calls_requested",
    "tool_calls_executed",
    "tool_calls_failed",
    "tool_calls_not_executed",
    "task_steps",
    "editing_steps",
    "edits_accepted",
    "edits_rejected",
    "edits_unchanged",
)

# Candidate meanings of "call" for a session call cap. Choosing one is a pending decision.
CALL_DEFINITIONS: dict[str, tuple[str, ...]] = {
    # logical main-model requests (retries of the same request not counted again)
    "main_calls": ("main_calls",),
    # every main-model attempt, retries included (the existing max_calls meaning, without
    # summaries, as with summary_calls_in_max_calls = false)
    "main_attempts": ("main_attempts",),
    # logical main and summary requests
    "main_and_summary_calls": ("main_calls", "summary_calls"),
    # every provider attempt of either role (the existing max_calls meaning with the default
    # summary_calls_in_max_calls = true; also the natural financial bound)
    "all_attempts": ("main_attempts", "summary_attempts"),
}


@dataclass(frozen=True)
class ProposedLimit:
    name: str
    value: int
    scope: str
    counts: str
    status: str = "proposed; not enforced"


PROPOSED_LIMITS: tuple[ProposedLimit, ...] = (
    ProposedLimit(
        "task_steps_per_user_turn",
        60,
        "each user turn",
        "task steps; which steps are free editing steps is a pending, arm-specific policy",
    ),
    ProposedLimit(
        "session_call_cap",
        144,
        "whole session (all user turns)",
        "PENDING: which of CALL_DEFINITIONS counts. 144 = 2 * 60 + 24, the reference harness "
        "README's default lm_call_cap formula at 60 steps; that README applies it per task and "
        "says free editing turns count toward it",
    ),
)


@dataclass(frozen=True)
class Event:
    seq: int
    turn: int
    kind: str
    data: dict[str, Any]


@dataclass
class SessionCounters:
    events: list[Event] = field(default_factory=list)
    totals: Counter[str] = field(default_factory=Counter)
    per_turn: list[Counter[str]] = field(default_factory=list)
    requested_by_category: Counter[str] = field(default_factory=Counter)
    executed_by_category: Counter[str] = field(default_factory=Counter)
    not_executed_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def turn(self) -> int:
        return len(self.per_turn)

    def _log(self, kind: str, inc: dict[str, int], **data: Any) -> None:
        if kind != "user_turn" and not self.per_turn:
            raise ValueError(f"{kind} event before the first user turn")
        self.events.append(Event(len(self.events), self.turn, kind, data))
        for k, v in inc.items():
            if k not in FIELDS:
                raise KeyError(k)
            if v:
                self.totals[k] += v
                self.per_turn[-1][k] += v

    def user_turn(self) -> None:
        self.per_turn.append(Counter())
        self._log("user_turn", {"user_turns": 1})

    def attempt(self, role: Role, attempt: int, ok: bool, final: bool) -> None:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        if attempt < 0:
            raise ValueError("attempt index must be >= 0")
        self._log(
            "attempt",
            {
                f"{role}_attempts": 1,
                f"{role}_calls": int(attempt == 0),
                f"{role}_retries": int(attempt > 0),
                f"{role}_failed_calls": int(not ok and final),
            },
            role=role,
            attempt=attempt,
            ok=ok,
            final=final,
        )

    def call_abandoned(self, role: Role, reason: str) -> None:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        self._log("call_abandoned", {f"{role}_failed_calls": 1}, role=role, reason=reason)

    def response(self, kind: ResponseKind, requested: dict[str, int]) -> None:
        n = sum(requested.values())
        self.requested_by_category.update(requested)
        self._log(
            "response",
            {f"responses_{kind}": 1, "tool_calls_requested": n},
            response_kind=kind,
            requested=dict(requested),
        )

    def tool_executed(self, category: str, ok: bool, name: str = "") -> None:
        self.executed_by_category[category] += 1
        self._log(
            "tool_executed",
            {"tool_calls_executed": 1, "tool_calls_failed": int(not ok)},
            category=category,
            ok=ok,
            name=name,
        )

    def tool_not_executed(self, category: str, reason: str, name: str = "") -> None:
        self.not_executed_reasons[reason] += 1
        self._log(
            "tool_not_executed",
            {"tool_calls_not_executed": 1},
            category=category,
            reason=reason,
            name=name,
        )

    def step(self, kind: StepKind) -> None:
        if kind not in ("task", "editing"):
            raise ValueError(f"unknown step kind {kind!r}")
        self._log("step", {f"{kind}_steps": 1}, step_kind=kind)

    def edit(self, outcome: EditOutcome) -> None:
        if outcome not in ("accepted", "rejected", "unchanged"):
            raise ValueError(f"unknown edit outcome {outcome!r}")
        self._log("edit", {f"edits_{outcome}": 1}, outcome=outcome)

    # ------------------------------------------------------------ reading
    def get(self, name: str, turn: int | None = None) -> int:
        if name == "edit_attempts":
            return sum(self.get(f"edits_{o}", turn) for o in ("accepted", "rejected", "unchanged"))
        if name not in FIELDS:
            raise KeyError(name)
        src = self.totals if turn is None else self.per_turn[turn - 1]
        return src[name]

    def call_totals(self) -> dict[str, int]:
        """Session totals under every candidate call definition."""
        return {d: sum(self.get(f) for f in fs) for d, fs in CALL_DEFINITIONS.items()}

    def consistency_problems(self) -> list[str]:
        """Invariants that must hold once every emitted call has been answered."""
        out = []
        req = self.get("tool_calls_requested")
        done = self.get("tool_calls_executed") + self.get("tool_calls_not_executed")
        if req != done:
            out.append(f"{req} tool calls requested but {done} executed or declined")
        for role in ROLES:
            calls, attempts = self.get(f"{role}_calls"), self.get(f"{role}_attempts")
            if attempts != calls + self.get(f"{role}_retries"):
                out.append(f"{role}: attempts != calls + retries")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": COUNTERS_VERSION,
            "totals": {f: self.get(f) for f in FIELDS}
            | {"edit_attempts": self.get("edit_attempts")},
            "per_turn": [{f: c[f] for f in FIELDS if c[f]} for c in self.per_turn],
            "call_totals": self.call_totals(),
            "tool_calls_requested_by_category": dict(self.requested_by_category),
            "tool_calls_executed_by_category": dict(self.executed_by_category),
            "tool_calls_not_executed_by_reason": dict(self.not_executed_reasons),
            "proposed_limits": [p.__dict__ for p in PROPOSED_LIMITS],
        }
