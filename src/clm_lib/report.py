"""Markdown report built only from recorded run evidence and the ledger."""

from __future__ import annotations

import json
import re
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


def _revision(run_dir: Path, number: int | None) -> list[dict[str, str]] | None:
    """Entries of a saved revision; [] for revision 0 or an empty revision; None if missing."""
    if number == 0:
        return []  # the initial empty revision is never written to disk
    if number is None:
        return None
    path = run_dir / "context" / f"rev-{number:04d}.json"
    if not path.exists():
        return None
    entries: list[dict[str, str]] = _load(path)["entries"]
    return entries


# Runtime-generated observation framing; too generic to count as removed content.
_BOILERPLATE = re.compile(
    r"^(exit_code=\S+ duration=|--- (stdout|stderr) ---$|\[(stdout|stderr) truncated"
    r"|\[step \d+\] execute$|thought:|code:$)"
)
STRONG_MIN = 40  # lines this long are distinctive
WEAK_MIN = 8  # shorter lines down to this length are checked but may match by coincidence


def removed_text_check(body: str, sent: list[dict[str, str]]) -> dict[str, Any]:
    """Search the next request's entries for text from a removed entry.

    Every non-boilerplate line of at least WEAK_MIN characters is checked. Coverage is
    the share of the removed body's characters that were checked; unchecked text
    (short lines, boilerplate) is reported, never assumed absent.
    """
    lines = [ln.strip() for ln in body.splitlines()]
    checked = [
        ln for ln in dict.fromkeys(lines) if len(ln) >= WEAK_MIN and not _BOILERPLATE.match(ln)
    ]
    checked_chars = sum(len(ln) * lines.count(ln) for ln in checked)
    strong = [ln for ln in checked if len(ln) >= STRONG_MIN and any(ln in e["body"] for e in sent)]
    weak = [ln for ln in checked if len(ln) < STRONG_MIN and any(ln in e["body"] for e in sent)]
    where = sorted({e["id"] for e in sent for ln in strong + weak if ln in e["body"]})
    return {
        "body_chars": len(body),
        "checked_chars": checked_chars,
        "coverage": round(checked_chars / len(body), 3) if body else 1.0,
        "lines_checked": len(checked),
        "whole_body_reappears": bool(body) and any(body in e["body"] for e in sent),
        "strong_hits": len(strong),
        "weak_hits": len(weak),
        "in": where,
    }


def edit_evidence(run_dir: Path) -> list[dict[str, Any]]:
    """Check each accepted content-changing edit against the very next action request.

    Structural check: removed ids absent and added ids present in the next request.
    Content check: the next request's working context starts with exactly the accepted
    revision's entries (an empty revision is a valid, trivially matching prefix), and
    rewritten entries carry their new body. Removed-text check: see removed_text_check;
    its coverage is reported because it cannot prove that all removed text is absent.
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
        if after is None:
            rec["content_prefix_equals_accepted_revision"] = None
            rec["content_verdict"] = "unverifiable: accepted revision artifact missing"
            out.append(rec)
            continue
        prefix_ok = sent[: len(after)] == after
        rec["accepted_revision_entries"] = len(after)
        rec["content_prefix_equals_accepted_revision"] = prefix_ok
        rec["appended_after_edit"] = ids[len(after) :] if prefix_ok else None
        new_bodies = {e["id"]: e["body"] for e in sent}
        if before is None:
            rec["rewritten_verified"] = None
            rec["removed_text"] = "unavailable: pre-edit revision artifact missing"
        else:
            old_bodies = {e["id"]: e["body"] for e in before}
            rec["rewritten_verified"] = all(
                new_bodies.get(r) not in (None, old_bodies.get(r)) for r in rec["rewritten"]
            )
            rec["removed_text"] = {
                rid: removed_text_check(old_bodies.get(rid, ""), sent) for rid in rec["removed"]
            }
        if not (
            prefix_ok
            and rec["structural_removed_absent"]
            and rec["rewritten_verified"] is not False
        ):
            rec["content_verdict"] = "mismatch"
        elif not isinstance(rec["removed_text"], dict):
            rec["content_verdict"] = "replaced; removed-text check unavailable"
        else:
            checks = list(rec["removed_text"].values())
            strong = sum(c["strong_hits"] for c in checks)
            weak = sum(c["weak_hits"] for c in checks)
            cov = (
                sum(c["checked_chars"] for c in checks)
                / max(1, sum(c["body_chars"] for c in checks))
                if checks
                else 1.0
            )
            if strong:
                rec["content_verdict"] = "replaced; removed text reappears in the next request"
            elif weak:
                rec["content_verdict"] = (
                    "replaced; short removed lines reappear (possibly coincidental)"
                )
            else:
                rec["content_verdict"] = (
                    f"replaced; no checked removed text found (checked {cov:.0%} of removed chars)"
                )
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
            f"{h.get('candidate_invocation_steps')}; helper function executions "
            f"{h.get('function_execution_steps')}; accepted edits written from helper code "
            f"{h.get('helper_written_accepted_edit_steps')}"
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
