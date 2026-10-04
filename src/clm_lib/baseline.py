"""Summary baselines: replace older working entries with a model-written summary.

Two policies, selected by ``BaselineConfig.policy``:

- ``fixed-tail/1`` (experiments 001 and 002): keep the newest ``tail_entries`` entries verbatim,
  whatever their size, and summarise everything older.
- ``token-tail/1`` (experiment 003): keep the longest run of newest entries that fits a token
  allowance, summarising older entries and any recent entry too large for the allowance. The
  single newest entry is kept even above the allowance if it fits a larger newest-entry cap,
  so the agent always sees its latest result once; it is compacted at the next summary.
  Ask for a summary sized so the request lands near a target well below the pressure point.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .context import Entry
from .prompts import SUMMARY_SYSTEM, SUMMARY_SYSTEM_TOKEN_TAIL, summary_user
from .provider import ModelRequest

FIXED_TAIL = "fixed-tail/1"
TOKEN_TAIL = "token-tail/1"
POLICIES = (FIXED_TAIL, TOKEN_TAIL)


@dataclass(frozen=True)
class SummaryPolicy:
    tail_entries: int = 4
    max_chars: int = 3000
    retries: int = 1
    policy: str = FIXED_TAIL
    tail_tokens: int = 1200  # token-tail/1: verbatim allowance for recent entries
    newest_tokens: int = 2000  # token-tail/1: the newest entry is kept if it fits this cap
    target_tokens: int = 4000  # token-tail/1: desired request size after compaction
    min_chars: int = 600  # token-tail/1: smallest summary length requested

    def __post_init__(self) -> None:
        if self.policy not in POLICIES:
            raise ValueError(f"unknown summary policy {self.policy!r}; expected one of {POLICIES}")

    def split(
        self, entries: tuple[Entry, ...], tokens_of: Callable[[Entry], int] | None = None
    ) -> tuple[list[Entry], list[Entry]]:
        """Return (older entries to summarise, recent tail kept verbatim)."""
        if self.policy == FIXED_TAIL:
            if len(entries) <= self.tail_entries:
                return [], list(entries)
            cut = len(entries) - self.tail_entries
            return list(entries[:cut]), list(entries[cut:])
        assert tokens_of is not None, "token-tail/1 needs a token estimator"
        if entries and tokens_of(entries[-1]) > self.tail_tokens:
            # Oversized newest entry: keep it alone if it fits the newest-entry cap.
            if tokens_of(entries[-1]) <= self.newest_tokens:
                return list(entries[:-1]), [entries[-1]]
            return list(entries), []
        used, cut = 0, len(entries)
        for i in range(len(entries) - 1, -1, -1):
            cost = tokens_of(entries[i])
            if used + cost > self.tail_tokens:
                break
            used += cost
            cut = i
        return list(entries[:cut]), list(entries[cut:])

    def eligible(
        self, entries: tuple[Entry, ...], tokens_of: Callable[[Entry], int] | None = None
    ) -> bool:
        """Summarise only if the older section holds something not yet summarised."""
        older, _ = self.split(entries, tokens_of)
        return any(e.role != "summary" for e in older)

    def char_limit(self, attempt: int, room_tokens: int = 0, chars_per_token: float = 3.0) -> int:
        if self.policy == FIXED_TAIL:
            return max(400, self.max_chars // (2**attempt))
        wanted = int(room_tokens * chars_per_token)
        base = max(self.min_chars, min(self.max_chars, wanted))
        return max(self.min_chars // 2, base // (2**attempt))

    def accept_tokens(self, attempt: int, pressure_tokens: float, hard_tokens: int) -> float:
        """Largest acceptable request after compaction for this attempt."""
        if self.policy == FIXED_TAIL or attempt >= self.retries:
            return hard_tokens
        return pressure_tokens  # token-tail/1: first attempt must relieve pressure

    def request(
        self,
        task: str,
        older: list[Entry],
        attempt: int,
        max_tokens: int,
        limit_chars: int | None = None,
    ) -> ModelRequest:
        limit = limit_chars if limit_chars is not None else self.char_limit(attempt)
        return ModelRequest(
            system=SUMMARY_SYSTEM if self.policy == FIXED_TAIL else SUMMARY_SYSTEM_TOKEN_TAIL,
            user=summary_user(task, older, limit),
            max_tokens=max_tokens,
            purpose="summary",
        )

    @staticmethod
    def apply(summary_text: str, tail: list[Entry], index: int) -> list[Entry]:
        """The summary replaces the older section; it is not appended to it."""
        return [Entry(f"sum{index}", "summary", summary_text.strip()), *tail]
