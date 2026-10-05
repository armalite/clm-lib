"""Experiment 007: classify context-management helper evidence from a run's recorded artifacts.

Evidence tiers for each workspace file that holds code touching ``context.json``:

1. ``created``: a file was written that contains code referring to context.json;
2. ``executed``: a function (or module body) defined in that file began executing in a step
   that exited 0 (in-container sys.monitoring record);
3. ``repeated_accepted_edits``: code from that file was on the call stack when context.json was
   written, the step's edit was accepted, in at least two distinct steps, and each accepted
   revision appears as the prefix of the next request (report.edit_evidence);
4. ``substantive``: the function that wrote the context contains its own selection, filtering,
   merging or replacement logic over existing entries (static AST heuristic, see
   ``classify_function``), as opposed to a wrapper that only writes text supplied by the caller.

Direct-edit compliance (the clm_direct condition) flags saved files with context-management
code that a later step reuses: imported/executed (runtime record), compiled or opened for
reading from a file, run as a subprocess, or exec'd/runpy'd according to the step's code text.

Limitations: the runtime record misses code run in child processes and os.open-level writes;
code-text checks are pattern-based. Ambiguous cases are inspected manually and reported.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from .report import edit_evidence

WS = "/task/workspace/"
# Revised experiment 007 reporting categories for what a helper's writing function does.
CATEGORY = {
    "wrapper (writes caller-supplied content)": "writes or replaces supplied note content",
    "replacement over existing entries": "selects, removes or reorganises existing entries",
    "selection/filtering": "selects, removes or reorganises existing entries",
    "merging/rewriting": "more substantial management logic",
    "selection plus merging": "more substantial management logic",
}
CTX_RE = re.compile(r"context\.json")
ENTRY_FIELDS = {"id", "role", "body"}


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines() if x]


def file_versions(run_dir: Path) -> dict[str, list[tuple[int, str]]]:
    """Every saved version of each changed workspace file: path -> [(step, text), ...]."""
    out: dict[str, list[tuple[int, str]]] = {}
    root = run_dir / "files"
    if not root.exists():
        return out
    for step_dir in sorted(root.glob("step-*")):
        step = int(step_dir.name.split("-")[1])
        after = step_dir / "after"
        if not after.exists():
            continue
        for p in sorted(after.rglob("*")):
            if p.is_file():
                out.setdefault(str(p.relative_to(after)), []).append(
                    (step, p.read_text("utf-8", "replace"))
                )
    return out


def version_at(versions: list[tuple[int, str]], step: int) -> str | None:
    """The file text in force during ``step`` (the latest version saved before it)."""
    text = None
    for s, t in versions:
        if s < step:
            text = t
    return text


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _strings(node: ast.AST) -> set[str]:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def classify_function(source: str, qualname: str) -> dict[str, Any]:
    """Heuristic: what does the named function do with the existing context entries?"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"kind": "unparseable"}
    target = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == qualname.split(".")[-1]
        ):
            target = node
            break
    if qualname == "<module>":
        target = tree  # type: ignore[assignment]
    if target is None:
        return {"kind": "not_found"}
    loads = [
        n
        for n in ast.walk(target)
        if isinstance(n, ast.Subscript)
        and isinstance(n.ctx, ast.Load)
        and isinstance(n.slice, ast.Constant)
        and isinstance(n.slice.value, str)
    ]
    # Existing entries are read when an "entries" list is loaded (iterated, sliced, indexed),
    # and their fields are read via subscripts such as e["body"] in load context. Keys of newly
    # built entry dicts do not count.
    reads_existing = any(n.slice.value == "entries" for n in loads) or any(  # type: ignore[attr-defined]
        isinstance(n, ast.Call)
        and getattr(n.func, "attr", "") == "get"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "entries"
        for n in ast.walk(target)
    )
    field_refs = any(n.slice.value in ENTRY_FIELDS for n in loads) or any(  # type: ignore[attr-defined]
        isinstance(n, ast.Call)
        and getattr(n.func, "attr", "") == "get"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value in ENTRY_FIELDS
        for n in ast.walk(target)
    )
    has_filter = any(isinstance(n, ast.comprehension) and n.ifs for n in ast.walk(target)) or any(
        isinstance(n, ast.For) and any(isinstance(m, ast.If) for m in ast.walk(n))
        for n in ast.walk(target)
    )
    has_slice = any(isinstance(n, ast.Slice) for n in ast.walk(target))
    has_sort = bool(
        {"sorted", "sort", "filter"}
        & (_names(target) | {getattr(n, "attr", "") for n in ast.walk(target)})
    )
    builds_text = any(
        isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "join" for n in ast.walk(target)
    )
    args = [a.arg for a in getattr(getattr(target, "args", None), "args", [])]
    if reads_existing and field_refs and (has_filter or has_slice or has_sort) and builds_text:
        kind = "selection plus merging"
    elif reads_existing and field_refs and (has_filter or has_slice or has_sort):
        kind = "selection/filtering"
    elif reads_existing and field_refs and builds_text:
        kind = "merging/rewriting"
    elif reads_existing and field_refs:
        kind = "replacement over existing entries"
    else:
        kind = "wrapper (writes caller-supplied content)"
    return {
        "kind": kind,
        "category": CATEGORY[kind],
        "substantive": kind
        in ("selection/filtering", "merging/rewriting", "selection plus merging"),
        "reads_existing_entries": reads_existing,
        "references_entry_fields": field_refs,
        "filter": has_filter,
        "slice": has_slice,
        "sort_or_filter_call": has_sort,
        "builds_text": builds_text,
        "parameters": args,
        "lines": len((ast.get_source_segment(source, target) or source).splitlines())
        if hasattr(target, "lineno")
        else len(source.splitlines()),
    }


