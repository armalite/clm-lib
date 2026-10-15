"""The p015 session recorder (decision G6): observes, never adds to what the model sees.

A session directory holds:

- `session.json`: format, evidence label, settings, provenance of guarded-clm and of this harness, endings;
- `attempts.jsonl`: one line per provider attempt, started (the exact messages, settings and request parameters
  sent) and finished (outcome, usage, latency, ledger settlement), or refused before dispatch;
- `trace.jsonl`: the capability's own trace records (`on_record`), in order;
- `tools.jsonl`: each bridged task-tool result (arguments, delivered text, output-cap record) or raised exception;
- `turns.jsonl`: each user turn (prompt, output, ending kind, error, and the full message history Pydantic AI
  returned, which is the model-visible history and stays separate from these logs).

Nothing here is written to the model; the recorder is called from the accounting wrapper, the tool bridge, the
capability's `on_record` and the session runner.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python

from . import EVIDENCE_LABEL, SESSION_FORMAT

FILES = ("session.json", "attempts.jsonl", "trace.jsonl", "tools.jsonl", "turns.jsonl")


class SessionRecorder:
    def __init__(self, run_dir: Path, *, run_id: str, evidence: str = EVIDENCE_LABEL) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.evidence = evidence
        self._lock = threading.Lock()
        self._raised: list[BaseException] = []
        run_dir.mkdir(parents=True, exist_ok=False)
        for name in FILES[1:]:
            (run_dir / name).write_text("", encoding="utf-8")
        self.meta: dict[str, Any] = {
            "format": SESSION_FORMAT,
            "run_id": run_id,
            "evidence": evidence,
            "turns": [],
        }

    def _append(self, name: str, entry: dict[str, Any]) -> None:
        line = json.dumps(to_jsonable_python(entry), ensure_ascii=False, sort_keys=True)
        with self._lock, (self.run_dir / name).open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # accounting wrapper
    def attempt_started(self, entry: dict[str, Any]) -> None:
        self._append("attempts.jsonl", {"event": "started", **entry})

    def attempt_finished(self, entry: dict[str, Any]) -> None:
        self._append("attempts.jsonl", {"event": "finished", **entry})

    # capability trace
    def trace(self, record: dict[str, Any]) -> None:
        self._append("trace.jsonl", record)

    # tool bridge
    def tool_result(self, entry: dict[str, Any]) -> None:
        self._append("tools.jsonl", {"event": "result", **entry})

    def tool_raised(self, name: str, exc: BaseException) -> None:
        with self._lock:
            self._raised.append(exc)
        self._append(
            "tools.jsonl",
            {"event": "raised", "tool": name, "error": f"{type(exc).__name__}: {exc}"},
        )

    def raised_by_tool(self, exc: BaseException) -> bool:
        with self._lock:
            return any(e is exc for e in self._raised)

    # session
    def turn(self, entry: dict[str, Any]) -> None:
        self._append("turns.jsonl", entry)
        self.meta["turns"].append({k: entry[k] for k in ("turn", "ending", "error") if k in entry})
        self.write_meta()

    def write_meta(self, **extra: Any) -> None:
        self.meta.update(extra)
        text = (
            json.dumps(to_jsonable_python(self.meta), ensure_ascii=False, indent=1, sort_keys=True)
            + "\n"
        )
        (self.run_dir / "session.json").write_text(text, encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
