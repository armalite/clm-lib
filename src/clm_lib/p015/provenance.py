"""Provenance of the guarded-clm source actually loaded, and of this harness, without absolute paths."""

from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import platform
import subprocess
from pathlib import Path
from typing import Any

DEPENDENCIES = (
    "guarded-clm",
    "pydantic-ai-slim",
    "pydantic",
    "pydantic-core",
    "pydantic-graph",
    "genai-prices",
)


def tree_fingerprint(package_dir: Path) -> dict[str, Any]:
    """SHA-256 over the package's files (relative path, NUL, file SHA-256, newline), in sorted order. Bytecode and
    caches are excluded, so the fingerprint is of the source that is loaded, not of build artefacts."""
    files = sorted(
        p
        for p in package_dir.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    digest = hashlib.sha256()
    listing = {}
    for p in files:
        rel = p.relative_to(package_dir).as_posix()
        file_sha = hashlib.sha256(p.read_bytes()).hexdigest()
        listing[rel] = file_sha
        digest.update(f"{rel}\0{file_sha}\n".encode())
    return {"sha256": digest.hexdigest(), "files": listing}


def _git(directory: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def git_state(package_dir: Path) -> dict[str, Any]:
    """The git revision and dirty state of the checkout holding `package_dir`, if it is in one. Dirty is reported
    for the package's own files and for the whole checkout separately. No paths are recorded."""
    top = _git(package_dir, "rev-parse", "--show-toplevel")
    if top is None:
        return {"in_git_checkout": False}
    rel = package_dir.resolve().relative_to(Path(top).resolve()).as_posix()
    package_status = _git(Path(top), "status", "--porcelain", "--", rel) or ""
    all_status = _git(Path(top), "status", "--porcelain") or ""
    return {
        "in_git_checkout": True,
        "revision": _git(Path(top), "rev-parse", "HEAD"),
        "package_dirty": bool(package_status),
        "checkout_dirty": bool(all_status),
        "package_path_in_checkout": rel,
    }


def _install_kind(distribution: str) -> str:
    try:
        text = md.distribution(distribution).read_text("direct_url.json")
    except md.PackageNotFoundError:
        return "not-installed"
    if not text:
        return "index"
    info = json.loads(text)
    if info.get("dir_info", {}).get("editable"):
        return "editable-local"
    if "vcs_info" in info:
        return f"vcs:{info['vcs_info'].get('commit_id', '')}"
    return "local-archive" if info.get("url", "").startswith("file:") else "direct-url"


def versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in DEPENDENCIES:
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            out[name] = None
    return out


def library_provenance() -> dict[str, Any]:
    import guarded_clm

    package_dir = Path(guarded_clm.__file__).resolve().parent
    fingerprint = tree_fingerprint(package_dir)
    return {
        "package": "guarded_clm",
        "version": getattr(guarded_clm, "__version__", None),
        "install": _install_kind("guarded-clm"),
        "git": git_state(package_dir),
        "source_sha256": fingerprint["sha256"],
        "source_files": fingerprint["files"],
        "dependencies": versions(),
        "python": platform.python_version(),
    }


def harness_provenance() -> dict[str, Any]:
    package_dir = Path(__file__).resolve().parent
    fingerprint = tree_fingerprint(package_dir)
    return {
        "package": "clm_lib.p015",
        "git": git_state(package_dir),
        "source_sha256": fingerprint["sha256"],
        "source_files": fingerprint["files"],
    }
