"""W3 changing-requirements register workload (experiment 014 comparison phase).

Generator ``export-gen/2``, code scorer ``export-checks/2``; the working-context register is
scored by ``register_score`` (``register-checks/1``). It extends W2 (``w2.py``, export-gen/1):
the same records-export service, staged release, read-only fixtures, visible tests, hidden
checks and per-stage workspace snapshots, with these differences:

- two stages of initial requirements followed by five change notices (seven stages);
- every requirement, change and note has a visible item id (``S<stage>.<n>``); a notice item
  does not say which earlier item it changes. A change wins over anything earlier that it
  conflicts with; everything it does not change stays in force;
- relationships vary: full replacements, partial amendments (including an amendment of an
  amendment), a withdrawal, an exception that is narrowed or widened but survives, two
  obligations stated early that apply only once later, unrelated-looking changes trigger them
  (never restated), decision records that stay true as history, and information items that
  share terminology with requirements without changing them;
- the task asks for a requirements register in the working context (``CURRENT`` / ``HISTORY``
  items citing ids), assessed after the agent has processed each stage.

Evaluator-only material (host memory until scoring): the item effects (which earlier items each
item replaces, amends, withdraws or activates), the register truth per stage, the semantic
probes used by the register scorer, and the hidden code checks. None of it is written into the
sandbox, the prompts or Jev payloads.
"""

from __future__ import annotations

import csv
import io
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any

from . import w2
from .tasks import InstanceSpec, TaskInstance

GENERATOR_VERSION = "export-gen/2"
SCORER_VERSION = "export-checks/2"
MODULES = (
    "FORMAT",
    "VIEWER",
    "SCOPE",
    "STATUS",
    "CONF",
    "SINCE",
    "REGION",
    "LIMIT",
    "TITLE",
    "BY",
    "CC",
)
OBLIGATIONS = ("BY", "CC")
CATEGORIES = (
    "retained",
    "exception",
    "replaced",
    "amended",
    "kept_part",
    "deferred",
    "interaction",
)
OWNERS = w2.OWNERS
PROJECTS = w2.PROJECTS
TITLES = w2.TITLES
REGIONS = ("eu", "uk", "ch", "us", "apac")

# Effect of a module's version relative to the module's items in force before it.
EFFECT: dict[tuple[str, int], str] = {
    ("FORMAT", 2): "replaces",
    ("FORMAT", 3): "amends",
    ("VIEWER", 2): "replaces",
    ("SCOPE", 2): "amends",
    ("STATUS", 2): "amends",
    ("CONF", 2): "amends",
    ("SINCE", 2): "amends",
    ("REGION", 2): "amends",
    ("REGION", 0): "withdraws",
    ("LIMIT", 2): "amends",
    ("TITLE", 2): "amends",
    ("BY", 2): "amends",
}

