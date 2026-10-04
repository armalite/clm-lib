"""Per-run evidence: append-only event log plus saved requests, scripts and diffs.

Layout of ``runs/<run_id>/``::

    run.json            configuration, identity, limits (written at start)
    events.jsonl        immutable chronological event history (read-only at close)
    requests/NNNN.json  exact provider payloads (no headers), with hashes
    context/rev-N.json  accepted context revisions; context/rev-N.diff diffs
    scripts/step-N.py   model code, saved before it runs
    files/step-N/       workspace files (helpers etc.) that changed during step N
    workspace/          the sandbox's writable mount (live editable file)
    fixtures/           the sandbox's read-only mount
    evaluator/          ground truth + score, written only after the run ends
    summary.json        metrics
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RunTrace:
    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        self.dir.mkdir(parents=True, exist_ok=False)
        for sub in ("requests", "context", "scripts", "files", "evaluator"):
            (self.dir / sub).mkdir()
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._seq = 0
        self._req = 0
        self.closed = False

    def event(self, event_type: str, /, **data: Any) -> int:
        self._seq += 1
        rec = {"seq": self._seq, "ts": now_iso(), "event": event_type, **data}
        self._events.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._events.flush()
        return self._seq

    def write(self, rel: str, content: str) -> Path:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_json(self, rel: str, obj: Any) -> Path:
        return self.write(rel, json.dumps(obj, indent=1, ensure_ascii=False, default=str) + "\n")

    def save_request(self, payload: dict[str, Any], meta: dict[str, Any]) -> tuple[str, str]:
        self._req += 1
        rel = f"requests/{self._req:04d}.json"
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.write_json(rel, {"meta": meta, "sha256": digest, "payload": payload})
        return rel, digest

    def close(self) -> None:
        if self.closed:
            return
        self._events.close()
        path = self.dir / "events.jsonl"
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        self.closed = True


def snapshot_files(
    root: Path, max_file_bytes: int = 200_000, max_files: int = 200
) -> dict[str, str]:
    """Return {relative path: text} for regular files under root (symlinks skipped)."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                out[rel] = f"<non-regular file mode={oct(st.st_mode)}>"
                continue
            if st.st_size > max_file_bytes:
                out[rel] = f"<{st.st_size} bytes; not captured>"
                continue
            with open(full, "rb") as fh:
                out[rel] = fh.read().decode("utf-8", errors="replace")
            if len(out) >= max_files:
                return out
    return out
