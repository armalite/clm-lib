"""Offline replay attribution for jev-primitive/1 runs (experiment 014 comparison phase).

For each execution of a recorded run in which model-written code called ``clm_jev.judge``, the
step script is re-executed in the normal sandbox (DockerExecutor, no network) on an isolated
copy of the recorded pre-execution workspace (``workspace_pre/step-NNN.json``, written by the
runtime for W3 runs) and the fixtures as released at that step, with scripted Jev responses:

- ``recorded``: the recorded answers, served for requests that match a recorded request
  exactly (question, background, items). This must reproduce the accepted context revision;
- ``withheld``: every request is answered ``status: "error"`` with no answers (the code's own
  no-answer path);
- ``inverted``: each recorded probability p is served as 1 - p (a sensitivity check).

No paid call is made and no original artifact is modified; output goes to a separate folder.

The comparison of ``recorded`` and ``withheld`` says which context changes the helper's
behaviour made **dependent on its Jev inputs**. It is not evidence that an edit was correct or
that it improved the task. Classes per step:

- ``substantive``: with answers withheld the execution still exits 0 and writes context, and
  the register differs in at least one item's label (CURRENT/HISTORY) or content (cited ids or
  text beyond one constant string);
- ``marker_only``: the only register differences are one constant string added to or removed
  from items;
- ``no_register_dependence``: the register is the same either way (other entries may differ);
- ``crash_without_answers`` / ``no_edit_without_answers``: withholding the answers made the code
  fail or leave the context unchanged; this is reported separately and is not counted as
  substantive selection;
- ``not_reproduced``: the recorded replay did not reproduce the accepted revision (the step is
  then not classified).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .context import CONTEXT_FILENAME
from .executor import DockerExecutor
from .jev_primitive import JevBroker, PrimitiveLimits
from .register_score import CITE, parse_register
from .tasks import generate

VERSION = "jev-replay/1"
VARIANTS = ("recorded", "withheld", "inverted")


def _canon(question: str, background: str, items: list[dict[str, str]]) -> str:
    return json.dumps([question, background, items], sort_keys=True)


def _from_body(body: dict[str, Any]) -> tuple[str, str, list[dict[str, str]]]:
    qs = body["questions"]
    first = qs[sorted(qs, key=lambda k: int(k[1:]))[0]]
    return first["instructions"]["question"], body["state"]["background"], body["state"]["items"]


@dataclass
class _Scripted:
    """The broker's ``call``: serve scripted answers for one variant."""

    variant: str
    answers: dict[str, dict[str, float | None]]  # canonical request -> answers by item key
    mismatches: int = 0
    served: int = 0

    def __call__(self, body: dict[str, Any]) -> tuple[Any, str | None]:
        self.served += 1
        if self.variant == "withheld":
            return None, "replay: answers withheld"
        q, bg, items = _from_body(body)
        rec = self.answers.get(_canon(q, bg, items))
        if rec is None:
            self.mismatches += 1
            return None, "replay: request differs from every recorded request"
        probs = {}
        for n, it in enumerate(items):
            p = rec.get(it["key"])
            probs[f"i{n}"] = None if p is None else (1 - p if self.variant == "inverted" else p)
        return SimpleNamespace(probabilities=probs, model="replay"), None


