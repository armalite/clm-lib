"""Experiment 011 agent pilot: per-run table and a mechanical edit screen (exploratory).

Usage: python experiments/011-jev-edit-check/pilot_analysis.py <comparison.json> [--verdicts]

The edit screen lists, for every applied context edit (A: accepted model edits; B/C: accepted
proposals and OVER LIMIT recovery edits), the requirement terms of the rules then in force that
were present in the context before the edit and absent from the whole context after it. It is a
screen for the author's rubric review (protocol "Edit-level analysis"), not a judgement: a term
can be dropped harmlessly (the rule is implemented, or rereadable from the fixture files), and a
loss can occur without a listed term disappearing. Jev's verdicts are printed only with
``--verdicts``, so the screen can be reviewed before they are read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clm_lib.tasks import generate  # noqa: E402
from clm_lib.w2 import config_at  # noqa: E402

# Distinctive terms an agent's notes would need to carry each rule (lower-cased match).
TERMS = {
    "FORMAT": ["json"],
    "PERM": ["viewer", "permissionerror"],
    "STATUS": ["include_archived"],
    "CONF": ["audit"],
    "SINCE": ["since"],
    "OBLIG": ["exported_by"],
    "LIMIT": ["max_records", "truncated"],
}
PARTIAL = {"PERM": ["owner", "own"], "STATUS": ["admin"]}
COND = {"off": "A", "control": "B", "jev": "C"}


def text(entries: list[dict]) -> str:
    return "\n".join(e["body"] for e in entries).lower()


def revisions(run: Path) -> dict[int, list[dict]]:
    return {
        json.loads(p.read_text())["number"]: json.loads(p.read_text())["entries"]
        for p in sorted((run / "context").glob("rev-*.json"))
    }


def edit_screen(run: Path, m: dict) -> list[dict]:
    task = generate(m["task"])
    sched = task.truth.schedule
    revs = revisions(run)
    events = [json.loads(x) for x in (run / "events.jsonl").read_text().splitlines()]
    stage_at = {}
    stage = 1
    for e in events:
        if e["event"] == "stage_released":
            stage = e["stage"]
        if "step" in e:
            stage_at.setdefault(e["step"], stage)
    rows = []
    for e in events:
        if e["event"] != "context_edit" or not e.get("changed"):
            continue
        before, after = revs[e["before_revision"]], revs[e["after_revision"]]
        st = stage_at.get(e["step"], 1)
        cfg = config_at(sched, st)
        dropped = []
        for module in cfg:
            if module == "FORMAT" and cfg[module] == 1:
                continue  # "json" is a requirement term only once the format is JSON
            terms = TERMS[module] + (PARTIAL.get(module, []) if cfg[module] > 1 else [])
            for t in terms:
                if t in text(before) and t not in text(after):
                    dropped.append(f"{module}:{t}")
        rereads = [
            x["step"]
            for x in events
            if x["event"] == "execution_start"
            and x["step"] > e["step"]
            and "REQUIREMENTS.md" in (run / x["script"]).read_text()
        ]
        rows.append(
            {
                "step": e["step"],
                "stage": st,
                "chars": f"{e['chars_before']}->{e['chars_after']}",
                "removed": len(e["removed"]),
                "rewritten": len(e["rewritten"]),
                "dropped_terms": dropped,
                "later_requirement_reads": rereads[:6],
            }
        )
    return rows


def main() -> None:
    comp = json.loads(Path(sys.argv[1]).read_text())
    show = "--verdicts" in sys.argv
    runs_dir = ROOT / "runs"
    for cell in comp["cells"]:
        if "run_id" not in cell:
            print(cell)
            continue
        run = runs_dir / cell["run_id"]
        m = json.loads((run / "summary.json").read_text())
        s, ec, u = m["score"], m["edit_check"], m["usage_provider_reported"]
        cond = COND[ec["mode"]]
        cats = {k: f"{v['passed']}/{v['total']}" for k, v in s["by_category"].items()}
        print(
            f"\n== {m['task']} {cond} (block {cell['block']} pos {cell['position']}) "
            f"{m['status']} strict={s['strict_success']} {s['passed']}/{s['checks']}"
        )
        print(f"   categories {cats} stale {s['stale_modules']} failed {s['failed_modules']}")
        print(
            f"   cost {m['cost']['by_provider']} calls {m['counts']['provider_calls']} "
            f"steps {m['steps']} elapsed {m['elapsed_s']}s stages {m['management']['stages_released']}"
        )
        print(
            f"   tokens in {u['input_tokens_uncached']} cache_read {u['cache_read_input_tokens']} "
            f"cache_write {u['cache_creation_input_tokens']} out {u['output_tokens']}"
        )
        c = m["counts"]
        print(
            f"   edits accepted {c['edits_accepted_changed']} rejected {c['edits_rejected']} "
            f"pressure_steps {c['pressure_steps']} recovery {c['recovery_requests']} "
            f"spills {c['spills']} repairs {c['repair_calls']}"
        )
        print(
            f"   proposals {ec['proposals']} {ec['proposals_by_status']} confirms "
            f"{ec['confirm_actions']} (invalid {ec['confirm_invalid']}) recovery_exempt "
            f"{ec['recovery_exempt_edits']} req_reads {ec['requirement_reads']} "
            f"test_runs {ec['test_runs']}"
        )
        if show:
            print(f"   verdicts {ec['verdicts']} reactions {ec['reactions_by_verdict']}")
        for row in edit_screen(run, m):
            print("   edit", row)


if __name__ == "__main__":
    main()
