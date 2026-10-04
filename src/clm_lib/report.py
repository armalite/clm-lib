"""Markdown report built only from recorded run evidence and the ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def context_entries(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Decode the <working_context> entries exactly as they were sent."""
    user = payload["messages"][0]["content"]
    block = user.split("<working_context>\n", 1)[1].split("\n</working_context>", 1)[0]
    return [json.loads(line) for line in block.splitlines() if line.strip()]


def _revision(run_dir: Path, number: int | None) -> list[dict[str, str]]:
    if number is None:
        return []
    path = run_dir / "context" / f"rev-{number:04d}.json"
    return _load(path)["entries"] if path.exists() else []


def _distinctive_lines(body: str, min_len: int = 40) -> list[str]:
    seen: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if len(line) >= min_len and line not in seen:
            seen.append(line)
    return seen[:200]


def edit_evidence(run_dir: Path) -> list[dict[str, Any]]:
    """Check each accepted content-changing edit against the very next action request.

    Structural check: removed ids are absent and added ids present in the next request.
    Content check: the next request's working context starts with exactly the accepted
    revision's entries (id, role, body), rewritten entries carry the new body, and
    distinctive lines of removed entries are searched for anywhere in the next request's
    entries (they may legitimately reappear if the step's code printed them again).
    """
    events = _events(run_dir)
    out = []
    for i, ev in enumerate(events):
        if ev.get("event") != "context_edit" or not ev.get("changed"):
            continue
        nxt = next(
            (
                e
                for e in events[i + 1 :]
                if e.get("event") == "request" and e.get("kind") == "action"
            ),
            None,
        )
        before = _revision(run_dir, ev.get("before_revision"))
        after = _revision(run_dir, ev.get("after_revision"))
        rec: dict[str, Any] = {
            "step": ev["step"],
            "before_revision": ev.get("before_revision"),
            "after_revision": ev.get("after_revision"),
            "removed": ev.get("removed", []),
            "added": ev.get("added", []),
            "rewritten": ev.get("rewritten", []),
            "chars": f"{ev.get('chars_before')} -> {ev.get('chars_after')}",
        }
        if nxt is None:
            rec["next_request"] = "none (run ended)"
            rec["content_verdict"] = "not_applicable"
            out.append(rec)
            continue
        sent = context_entries(_load(run_dir / nxt["file"])["payload"])
        ids = [e["id"] for e in sent]
        rec["next_request"] = nxt["file"]
        rec["structural_removed_absent"] = all(r not in ids for r in rec["removed"])
        rec["structural_added_present"] = all(a in ids for a in rec["added"])
        prefix_ok = bool(after) and sent[: len(after)] == after
        rec["content_prefix_equals_accepted_revision"] = prefix_ok
        rec["appended_after_edit"] = ids[len(after) :] if prefix_ok else None
        old_bodies = {e["id"]: e["body"] for e in before}
        new_bodies = {e["id"]: e["body"] for e in sent}
        rec["rewritten_verified"] = all(
            new_bodies.get(r) not in (None, old_bodies.get(r)) for r in rec["rewritten"]
        )
        reappear: dict[str, Any] = {}
        for rid in rec["removed"]:
            lines = _distinctive_lines(old_bodies.get(rid, ""))
            hits = sorted({e["id"] for e in sent for ln in lines if ln in e["body"]})
            found = sum(1 for ln in lines if any(ln in e["body"] for e in sent))
            if lines:
                reappear[rid] = {"distinctive_lines": len(lines), "found": found, "in": hits}
        rec["removed_content_reappearance"] = reappear
        any_back = any(v["found"] for v in reappear.values())
        if not (prefix_ok and rec["rewritten_verified"] and rec["structural_removed_absent"]):
            rec["content_verdict"] = "mismatch"
        elif any_back:
            rec["content_verdict"] = "replaced; some removed text reappears in later entries"
        else:
            rec["content_verdict"] = "replaced; removed text absent from next request"
        out.append(rec)
    return out


