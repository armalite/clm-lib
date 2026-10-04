"""Working-context schema, host-side revisions and candidate validation.

The model-editable file is ``context.json`` in the run workspace::

    {"format": "clm-context/v1",
     "entries": [{"id": "s1.act", "role": "assistant", "body": "..."}, ...]}

Only ``id``, ``role`` and ``body`` exist per entry. Revision numbers, hashes and
provenance live in :class:`ContextStore` on the host, never in the file.
Parsing uses ``json.loads`` only; no model-authored code runs host-side.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = "clm-context/v1"
CONTEXT_FILENAME = "context.json"
ALLOWED_ROLES = frozenset({"assistant", "observation", "note", "summary", "receipt"})
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:~-]{0,63}$")


@dataclass(frozen=True)
class Entry:
    id: str
    role: str
    body: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "role": self.role, "body": self.body}


@dataclass(frozen=True)
class ContextLimits:
    max_file_bytes: int = 256_000
    max_entries: int = 200
    max_body_chars: int = 24_000
    max_total_chars: int = 120_000


@dataclass(frozen=True)
class Revision:
    number: int
    entries: tuple[Entry, ...]
    sha256: str
    parent: int | None
    source: str  # "init" | "runtime" | "model_edit" | "summary" | "spill"
    step: int


class ContextValidationError(ValueError):
    """A candidate context file violates the documented format."""


def serialize(entries: tuple[Entry, ...] | list[Entry]) -> str:
    doc = {"format": FORMAT, "entries": [e.to_dict() for e in entries]}
    return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"


def content_hash(entries: tuple[Entry, ...] | list[Entry]) -> str:
    canonical = json.dumps([e.to_dict() for e in entries], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def total_chars(entries: tuple[Entry, ...] | list[Entry]) -> int:
    return sum(len(e.body) for e in entries)


def parse_candidate(raw: bytes, limits: ContextLimits) -> tuple[Entry, ...]:
    """Validate raw file bytes and return entries, or raise ContextValidationError."""
    if len(raw) > limits.max_file_bytes:
        raise ContextValidationError(f"file is {len(raw)} bytes; limit {limits.max_file_bytes}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContextValidationError("file is not valid UTF-8") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContextValidationError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from exc
    if not isinstance(doc, dict):
        raise ContextValidationError("top level must be an object")
    if set(doc) != {"format", "entries"}:
        extra = sorted(set(doc) - {"format", "entries"})
        missing = sorted({"format", "entries"} - set(doc))
        raise ContextValidationError(
            f"top-level keys must be format, entries (extra={extra}, missing={missing})"
        )
    if doc["format"] != FORMAT:
        raise ContextValidationError(f"format must be {FORMAT!r}")
    items = doc["entries"]
    if not isinstance(items, list):
        raise ContextValidationError("entries must be a list")
    if len(items) > limits.max_entries:
        raise ContextValidationError(f"{len(items)} entries; limit {limits.max_entries}")
    seen: set[str] = set()
    out: list[Entry] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"id", "role", "body"}:
            raise ContextValidationError(f"entry {i} must have exactly id, role, body")
        eid, role, body = item["id"], item["role"], item["body"]
        if not all(isinstance(v, str) for v in (eid, role, body)):
            raise ContextValidationError(f"entry {i}: id, role, body must be strings")
        if not ID_RE.match(eid):
            raise ContextValidationError(f"entry {i}: invalid id {eid[:40]!r}")
        if eid in seen:
            raise ContextValidationError(f"duplicate id {eid!r}")
        seen.add(eid)
        if role not in ALLOWED_ROLES:
            raise ContextValidationError(
                f"entry {eid!r}: role {role[:20]!r} not allowed (allowed: {sorted(ALLOWED_ROLES)})"
            )
        if len(body) > limits.max_body_chars:
            raise ContextValidationError(
                f"entry {eid!r}: body {len(body)} chars; limit {limits.max_body_chars}"
            )
        out.append(Entry(eid, role, body))
    total = total_chars(out)
    if total > limits.max_total_chars:
        raise ContextValidationError(f"total body chars {total}; limit {limits.max_total_chars}")
    return tuple(out)


@dataclass
class EditOutcome:
    """Result of reading back the context file after one execution."""

    status: str  # "not_written" | "unchanged_write" | "accepted" | "rejected"
    reason: str = ""
    before: Revision | None = None
    after: Revision | None = None

    @property
    def attempted(self) -> bool:
        return self.status != "not_written"

    @property
    def changed(self) -> bool:
        return self.status == "accepted"


def read_workspace_file(workspace: Path, name: str, max_bytes: int) -> bytes:
    """Read a regular file directly inside ``workspace`` without following symlinks."""
    path = workspace / name
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise ContextValidationError(f"{name} is missing") from exc
    if stat.S_ISLNK(st.st_mode):
        raise ContextValidationError(f"{name} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise ContextValidationError(f"{name} is not a regular file")
    if os.path.realpath(path.parent) != os.path.realpath(workspace):
        raise ContextValidationError(f"{name} escapes the workspace")
    if st.st_size > max_bytes:
        raise ContextValidationError(f"file is {st.st_size} bytes; limit {max_bytes}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as fh:
        return fh.read(max_bytes + 1)


@dataclass
class ContextStore:
    """Authoritative, host-side record of accepted context revisions."""

    limits: ContextLimits = field(default_factory=ContextLimits)
    revisions: list[Revision] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.revisions:
            self.revisions.append(Revision(0, (), content_hash(()), None, "init", 0))

    @property
    def current(self) -> Revision:
        return self.revisions[-1]

    @property
    def entries(self) -> tuple[Entry, ...]:
        return self.current.entries

    def commit(self, entries: tuple[Entry, ...] | list[Entry], source: str, step: int) -> Revision:
        entries = tuple(entries)
        rev = Revision(
            number=self.current.number + 1,
            entries=entries,
            sha256=content_hash(entries),
            parent=self.current.number,
            source=source,
            step=step,
        )
        self.revisions.append(rev)
        return rev

    def append(self, new: list[Entry], source: str, step: int) -> Revision:
        existing = {e.id for e in self.entries}
        fixed: list[Entry] = []
        for e in new:
            eid = e.id
            n = 2
            while eid in existing:
                eid = f"{e.id}~{n}"
                n += 1
            existing.add(eid)
            fixed.append(Entry(eid, e.role, e.body))
        return self.commit(self.entries + tuple(fixed), source, step)

    def mirror(self, workspace: Path) -> str:
        """Write the accepted state to the workspace file atomically; return its text."""
        text = serialize(self.entries)
        target = workspace / CONTEXT_FILENAME
        tmp = workspace / f".{CONTEXT_FILENAME}.host-tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return text

    def read_back(self, workspace: Path, mirrored_text: str, step: int) -> EditOutcome:
        """Validate the workspace file after execution and accept it if valid."""
        before = self.current
        try:
            raw = read_workspace_file(workspace, CONTEXT_FILENAME, self.limits.max_file_bytes)
        except ContextValidationError as exc:
            return EditOutcome("rejected", str(exc), before=before)
        if raw == mirrored_text.encode("utf-8"):
            return EditOutcome("not_written", before=before)
        try:
            entries = parse_candidate(raw, self.limits)
        except ContextValidationError as exc:
            return EditOutcome("rejected", str(exc), before=before)
        if content_hash(entries) == before.sha256:
            return EditOutcome("unchanged_write", before=before)
        after = self.commit(entries, "model_edit", step)
        return EditOutcome("accepted", before=before, after=after)


def unified_diff(a: Revision, b: Revision) -> str:
    return "".join(
        difflib.unified_diff(
            serialize(a.entries).splitlines(keepends=True),
            serialize(b.entries).splitlines(keepends=True),
            fromfile=f"rev-{a.number}",
            tofile=f"rev-{b.number}",
        )
    )
