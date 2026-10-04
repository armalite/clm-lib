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


def rescore(run_dir: Path) -> dict[str, Any] | None:
    """Re-score a run's recorded final answer with the current scorer (never overwrites)."""
    from .tasks import SCORER_VERSION, Truth, score_answer

    answer = next((e["answer"] for e in _events(run_dir) if e.get("event") == "final_answer"), None)
    truth_path = run_dir / "evaluator" / "truth.json"
    if not truth_path.exists():
        return None
    t = _load(truth_path)
    if t.get("kind") == "coding":
        # Coding checks are deterministic data; the recorded score is the current score.
        score: dict[str, Any] = _load(run_dir / "evaluator" / "score.json")
        return score
    if answer is None:
        return None
    truth = Truth(
        cause=t["cause"],
        remedies=t["remedies"],
        value_variants=t["value_variants"],
        stale_variants=t["stale_variants"],
        evidence_groups={k: [tuple(x) for x in v] for k, v in t["evidence_groups"].items()},
        stale_causes=t.get("stale_causes", []),
    )
    fx = run_dir / "fixtures"
    files = {
        str(p.relative_to(fx)): p.read_text(encoding="utf-8") for p in fx.rglob("*") if p.is_file()
    }
    sc = score_answer(answer, truth, files)
    return {"scorer_version": SCORER_VERSION, **sc.to_dict()}


def bound_checks(run_dir: Path) -> tuple[int, int]:
    """(responses with bound_held recorded, of which held)."""
    rs = [e for e in _events(run_dir) if e.get("event") == "response" and "bound_held" in e]
    return len(rs), sum(1 for e in rs if e["bound_held"])