RULES: dict[tuple[str, int], str] = {
    ("FORMAT", 1): (
        "Format: return CSV text. The first row is the header id,title,owner,amount; then one row "
        "per exported record, in ascending id order, with those four fields. Produce it with "
        "Python's csv.writer (default dialect) and lineterminator='\\n'. With no exported records, "
        "return the header row alone (ending in '\\n')."
    ),
    ("FORMAT", 2): (
        'Exports are now JSON text (json.dumps) of an object whose key "records" lists one '
        'object per exported record, in ascending id order, with exactly the keys "id" (int), '
        '"title", "owner" and "amount" (str, exactly as given). The hidden tests parse the JSON '
        "and compare values, not spacing or key order."
    ),
    (
        "FORMAT",
        3,
    ): 'Each exported JSON record now also has the key "project" (the record\'s project).',
    (
        "VIEWER",
        1,
    ): "Viewers: if user['role'] is 'viewer', raise PermissionError; nothing is exported.",
    ("VIEWER", 2): (
        "Viewers no longer receive an error: a viewer now exports only the records they own "
        "(record 'owner' equal to user['name']), in any project; every other rule still applies "
        "to those records."
    ),
    ("SCOPE", 1): (
        "Scope: a user whose role is 'admin' may export records of any project. Any other role "
        "exports only records whose 'project' is in user['projects']; other records are left out "
        "without an error."
    ),
    ("SCOPE", 2): (
        "Users whose role is neither 'admin' nor 'viewer' may now also export the records they "
        "own (record 'owner' equal to user['name']), even outside their projects."
    ),
    ("STATUS", 1): (
        "Status: only records with status 'active' are exported, unless "
        "params.get('include_archived') is True; then records with status 'archived' are "
        "exported too."
    ),
    ("STATUS", 2): (
        "include_archived is now honoured only when user['role'] is 'admin'; for every other "
        "role, archived records are never exported."
    ),
    ("CONF", 1): (
        "Confidential records: a record whose 'confidential' is True is never exported, with one "
        "exception: it is exported when user['role'] is 'admin' and params.get('purpose') == "
        "'audit'. Every other rule still applies to it."
    ),
    ("CONF", 2): (
        "Confidential records may now also be exported when user['role'] is 'admin' and "
        "params.get('purpose') == '{p2}'."
    ),
    ("SINCE", 1): (
        "Date filter: params may contain 'since', a date string 'YYYY-MM-DD'. When it is "
        "present, only records whose 'created' date is on or after that date are exported."
    ),
    ("SINCE", 2): (
        "The date filter now uses a record's 'updated' date when the record has one, and its "
        "'created' date otherwise."
    ),
    ("REGION", 1): (
        "Residency: a record whose 'region' is '{region}' is exported only when '{region}' is in "
        "user['regions']; this applies to admins too."
    ),
    ("REGION", 2): (
        "Region values may now be written in upper or lower case, in records and in "
        "user['regions']; compare region values case-insensitively."
    ),
    ("REGION", 0): (
        "The residency restriction is withdrawn: records of every region are exported under the "
        "ordinary rules."
    ),
    ("LIMIT", 1): (
        "Record limit: params may contain 'max_records', an int of at least 1. Export only the "
        "first max_records records, in ascending id order, that remain after every other rule."
    ),
    ("LIMIT", 2): "A max_records value above {cap} is treated as {cap}.",
    (
        "TITLE",
        1,
    ): "Titles: export each record's title with leading and trailing whitespace removed.",
    ("TITLE", 2): (
        "Titles: in addition, replace every run of whitespace inside a title with a single space."
    ),
    ("BY", 1): (
        "Compliance obligation (nothing to do while the format is CSV): if a later stage changes "
        "the export format from CSV, every export in the new format must include a top-level field "
        "\"exported_by\" equal to user['name']. It stays in force unless a later stage withdraws "
        "or changes it."
    ),
    ("BY", 2): (
        "The \"exported_by\" field is no longer required when user['role'] is 'admin'; admin "
        "exports omit it."
    ),
    ("CC", 1): (
        "Audit-trail obligation (nothing to do today): if confidential records are ever exported "
        "for a purpose other than 'audit', every such export must include a top-level field "
        '"confidential_count" giving the number of confidential records it contains.'
    ),
}
DECISIONS = {
    "csv": (
        "Decision record: CSV was chosen for the first release because the finance team imports "
        "exports into spreadsheets."
    ),
    "json": "Decision record: the move to JSON was made for the new reporting API.",
    "since": "Decision record: the date filter was introduced for the monthly reconciliation runs.",
    "region": (
        "Decision record: the residency restriction was lifted after the regional data agreement "
        "was signed."
    ),
}
INFORMATION = {
    "archive": (
        "For information: storage for archived projects moves to a cheaper tier next quarter. "
        "This does not change what exports contain."
    ),
    "conf": (
        "For information: the security team confirmed that this notice does not change the "
        "confidential-record rules."
    ),
    "owner": (
        "For information: the web interface will show record owners by display name; exports keep "
        "the stored 'owner' value."
    ),
    "limit": "For information: the web interface now shows 50 records per page. This does not change exports.",
}
# Modules an information item or decision record shares terminology with (evaluator note only).
RELATED = {
    "archive": "STATUS",
    "conf": "CONF",
    "owner": "SCOPE",
    "limit": "LIMIT",
    "csv": "FORMAT",
    "json": "FORMAT",
    "since": "SINCE",
    "region": "REGION",
}
# Semantic probes (register scorer, evaluator-only): every group must be matched (any of its
# literals, case-insensitive) by the CURRENT register text that cites the item.
PROBES: dict[tuple[str, int], list[list[str]]] = {
    ("FORMAT", 1): [["csv"]],
    ("FORMAT", 2): [["json"]],
    ("FORMAT", 3): [["project"]],
    ("VIEWER", 1): [["permissionerror", "permission error"]],
    ("VIEWER", 2): [["own"]],
    ("SCOPE", 1): [["any project", "all projects", "every project"]],
    ("SCOPE", 2): [["own"]],
    ("STATUS", 1): [["active"]],
    ("STATUS", 2): [["admin"]],
    ("CONF", 1): [["confidential"], ["audit"]],
    ("CONF", 2): [["{p2}"]],
    ("SINCE", 1): [["since"]],
    ("SINCE", 2): [["updated"]],
    ("REGION", 1): [["{region}"]],
    ("REGION", 2): [["case"]],
    ("LIMIT", 1): [["first", "at most", "limit"]],
    ("LIMIT", 2): [["{cap}"]],
    ("TITLE", 1): [["trim", "strip", "leading", "trailing", "surrounding"]],
    ("TITLE", 2): [["internal", "inside", "collapse", "single space", "run"]],
    ("BY", 1): [["exported_by"], ["name"]],
    ("BY", 2): [["admin"]],
    ("CC", 1): [["confidential_count"]],
}

# A schedule: per stage, items ("M", module, version) | ("D", decision key) | ("I", info key).
Item = tuple[Any, ...]
SCHEDULES: dict[str, list[list[Item]]] = {
    "dev-1": [
        [
            ("M", "FORMAT", 1),
            ("M", "VIEWER", 1),
            ("M", "SCOPE", 1),
            ("M", "STATUS", 1),
            ("M", "CONF", 1),
            ("D", "csv"),
        ],
        [
            ("M", "SINCE", 1),
            ("M", "REGION", 1),
            ("M", "LIMIT", 1),
            ("M", "BY", 1),
            ("M", "TITLE", 1),
            ("M", "CC", 1),
        ],
        [("M", "FORMAT", 2), ("M", "SCOPE", 2), ("I", "archive")],
        [("M", "STATUS", 2), ("M", "REGION", 2), ("M", "TITLE", 2)],
        [("M", "CONF", 2), ("M", "VIEWER", 2), ("D", "json")],
        [("M", "BY", 2), ("M", "LIMIT", 2), ("I", "limit")],
        [("M", "FORMAT", 3), ("M", "REGION", 0), ("I", "conf")],
    ],
    "dev-2": [
        [
            ("M", "FORMAT", 1),
            ("M", "SCOPE", 1),
            ("M", "STATUS", 1),
            ("M", "SINCE", 1),
            ("M", "TITLE", 1),
            ("D", "since"),
        ],
        [
            ("M", "VIEWER", 1),
            ("M", "CONF", 1),
            ("M", "LIMIT", 1),
            ("M", "BY", 1),
            ("M", "CC", 1),
            ("M", "REGION", 1),
        ],
        [("M", "FORMAT", 2), ("M", "SINCE", 2), ("I", "owner")],
        [("M", "CONF", 2), ("M", "SCOPE", 2), ("D", "json")],
        [("M", "STATUS", 2), ("M", "VIEWER", 2), ("M", "LIMIT", 2)],
        [("M", "REGION", 0), ("I", "limit")],
        [("M", "FORMAT", 3), ("M", "BY", 2), ("D", "region")],
    ],
    # The live integration smoke only (never part of an outcome comparison).
    "smoke-1": [
        [("M", "FORMAT", 1), ("M", "SCOPE", 1), ("M", "CONF", 1), ("D", "csv")],
        [("M", "STATUS", 1), ("M", "BY", 1), ("M", "TITLE", 1)],
        [("M", "FORMAT", 2), ("M", "SCOPE", 2), ("I", "archive")],
        [("M", "STATUS", 2), ("M", "TITLE", 2), ("D", "json")],
        [("M", "CONF", 2), ("M", "BY", 2)],
    ],
}
Schedule = list[list[Item]]
Config = dict[str, int]


