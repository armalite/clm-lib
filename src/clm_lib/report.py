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


def _context_ids(payload: dict[str, Any]) -> list[str]:
    user = payload["messages"][0]["content"]
    block = user.split("<working_context>\n", 1)[1].split("\n</working_context>", 1)[0]
    return [json.loads(line)["id"] for line in block.splitlines() if line.strip()]


def edit_evidence(run_dir: Path) -> list[dict[str, Any]]:
    """For each accepted content-changing edit, check the next action request.

    Verifies, from saved payloads, that ids removed by the edit are absent from the
    very next action request while the event log still holds their original text.
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
        rec: dict[str, Any] = {
            "step": ev["step"],
            "removed": ev.get("removed", []),
            "added": ev.get("added", []),
            "rewritten": ev.get("rewritten", []),
            "chars": f"{ev.get('chars_before')} -> {ev.get('chars_after')}",
        }
        if nxt is None:
            rec["next_request"] = "none (run ended)"
        else:
            payload = _load(run_dir / nxt["file"])["payload"]
            ids = _context_ids(payload)
            rec["next_request"] = nxt["file"]
            rec["removed_absent_from_next_request"] = all(r not in ids for r in rec["removed"])
            rec["added_present_in_next_request"] = all(a in ids for a in rec["added"])
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
            f"helpers created {h['created']}, invocations {len(h['invocations'])}, revisions {len(h['revised'])}"
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
