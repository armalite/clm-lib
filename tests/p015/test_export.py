"""guarded-session/1 records and their export: completeness, checksums, immutability, provenance, labels."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clm_lib.budget import Ledger
from clm_lib.p015 import EVIDENCE_LABEL
from clm_lib.p015.export import ExportConflict, export_session, verify_export
from clm_lib.p015.records import read_jsonl

from . import harness as h


def recorded_session(tmp_path: Path, name: str = "edits") -> tuple[Path, dict]:
    run_dir = tmp_path / "runs" / name
    transcript, _ = h.run_session(
        h.CASES[name], run_dir, Ledger.open(tmp_path / "ledger.json", 100.0)
    )
    return run_dir, transcript


def test_export_round_trip_is_complete_and_checksummed(tmp_path: Path) -> None:
    run_dir, transcript = recorded_session(tmp_path)
    dest = export_session(run_dir, tmp_path / "export")
    assert verify_export(dest) == []
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["evidence"] == EVIDENCE_LABEL == "scripted-offline"
    assert (
        manifest["attempts"]["started"]
        == manifest["attempts"]["finished"]
        == len(transcript["calls"])
    )
    assert [t["ending"] for t in manifest["turns"]] == ["completed", "completed"]
    # Reconstruct the session from the export alone.
    attempts = read_jsonl(dest / "attempts.jsonl")
    requests = [a for a in attempts if a["event"] == "started"]
    responses = [a["response"] for a in attempts if a["event"] == "finished"]
    assert [h.dump(r["messages"]) for r in requests] == [c["messages"] for c in transcript["calls"]]
    assert all(
        r["parameters"]["function_tools"] for r in requests
    )  # tool definitions sent on every request
    tool_calls = [p for r in responses for p in r["parts"] if p["part_kind"] == "tool-call"]
    assert len(tool_calls) == 8  # one per tool response in the case
    assert h.dump(read_jsonl(dest / "trace.jsonl")) == transcript["records"]
    turns = read_jsonl(dest / "turns.jsonl")
    assert [t["output"] for t in turns] == ["answer one", "answer two"]
    session = json.loads((dest / "session.json").read_text())
    lib = session["provenance"]["guarded_clm"]
    assert {"version", "install", "git", "source_sha256", "source_files", "dependencies"} <= set(
        lib
    )
    assert lib["dependencies"]["pydantic-ai-slim"] == "2.27.1"
    assert set(lib["git"]) >= {"in_git_checkout"}
    text = "".join(p.read_text() for p in dest.iterdir())
    assert (
        str(Path.home()) not in text and str(tmp_path) not in text
    )  # no local paths in the evidence


def test_export_is_immutable_and_tamper_evident(tmp_path: Path) -> None:
    run_dir, _ = recorded_session(tmp_path)
    dest = export_session(run_dir, tmp_path / "export")
    assert export_session(run_dir, tmp_path / "export") == dest  # identical: no-op
    other_dir, _ = recorded_session(tmp_path / "other")
    (other_dir / "turns.jsonl").write_text(
        (other_dir / "turns.jsonl").read_text().replace("answer one", "answer 1")
    )
    with pytest.raises(ExportConflict):
        export_session(other_dir, tmp_path / "export")
    (dest / "trace.jsonl").write_text((dest / "trace.jsonl").read_text() + "\n")
    assert "checksum mismatch: trace.jsonl" in verify_export(dest)


def test_refused_attempts_are_recorded_and_exported(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "ledger.json", 100.0)
    run_dir = tmp_path / "runs" / "refusal"
    h.run_session(
        h.CASES["answers_retained"],
        run_dir,
        ledger,
        after_turn=lambda turn: setattr(
            ledger, "limit_usd", ledger.spent_usd - ledger.start_spent_usd
        ),
    )
    dest = export_session(run_dir, tmp_path / "export")
    assert verify_export(dest) == []
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["attempts"]["by_outcome"] == {"ok": 2, "refused": 1}
    assert [t["ending"] for t in manifest["turns"]] == ["completed", "spend_refused"]