def _classify_all(source: str) -> dict[str, str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def writes_directly(fn: ast.AST) -> bool:
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                attr = getattr(n.func, "attr", "")
                if attr in ("dump", "write", "write_text", "replace", "rename"):
                    return True
                if getattr(n.func, "id", "") == "open" and any(
                    isinstance(a, ast.Constant) and isinstance(a.value, str) and "w" in a.value
                    for a in n.args[1:]
                ):
                    return True
        return False

    writers = {f.name for f in funcs if writes_directly(f)}
    for f in funcs:  # functions that call a writer in the same module also write
        if any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") in writers for n in ast.walk(f)
        ):
            writers.add(f.name)
    out = {}
    for node in funcs:
        if node.name in writers:
            out[node.name] = classify_function(source, node.name).get("category", "unknown")
        else:
            out[node.name] = "reads or utility (no context write)"
    return out


def audit_run(run_dir: Path) -> dict[str, Any]:
    m = json.loads((run_dir / "summary.json").read_text())
    h = m.get("helpers") or {}
    versions = file_versions(run_dir)
    cm_files = {
        p
        for p, vs in versions.items()
        if p != "context.json"
        and any(CTX_RE.search(t) and (p.endswith(".py") or "def " in t) for _, t in vs)
    }
    created = {p: versions[p][0][0] for p in sorted(cm_files)}
    revisions = {p: [s for s, _ in versions[p][1:]] for p in sorted(cm_files)}
    executed = {p: v for p, v in (h.get("function_execution_steps") or {}).items() if p in cm_files}
    attributed = {
        p: v
        for p, v in (h.get("helper_written_accepted_edit_steps") or {}).items()
        if p in cm_files
    }
    ev = {rec["step"]: rec for rec in edit_evidence(run_dir)}
    uses = {u["step"]: u for u in h.get("uses") or []}
    files_out: dict[str, Any] = {}
    for p in sorted(cm_files):
        steps = sorted(set(attributed.get(p, [])))
        confirmed = [
            s for s in steps if ev.get(s, {}).get("content_prefix_equals_accepted_revision") is True
        ]
        writers: dict[str, dict[str, Any]] = {}
        for s in steps:
            src = version_at(versions[p], s)
            for fn in uses.get(s, {}).get("files", {}).get(p, {}).get("wrote_context_from", []):
                key = f"{fn}@v{s}"
                writers[key] = {
                    "step": s,
                    **(classify_function(src, fn) if src else {"kind": "source_missing"}),
                }
        files_out[p] = {
            "created_step": created[p],
            "revision_steps": revisions[p],
            "function_execution_steps": sorted(set(executed.get(p, []))),
            "helper_written_accepted_edit_steps": steps,
            "next_request_confirmed_steps": confirmed,
            "repeated_accepted_edits": len(confirmed) >= 2,
            "writers": writers,
            "substantive_repeated": len(confirmed) >= 2
            and sum(1 for w in writers.values() if w.get("substantive")) >= 2,
            "final_chars": len(versions[p][-1][1]),
            # every top-level function of the final version, classified the same way
            "functions": _classify_all(versions[p][-1][1]),
        }
    scripts = sorted((run_dir / "scripts").glob("*.py")) if (run_dir / "scripts").exists() else []
    events = _events(run_dir)
    accepted = sorted(
        {e["step"] for e in events if e.get("event") == "context_edit" and e.get("changed")}
    )
    exec_steps = {s for f in files_out.values() for s in f["function_execution_steps"]}
    assisted = [s for s in accepted if s in exec_steps]
    attributed_all = sorted(
        {s for f in files_out.values() for s in f["helper_written_accepted_edit_steps"]}
    )
    reuse = reuse_findings(run_dir, cm_files, versions)
    if m["mode"] == "clm_reuse":
        adherence = (
            "none" if not assisted else "full" if len(assisted) == len(accepted) else "partial"
        )
    elif m["mode"] == "clm_direct":
        adherence = "adherent" if not reuse else "non-adherent"
    else:
        adherence = None
    tiers = {
        "created": bool(files_out),
        "executed": any(f["function_execution_steps"] for f in files_out.values()),
        "repeated_execution": any(
            len(f["function_execution_steps"]) >= 2 for f in files_out.values()
        ),
        "repeated_accepted_edits": any(f["repeated_accepted_edits"] for f in files_out.values()),
        "substantive_repeated": any(f["substantive_repeated"] for f in files_out.values()),
    }
    return {
        "run_id": m["run_id"],
        "mode": m["mode"],
        "tiers": tiers,
        "files": files_out,
        "code_chars_all_steps": sum(len(p.read_text()) for p in scripts),
        "steps_with_code": len(scripts),
        "reuse_violations": reuse,
        "accepted_edit_steps": accepted,
        # accepted edits in steps where saved context-management code executed
        "helper_assisted_edit_steps": assisted,
        # accepted edits whose context.json write had saved helper code on the call stack
        "helper_attributed_edit_steps": attributed_all,
        "adherence": adherence,
        "categories": sorted(
            {
                w["category"]
                for f in files_out.values()
                for w in f["writers"].values()
                if "category" in w
            }
        ),
    }


