"""Register scorer ``register-checks/1`` for the W3 workload (experiment 014 comparison phase).

Scores the requirements register that the W3 task asks the agent to keep in its working
context. It runs on the host after the run, on accepted context revisions only, and reads the
evaluator-only truth (``w3.register_truth``) and semantic probes; nothing here is shown to the
agent or sent to Jev.

**Assessment timing** (frozen in the protocol): stage k is assessed on the accepted revision
that is live when the agent releases stage k+1 (it has had its turns to process stage k), and
the final stage on the revision live at the accepted final answer.

**What a register item is:** any line, in an entry that the runtime did not write (runtime step
entries ``sN.act``, ``sN.obs`` and ``sN.rcpt`` are ignored), that starts with ``CURRENT`` or
``HISTORY`` (upper case; ``Current:``/``History:`` with a colon also count), optionally after a
bullet. The item runs until the next marker line. A marker line that cites no item id is a
section header: each following bullet line is then its own item with that label. Cited ids are
``S<stage>.<n>`` anywhere in the item.

**Per released item, after the stage:**
- in force and cited in a CURRENT item: ``retained`` (no amendments) or ``amended_ok`` (every
  amendment in force is also represented in CURRENT), else ``stale_unamended``; if the CURRENT
  text citing it lacks the item's semantic probes (distinctive literals such as a field name or
  value), also ``content_missing``;
- in force, not cited, but its probes appear in a CURRENT item citing another in-force item of
  the same rule: ``represented_uncited`` (an accepted alternative representation);
- in force and only in HISTORY: ``lost_obligation`` / ``lost_exception`` for obligations and the
  confidential exception, else ``incorrect_retirement``;
- in force and absent: ``lost_obligation`` / ``lost_exception`` as above, else ``omitted``;
- replaced or withdrawn, and cited in a CURRENT item that cites neither an in-force item of the
  same rule nor its replacing item: ``stale_superseded``; kept somewhere (HISTORY, or a CURRENT
  item together with its successor): ``history_kept``, else ``history_lost``;
- decision records: in HISTORY ``history_kept``; only in CURRENT ``history_as_current``; absent
  ``history_lost``. Information items are not scored.

**Limits:** labels, citations and probe literals are evidence of meaning, not proof of it. A
fluent but wrong paraphrase that keeps the probe literal is not caught; the code checks score
the implementation separately, and the analysis reads the final registers by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

VERSION = "register-checks/1"
RUNTIME_ID = re.compile(r"^s\d+\.(act|obs|rcpt)$")
MARKER = re.compile(
    r"^\s*(?:[-*•>]|\d+[.)])?\s*[\[(]?\*{0,2}(?:(CURRENT|HISTORY)\b|(Current|History|current|history)\s*:)"
)
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
CITE = re.compile(r"\bS(\d+)\.(\d+)\b")
HARM = (
    "stale_superseded",
    "stale_unamended",
    "lost_obligation",
    "lost_exception",
    "incorrect_retirement",
)
HISTORY_ERRORS = ("history_as_current", "history_lost")
INCOMPLETE = ("omitted", "content_missing")
OK = ("retained", "amended_ok", "represented_uncited", "history_kept")
CATEGORIES = OK + HARM + HISTORY_ERRORS + INCOMPLETE


@dataclass
class RegisterItem:
    label: str  # CURRENT | HISTORY
    text: str
    entry_id: str

    @property
    def ids(self) -> set[str]:
        return {f"S{a}.{b}" for a, b in CITE.findall(self.text)}


def _label(line: str) -> str | None:
    m = MARKER.match(line)
    if not m:
        return None
    return (m.group(1) or m.group(2)).upper()


def parse_register(entries: list[dict[str, Any]]) -> list[RegisterItem]:
    """Register items in model-written entries (see the module docstring)."""
    items: list[RegisterItem] = []
    for e in entries:
        if RUNTIME_ID.match(str(e.get("id", ""))):
            continue
        section: str | None = None
        current: RegisterItem | None = None
        for line in str(e.get("body", "")).splitlines():
            label = _label(line)
            if label is not None:
                section = None
                if CITE.search(line):
                    current = RegisterItem(label, line, e["id"])
                    items.append(current)
                else:  # a section header: following bullets are its items
                    section, current = label, None
                continue
            if section is not None and BULLET.match(line):
                current = RegisterItem(section, line, e["id"])
                items.append(current)
            elif current is not None and line.strip():
                current.text += "\n" + line
    return items


@dataclass
class StageScore:
    stage: int
    revision: int | None
    items: int
    by_item: dict[str, list[str]] = field(default_factory=dict)  # item id -> categories

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(CATEGORIES, 0)
        for cats in self.by_item.values():
            for c in cats:
                out[c] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        c = self.counts()
        return {
            "stage": self.stage,
            "revision": self.revision,
            "register_items": self.items,
            "counts": c,
            "harm": sum(c[k] for k in HARM),
            "history_errors": sum(c[k] for k in HISTORY_ERRORS),
            "incomplete": sum(c[k] for k in INCOMPLETE),
            "by_item": self.by_item,
        }


def _probes_ok(text: str, groups: list[list[str]]) -> bool:
    low = text.lower()
    return all(any(lit in low for lit in g) for g in groups)


def score_stage(
    entries: list[dict[str, Any]],
    truth: dict[str, dict[str, Any]],
    probes: dict[str, list[list[str]]],
    stage: int,
    revision: int | None = None,
) -> StageScore:
    items = parse_register(entries)
    cur = [i for i in items if i.label == "CURRENT"]
    his = [i for i in items if i.label == "HISTORY"]
    in_force_by_module: dict[str, set[str]] = {}
    for iid, t in truth.items():
        if t["in_force"] and t["module"]:
            in_force_by_module.setdefault(t["module"], set()).add(iid)
    out = StageScore(stage, revision, len(items))

    def cited(iid: str, group: list[RegisterItem]) -> list[RegisterItem]:
        return [i for i in group if iid in i.ids]

    def represented(iid: str) -> bool:
        t = truth[iid]
        siblings = in_force_by_module.get(t["module"], set()) - {iid}
        texts = [i.text for i in cur if i.ids & siblings]
        return bool(texts) and _probes_ok("\n".join(texts), probes.get(iid, [[]]))

    for iid, t in truth.items():
        kind = t["kind"]
        cats: list[str] = []
        c_cur, c_his = cited(iid, cur), cited(iid, his)
        if kind == "information" or kind == "withdrawal":
            continue
        if kind == "decision":
            cats.append(
                "history_kept" if c_his else "history_as_current" if c_cur else "history_lost"
            )
        elif t["in_force"]:
            special = (
                "lost_obligation"
                if kind == "obligation"
                else "lost_exception"
                if kind == "exception"
                else None
            )
            if c_cur:
                missing = [
                    a
                    for a in t["amended_by"]
                    if truth.get(a, {}).get("in_force") and not cited(a, cur) and not represented(a)
                ]
                cats.append(
                    "stale_unamended"
                    if missing
                    else "amended_ok"
                    if t["amended_by"]
                    else "retained"
                )
                if not _probes_ok("\n".join(i.text for i in c_cur), probes.get(iid, [])):
                    cats.append("content_missing")
            elif c_his:  # explicitly filed as history: retired, whatever else is current
                cats.append(special or "incorrect_retirement")
            elif represented(iid):
                cats.append("represented_uncited")
            else:
                cats.append(special or "omitted")
        else:  # replaced or withdrawn
            succ = set(t["replaced_by"]) | in_force_by_module.get(t["module"], set())
            if any(not (i.ids & succ) for i in c_cur):
                cats.append("stale_superseded")
            kept = bool(c_his) or any(i.ids & succ for i in c_cur)
            cats.append("history_kept" if kept else "history_lost")
        out.by_item[iid] = cats
    return out


def score_run(
    points: list[dict[str, Any]],
    revisions: dict[int, list[dict[str, Any]]],
    truth: Any,
) -> dict[str, Any]:
    """Score every assessment point ({"stage", "revision"}) of one run."""
    stages = []
    for p in points:
        entries = revisions.get(p["revision"], [])
        sc = score_stage(
            entries, truth.register[p["stage"]], truth.probes, p["stage"], p["revision"]
        )
        stages.append({**sc.to_dict(), "step": p.get("step"), "point": p.get("point")})
    n = len(truth.register)
    assessed = {s["stage"] for s in stages}
    totals = dict.fromkeys(CATEGORIES, 0)
    for s in stages:
        for k, v in s["counts"].items():
            totals[k] += v
    final = next((s for s in stages if s["stage"] == n and s.get("point") == "final"), None)
    return {
        "scorer_version": VERSION,
        "stages": stages,
        "missing_stages": [k for k in range(1, n + 1) if k not in assessed],
        "totals": totals,
        "harm": sum(totals[k] for k in HARM),
        "history_errors": sum(totals[k] for k in HISTORY_ERRORS),
        "incomplete": sum(totals[k] for k in INCOMPLETE),
        "final": None
        if final is None
        else {k: final[k] for k in ("harm", "history_errors", "incomplete", "counts")},
        "register_followed": any(s["register_items"] for s in stages),
    }