def schedule_for(name: str) -> Schedule:
    return SCHEDULES[name.split("-", 1)[1]]


def values_for(seed: int) -> dict[str, Any]:
    rng = random.Random(seed * 31 + 7)
    return {
        "p2": rng.choice(["legal", "litigation", "regulator"]),
        "region": rng.choice(["eu", "uk", "ch"]),
        "cap": rng.choice([20, 25, 30]),
    }


def config_at(schedule: Schedule, stage: int) -> Config:
    cfg: Config = {}
    for items in schedule[:stage]:
        for it in items:
            if it[0] == "M":
                if it[2] == 0:
                    cfg.pop(it[1], None)
                else:
                    cfg[it[1]] = it[2]
    return cfg


def history_of(schedule: Schedule, upto: int | None = None) -> dict[str, list[int]]:
    """Per module, the versions in the order they were released (0 = withdrawn)."""
    out: dict[str, list[int]] = {}
    for items in schedule[: upto or len(schedule)]:
        for it in items:
            if it[0] == "M":
                out.setdefault(it[1], []).append(it[2])
    return out


def previous_state(schedule: Schedule, module: str, upto: int | None = None) -> int | None:
    """The module's version before its last change (for a withdrawn module: its last version)."""
    vs = history_of(schedule, upto).get(module, [])
    if len(vs) < 2:
        return None
    return vs[-2]  # for a withdrawn rule (last entry 0), its last version


def active_obligations(cfg: Config) -> set[str]:
    out = set()
    if cfg.get("BY") and cfg.get("FORMAT", 1) >= 2:
        out.add("BY")
    if cfg.get("CC") and cfg.get("FORMAT", 1) >= 2 and cfg.get("CONF", 0) >= 2:
        out.add("CC")
    return out


# ------------------------------------------------------------- reference implementation
# Faults (offline scorer validation only): "<MODULE>:replace" applies an amendment as if it
# replaced the whole earlier rule; "CONF:none" drops the confidential exception;
# "order:LIMIT_FIRST" applies the record limit before the other rules.
def ref_export(
    records: list[dict[str, Any]],
    user: dict[str, Any],
    params: dict[str, Any],
    cfg: dict[str, int],
    vals: dict[str, Any],
    faults: frozenset[str] = frozenset(),
) -> str:
    import csv as _csv
    import io as _io
    import json as _json
    import re as _re

    role = user["role"]
    if cfg.get("VIEWER") == 1 and role == "viewer":
        raise PermissionError("viewers may not export")
    rows = sorted(records, key=lambda r: r["id"])
    limit = None
    if cfg.get("LIMIT") and "max_records" in params:
        limit = params["max_records"]
        if cfg["LIMIT"] >= 2:
            limit = vals["cap"] if "LIMIT:replace" in faults else min(limit, vals["cap"])
    if limit is not None and "order:LIMIT_FIRST" in faults:
        rows = rows[:limit]
    out = []
    for r in rows:
        own = r["owner"] == user["name"]
        if role == "viewer" and cfg.get("VIEWER") == 2:
            if not own:
                continue
        elif cfg.get("SCOPE") and role != "admin":
            in_project = r["project"] in user["projects"]
            if cfg["SCOPE"] >= 2:
                ok = own if "SCOPE:replace" in faults else (in_project or own)
            else:
                ok = in_project
            if not ok:
                continue
        if cfg.get("STATUS") and r["status"] != "active":
            include = params.get("include_archived") is True
            if cfg["STATUS"] >= 2 and (role != "admin" or "STATUS:replace" in faults):
                include = False
            if not include:
                continue
        if cfg.get("CONF") and r["confidential"]:
            purposes = {"audit"}
            if cfg["CONF"] >= 2:
                purposes = {vals["p2"]} if "CONF:replace" in faults else {"audit", vals["p2"]}
            allowed = role == "admin" and params.get("purpose") in purposes
            if "CONF:none" in faults:
                allowed = False
            if not allowed:
                continue
        if cfg.get("SINCE") and "since" in params:
            when = r["created"]
            if cfg["SINCE"] >= 2:
                if "SINCE:replace" in faults:
                    when = r.get("updated") or ""
                elif r.get("updated"):
                    when = r["updated"]
            if when < params["since"]:
                continue
        if cfg.get("REGION") and "REGION:replace" not in faults:
            code = vals["region"]
            if cfg["REGION"] >= 2:
                restricted = str(r.get("region", "")).lower() == code
                allowed = code in [str(x).lower() for x in user.get("regions", [])]
            else:
                restricted = r.get("region") == code
                allowed = code in user.get("regions", [])
            if restricted and not allowed:
                continue
        out.append(r)
    if limit is not None and "order:LIMIT_FIRST" not in faults:
        out = out[:limit]

    def title(t: str) -> str:
        if not cfg.get("TITLE"):
            return t
        if cfg["TITLE"] >= 2:
            if "TITLE:replace" in faults:
                return _re.sub(r"\s+", " ", t)  # collapses runs but no longer trims
            return " ".join(t.split())
        return t.strip()

    fmt = cfg.get("FORMAT", 1)
    if fmt == 1:
        buf = _io.StringIO()
        w = _csv.writer(buf, lineterminator="\n")
        w.writerow(["id", "title", "owner", "amount"])
        for r in out:
            w.writerow([r["id"], title(r["title"]), r["owner"], r["amount"]])
        return buf.getvalue()
    keys = ["id", "title", "owner", "amount"]
    if fmt >= 3:
        keys = ["id", "project"] if "FORMAT:replace" in faults else [*keys, "project"]
    recs = []
    for r in out:
        d = {k: r[k] for k in keys}
        if "title" in d:
            d["title"] = title(r["title"])
        recs.append(d)
    obj: dict[str, Any] = {"records": recs}
    if cfg.get("BY"):
        drop = cfg["BY"] >= 2 and (role == "admin" or "BY:replace" in faults)
        if not drop:
            obj["exported_by"] = user["name"]
    if cfg.get("CC") and cfg.get("CONF", 0) >= 2:
        n_conf = sum(1 for r in out if r["confidential"])
        if n_conf and params.get("purpose") != "audit":
            obj["confidential_count"] = n_conf
    return _json.dumps(obj)