def _entries(ws: Path) -> list[dict[str, Any]] | None:
    try:
        doc = json.loads((ws / CONTEXT_FILENAME).read_text(encoding="utf-8"))
        return [{"id": e["id"], "role": e["role"], "body": e["body"]} for e in doc["entries"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def _register(entries: list[dict[str, Any]] | None) -> list[tuple[str, tuple[str, ...], str]]:
    if not entries:
        return []
    return sorted((i.label, tuple(sorted(i.ids)), i.text.strip()) for i in parse_register(entries))


def classify(
    pre: list[dict[str, Any]] | None,
    rec: dict[str, Any],
    wit: dict[str, Any],
) -> dict[str, Any]:
    """Compare the recorded-answers and withheld-answers outcomes of one step."""
    if wit["exit_code"] != 0 or wit["timed_out"]:
        return {"class": "crash_without_answers"}
    if rec["entries"] != pre and wit["entries"] == pre:
        return {"class": "no_edit_without_answers"}
    ra, rb = _register(rec["entries"]), _register(wit["entries"])
    only_a = [x for x in ra if x not in rb]
    only_b = [x for x in rb if x not in ra]
    by_ids_a = {x[1]: x for x in only_a}
    by_ids_b = {x[1]: x for x in only_b}
    status, content, extras = [], [], set()
    for ids in sorted(set(by_ids_a) | set(by_ids_b)):
        a, b = by_ids_a.get(ids), by_ids_b.get(ids)
        if a and b and a[0] != b[0]:
            status.append({"ids": list(ids), "with_answers": a[0], "without": b[0]})
            continue
        if a and b:
            longer, shorter = (a[2], b[2]) if len(a[2]) >= len(b[2]) else (b[2], a[2])
            i = longer.find(shorter) if shorter else -1
            extra = longer[:i] + longer[i + len(shorter) :] if i >= 0 else None
            # A short, id-free string added to or removed from an item is a marker candidate;
            # anything citing an item or longer is a content change.
            if extra is not None and extra.strip() and len(extra) <= 80 and not CITE.search(extra):
                extras.add(extra.strip())
                continue
        content.append(
            {
                "ids": list(ids),
                "with_answers": a[2][:300] if a else None,
                "without": b[2][:300] if b else None,
            }
        )
    other = sorted(
        {e["id"] for e in (rec["entries"] or [])} ^ {e["id"] for e in (wit["entries"] or [])}
    )
    pre_ids = {e["id"] for e in (pre or [])}
    a_ids = {e["id"] for e in (rec["entries"] or [])}
    b_ids = {e["id"] for e in (wit["entries"] or [])}
    independent = {
        "removed_either_way": sorted((pre_ids - a_ids) & (pre_ids - b_ids)),
        "added_either_way": sorted((a_ids - pre_ids) & (b_ids - pre_ids)),
    }
    if status or content or len(extras) > 1:
        cls = "substantive"
    elif extras:
        cls = "marker_only"
    else:
        cls = "no_register_dependence"
    return {
        "class": cls,
        "status_changes": status,
        "content_changes": content,
        "constant_strings": sorted(extras),
        "entry_ids_differing": other,
        "edits_independent_of_answers": independent,
    }


def replay_run(
    run_dir: Path, out_dir: Path, executor: DockerExecutor | None = None
) -> dict[str, Any]:
    run = json.loads((run_dir / "run.json").read_text())
    task = generate(run["task"])
    if task.fixture_sha256 != run.get("fixture_sha256"):
        raise ValueError("regenerated fixtures differ from the run's fixture_sha256")
    events = [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines()]
    prim = [json.loads(p.read_text()) for p in sorted((run_dir / "jev_primitive").glob("*.json"))]
    jp = (run.get("config") or {}).get("jev_primitive") or {}
    limits = PrimitiveLimits(
        jp.get("max_calls_per_execution", 4),
        jp.get("max_calls_per_run", 30),
        jp.get("max_items", 20),
    )
    timeout = float(((run.get("config") or {}).get("limits") or {}).get("exec_timeout_s", 30))
    ex = executor or DockerExecutor(timeout_s=timeout)
    edits = {e["step"]: e for e in events if e["event"] == "context_edit"}
    released = [(e["step"], e["stage"]) for e in events if e["event"] == "stage_released"]
    steps = sorted({p["step"] for p in prim if p.get("called")})
    out: dict[str, Any] = {"version": VERSION, "run_id": run["run_id"], "steps": []}
    for step in steps:
        stage = 1 + sum(1 for s, _ in released if s < step)
        pre_files = json.loads((run_dir / "workspace_pre" / f"step-{step:03d}.json").read_text())
        incomplete = [p for p, t in pre_files.items() if t.startswith("<") and t.endswith(">")]
        answers = {
            _canon(
                p["request"]["question"], p["request"].get("background", ""), p["request"]["items"]
            ): p["answers"]
            for p in prim
            if p["step"] == step and p.get("status") == "ok"
        }
        code = (run_dir / "scripts" / f"step-{step:03d}.py").read_text()
        pre_entries = None
        variants: dict[str, Any] = {}
        for variant in VARIANTS:
            tmp = Path(tempfile.mkdtemp(prefix=f"clm-replay-{step}-{variant}-"))
            ws, fx = tmp / "workspace", tmp / "fixtures"
            ws.mkdir()
            fx.mkdir()
            try:
                for rel, text in pre_files.items():
                    if rel in incomplete:
                        continue
                    p = ws / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(text, encoding="utf-8")
                for k in range(1, stage + 1):
                    task.write_fixtures(fx, k)
                if pre_entries is None:
                    pre_entries = _entries(ws)
                call = _Scripted(variant, answers)
                broker = JevBroker(ws, call, lambda rec: None, "replay", limits)
                res = ex.run(code, ws, fx, broker=broker)
                broker.finish()
                entries = _entries(ws)
                variants[variant] = {
                    "exit_code": res.exit_code,
                    "timed_out": res.timed_out,
                    "requests_served": call.served,
                    "request_mismatches": call.mismatches,
                    "entries": entries,
                    "entries_sha": _sha(entries),
                    "stderr_tail": (res.stderr or "")[-400:],
                }
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        edit = edits.get(step)
        accepted = None
        if edit and edit.get("status") == "accepted" and edit.get("changed"):
            rev = json.loads(
                (run_dir / "context" / f"rev-{edit['after_revision']:04d}.json").read_text()
            )
            accepted = [
                {"id": e["id"], "role": e["role"], "body": e["body"]} for e in rev["entries"]
            ]
        reproduced = variants["recorded"]["entries"] == (
            accepted if accepted is not None else pre_entries
        )
        rec = {
            "step": step,
            "stage": stage,
            "edit_status": edit.get("status") if edit else "not_written",
            "reproduced": reproduced and variants["recorded"]["request_mismatches"] == 0,
            "incomplete_snapshot_files": incomplete,
            "variants": {
                v: {k: x for k, x in d.items() if k != "entries"} for v, d in variants.items()
            },
        }
        if rec["reproduced"]:
            rec["dependence"] = classify(pre_entries, variants["recorded"], variants["withheld"])
            rec["inverted"] = classify(pre_entries, variants["recorded"], variants["inverted"])
        else:
            rec["dependence"] = {"class": "not_reproduced"}
        out["steps"].append(rec)
        (out_dir / f"step-{step:03d}").mkdir(parents=True, exist_ok=True)
        for v, d in variants.items():
            (out_dir / f"step-{step:03d}" / f"{v}-context.json").write_text(
                json.dumps({"entries": d["entries"]}, indent=1), encoding="utf-8"
            )
    classes = [s["dependence"]["class"] for s in out["steps"]]
    out["summary"] = {c: classes.count(c) for c in sorted(set(classes))}
    out["note"] = (
        "Dependence of the helper's behaviour on its Jev inputs (recorded vs withheld answers), "
        "not evidence that an edit was correct or improved the task."
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "replay.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out
