"""Offline replay attribution for jev-primitive/1 runs (experiment 014; ``jev-replay/2``).

For each execution of a recorded run in which model-written code called ``clm_jev.judge``, the
step script is re-executed in the normal sandbox (DockerExecutor, no network) on an isolated
copy of the recorded pre-execution workspace (``workspace_pre/step-NNN.json``, written by the
runtime for W3 runs) and the fixtures as released at that step, with scripted Jev responses.
No paid call is made and no original artifact is modified; output goes to a separate folder.

Responses are matched **per judged item** (question, background, item key, item text): Jev
judges each item separately against the shared background, so an item the original run asked
about can be answered wherever the replayed code asks it again. Variants:

- ``recorded``: the recorded probabilities. It must reproduce the accepted revision; otherwise
  the step is ``not_reproduced`` (a nondeterminism or reconstruction problem, reported with
  its logs, never classified as dependence);
- ``withheld``: every request is answered ``status: "error"`` (tests the code's fallback
  path; a clean no-edit fallback is not, by itself, evidence that Jev selected the edits);
- ``inverted`` and ``permuted``: **valid substituted probabilities** of the same shape, keys
  and matching (1 - p; each recorded request's answers rotated by one position). Every
  served value is logged. If the substituted answers make the code ask something the
  original run never asked, no valid answer exists: the step's counterfactual is
  ``unsupported_counterfactual`` for that variant.
- ``inverted_last`` and ``permuted_last`` (post-hoc diagnostic, added after the R1 smoke and
  not part of its frozen criteria): the same substitutions applied only to the execution's last
  Jev request, so no later request can depend on them and the counterfactual is supported.

Classes (register-level comparison with the recorded outcome): ``substantive`` (an item's
CURRENT/HISTORY label or content changes), ``marker_only`` (only one constant short string
differs), ``no_register_dependence``; and, kept apart, ``crash_without_answers`` /
``no_edit_without_answers`` (withheld) and ``crash_under_substitution`` /
``no_edit_under_substitution`` / ``substitution_identical`` / ``unsupported_counterfactual``
(substitutions). A step shows **judgment-dependent selection** only if a valid substitution is
``substantive``. A static audit of the caller and helper code (register-item literals, ids of
existing items) is supporting evidence only. None of this shows an edit was correct.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .context import CONTEXT_FILENAME
from .executor import DockerExecutor
from .jev_primitive import JevBroker, PrimitiveLimits
from .register_score import CITE, parse_register
from .tasks import generate

VERSION = "jev-replay/2"
VARIANTS = ("recorded", "withheld", "inverted", "permuted", "inverted_last", "permuted_last")
SUBSTITUTIONS = ("inverted", "permuted")  # valid probabilities, same shape, keys and matching
# Post-hoc diagnostic (added after the R1 smoke; not part of R1's frozen criteria): substitute
# only the execution's last Jev request, so no later request can depend on the change and the
# counterfactual is always supported. Earlier requests get their recorded answers.
LAST_ONLY = ("inverted_last", "permuted_last")


def _canon(question: str, background: str, items: list[dict[str, str]]) -> str:
    return json.dumps([question, background, items], sort_keys=True)


def _item_key(question: str, background: str, item: dict[str, str]) -> str:
    return json.dumps([question, background, item["key"], item["text"]])


def _from_body(body: dict[str, Any]) -> tuple[str, str, list[dict[str, str]]]:
    qs = body["questions"]
    first = qs[sorted(qs, key=lambda k: int(k[1:]))[0]]
    return first["instructions"]["question"], body["state"]["background"], body["state"]["items"]


def answer_table(exchanges: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    """Per judged item (question, background, key, text): the recorded probability and the
    predefined substitutes. Jev judges each item separately against the shared background,
    so an item seen in any recorded request can be answered wherever it is asked again.
    ``permuted`` rotates each recorded request's answers by one position (a request with one
    item keeps its answer); ``inverted`` serves 1 - p."""
    table: dict[str, dict[str, float | None]] = {}
    for x in exchanges:
        q, bg, items = (
            x["request"]["question"],
            x["request"].get("background", ""),
            x["request"]["items"],
        )
        ps = [x["answers"].get(it["key"]) for it in items]
        for n, it in enumerate(items):
            p, rot = ps[n], ps[(n + 1) % len(ps)]
            table[_item_key(q, bg, it)] = {
                "recorded": p,
                "inverted": None if p is None else round(1 - p, 6),
                "permuted": rot,
            }
    return table


@dataclass
class _Scripted:
    """The broker's ``call``: serve scripted answers for one variant, item by item."""

    variant: str
    table: dict[str, dict[str, float | None]]
    substitute_from: int = 1  # 1-based request number from which the variant's values are served
    unmatched: int = 0
    served: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, body: dict[str, Any]) -> tuple[Any, str | None]:
        self.served += 1
        if self.variant == "withheld":
            self.log.append({"request": self.served, "served": "error (withheld)"})
            return None, "replay: answers withheld"
        q, bg, items = _from_body(body)
        rows = [self.table.get(_item_key(q, bg, it)) for it in items]
        if any(r is None for r in rows):
            # Not a request the original run made (the substituted answers changed what the
            # code asked): no valid answer exists, so the counterfactual is unsupported.
            self.unmatched += 1
            self.log.append(
                {
                    "request": self.served,
                    "served": "error (unmatched request)",
                    "unmatched_keys": [
                        it["key"] for it, r in zip(items, rows, strict=True) if r is None
                    ],
                }
            )
            return None, "replay: request differs from every recorded request"
        probs, logged = {}, []
        for n, (it, r) in enumerate(zip(items, rows, strict=True)):
            assert r is not None
            col = (
                self.variant.removesuffix("_last")
                if self.served >= self.substitute_from
                else "recorded"
            )
            probs[f"i{n}"] = r[col]
            logged.append({"key": it["key"], "recorded": r["recorded"], "served": r[col]})
        self.log.append({"request": self.served, "question": q[:200], "items": logged})
        return SimpleNamespace(probabilities=probs, model="replay"), None