def build_report(runs_dir: Path, ledger_path: Path) -> str:
    runs = []
    for summary in sorted(runs_dir.glob("*/summary.json")):
        runs.append((summary.parent, _load(summary)))
    lines = [
        "# clm-lib run report",
        "",
        "Generated from `runs/*/summary.json`, events and the ledger.",
        "",
    ]
    if ledger_path.exists():
        led = _load(ledger_path)
        entries = led.get("entries", [])
        spent = sum(e["charged_usd"] for e in entries)
        assumed = sum(e["charged_usd"] for e in entries if e["cost_basis"] == "reservation_assumed")
        by_kind: dict[str, float] = {}
        for e in entries:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0.0) + e["charged_usd"]
        lines += [
            "## Spend (ledger)",
            "",
            f"- ceiling: ${led['ceiling_usd']:.2f}; charged: ${spent:.4f} over {len(entries)} recorded call attempts",
            f"- of which assumed from reservations (outcome unknown): ${assumed:.4f}",
            "- by call kind: " + ", ".join(f"{k} ${v:.4f}" for k, v in sorted(by_kind.items())),
            "",
        ]
    lines += [
        "## Runs",
        "",
        "| run | label | mode | task | status | outcome | strict | cause/remedy/value/evidence | calls (act/rep/sum/retry) | exec | edits ok/unch/rej | summaries | peak req tok (rep) | cost $ |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, m in runs:
        c, s = m["counts"], m["score"]
        comp = s["components"]
        lines.append(
            f"| {m['run_id']} | {m.get('label', '')} | {m['mode']} | {m['task']} | {m['status']} | "
            f"{s['outcome']} | {s['strict_success']} | {comp['cause']:.0f}/{comp['remedy']:.0f}/"
            f"{comp['value']:.0f}/{comp['evidence']:.2f} | {c['provider_calls']} ({c['action_calls']}/"
            f"{c['repair_calls']}/{c['summary_calls']}/{c['api_retries']}) | {c['executions']} | "
            f"{c['edits_accepted_changed']}/{c['edits_unchanged_write']}/{c['edits_rejected']} | "
            f"{c['summaries_applied']} | {m['peak_request_input_tokens_reported']} | "
            f"{m['cost']['incurred_usd']:.4f} |"
        )
    lines += ["", "## Context-edit evidence (CLM and guided runs)", ""]
    for run_dir, m in runs:
        if m["mode"] == "summary":
            continue
        ev = edit_evidence(run_dir)
        h = m["helpers"]
        lines.append(
            f"- {m['run_id']} ({m['mode']}): {len(ev)} accepted content-changing edits; "
            f"helpers created {h.get('created')}; candidate invocation steps "
            f"{h.get('candidate_invocation_steps')}; verified executions "
            f"{h.get('verified_execution_steps')}; verified with accepted edit "
            f"{h.get('verified_execution_with_accepted_edit_steps')}"
        )
        for rec in ev:
            lines.append(f"  - step {rec['step']}: {json.dumps(rec)}")
    comps = (
        sorted((runs_dir / "comparisons").glob("*.json"))
        if (runs_dir / "comparisons").exists()
        else []
    )
    for comp_path in comps:
        comp = _load(comp_path)
        lines += [
            "",
            f"## Comparison {comp['comparison']} (prompt {comp.get('frozen_prompt_version')})",
            "",
        ]
        lines += [
            "| pair | task | rep | mode | status | outcome | strict | cost $ | run |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for cell in comp["cells"]:
            lines.append(
                f"| {cell['pair']} | {cell['task']} | {cell['rep']} | {cell['mode']} | {cell['status']} | "
                f"{cell.get('outcome', '-')} | {cell.get('strict', '-')} | "
                f"{cell.get('cost_usd', 0):.4f} | {cell.get('run_id', cell.get('reason', ''))} |"
            )
    return "\n".join(lines) + "\n"