def reuse_findings(
    run_dir: Path, cm_files: set[str], versions: dict[str, list[tuple[int, str]]]
) -> list[dict[str, Any]]:
    """Later-step reuse of saved context-management code (any condition; a violation in
    clm_direct). Each finding names the step, file and the evidence that indicates reuse."""
    out: list[dict[str, Any]] = []
    events = _events(run_dir)
    results = {e["step"]: e for e in events if e.get("event") == "execution_result"}
    for p in sorted(cm_files):
        first = versions[p][0][0]
        full = WS + p
        stem = Path(p).stem
        for step, res in sorted(results.items()):
            if step <= first:
                continue
            rt = res.get("runtime_trace") or {}
            code_path = run_dir / "scripts" / f"step-{step:03d}.py"
            code = code_path.read_text() if code_path.exists() else ""
            evidence = []
            if full in (rt.get("imported") or []):
                evidence.append("imported")
            if any(k.split("::", 1)[0] == full for k in (rt.get("calls") or {})):
                evidence.append("executed (runtime record)")
            if full in (rt.get("compiled") or []):
                evidence.append("compiled")
            if any(
                o[0] == full and "r" in (o[1] or "r") for o in (rt.get("opened") or [])
            ) and re.search(r"\bexec\s*\(|runpy|compile\s*\(", code):
                evidence.append("read and exec/compile in code")
            if any(p in s or stem in s for s in (rt.get("spawned") or [])):
                evidence.append("subprocess")
            if (
                re.search(rf"\b(import|from)\s+{re.escape(stem)}\b", code)
                and "imported" not in evidence
            ):
                evidence.append("import in code text")
            if evidence:
                out.append({"step": step, "file": p, "evidence": evidence})
    return out