AUDIT_LITERAL = re.compile(r"""["'](?:CURRENT|HISTORY)\s*\[""")


def audit_code(code: str, stage: int) -> dict[str, Any]:
    """Supporting static evidence (not proof): register-item string literals, and ids of items
    released before ``stage`` (existing items), named in code."""
    earlier = sorted({f"S{a}.{b}" for a, b in CITE.findall(code) if int(a) < stage})
    return {"register_literals": len(AUDIT_LITERAL.findall(code)), "earlier_item_ids": earlier}


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


def classify_substitution(
    pre: list[dict[str, Any]] | None, rec: dict[str, Any], sub: dict[str, Any]
) -> dict[str, Any]:
    """Recorded answers vs valid substituted answers for one step."""
    if sub["unmatched"]:
        return {"class": "unsupported_counterfactual", "unmatched_requests": sub["unmatched"]}
    if not sub["answers_changed"]:
        return {"class": "substitution_identical"}
    if sub["exit_code"] != 0 or sub["timed_out"]:
        return {"class": "crash_under_substitution"}
    if rec["entries"] != pre and sub["entries"] == pre:
        return {"class": "no_edit_under_substitution"}
    out = classify(pre, rec, sub)
    return out


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
        table = answer_table([p for p in prim if p["step"] == step and p.get("status") == "ok"])
        code = (run_dir / "scripts" / f"step-{step:03d}.py").read_text()
        pre_entries = None
        audit: dict[str, Any] = {}
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
                n_recorded = sum(1 for p in prim if p["step"] == step and p.get("called"))
                call = _Scripted(variant, table, n_recorded if variant in LAST_ONLY else 1)
                broker = JevBroker(ws, call, lambda rec: None, "replay", limits)
                res = ex.run(code, ws, fx, broker=broker)
                broker.finish()
                entries = _entries(ws)
                served = [i["served"] for r in call.log for i in r.get("items", [])]
                recorded = [i["recorded"] for r in call.log for i in r.get("items", [])]
                out_text = res.stdout or ""
                variants[variant] = {
                    "exit_code": res.exit_code,
                    "timed_out": res.timed_out,
                    "requests_served": call.served,
                    "request_mismatches": call.unmatched if variant == "recorded" else 0,
                    "unmatched": call.unmatched,
                    "answers_changed": served != recorded,
                    "fallback_lines": sum(
                        1 for x in out_text.splitlines() if x.startswith("FALLBACK:")
                    ),
                    "override_lines": sum(
                        1 for x in out_text.splitlines() if x.startswith("OVERRIDE:")
                    ),
                    "substitution_log": call.log,
                    "entries": entries,
                    "entries_sha": _sha(entries),
                    "stderr_tail": (res.stderr or "")[-400:],
                }
                if variant == "recorded":
                    helpers = {
                        str(f.relative_to(ws)): f.read_text(errors="replace")
                        for f in ws.rglob("*.py")
                        if f.name != "clm_jev.py"
                        and "exporter" not in f.parts
                        and "__pycache__" not in f.parts
                    }
                    audit = {
                        "caller": audit_code(code, stage),
                        "helpers": {k: audit_code(v, stage) for k, v in helpers.items()},
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
            "audit": audit,
            "variants": {
                v: {k: x for k, x in d.items() if k != "entries"} for v, d in variants.items()
            },
        }
        if rec["reproduced"]:
            rec["dependence"] = classify(pre_entries, variants["recorded"], variants["withheld"])
            rec["substitutions"] = {
                v: classify_substitution(pre_entries, variants["recorded"], variants[v])
                for v in SUBSTITUTIONS
            }
            rec["substantive_under_valid_substitution"] = any(
                x["class"] == "substantive" for x in rec["substitutions"].values()
            )
            rec["post_hoc_last_request"] = {
                v: classify_substitution(pre_entries, variants["recorded"], variants[v])
                for v in LAST_ONLY
            }
        else:
            rec["dependence"] = {"class": "not_reproduced"}
            rec["substitutions"] = {}
            rec["substantive_under_valid_substitution"] = False
            rec["post_hoc_last_request"] = {}
        out["steps"].append(rec)
        (out_dir / f"step-{step:03d}").mkdir(parents=True, exist_ok=True)
        for v, d in variants.items():
            (out_dir / f"step-{step:03d}" / f"{v}-context.json").write_text(
                json.dumps({"entries": d["entries"]}, indent=1), encoding="utf-8"
            )
    classes = [s["dependence"]["class"] for s in out["steps"]]
    out["summary"] = {c: classes.count(c) for c in sorted(set(classes))}
    out["substitution_summary"] = {
        v: dict(
            sorted(
                {
                    c: sum(
                        1 for s in out["steps"] if s["substitutions"].get(v, {}).get("class") == c
                    )
                    for c in {
                        s["substitutions"].get(v, {}).get("class", "none") for s in out["steps"]
                    }
                }.items()
            )
        )
        for v in SUBSTITUTIONS
    }
    out["stages_substantive_under_valid_substitution"] = sorted(
        {s["stage"] for s in out["steps"] if s["substantive_under_valid_substitution"]}
    )
    out["post_hoc_stages_substantive_last_request"] = sorted(
        {
            s["stage"]
            for s in out["steps"]
            if any(x.get("class") == "substantive" for x in s["post_hoc_last_request"].values())
        }
    )
    out["note"] = (
        "Dependence of the helper's behaviour on its Jev inputs: recorded vs withheld answers "
        "(fallback behaviour) and recorded vs valid substituted answers (inverted, permuted). "
        "Not evidence that an edit was correct or improved the task."
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "replay.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out