OBLIGATION_KEYS = {"BY": "exported_by", "CC": "confidential_count"}


def keys_for(module: str) -> list[str]:
    """Top-level JSON keys a check compares (each check fails only on what it is about)."""
    return [OBLIGATION_KEYS[module]] if module in OBLIGATION_KEYS else ["records"]


def observe(
    args: list[Any],
    cfg: Config,
    vals: dict[str, Any],
    keys: list[str],
    faults: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    try:
        recs, user, params = json.loads(json.dumps(args))
        text = ref_export(recs, user, params, cfg, vals, faults)
    except PermissionError:
        return {"raises": "PermissionError"}
    if cfg.get("FORMAT", 1) == 1:
        return {"text": text}
    parsed = json.loads(text)
    return {"json": {k: parsed.get(k) for k in keys}}


def _with(cfg: Config, module: str, version: int | None) -> Config:
    out = {k: v for k, v in cfg.items() if k != module}
    if version:
        out[module] = version
    return out


# ------------------------------------------------------------- case generation
def _title(rng: random.Random, hist: dict[str, list[int]]) -> str:
    """A title; once a title rule is released, some have extra whitespace."""
    t = rng.choice(TITLES)
    if "TITLE" in hist and rng.random() < 0.6:
        words = t.split(" ")
        if len(words) > 1 and rng.random() < 0.6:
            i = rng.randrange(1, len(words))
            t = " ".join(words[:i]) + rng.choice(["  ", " \t ", "   "]) + " ".join(words[i:])
        t = rng.choice(["", " ", "  ", "\t"]) + t + rng.choice(["", " ", "  "])
    return t


def _region(rng: random.Random, hist: dict[str, list[int]]) -> str:
    """A region code; once case-insensitive regions are released, some are upper case."""
    code = rng.choice(REGIONS)
    if 2 in hist.get("REGION", []) and rng.random() < 0.5:
        code = code.upper()
    return code


def _records(
    rng: random.Random, n: int, cfg: Config, hist: dict[str, list[int]]
) -> list[dict[str, Any]]:
    ids = rng.sample(range(1, 1000), n)
    out = []
    for i in ids:
        dec = rng.choice([0, 1, 2, 2])
        amount = str(rng.randrange(1, 900)) + (
            f".{rng.randrange(0, 10**dec):0{dec}d}" if dec else ""
        )
        created = f"2026-{rng.randrange(1, 9):02d}-{rng.randrange(1, 29):02d}"
        r: dict[str, Any] = {
            "id": i,
            "title": _title(rng, hist),
            "owner": rng.choice(OWNERS),
            "project": rng.choice(PROJECTS),
            "status": rng.choice(["active", "active", "archived"]),
            "amount": amount,
            "confidential": rng.random() < 0.3,
            "created": created,
            "region": _region(rng, hist),
        }
        if 2 in hist.get("SINCE", []) and rng.random() < 0.6:
            r["updated"] = (
                f"2026-{rng.randrange(int(created[5:7]), 10):02d}-{rng.randrange(1, 29):02d}"
            )
            if r["updated"] < created:
                r["updated"] = created
        out.append(r)
    return out


def _user(
    rng: random.Random, hist: dict[str, list[int]], role: str | None = None
) -> dict[str, Any]:
    regions = sorted(rng.sample(REGIONS, rng.randrange(0, 3)))
    if 2 in hist.get("REGION", []):
        regions = [x.upper() if rng.random() < 0.5 else x for x in regions]
    return {
        "name": rng.choice(OWNERS),
        "role": role or rng.choice(["admin", "member", "member", "viewer"]),
        "projects": sorted(rng.sample(PROJECTS, rng.randrange(1, 3))),
        "regions": regions,
    }


def _params(
    rng: random.Random, cfg: Config, vals: dict[str, Any], hist: dict[str, list[int]]
) -> dict[str, Any]:
    """Random options, only those disclosed by the rules released so far (``hist``)."""
    p: dict[str, Any] = {}
    if "STATUS" in hist and rng.random() < 0.5:
        p["include_archived"] = rng.random() < 0.7
    if "CONF" in hist and rng.random() < 0.5:
        p["purpose"] = rng.choice(["audit", "report"] + ([vals["p2"]] if 2 in hist["CONF"] else []))
    if "SINCE" in hist and rng.random() < 0.5:
        p["since"] = f"2026-{rng.randrange(2, 9):02d}-{rng.randrange(1, 29):02d}"
    if "LIMIT" in hist and rng.random() < 0.5:
        p["max_records"] = rng.randrange(1, 4)
    return p


Case = tuple[list[Any], bool]


def module_case(
    module: str, rng: random.Random, cfg: Config, vals: dict[str, Any], hist: dict[str, list[int]]
) -> Case:
    """A random case biased towards ``module`` mattering (rejection sampling picks the rest)."""
    n = rng.randrange(3, 7)
    user = _user(rng, hist)
    params = _params(rng, cfg, vals, hist)
    boundary = False
    if module == "LIMIT" and 2 in hist.get("LIMIT", []) and rng.random() < 0.7:
        n = vals["cap"] + rng.randrange(1, 4)
    recs = _records(rng, n, cfg, hist)
    in_proj = user["projects"][0]
    if module in ("FORMAT", "TITLE", "LIMIT", "BY"):
        user["role"] = rng.choice(["admin", "member"])
        for r in recs[: max(2, len(recs) // 2)]:
            r.update(project=in_proj, status="active", confidential=False, region="us")
        if module == "LIMIT":
            for r in recs:
                r.update(project=in_proj, status="active", confidential=False, region="us")
            params.pop("since", None)
            if 2 in hist.get("LIMIT", []) and n > vals["cap"]:
                params["max_records"] = vals["cap"] + rng.randrange(1, 6)
            else:
                params["max_records"] = rng.randrange(1, max(2, n))
            boundary = params["max_records"] >= n - 1
    elif module == "VIEWER":
        user["role"] = "viewer"
        recs[0].update(owner=user["name"], status="active", confidential=False, region="us")
    elif module == "SCOPE":
        user["role"] = rng.choice(["member", "member", "admin"])
        outside = next(p for p in PROJECTS if p not in user["projects"])
        recs[0].update(
            owner=user["name"], project=outside, status="active", confidential=False, region="us"
        )
        recs[1].update(project=in_proj, status="active", confidential=False, region="us")
        boundary = True
    elif module == "STATUS":
        user["role"] = rng.choice(["admin", "member"])
        params["include_archived"] = rng.random() < 0.8
        for r in recs[:2]:
            r.update(status="archived", project=in_proj, confidential=False, region="us")
    elif module in ("CONF", "CC"):
        user["role"] = rng.choice(["admin", "admin", "member"])
        choices = ["audit", "report"] + ([vals["p2"]] if 2 in hist.get("CONF", []) else [])
        params["purpose"] = rng.choice(choices)
        for r in recs[:2]:
            r.update(confidential=True, status="active", project=in_proj, region="us")
        boundary = params["purpose"] != "report"
    elif module == "SINCE":
        user["role"] = "admin"
        r0 = recs[0]
        r0.update(status="active", confidential=False, region="us")
        params["since"] = (
            r0.get("updated") or r0["created"] if rng.random() < 0.5 else r0["created"]
        )
        boundary = True
    elif module == "REGION":
        user["role"] = rng.choice(["admin", "member"])
        code, mixed = vals["region"], 2 in hist.get("REGION", [])
        for r in recs[:2]:
            r.update(project=in_proj, status="active", confidential=False)
            r["region"] = code.upper() if mixed and rng.random() < 0.5 else code
        regions = [x for x in user["regions"] if x.lower() != code]
        if rng.random() < 0.5:
            regions.append(code.upper() if mixed and rng.random() < 0.5 else code)
        user["regions"] = sorted(regions)
        boundary = True
    return [recs, user, params], boundary


def interaction_case(
    rng: random.Random, cfg: Config, vals: dict[str, Any], hist: dict[str, list[int]]
) -> Case:
    recs = _records(rng, rng.randrange(6, 10), cfg, hist)
    user = _user(rng, hist, rng.choice(["admin", "member"]))
    params = _params(rng, cfg, vals, hist)
    for r in recs[:4]:
        r["project"] = user["projects"][0]
    return [recs, user, params], False


FILTERS = ("VIEWER", "SCOPE", "STATUS", "CONF", "SINCE", "REGION", "LIMIT")


def purpose_ok(
    purpose: str,
    module: str,
    args: list[Any],
    cfg: Config,
    vals: dict[str, Any],
    schedule: Schedule,
    upto: int | None,
) -> bool:
    keys = keys_for(module) if module != "*" else ["records"]
    exp = observe(args, cfg, vals, keys)
    if "json" in exp and exp["json"] == {"records": []} and purpose != "interaction":
        return False  # an empty export says little about the rule
    if purpose == "meaningful" and module == "FORMAT":  # the format always matters
        return exp.get("text", "").count("\n") > 1 or bool(exp.get("json", {}).get("records"))
    if purpose == "meaningful":
        return exp != observe(args, _with(cfg, module, None), vals, keys)
    if purpose == "changed":
        prev = previous_state(schedule, module, upto)
        return exp != observe(args, _with(cfg, module, prev), vals, keys)
    if purpose == "kept":
        return exp != observe(args, cfg, vals, keys, frozenset({f"{module}:replace"}))
    if purpose == "exception":
        return exp != observe(args, cfg, vals, keys, frozenset({"CONF:none"}))
    if purpose == "interaction":
        others = [m for m in cfg if m in FILTERS]
        return sum(exp != observe(args, _with(cfg, m, None), vals, keys) for m in others) >= 3
    raise KeyError(purpose)


def pick_cases(
    module: str,
    purpose: str,
    rng: random.Random,
    cfg: Config,
    vals: dict[str, Any],
    schedule: Schedule,
    upto: int | None,
    n: int,
    avoid: set[str],
) -> list[Case]:
    hist = history_of(schedule, upto)
    picked: list[Case] = []
    for _ in range(20000):
        if len(picked) >= n:
            break
        case = (
            interaction_case(rng, cfg, vals, hist)
            if purpose == "interaction"
            else module_case(module, rng, cfg, vals, hist)
        )
        key = json.dumps(case[0], sort_keys=True)
        if key in avoid or not purpose_ok(purpose, module, case[0], cfg, vals, schedule, upto):
            continue
        avoid.add(key)
        picked.append(case)
    if len(picked) < n:
        raise RuntimeError(f"could not generate {n} {purpose} cases for {module} under {cfg}")
    return picked


def _check(
    cid: str,
    module: str,
    category: str,
    case: Case,
    cfg: Config,
    vals: dict[str, Any],
    schedule: Schedule,
    upto: int | None,
) -> dict[str, Any]:
    args, boundary = case
    keys = keys_for(module) if module != "*" else ["records"]
    expect = observe(args, cfg, vals, keys)
    stale = {}
    for m in history_of(schedule, upto):
        prev = previous_state(schedule, m, upto)
        if prev is None:
            continue
        old = observe(args, _with(cfg, m, prev), vals, keys)
        if old != expect:
            stale[m] = old
    return {
        "id": cid,
        "module": module,
        "category": category,
        "boundary": boundary,
        "fn": "export_records",
        "args": args,
        "mode": "csv" if cfg.get("FORMAT", 1) == 1 else "json",
        "keys": keys,
        "expect": expect,
        "stale": stale,
    }


def categories(schedule: Schedule) -> dict[str, str]:
    """Each module's main category at the end (W2 names; ``amended`` is new)."""
    final = config_at(schedule, len(schedule))
    out: dict[str, str] = {}
    for m, vs in history_of(schedule).items():
        last = EFFECT.get((m, vs[-1])) if len(vs) > 1 else None
        if m in OBLIGATIONS:
            out[m] = "deferred"
        elif m not in final or last == "replaces":
            out[m] = "replaced"
        elif last == "amends":
            out[m] = "amended"
        elif m == "CONF":
            out[m] = "exception"
        else:
            out[m] = "retained"
    return out


def build_checks(
    schedule: Schedule, vals: dict[str, Any], seed: int, avoid: set[str]
) -> list[dict[str, Any]]:
    n = len(schedule)
    final = config_at(schedule, n)
    cats = categories(schedule)
    rng = random.Random(seed * 7919 + 17)
    checks: list[dict[str, Any]] = []

    def add(module: str, cat: str, purpose: str, k: int) -> None:
        cases = pick_cases(module, purpose, rng, final, vals, schedule, None, k, avoid)
        for case in cases:
            idx = sum(1 for c in checks if c["module"] == module and c["category"] == cat) + 1
            checks.append(
                _check(f"{module}-{cat}-{idx}", module, cat, case, final, vals, schedule, None)
            )

    for m in MODULES:
        if m not in cats:
            continue
        cat = cats[m]
        vs = history_of(schedule)[m]
        if cat == "deferred":
            if m in active_obligations(final):
                add(m, "deferred", "meaningful", 3)
                if len(vs) > 1:
                    add(m, "amended", "changed", 2)
                    add(m, "kept_part", "kept", 2)
                    add(m, "kept_part", "meaningful", 1)
        elif cat == "retained":
            add(m, "retained", "meaningful", 4)
        elif cat == "exception":
            add(m, "exception", "meaningful", 2)
            add(m, "exception", "exception", 2)
        elif cat == "replaced":
            add(m, "replaced", "changed", 3)
        elif cat == "amended":
            add(m, "amended", "changed", 3)
            add(m, "kept_part", "kept", 2)
            add(m, "kept_part", "meaningful", 1)  # the earlier rule as a whole still applies
            if m == "CONF":
                add(m, "exception", "exception", 2)
    if len([m for m in final if m in FILTERS]) >= 3:
        add("*", "interaction", "interaction", 5)
    return checks


def build_snapshot_checks(
    schedule: Schedule, vals: dict[str, Any], seed: int, avoid: set[str]
) -> dict[int, list[dict[str, Any]]]:
    rng = random.Random(seed * 104729 + 11)
    out: dict[int, list[dict[str, Any]]] = {}
    for stage in range(1, len(schedule)):
        cfg = config_at(schedule, stage)
        active = active_obligations(cfg)
        checks = []
        for m in [m for m in MODULES if m in cfg]:
            if m in OBLIGATIONS and m not in active:
                continue
            cases = pick_cases(m, "meaningful", rng, cfg, vals, schedule, stage, 2, avoid)
            for i, case in enumerate(cases, start=1):
                c = _check(f"s{stage}-{m}-{i}", m, "snapshot", case, cfg, vals, schedule, stage)
                c["version"] = cfg[m]
                checks.append(c)
        out[stage] = checks
    return out


# ------------------------------------------------------------- items, documents and tests
@dataclass
class W3Item:
    id: str
    stage: int
    kind: str  # requirement | obligation | exception | withdrawal | decision | information
    text: str
    module: str | None = None
    version: int | None = None
    key: str | None = None


def build_items(schedule: Schedule, vals: dict[str, Any]) -> list[W3Item]:
    items: list[W3Item] = []
    for stage, entries in enumerate(schedule, start=1):
        for n, it in enumerate(entries, start=1):
            iid = f"S{stage}.{n}"
            if it[0] == "M":
                m, v = it[1], it[2]
                kind = (
                    "withdrawal"
                    if v == 0
                    else (
                        "obligation"
                        if m in OBLIGATIONS
                        else "exception"
                        if m == "CONF"
                        else "requirement"
                    )
                )
                items.append(W3Item(iid, stage, kind, RULES[(m, v)].format(**vals), m, v))
            elif it[0] == "D":
                items.append(W3Item(iid, stage, "decision", DECISIONS[it[1]], key=it[1]))
            else:
                items.append(W3Item(iid, stage, "information", INFORMATION[it[1]], key=it[1]))
    return items


def item_effects(items: list[W3Item]) -> dict[str, list[dict[str, str]]]:
    """Evaluator-only: for each item, the earlier items it replaces, amends, withdraws or
    activates (obligations), and the items it only shares terminology with ("none")."""
    effects: dict[str, list[dict[str, str]]] = {}
    chain: dict[str, list[str]] = {}  # module -> item ids in force
    cfg: Config = {}
    for it in items:
        eff: list[dict[str, str]] = []
        if it.module is not None:
            m = it.module
            prev_active = active_obligations(cfg)
            e = EFFECT.get((m, it.version or 0))
            if e is None:
                chain[m] = [it.id]
            elif e == "amends":
                eff += [{"target": t, "effect": "amends"} for t in chain.get(m, [])]
                chain[m] = [*chain.get(m, []), it.id]
            elif e == "replaces":
                eff += [{"target": t, "effect": "replaces"} for t in chain.get(m, [])]
                chain[m] = [it.id]
            elif e == "withdraws":
                eff += [{"target": t, "effect": "replaces"} for t in chain.get(m, [])]
                chain[m] = []
            if it.version == 0:
                cfg.pop(m, None)
            else:
                cfg[m] = it.version or 0
            for ob in sorted(active_obligations(cfg) - prev_active):
                eff += [{"target": t, "effect": "activates"} for t in chain.get(ob, [])]
        else:
            rel = RELATED.get(it.key or "")
            if rel:
                eff += [{"target": t, "effect": "none"} for t in chain.get(rel, [])]
        effects[it.id] = eff
    return effects


def register_truth(
    items: list[W3Item], effects: dict[str, list[dict[str, str]]], stage: int
) -> dict[str, dict[str, Any]]:
    """Evaluator-only truth after ``stage``: each released item's expected register status."""
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        if it.stage > stage:
            continue
        out[it.id] = {
            "kind": it.kind,
            "module": it.module,
            "version": it.version,
            "in_force": it.kind in ("requirement", "obligation", "exception"),
            "replaced_by": [],
            "amended_by": [],
            "activated": False,
        }
    for it in items:
        if it.stage > stage:
            continue
        for e in effects[it.id]:
            t = out[e["target"]]
            if e["effect"] == "replaces":
                t["in_force"] = False
                t["replaced_by"].append(it.id)
            elif e["effect"] == "amends":
                t["amended_by"].append(it.id)
            elif e["effect"] == "activates":
                t["activated"] = True
    return out


API_DOC = """## Package and API

Implement `exporter.export_records(records, user, params) -> str` in the package
/task/workspace/exporter/ (a starting skeleton is provided). Standard library only.

- `records`: a list of dicts with the keys "id" (int, unique in the list), "title" (str),
  "owner" (str), "project" (str), "status" ("active" or "archived"), "amount" (a decimal string
  such as "12.50"; keep it exactly as given), "confidential" (bool), "created" (a date string
  "YYYY-MM-DD") and "region" (str). A record may also have "updated" (a date string). Do not
  modify the input.
- `user`: a dict with "name" (str), "role" (str), "projects" (list of str) and "regions" (list
  of str).
- `params`: a dict of export options; it may be empty. Rules introduce the options; ignore keys
  that no rule in force mentions.
- Base behaviour, unless a rule says otherwise: every record is exported, in ascending id
  order, with field values exactly as given, in the format that the format rule specifies.
"""

NOTICE_HEAD = (
    "This notice lists decisions taken since the previous stage. An item does not say which "
    "earlier item it affects. A change wins over anything earlier that it conflicts with; every "
    "earlier requirement, obligation and exception, and every part of one, that it does not "
    "change stays in force."
)


def stage_doc(items: list[W3Item], stage: int, n: int) -> str:
    mine = [i for i in items if i.stage == stage]
    reqs = [i for i in mine if i.kind not in ("decision", "information")]
    notes = [i for i in mine if i.kind in ("decision", "information")]
    if stage == 1:
        head = [f"# Stage 1 of {n}: initial requirements", "", API_DOC, "## Requirements", ""]
    elif stage == 2:
        head = [f"# Stage 2 of {n}: further initial requirements", "", "## Requirements", ""]
    else:
        head = [f"# Stage {stage} of {n}: change notice", "", NOTICE_HEAD, "", "## Changes", ""]
    head += [f"- [{i.id}] {i.text}" for i in reqs]
    if notes:
        head += ["", "## Notes", ""] + [f"- [{i.id}] {i.text}" for i in notes]
    head += [
        "",
        "Visible tests for the rules introduced or changed in this stage only: "
        "/task/fixtures/current-tests/test_stage.py (replaced at each stage). Earlier rules still "
        "apply and are checked by the hidden evaluation.",
        "",
    ]
    return "\n".join(head)


def visible_tests(
    schedule: Schedule, vals: dict[str, Any], stage: int, rng: random.Random, avoid: set[str]
) -> str:
    cfg = config_at(schedule, stage)
    active = active_obligations(cfg)
    out = [
        f'"""Visible tests for the rules introduced or changed in stage {stage} only.',
        "",
        "Earlier rules still apply; they are not repeated here.",
        "Run from /task/workspace:  python -m unittest discover -s /task/fixtures/current-tests",
        '"""',
        "",
        "import json",
        "import unittest",
        "",
        "import exporter",
        "",
        "",
        "class TestStage(unittest.TestCase):",
        "    maxDiff = None",
        "",
    ]
    n = 0
    for it in schedule[stage - 1]:
        if it[0] != "M":
            continue
        m, v = it[1], it[2]
        if m in OBLIGATIONS and m not in active:
            continue
        if v == 0 or v > 1:
            purpose, module = "changed", m
        else:
            purpose, module = "meaningful", m
        cases = pick_cases(module, purpose, rng, cfg, vals, schedule, stage, 2, avoid)
        if m == "CONF" and v == 1:
            cases = cases[:1] + pick_cases(
                m, "exception", rng, cfg, vals, schedule, stage, 1, avoid
            )
        for args, _ in cases:
            n += 1
            exp = observe(args, cfg, vals, keys_for(module))
            call = f"exporter.export_records(*{args!r})"
            out.append(f"    def test_{m.lower()}_{n}(self):")
            if "raises" in exp:
                out.append(f"        with self.assertRaises(PermissionError):\n            {call}")
            elif "text" in exp:
                out.append(f"        self.assertEqual({call}, {exp['text']!r})")
            else:
                out.append(f"        out = json.loads({call})")
                for k, val in exp["json"].items():
                    out.append(f"        self.assertEqual(out.get({k!r}), {val!r})")
            out.append("")
    if n == 0:
        out += ["    def test_nothing_new(self):", "        pass", ""]
    out += ["", 'if __name__ == "__main__":', "    unittest.main()", ""]
    return "\n".join(out)


REGISTER_OBJECTIVE = """Requirements register (part of this task): keep, in your working context (context.json), a
register of the requirements. A register item starts with CURRENT or HISTORY at the start of an
entry body or of a line, cites in square brackets the item ids it covers (for example
[S1.3, S3.2]), and states what they say. CURRENT items state what is in force now, including
every part of an earlier item that later changes left in force. HISTORY items record items that
were replaced or withdrawn, and decision records. You decide how to organise, merge, word and
update the register, and how to maintain it. It is checked after you have processed each stage:
when you release the next stage, and at your final answer."""


def task_prompt(n_stages: int) -> str:
    return f"""Software task with requirements released in {n_stages} stages over time.

Implement the Python package `exporter` in /task/workspace/exporter/ (a skeleton is provided; you
may add modules and functions). Requirements are under /task/fixtures/stage-N/REQUIREMENTS.md;
only stage 1 is available now, and earlier requirement files stay readable after later stages
are released. Every requirement, change and note has an item id such as S3.2.

Visible tests: /task/fixtures/current-tests/test_stage.py covers only the rules introduced or
changed in the latest released stage, and is replaced at each stage. Rules from earlier stages
still apply but are not in the visible tests. To run them from your code:
  import subprocess, sys
  subprocess.run([sys.executable, "-m", "unittest", "discover", "-s",
                  "/task/fixtures/current-tests"], cwd="/task/workspace")

Staged requirements:
- Reply {{"action": "advance"}} to release the next stage when you are ready. Its requirements
  appear under /task/fixtures/stage-N/ and are added to your working context.
- Stages 1 and 2 state the initial requirements. Stages 3 to {n_stages} are change notices. A
  notice item does not say which earlier item it affects; a change wins over anything earlier
  that it conflicts with, and every earlier requirement, obligation and exception, and every
  part of one, that it does not change stays in force. Notes record decisions (history) and
  information; they do not change requirements.
- A final answer is accepted only after all {n_stages} stages are released.

{REGISTER_OBJECTIVE}

Finish with {{"action": "final", "answer": {{"summary": "..."}}}} once the package implements every
requirement in force at the end of the final stage. Your code in /task/workspace is evaluated
afterwards by separate hidden tests of every requirement in force at the end, including
requirements that have stayed unchanged since earlier stages.
"""


SEED_INIT = w2.SEED_INIT
SEED_CORE = w2.SEED_CORE


@dataclass
class W3Truth(w2.ExportTruth):
    values: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    effects: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    register: dict[int, dict[str, dict[str, Any]]] = field(default_factory=dict)
    probes: dict[str, list[list[str]]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d.update(
            generator_version=GENERATOR_VERSION,
            schedule=[[list(x) for x in st] for st in self.schedule],
            values=self.values,
            items=self.items,
            effects=self.effects,
            register={str(k): v for k, v in self.register.items()},
            probes=self.probes,
        )
        return d


def generate_w3(spec: InstanceSpec) -> TaskInstance:
    schedule = schedule_for(spec.name)
    n = len(schedule)
    vals = values_for(spec.seed)
    items = build_items(schedule, vals)
    effects = item_effects(items)
    rng = random.Random(spec.seed)
    avoid: set[str] = set()
    stages = [
        {
            f"stage-{k}/REQUIREMENTS.md": stage_doc(items, k, n),
            "current-tests/test_stage.py": visible_tests(schedule, vals, k, rng, avoid),
        }
        for k in range(1, n + 1)
    ]
    final = config_at(schedule, n)
    probes = {
        i.id: [
            [lit.format(**{k: str(v) for k, v in vals.items()}).lower() for lit in g]
            for g in PROBES[(i.module, i.version)]
        ]
        for i in items
        if i.module is not None and i.version
    }
    truth = W3Truth(
        schedule=schedule,
        final_config=final,
        checks=build_checks(schedule, vals, spec.seed, avoid),
        categories=categories(schedule),
        snapshot_checks=build_snapshot_checks(schedule, vals, spec.seed, avoid),
        notes=f"final config {final}; values {vals}",
        values=vals,
        items=[i.__dict__ for i in items],
        effects=effects,
        register={k: register_truth(items, effects, k) for k in range(1, n + 1)},
        probes=probes,
    )
    files: dict[str, str] = {}
    for st in stages:
        files.update(st)
    return TaskInstance(
        spec=spec,
        prompt=task_prompt(n),
        files=files,
        truth=truth,
        stages=stages,
        stage_updates=[st[f"stage-{k}/REQUIREMENTS.md"] for k, st in enumerate(stages, start=1)],
        generator_version=GENERATOR_VERSION,
        kind="coding",
        workspace_seed={"exporter/__init__.py": SEED_INIT, "exporter/core.py": SEED_CORE},
        scorer_version=SCORER_VERSION,
    )


# ------------------------------------------------------------- evaluation (W2's runner)
EVAL_RUNNER = w2.EVAL_RUNNER
EVAL_MARKER = w2.EVAL_MARKER


def score_export(
    truth: W3Truth,
    eval_output: str | None,
    completed: bool,
    error: str | None = None,
    snapshot_outputs: dict[int, str | None] | None = None,
) -> w2.ExportScore:
    return w2.score_export(truth, eval_output, completed, error, snapshot_outputs)


def checks_json(truth: W3Truth) -> str:
    return json.dumps(truth.checks)


def snapshot_checks_json(truth: W3Truth, stage: int) -> str:
    return json.dumps(truth.snapshot_checks.get(stage, []))


_ = (csv, io, re)  # standard-library modules used by the reference source when embedded
