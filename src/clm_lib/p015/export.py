"""Export of a p015 session directory (`guarded-session/1`), separate from the historical exporter.

    python -m clm_lib.p015.export RUN_DIR DEST_DIR

The session is copied (never moved) to `DEST_DIR/<run_id>/`, with `manifest.json` (format, evidence label,
provenance summary, counts, endings) and `SHA256SUMS` over every exported file. As with historical run trees, the
export is immutable: re-exporting identical content is a no-op, and different content under an existing name is
refused. `verify_export` re-checks the sums and the record's internal consistency. The historical `clm-lib export`
command and formats are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from . import SESSION_FORMAT
from .records import FILES, read_jsonl

EXPORT_FORMAT = "guarded-session-export/1"


class ExportConflict(RuntimeError):
    """A different export already exists under this run id."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarise(run_dir: Path) -> dict[str, Any]:
    session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    attempts = read_jsonl(run_dir / "attempts.jsonl")
    finished = [a for a in attempts if a["event"] == "finished"]
    provenance = session["provenance"]
    out: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "session_format": session["format"],
        "run_id": session["run_id"],
        "evidence": session["evidence"],
        "mode": session.get("mode"),
        # Sessions of the paper and summary arms do not use guarded-clm, and record no guarded-clm provenance.
        "guarded_clm": {
            k: provenance["guarded_clm"].get(k)
            for k in ("version", "install", "git", "source_sha256")
        }
        if "guarded_clm" in provenance
        else None,
        "harness": {k: provenance["harness"].get(k) for k in ("git", "source_sha256")},
        "turns": session["turns"],
        "attempts": {
            "started": sum(1 for a in attempts if a["event"] == "started"),
            "finished": len(finished),
            "by_outcome": _count(a["outcome"] for a in finished),
            "charged_usd": round(sum(a.get("charged_usd", 0.0) for a in finished), 6),
        },
        "trace_records": _count(r["type"] for r in read_jsonl(run_dir / "trace.jsonl")),
        "tool_events": _count(r["event"] for r in read_jsonl(run_dir / "tools.jsonl")),
    }
    settings = session.get("settings") or {}
    if (
        "request_layout" in settings
    ):  # guarded sessions recorded with p015-request-layout/1; earlier ones unchanged
        out["guarded_request"] = {
            "request_layout": settings["request_layout"],
            "cache_point": settings.get("cache_point"),
            "cache_settings": {k: v for k, v in sorted((session.get("model_settings") or {}).items())
                               if k.startswith("anthropic_cache")},
        }  # fmt: skip
    if "arm" in session:  # absent from guarded-clm sessions, whose manifests are unchanged
        out["arm"] = session["arm"]
        out["attempts"]["by_category"] = _count(a.get("category", "main") for a in finished)
        out["attempts"]["charged_usd_by_category"] = {
            c: round(
                sum(a.get("charged_usd", 0.0) for a in finished if a.get("category", "main") == c),
                6,
            )
            for c in sorted({a.get("category", "main") for a in finished})
        }
        # Confirmed (provider-reported usage at the dated price) apart from charges assumed at the reservation.
        out["attempts"]["charged_usd_by_basis"] = {
            b: round(
                sum(a.get("charged_usd", 0.0) for a in finished if a.get("cost_basis") == b), 6
            )
            for b in sorted({a["cost_basis"] for a in finished if "cost_basis" in a})
        }
    return out


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return dict(sorted(out.items()))


def export_session(run_dir: Path, dest_root: Path, *, partial: bool = False) -> Path:
    """A complete export refuses an inconsistent record. A partial export (`partial=True`, for a session that was
    interrupted, stopped or left an attempt in flight) copies the record as it is and lists its consistency problems
    in the manifest's `incomplete` field, which `verify_export` then checks against the records."""
    problems = consistency_problems(run_dir)
    if problems and not partial:
        raise ValueError(f"session record is inconsistent: {problems}")
    manifest = summarise(run_dir)
    if partial:
        manifest["incomplete"] = problems
    dest = dest_root / manifest["run_id"]
    staging = dest_root / f".{manifest['run_id']}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for name in FILES:
        shutil.copy2(run_dir / name, staging / name)
    (staging / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    names = sorted([*FILES, "manifest.json"])
    (staging / "SHA256SUMS").write_text(
        "".join(f"{_sha256(staging / n)}  {n}\n" for n in names), encoding="utf-8"
    )
    if dest.exists():
        same = (dest / "SHA256SUMS").read_text(encoding="utf-8") == (
            staging / "SHA256SUMS"
        ).read_text(encoding="utf-8")
        shutil.rmtree(staging)
        if not same:
            raise ExportConflict(f"{dest.name} already exists with different content")
        return dest
    staging.rename(dest)
    return dest


def consistency_problems(run_dir: Path) -> list[str]:
    """Internal consistency of a session record (no checksums): every dispatched attempt finished exactly once, every
    turn has an ending, and the format and evidence label are the expected ones."""
    out: list[str] = []
    session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    if session.get("format") != SESSION_FORMAT:
        out.append(f"format {session.get('format')!r}")
    attempts = read_jsonl(run_dir / "attempts.jsonl")
    started = [a["attempt"] for a in attempts if a["event"] == "started"]
    finished = [a for a in attempts if a["event"] == "finished"]
    dispatched = [a["attempt"] for a in finished if a["outcome"] != "refused"]
    if sorted(started) != sorted(dispatched):
        out.append(f"started attempts {started} != dispatched finished attempts {dispatched}")
    if len({a["attempt"] for a in finished}) != len(finished):
        out.append("an attempt finished more than once")
    turns = read_jsonl(run_dir / "turns.jsonl")
    if [t["turn"] for t in turns] != list(range(1, len(turns) + 1)) or any(
        "ending" not in t for t in turns
    ):
        out.append("turn records are incomplete")
    home = str(Path.home())
    for name in FILES:
        if home in (run_dir / name).read_text(encoding="utf-8"):
            out.append(f"{name} contains a local home-directory path")
    return out


def verify_export(dest: Path) -> list[str]:
    out: list[str] = []
    for line in (dest / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if not (dest / name).exists():
            out.append(f"missing {name}")
        elif _sha256(dest / name) != digest:
            out.append(f"checksum mismatch: {name}")
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    problems = consistency_problems(dest)
    if (
        "incomplete" in manifest
    ):  # a partial export: its problems are declared, and must be exactly these
        if manifest["incomplete"] != problems:
            out.append("the declared incomplete records differ from the exported records")
        expected = {**summarise(dest), "incomplete": problems}
    else:
        out += problems
        expected = summarise(dest)
    if manifest != expected:
        out.append("manifest does not match the exported records")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m clm_lib.p015.export RUN_DIR DEST_DIR", file=sys.stderr)
        return 2
    dest = export_session(Path(argv[0]), Path(argv[1]))
    problems = verify_export(dest)
    print(f"exported {dest.name}: {problems if problems else 'OK'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
