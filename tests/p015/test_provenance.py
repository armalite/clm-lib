"""Provenance of loaded source: fingerprints follow content, and git state reports dirtiness without paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

from clm_lib.p015.provenance import git_state, library_provenance, tree_fingerprint


def test_fingerprint_follows_content_and_ignores_bytecode(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    first = tree_fingerprint(pkg)["sha256"]
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\0")
    assert tree_fingerprint(pkg)["sha256"] == first
    (pkg / "a.py").write_text("x = 2\n")
    assert tree_fingerprint(pkg)["sha256"] != first


def test_git_state_reports_revision_and_dirtiness(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "a.py").write_text("x = 1\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    clean = git_state(repo / "src" / "pkg")
    assert clean["in_git_checkout"] and not clean["package_dirty"] and len(clean["revision"]) == 40
    assert clean["package_path_in_checkout"] == "src/pkg"
    (repo / "src" / "pkg" / "a.py").write_text("x = 2\n")
    assert git_state(repo / "src" / "pkg")["package_dirty"]
    assert git_state(tmp_path)["in_git_checkout"] is False
    assert str(tmp_path) not in str(git_state(repo / "src" / "pkg"))


def test_library_provenance_names_the_loaded_package() -> None:
    p = library_provenance()
    assert p["package"] == "guarded_clm" and p["dependencies"]["guarded-clm"] is not None
    assert "capability.py" in p["source_files"] and len(p["source_sha256"]) == 64
