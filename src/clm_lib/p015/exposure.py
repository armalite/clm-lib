"""The exposure report (`p015-exposure/1`): a read-only summary of exported p015 evidence.

    python -m clm_lib.p015.exposure EXPORT_DIR [--json OUT.json] [--markdown OUT.md]

`EXPORT_DIR` is a launch export (with `contract.json`, `events.jsonl` and `sessions/`) or one exported session
directory. Nothing is written into it: the report is printed, or written to the paths given.

**Per session:** the arm and version, ending and outcome; planned, attempted and completed turns; main-model and
summariser logical calls and attempts, retries, failed and refused attempts; reported tokens (total input, uncached
input, cache reads, cache writes, output) and the peak reported main-model input; the arm's own request-size estimates
(peak; and median ratio of estimate to reported input, where the arm records an estimate); the notices the model was
shown; context management (accepted and rejected edits, held outputs, refetches, rollbacks, summaries); task-tool calls,
their delivered characters and repeated calls; and provider-call time, wall time and cost (confirmed, and assumed at
the reservation).

**Per turn,** where the artifacts support it: the ending; attempts by category and their reported input, cost and
time; notices; and task-tool calls and characters.

**Units are kept apart:** `*_chars` are characters of text, `*_tokens` are tokens as the provider reported them (or as
the arm estimated them, labelled `estimated`), `*_bytes` are bytes of command output, and `*_usd` are dollars.
A measurement the artifacts do not hold is `None` (shown as "n/a"), never 0.

**How actions are attributed, and the limits:**
- **Turns of main-model attempts:** the current question is the latest question whose text appears in the request
  (every arm sends the current question; earlier ones may also appear). Summariser attempts belong to the turn of the
  next main-model attempt. Refused attempts, which have no request, belong to the turn of the previous attempt.
- **Turns of task-tool calls:** by `calls_by_turn` in order. If its counts disagree with the tool records, per-turn
  tool figures are left absent.
- **Context-management records** (edits, holds, rollbacks, summaries) carry no turn, so they are reported per session.
- **Refetches:** "repeated calls" are task-tool calls with the same tool and arguments as an earlier call; whether a
  repeat was deliberate cannot be told from the artifacts. guarded-clm's own `lite_refetch` record marks a re-call of
  an output it had held back, and is reported separately.
- **Notices** are counted in each logical call's request text (a retry resends the same request and is not counted
  twice), by the wording each arm uses.
- **Estimates:** guarded-clm's from its runtime-status line, the paper and summary arms' from their request records
  (one per logical main-model call). Estimates are of the request as the arm measured it, before the provider's count.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

REPORT_VERSION = "p015-exposure/1"
_GUARDED_SIZE = re.compile(r"request size: about ([\d,]+) of ([\d,]+) tokens")
NOTICE_PATTERNS: dict[str, dict[str, str]] = {
    "guarded_clm": {
        "pressure": "PRESSURE: the request is about",
        "over_limit": "OVER LIMIT: this request uses",
        "finish_now": "step(s) left: finish now",
    },
    "paper_clm_shell": {
        "reminder": "tokens of the",  # "... within 2,048 tokens of the 29,952-token limit"
        "rollback": "were rolled back and their results removed",
        "rollback_feedback": "from the rolled-back step: edit rejected",
        "over_limit": "nothing more can be rolled back",
        "size_nudge": "% of the",
        "finish_now": "step(s) left in this turn",
    },
    "token_tail_p015": {
        "finish_now": "step(s) left",
    },
    # paper_clm_tools keeps its notices in the history, so matching request text would count each one again on
    # every later call; its notices are counted from its trace records instead (`management`).
    "paper_clm_tools": {},
}


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _text(messages: Iterable[dict[str, Any]] | None) -> str:
    """The user-prompt text of a recorded request."""
    out = []
    for m in messages or []:
        for p in m.get("parts", []):
            if p.get("part_kind") == "user-prompt":
                c = p.get("content")
                out.append(c if isinstance(c, str) else json.dumps(c))
    return "\n".join(out)


def _arm(session: dict[str, Any]) -> str:
    if session.get("arm"):
        return str(session["arm"])
    return "guarded_clm" if "guarded_clm" in (session.get("provenance") or {}) else "unknown"


def _sum(values: Iterable[float | int | None]) -> float | int | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _usage(finished: list[dict[str, Any]]) -> dict[str, Any]:
    keys = (
        "input_tokens_total",
        "input_tokens_uncached",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
    )
    reported: list[dict[str, Any]] = [a["reported"] for a in finished if a.get("reported")]
    if not reported:
        return {k: None for k in keys}
    return {k: sum(int(r.get(k) or 0) for r in reported) for k in keys}


def session_report(run_dir: Path, *, planned_turns: int | None = None,
                   wall_s: float | None = None) -> dict[str, Any]:  # fmt: skip
    s = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    attempts = _rows(run_dir / "attempts.jsonl")
    trace = _rows(run_dir / "trace.jsonl")
    tools = _rows(run_dir / "tools.jsonl")
    turns = _rows(run_dir / "turns.jsonl")
    arm = _arm(s)
    started = [a for a in attempts if a["event"] == "started"]
    finished = [a for a in attempts if a["event"] == "finished"]
    result = s.get("launch_result") or {}
    planned = (
        planned_turns if planned_turns is not None else (s.get("launch") or {}).get("questions")
    )
    attempted = [t for t in turns if not str(t.get("error") or "").startswith("before the turn")]
    prompts = [str(t.get("prompt") or "").split("\n", 1)[0][:60] for t in turns]

    # ---- turn of each attempt
    turn_of: dict[int, int | None] = {}
    for a in sorted(started, key=lambda x: x["attempt"]):
        if a["category"] != "main":
            continue
        text = _text(a.get("messages"))
        hits = [i + 1 for i, key in enumerate(prompts) if key and key in text]
        turn_of[a["attempt"]] = max(hits) if hits else None
    order = sorted({a["attempt"] for a in attempts})
    for n in order:  # summariser: the next main attempt's turn; refused (no request): the previous attempt's turn
        if n in turn_of:
            continue
        cat = next((a["category"] for a in attempts if a["attempt"] == n), None)
        has_request = any(a["attempt"] == n for a in started)
        if cat == "summary" and has_request:
            nxt = [turn_of[m] for m in order if m > n and turn_of.get(m) is not None]
            turn_of[n] = nxt[0] if nxt else None
        else:
            prv = [turn_of[m] for m in order if m < n and turn_of.get(m) is not None]
            turn_of[n] = prv[-1] if prv else (1 if turns else None)

    # ---- estimates against reported input, per main logical call
    first_attempt: dict[int, dict[str, Any]] = {}
    for a in sorted(started, key=lambda x: x["attempt"]):
        if a["category"] == "main":
            first_attempt.setdefault(a["logical_call"], a)
    estimates: dict[int, int] = {}
    if arm == "guarded_clm":
        for lc, a in first_attempt.items():
            m = _GUARDED_SIZE.search(_text(a.get("messages")))
            if m:
                estimates[lc] = int(m.group(1).replace(",", ""))
    else:
        kind = {
            "paper_clm_shell": "paper_request",
            "paper_clm_tools": "tools_request",
            "token_tail_p015": "summary_arm_request",
        }.get(arm)
        recs = [r for r in trace if r.get("type") == kind]
        for lc, r in zip(sorted(first_attempt), recs, strict=False):
            estimates[lc] = int(r["estimated_tokens"])
    reported_main: dict[int, int] = {}
    for a in finished:
        if a["category"] == "main" and a.get("reported"):
            reported_main[a["logical_call"]] = int(a["reported"]["input_tokens_total"])
    ratios = [estimates[k] / reported_main[k] for k in estimates if reported_main.get(k)]

    # ---- notices, once per logical main call
    patterns = NOTICE_PATTERNS.get(arm, {})
    notices: Counter[str] = Counter()
    notices_by_turn: dict[int, Counter[str]] = {}
    for a in first_attempt.values():
        text = _text(a.get("messages"))
        for name, pat in patterns.items():
            if name == "reminder" and "within" not in text:
                continue
            if pat in text:
                notices[name] += 1
                notices_by_turn.setdefault(turn_of.get(a["attempt"]) or 0, Counter())[name] += 1

    # ---- context management (per session; the records carry no turn)
    types = Counter(r.get("type") for r in trace)
    management: dict[str, Any]
    if arm == "guarded_clm":
        edits = Counter(r.get("status") for r in trace if r.get("type") == "lite_edit")
        management = {"edits_accepted": edits.get("accepted", 0), "edits_rejected": edits.get("rejected", 0),
                      "edits_unchanged": edits.get("unchanged_write", 0),
                      "protected_refused": types.get("lite_protected_refused", 0),
                      "held_outputs": types.get("lite_held_back", 0), "held_output_refetches": types.get("lite_refetch", 0),
                      "over_limit_requests": types.get("lite_over_limit", 0),
                      "rollbacks": None, "summaries": None}  # fmt: skip
    elif arm == "paper_clm_shell":
        rb = [r for r in trace if r.get("type") == "paper_rollback"]
        management = {"edits_accepted": types.get("paper_edit_applied", 0), "edits_rejected": types.get("paper_edit_rejected", 0),
                      "commands": types.get("paper_command", 0),
                      "command_output_bytes": _sum(int(r.get("stdout_bytes", 0)) + int(r.get("stderr_bytes", 0))
                                                   for r in trace if r.get("type") == "paper_command"),
                      "rollback_events": len(rb), "rollbacks": sum(int(r.get("units", 0)) for r in rb),
                      "carried_rejections": _sum(r.get("carried_rejections") for r in rb),
                      "over_limit_requests": types.get("paper_over_limit", 0),
                      "held_outputs": None, "summaries": None}  # fmt: skip
    elif arm == "paper_clm_tools":
        staged = [r for r in trace if r.get("type") == "tools_edit"]
        rb = [r for r in trace if r.get("type") == "tools_rollback"]
        management = {"edits_staged": sum(1 for r in staged if r.get("status") == "staged"),
                      "edits_rejected_at_staging": sum(1 for r in staged if r.get("status") == "rejected"),
                      "edits_committed": types.get("tools_commit", 0),
                      "edits_rejected_at_commit": types.get("tools_commit_rejected", 0),
                      "rollback_events": len(rb), "rollbacks": _sum(r.get("steps_undone") for r in rb),
                      "final_turns_given": sum(1 for r in rb if r.get("final_turn")),
                      "reminders": types.get("tools_reminder", 0),
                      "held_outputs": None, "summaries": None}  # fmt: skip
    elif arm == "token_tail_p015":
        management = {"pressure_triggers": types.get("pressure", 0),  # the arm summarises; the model is shown nothing
                      "summaries": types.get("summary_applied", 0), "summaries_too_large": types.get("summary_too_large", 0),
                      "summaries_empty": types.get("summary_empty", 0), "summary_cap_reached": types.get("summary_call_cap", 0),
                      "edits_accepted": None, "held_outputs": None, "rollbacks": None}  # fmt: skip
    else:
        management = {}
    management["context_limit_records"] = types.get("context_limit", 0)

    # ---- task tools
    keys = [json.dumps([t.get("tool"), t.get("arguments")], sort_keys=True) for t in tools]
    seen: set[str] = set()
    repeats = 0
    for k in keys:
        repeats += k in seen
        seen.add(k)
    calls_by_turn = s.get("calls_by_turn")
    tool_turns: list[dict[str, Any]] | None = None
    if calls_by_turn is not None and sum(len(c) for c in calls_by_turn) == len(tools):
        tool_turns, i = [], 0
        for c in calls_by_turn:
            chunk = tools[i : i + len(c)]
            tool_turns.append(
                {
                    "calls": len(chunk),
                    "delivered_chars": sum(len(t.get("delivered", "")) for t in chunk),
                }
            )
            i += len(c)

    # ---- per turn
    per_turn = []
    for t in turns:
        n = t["turn"]
        mine = [a for a in finished if turn_of.get(a["attempt"]) == n]
        main = [a for a in mine if a["category"] == "main"]
        rep = [int(a["reported"]["input_tokens_total"]) for a in main if a.get("reported")]
        per_turn.append({
            "turn": n,
            "ending": t.get("ending"),
            "attempts": {"main": len(main), "summary": len(mine) - len(main)},
            "retries": sum(1 for a in mine if a.get("retry")),
            "peak_reported_input_tokens": max(rep) if rep else None,
            "reported_input_tokens": sum(rep) if rep else None,
            "provider_time_s": _sum(a.get("latency_s") for a in mine),
            "charged_usd": round(sum(float(a.get("charged_usd") or 0) for a in mine), 6),
            "notices": dict(notices_by_turn.get(n, {})),
            "tool_calls": tool_turns[n - 1]["calls"] if tool_turns and n <= len(tool_turns) else None,
            "tool_delivered_chars": tool_turns[n - 1]["delivered_chars"] if tool_turns and n <= len(tool_turns) else None,
        })  # fmt: skip

    def logical(cat: str) -> int:
        return len({a["logical_call"] for a in finished if a["category"] == cat})

    confirmed = sum(
        float(a.get("charged_usd") or 0)
        for a in finished
        if a.get("cost_basis") == "provider_usage"
    )
    assumed = sum(float(a.get("charged_usd") or 0) for a in finished
                  if a.get("cost_basis") not in (None, "provider_usage"))  # fmt: skip
    main_reported = [int(a["reported"]["input_tokens_total"]) for a in finished
                     if a["category"] == "main" and a.get("reported")]  # fmt: skip
    settings = s.get("settings") or {}
    return {
        "run_id": s.get("run_id"),
        "arm": arm,
        "arm_version": settings.get("variant")
        or (
            f"guarded-clm {lib['version']}"
            if (lib := (s.get("provenance") or {}).get("guarded_clm"))
            else None
        ),
        "task_version": (s.get("launch") or {}).get("task_version"),
        "ending": (result.get("endings") or [None])[-1],
        "endings": result.get("endings"),
        "outcome": result.get("outcome"),
        "score": {
            "points": (s.get("score") or {}).get("points"),
            "out_of": (s.get("score") or {}).get("out_of"),
            "recall_points": (s.get("score") or {}).get("recall_points"),
        },
        "turns": {
            "planned": planned,
            "attempted": len(attempted),
            "completed": sum(1 for t in turns if t.get("ending") == "completed"),
        },
        "calls": {
            "main_logical": logical("main"),
            "summary_logical": logical("summary"),
            "attempts": len(finished),
            "retries": sum(1 for a in finished if a.get("retry")),
            "failed": sum(1 for a in finished if a.get("outcome") in ("error", "interrupted")),
            "refused": sum(1 for a in finished if a.get("outcome") == "refused"),
            "truncated": sum(1 for a in finished if "output_truncated" in a),
        },
        "tokens": {
            **_usage(finished),
            "peak_main_input_tokens": max(main_reported) if main_reported else None,
        },
        "estimates": {
            "source": "runtime status" if arm == "guarded_clm" else "request records",
            "peak_estimated_tokens": max(estimates.values()) if estimates else None,
            "median_estimate_to_reported": round(statistics.median(ratios), 4) if ratios else None,
            "compared_calls": len(ratios),
        },
        "notices": dict(notices),
        "management": management,
        "tools": {
            "calls": len(tools),
            "delivered_chars": sum(len(t.get("delivered", "")) for t in tools),
            "repeated_calls": repeats,
        },
        "time": {
            "provider_time_s": round(sum(float(a.get("latency_s") or 0) for a in finished), 3),
            "wall_s": wall_s,
        },
        "cost": {
            "confirmed_usd": round(confirmed, 6),
            "assumed_usd": round(assumed, 6),
            "total_usd": round(confirmed + assumed, 6),
        },
        "per_turn": per_turn,
    }


def _wall_times(events: list[dict[str, Any]]) -> dict[str, float]:
    starts: dict[str, datetime] = {}
    out: dict[str, float] = {}
    for e in events:
        if e.get("event") == "session_started":
            starts[e["session"]] = datetime.fromisoformat(e["ts"])
        elif e.get("event") == "session_ended" and e.get("session") in starts:
            out[e["session"]] = (
                datetime.fromisoformat(e["ts"]) - starts[e["session"]]
            ).total_seconds()
    return out


def report(export_dir: Path) -> dict[str, Any]:
    if (export_dir / "session.json").exists():
        return {
            "format": REPORT_VERSION,
            "launch_id": None,
            "sessions": [session_report(export_dir)],
        }
    contract = json.loads((export_dir / "contract.json").read_text(encoding="utf-8"))
    walls = _wall_times(_rows(export_dir / "events.jsonl"))
    sessions = []
    for d in sorted((export_dir / "sessions").iterdir()):
        sid = d.name.split(".")[1] if d.name.count(".") >= 2 else None
        sessions.append(
            session_report(d, planned_turns=int(contract["questions"]), wall_s=walls.get(sid or ""))
        )
    return {"format": REPORT_VERSION, "launch_id": contract.get("launch_id"), "sessions": sessions}


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:,.6f}".rstrip("0").rstrip(".") if abs(v) < 100 else f"{v:,.1f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def markdown(rep: dict[str, Any]) -> str:
    rows = [("arm (version)", lambda s: f"{s['arm']} ({s['arm_version']})"), ("outcome", lambda s: s["outcome"]),
            ("turns planned / attempted / completed", lambda s: "{planned} / {attempted} / {completed}".format(
                **{k: _fmt(v) for k, v in s["turns"].items()})),
            ("score (points / recall)", lambda s: f"{_fmt(s['score']['points'])}/{_fmt(s['score']['out_of'])}, "
                                                  f"{_fmt(s['score']['recall_points'])}/3"),
            ("main / summariser logical calls", lambda s: f"{s['calls']['main_logical']} / {s['calls']['summary_logical']}"),
            ("attempts (retries, failed, refused, truncated)", lambda s: "{attempts} ({retries}, {failed}, {refused}, "
                                                                         "{truncated})".format(**s["calls"])),
            ("peak reported main input (tokens)", lambda s: _fmt(s["tokens"]["peak_main_input_tokens"])),
            ("peak estimated request (tokens)", lambda s: _fmt(s["estimates"]["peak_estimated_tokens"])),
            ("input / cache read / cache write / output (tokens)", lambda s: " / ".join(_fmt(s["tokens"][k]) for k in (
                "input_tokens_total", "cache_read_tokens", "cache_write_tokens", "output_tokens"))),
            ("notices", lambda s: ", ".join(f"{k} {v}" for k, v in sorted(s["notices"].items())) or "none"),
            ("context management", lambda s: ", ".join(f"{k} {_fmt(v)}" for k, v in s["management"].items())),
            ("task-tool calls / delivered chars / repeated", lambda s: f"{s['tools']['calls']} / "
                f"{_fmt(s['tools']['delivered_chars'])} / {s['tools']['repeated_calls']}"),
            ("provider time / wall time (s)", lambda s: f"{_fmt(s['time']['provider_time_s'])} / {_fmt(s['time']['wall_s'])}"),
            ("cost confirmed / assumed (USD)", lambda s: f"{_fmt(s['cost']['confirmed_usd'])} / {_fmt(s['cost']['assumed_usd'])}")]  # fmt: skip
    head = "| | " + " | ".join(s["run_id"] or "?" for s in rep["sessions"]) + " |"
    lines = [f"# Exposure report ({REPORT_VERSION}): {rep['launch_id'] or 'one session'}", "", head,
             "| --- |" + " --- |" * len(rep["sessions"])]  # fmt: skip
    for label, get in rows:
        lines.append(f"| {label} | " + " | ".join(get(s) for s in rep["sessions"]) + " |")
    for s in rep["sessions"]:
        lines += ["", f"## {s['run_id']}: per turn", "",
                  "| turn | ending | attempts main / summary | retries | peak reported input (tokens) | cost (USD) | "
                  "notices | tool calls | tool chars |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]  # fmt: skip
        for t in s["per_turn"]:
            lines.append(
                f"| {t['turn']} | {t['ending']} | {t['attempts']['main']} / {t['attempts']['summary']} | {t['retries']} | "
                f"{_fmt(t['peak_reported_input_tokens'])} | {_fmt(t['charged_usd'])} | "
                f"{', '.join(f'{k} {v}' for k, v in sorted(t['notices'].items())) or 'none'} | {_fmt(t['tool_calls'])} | "
                f"{_fmt(t['tool_delivered_chars'])} |"
            )  # fmt: skip
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("export_dir", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    rep = report(args.export_dir)
    for target in (args.json, args.markdown):
        if target is not None and target.resolve().is_relative_to(args.export_dir.resolve()):
            print("refused: the report is never written into the export", file=sys.stderr)
            return 2
    if args.json:
        args.json.write_text(json.dumps(rep, indent=1) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(markdown(rep), encoding="utf-8")
    if not args.json and not args.markdown:
        print(markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
