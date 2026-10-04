"""Summary baseline: replace older working entries with a model-written summary."""

from __future__ import annotations

from dataclasses import dataclass

from .context import Entry
from .prompts import SUMMARY_SYSTEM, summary_user
from .provider import ModelRequest


@dataclass(frozen=True)
class SummaryPolicy:
    tail_entries: int = 4
    max_chars: int = 3000
    retries: int = 1

    def split(self, entries: tuple[Entry, ...]) -> tuple[list[Entry], list[Entry]]:
        if len(entries) <= self.tail_entries:
            return [], list(entries)
        cut = len(entries) - self.tail_entries
        return list(entries[:cut]), list(entries[cut:])

    def eligible(self, entries: tuple[Entry, ...]) -> bool:
        """Summarise only if the older section holds something not yet summarised."""
        older, _ = self.split(entries)
        return any(e.role != "summary" for e in older)

    def request(self, task: str, older: list[Entry], attempt: int, max_tokens: int) -> ModelRequest:
        limit = max(400, self.max_chars // (2**attempt))
        return ModelRequest(
            system=SUMMARY_SYSTEM,
            user=summary_user(task, older, limit),
            max_tokens=max_tokens,
            purpose="summary",
        )

    @staticmethod
    def apply(summary_text: str, tail: list[Entry], index: int) -> list[Entry]:
        """The summary replaces the older section; it is not appended to it."""
        return [Entry(f"sum{index}", "summary", summary_text.strip()), *tail]