def build_report(
    runs_dir: Path,
    ledger_path: Path,
    run_dirs: list[Path] | None = None,
    comparisons: list[Path] | None = None,
    title: str = "clm-lib run report",
) -> str:
    """Markdown report. By default covers every run under ``runs_dir``; ``run_dirs`` and
    ``comparisons`` restrict it (ledger entries are then filtered to those run ids)."""
    runs = []
    dirs = (
        run_dirs
        if run_dirs is not None
        else [p.parent for p in sorted(runs_dir.glob("*/summary.json"))]
    )
    for d in dirs:
        if (d / "summary.json").exists():
            runs.append((d, _load(d / "summary.json")))
    lines = [
        f"# {title}",
        "",
        "Generated by `clm-lib report` from run summaries, events and the ledger.",
        "",
    ]
    if ledger_path.exists():
        led = _load(ledger_path)
        entries = led.get("entries", [])
        if run_dirs is not None:
            wanted = {d.name for d in run_dirs}
            entries = [e for e in entries if e["run_id"] in wanted]
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
        "Outcome columns: recorded at run time (score/1) and re-scored from the same answer with "
        "the current scorer (score/2, post-hoc `setting=value` correction).",
        "",
        "| run | label | mode | task | status | outcome (recorded) | outcome (current scorer) | strict (current) | components: cause/remedy/value/evidence, or coding checks by category | calls (act/rep/sum/retry) | exec | edits ok/unch/rej | summaries | pressure steps | in/out tokens | peak req tok (rep) | bound held | cost $ | elapsed s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    rescored: dict[str, dict[str, Any] | None] = {}
    for run_dir, m in runs:
        c, sc = m["counts"], m["score"]
        rs = rescore(run_dir)
        rescored[m["run_id"]] = rs
        if rs and "by_category" in rs:  # coding task: evaluator checks by requirement category
            comp_str = f"{rs['passed']}/{rs['checks']} checks; " + "; ".join(
                f"{k} {v['passed']}/{v['total']}" + (f" (stale {v['stale']})" if v["stale"] else "")
                for k, v in sorted(rs["by_category"].items())
            )
        else:
            comp = (
                {
                    "cause": float(rs["cause_ok"]),
                    "remedy": float(rs["remedy_ok"]),
                    "value": float(rs["value_status"] == "exact"),
                    "evidence": sum(rs["groups_covered"].values())
                    / max(1, len(rs["groups_covered"])),
                }
                if rs
                else sc["components"]
            )
            comp_str = (
                f"{comp.get('cause', 0):.0f}/{comp.get('remedy', 0):.0f}/"
                f"{comp.get('value', 0):.0f}/{comp.get('evidence', 0):.2f}"
            )
        u = m["usage_provider_reported"]
        n, held = bound_checks(run_dir)
        lines.append(
            f"| {m['run_id']} | {m.get('label', '')} | {m['mode']} | {m['task']} | {m['status']} | "
            f"{sc['outcome']} | {rs['outcome'] if rs else 'no answer'} | "
            f"{rs['strict_success'] if rs else False} | {comp_str} | {c['provider_calls']} ({c['action_calls']}/"
            f"{c['repair_calls']}/{c['summary_calls']}/{c['api_retries']}) | {c['executions']} | "
            f"{c['edits_accepted_changed']}/{c['edits_unchanged_write']}/{c['edits_rejected']} | "
            f"{c['summaries_applied']} | {c['pressure_steps']} | {u['input_tokens_uncached']}/"
            f"{u['output_tokens']} | {m['peak_request_input_tokens_reported']} | {held}/{n} | "
            f"{m['cost']['incurred_usd']:.4f} | {m['elapsed_s']} |"
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
        comparisons
        if comparisons is not None
        else sorted((runs_dir / "comparisons").glob("*.json"))
        if (runs_dir / "comparisons").exists()
        else []
    )
    summaries = {m["run_id"]: m for _, m in runs}
    for comp_path in comps:
        comp = _load(comp_path)
        missing = [
            c["run_id"] for c in comp["cells"] if c.get("run_id") and c["run_id"] not in summaries
        ]
        for rid in missing:  # comparison runs outside the selected set
            if (runs_dir / rid / "summary.json").exists():
                summaries[rid] = _load(runs_dir / rid / "summary.json")
        lines += [
            "",
            f"## Comparison {comp['comparison']} (prompt {comp.get('frozen_prompt_version')})",
            "",
        ]
        for arm in ("summary", "clm"):
            ms = [
                (summaries[c["run_id"]], rescored.get(c["run_id"]))
                for c in comp["cells"]
                if c["mode"] == arm and c.get("run_id")
            ]
            if not ms:
                continue
            k = len(ms)
            lines.append(
                f"- {arm}: {k} runs; strict success score/1 "
                f"{sum(m['score']['strict_success'] for m, _ in ms)}/{k}, score/2 "
                f"{sum(bool(r and r['strict_success']) for _, r in ms)}/{k}; mean cost "
                f"${sum(m['cost']['incurred_usd'] for m, _ in ms) / k:.4f}; mean calls "
                f"{sum(m['counts']['provider_calls'] for m, _ in ms) / k:.2f}; edits "
                f"{sum(m['counts']['edits_accepted_changed'] for m, _ in ms)}; summaries "
                f"{sum(m['counts']['summaries_applied'] for m, _ in ms)}; mean input tokens "
                f"{sum(m['usage_provider_reported']['input_tokens_uncached'] for m, _ in ms) / k:.0f}"
            )
        lines.append("")
        lines += [
            "| pair | task | rep | mode | status | outcome score/1 | strict score/1 | cost $ | run |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for cell in comp["cells"]:
            lines.append(
                f"| {cell['pair']} | {cell['task']} | {cell['rep']} | {cell['mode']} | {cell['status']} | "
                f"{cell.get('outcome', '-')} | {cell.get('strict', '-')} | "
                f"{cell.get('cost_usd', 0):.4f} | {cell.get('run_id', cell.get('reason', ''))} |"
            )
    return "\n".join(lines) + "\n"
