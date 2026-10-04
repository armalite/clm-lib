"""Results export: repeatable, checksum-verified, never overwrites different evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from clm_lib.export import ExportConflict, export_experiment

from .conftest import FINAL, FakeExecutor, execute


def make_experiment(tmp_path: Path, run_ids: list[str]) -> Path:
    exp = tmp_path / "exp"
    exp.mkdir()
    (exp / "README.md").write_text("# test\n")
    (exp / "protocol.md").write_text("# protocol\n")
    spec = {
        "id": "999-test",
        "title": "test",
        "runs": [{"run_id": r, "role": "evaluation"} for r in run_ids],
        "comparisons": [],
    }
    (exp / "experiment.json").write_text(json.dumps(spec))
    return exp


def test_export_is_repeatable_redacted_and_refuses_conflicts(
    make_runner: Any, tmp_path: Path
) -> None:
    runner, _ = make_runner([execute("a"), FINAL], executor=FakeExecutor())
    from clm_lib.tasks import generate

    res = runner.run(generate("dev"), "clm")
    runs_root = res.run_dir.parent
    before = {p: p.read_bytes() for p in res.run_dir.rglob("*") if p.is_file()}
    exp = make_experiment(tmp_path, [res.run_id])
    dest = tmp_path / "results"
    log = export_experiment(exp, runs_root, tmp_path / "ledger.json", dest)
    assert log["runs"][res.run_id] == "written"
    out = dest / "experiments" / "999-test"
    run_out = out / "artifacts" / "runs" / "evaluation" / res.run_id
    assert "fixtures" not in {p.name for p in run_out.iterdir()}
    assert json.loads((run_out / "run.json").read_text())["executor"]["kind"] == "fake-test-double"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["runs"][0]["fixtures_match_generator"] is True
    assert (out / "artifacts" / "fixtures" / "dev").is_dir()
    assert (dest / "README.md").read_text().count("999-test") >= 1
    # Repeat: identical evidence is left alone.
    log2 = export_experiment(exp, runs_root, tmp_path / "ledger.json", dest)
    assert log2["runs"][res.run_id] == "unchanged" and log2["fixtures"]["dev"] == "unchanged"
    # Raw run untouched.
    assert before == {p: p.read_bytes() for p in res.run_dir.rglob("*") if p.is_file()}
    # Different evidence under the same run id is refused.
    (run_out / "summary.json").write_text("{}")
    shutil.copy(run_out / "SHA256SUMS", tmp_path / "sums")
    (run_out / "SHA256SUMS").write_text("tampered\n")
    with pytest.raises(ExportConflict):
        export_experiment(exp, runs_root, tmp_path / "ledger.json", dest)


def test_export_redacts_local_uid(tmp_path: Path) -> None:
    from clm_lib.export import collect

    run = tmp_path / "r"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"executor": {"user": "1000:1000 (non-root)"}}))
    (run / "fixtures").mkdir()
    (run / "fixtures" / "x").write_text("x")
    files = collect(run, skip_top=frozenset({"fixtures"}))
    assert set(files) == {"run.json"}
    assert "1000:1000" not in files["run.json"].decode()
