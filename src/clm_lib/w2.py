"""W2 changing-requirements workload (experiment 011 agent pilot): a data-export component.

Generator ``export-gen/1``, scorer ``export-checks/1``. The agent implements
``exporter.export_records(records, user, params) -> str`` while requirements arrive in 5 stages:

- a format change (CSV in stage 1, JSON from stage 3) that leaves every other rule in force;
- permission, status and date rules that the format change does not affect;
- an exception that must stay intact (confidential records, exported only for admin audits);
- a later CHANGED rule that replaces only part of an earlier rule (permissions or status);
- an obligation announced before it applies: when the format leaves CSV, every export must name
  who ran it (``exported_by``), stated in stage 2 and not restated when JSON arrives.

Visible material (fixture mount, read-only): ``stage-N/REQUIREMENTS.md`` (immutable once
released; earlier stages stay readable) and ``current-tests/test_stage.py``, which is replaced
at each release and covers only the rules introduced or changed in the latest stage. The task
prompt says so explicitly; earlier rules are checked only by the hidden evaluation.

Evaluator-only material (host memory until scoring): checks computed by the reference
implementation on inputs different from the visible tests, using only options disclosed by the
final stage, in the categories ``retained`` (rules never changed and unaffected by the format
change), ``exception``, ``replaced`` (the format change and the changed part of a partial
change, with the superseded result recorded so stale output is distinguished), ``kept_part``
(the part of a partially changed rule that stays in force), ``deferred`` (the obligation that
became relevant later), ``final_stage`` and ``interaction``. JSON checks compare parsed values of
named top-level keys, so one missing key fails only the checks about it. Snapshot checks score
the workspace copied at each release against the rules then in force (as in ``coding2``).
"""

from __future__ import annotations

import csv
import io
import json
import random
from dataclasses import dataclass, field
from typing import Any

from .tasks import InstanceSpec, TaskInstance

GENERATOR_VERSION = "export-gen/1"
SCORER_VERSION = "export-checks/1"
MODULES = ("FORMAT", "PERM", "STATUS", "CONF", "SINCE", "OBLIG", "LIMIT")
CATEGORIES = (
    "retained",
    "exception",
    "replaced",
    "kept_part",
    "deferred",
    "final_stage",
    "interaction",
)
OWNERS = ("ana", "ben", "chen", "dara", "eli", "fay")
PROJECTS = ("apollo", "borealis", "cygnus", "delta", "eos")
TITLES = (
    "Q3 report",
    "Budget, draft",
    'The "north" plan',
    "Vendor list",
    "Ops review",
    "Hiring plan, v2",
    "Roadmap",
    'Notes "final"',
    "Audit prep",
    "Risk log",
)

RULES: dict[str, dict[int, str]] = {
    "FORMAT": {
        1: (
            "Format: return CSV text. The first row is the header `id,title,owner,amount`; then "
            "one row per exported record, in ascending id order, with those four fields. Produce "
            "it with Python's csv.writer (default dialect and quoting) and lineterminator='\\n', "
            "writing id as an integer and amount exactly as given in the record. With no "
            "exported records the result is the header row alone (ending in '\\n')."
        ),
        2: (
            "Format (replaces the whole CSV format rule from stage {s}): return JSON text "
            '(json.dumps) of an object whose key "records" is a list with one object per '
            'exported record, in ascending id order, each with exactly the keys "id" (int), '
            '"title", "owner" and "amount" (str; amount exactly as given in the record). The '
            "hidden tests parse the JSON and compare values, not spacing or key order. Which "
            "records are exported does not change."
        ),
    },
    "PERM": {
        1: (
            "Permissions: if user['role'] is 'viewer', raise PermissionError (nothing is "
            "exported). A user whose role is 'admin' may export records of any project. Any other "
            "role exports only records whose 'project' is in user['projects']; other records are "
            "left out without an error."
        ),
        2: (
            "Permissions (partly replaces the stage-{s} rule): a user whose role is not 'admin' "
            "now also receives the records they own (record 'owner' equal to user['name']), even "
            "outside their projects. The 'viewer' rule (PermissionError) and the 'admin' rule from "
            "stage {s} are unchanged."
        ),
    },
    "STATUS": {
        1: (
            "Status: only records with status 'active' are exported, unless "
            "params.get('include_archived') is True; then records with status 'archived' are "
            "exported too."
        ),
        2: (
            "Status (partly replaces the stage-{s} rule): include_archived is now honoured only "
            "when user['role'] is 'admin'; for every other role, archived records are never "
            "exported. The rest of the stage-{s} status rule is unchanged."
        ),
    },
    "CONF": {
        1: (
            "Confidential records: a record whose 'confidential' is True is never exported, with "
            "one exception: it is exported when user['role'] is 'admin' and params.get('purpose') "
            "== 'audit' (every other rule still applies to it)."
        ),
    },
    "SINCE": {
        1: (
            "Date filter: params may contain 'since', a date string 'YYYY-MM-DD'. When it is "
            "present, only records whose 'created' date is on or after that date are exported."
        ),
    },
    "OBLIG": {
        1: (
            "Compliance obligation (no code change is needed while the format is CSV): if a "
            "later stage changes the export format from CSV, every export in the new format must "
            "also include a top-level field \"exported_by\" whose value is user['name']. This "
            "obligation stays in force unless a later stage withdraws it."
        ),
    },
    "LIMIT": {
        1: (
            "Record limit: params may contain 'max_records', an int of at least 1. Export only "
            "the first max_records records (in ascending id order) that remain after every other "
            'rule. The JSON object must also have the key "truncated": true if this limit left '
            "out at least one record, otherwise false (also when max_records is absent)."
        ),
    },
}

Schedule = list[list[tuple[str, int]]]
Config = dict[str, int]

SCHEDULES: dict[str, Schedule] = {
    "dev-1": [
        [("FORMAT", 1), ("PERM", 1), ("STATUS", 1)],
        [("CONF", 1), ("SINCE", 1), ("OBLIG", 1)],
        [("FORMAT", 2)],
        [("PERM", 2)],
        [("LIMIT", 1)],
    ],
    "dev-2": [
        [("FORMAT", 1), ("PERM", 1), ("CONF", 1)],
        [("STATUS", 1), ("OBLIG", 1)],
        [("SINCE", 1), ("FORMAT", 2)],
        [("STATUS", 2)],
        [("LIMIT", 1)],
    ],
    # The live integration smoke only (never part of an outcome comparison).
    "smoke-1": [
        [("FORMAT", 1), ("PERM", 1), ("OBLIG", 1)],
        [("FORMAT", 2)],
    ],
}
# The smoke instance's prompt asks for one context edit early, so the integration is exercised.
SMOKE_NOTE = """
Integration smoke run (this instance only): in one of your first three steps, add a short note
entry to context.json recording what the stage-1 requirements ask for, and then decide on that
context change as the runtime describes. After that, work normally.
"""


def schedule_for(name: str) -> Schedule:
    return SCHEDULES[name.split("-", 1)[1]]


def config_at(schedule: Schedule, stage: int) -> Config:
    cfg: Config = {}
    for changes in schedule[:stage]:
        for module, version in changes:
            cfg[module] = version
    return cfg


def introduced_at(schedule: Schedule) -> dict[str, int]:
    out: dict[str, int] = {}
    for stage, changes in enumerate(schedule, start=1):
        for module, _ in changes:
            out.setdefault(module, stage)
    return out


def categories(schedule: Schedule) -> dict[str, str]:
    final = config_at(schedule, len(schedule))
    intro = introduced_at(schedule)
    cats: dict[str, str] = {}
    for module, version in final.items():
        if version > 1:
            cats[module] = "replaced"
        elif module == "CONF":
            cats[module] = "exception"
        elif module == "OBLIG":
            cats[module] = "deferred"
        elif intro[module] == len(schedule):
            cats[module] = "final_stage"
        else:
            cats[module] = "retained"
    return cats


# ------------------------------------------------------------- reference implementation
# Faults (offline scorer validation only): "partial:PERM" applies the owner rule to viewers too;
# "partial:PERM_REPLACE" replaces the project rule by the owner rule; "partial:STATUS" ignores
# include_archived for everyone; "exception:CONF_NONE" drops the exception; "exception:CONF_ADMIN"
# widens it to every admin export; "order:LIMIT_FIRST" applies the limit before the other rules.
def ref_export(
    records: list[dict[str, Any]],
    user: dict[str, Any],
    params: dict[str, Any],
    cfg: Config,
    faults: frozenset[str] = frozenset(),
) -> str:
    role = user["role"]
    perm = cfg.get("PERM", 0)
    viewer_owner = perm == 2 and "partial:PERM" in faults
    if perm and role == "viewer" and not viewer_owner:
        raise PermissionError("viewers may not export")
    rows = sorted(records, key=lambda r: r["id"])
    limit = params.get("max_records") if cfg.get("LIMIT") else None
    truncated = False
    if limit is not None and "order:LIMIT_FIRST" in faults:
        truncated, rows = len(rows) > limit, rows[:limit]
    out = []
    for r in rows:
        if perm:
            own = r["owner"] == user["name"]
            if role == "viewer":
                ok = own
            elif role == "admin":
                ok = True
            elif perm == 2 and "partial:PERM_REPLACE" in faults:
                ok = own
            else:
                ok = r["project"] in user["projects"] or (perm == 2 and own)
            if not ok:
                continue
        status = cfg.get("STATUS", 0)
        if status and r["status"] != "active":
            include = params.get("include_archived") is True
            if status == 2 and role != "admin":
                include = False
            if status == 2 and "partial:STATUS" in faults:
                include = False
            if not include:
                continue
        if cfg.get("CONF") and r["confidential"]:
            allowed = role == "admin" and params.get("purpose") == "audit"
            if "exception:CONF_NONE" in faults:
                allowed = False
            if "exception:CONF_ADMIN" in faults:
                allowed = role == "admin"
            if not allowed:
                continue
        if cfg.get("SINCE") and "since" in params and r["created"] < params["since"]:
            continue
        out.append(r)
    if limit is not None and "order:LIMIT_FIRST" not in faults:
        truncated, out = len(out) > limit, out[:limit]
    if cfg["FORMAT"] == 1:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["id", "title", "owner", "amount"])
        for r in out:
            w.writerow([r["id"], r["title"], r["owner"], r["amount"]])
        return buf.getvalue()
    obj: dict[str, Any] = {
        "records": [{k: r[k] for k in ("id", "title", "owner", "amount")} for r in out]
    }
    if cfg.get("OBLIG"):
        obj["exported_by"] = user["name"]
    if cfg.get("LIMIT"):
        obj["truncated"] = truncated
    return json.dumps(obj)


def keys_for(category: str) -> list[str]:
    """Top-level JSON keys a check compares (each check fails only on what it is about)."""
    if category == "deferred":
        return ["exported_by"]
    if category == "final_stage":
        return ["records", "truncated"]
    return ["records"]


def observe(
    fn_args: list[Any], cfg: Config, keys: list[str], faults: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """What a check compares: an exception, the CSV text, or the named JSON values."""
    try:
        recs, user, params = json.loads(json.dumps(fn_args))
        text = ref_export(recs, user, params, cfg, faults)
    except PermissionError:
        return {"raises": "PermissionError"}
    if cfg["FORMAT"] == 1:
        return {"text": text}
    parsed = json.loads(text)
    return {"json": {k: parsed.get(k) for k in keys}}


def _without(cfg: Config, module: str) -> Config:
    return {k: v for k, v in cfg.items() if k != module}


def _with_old(cfg: Config, module: str) -> Config:
    return {**cfg, module: cfg[module] - 1}


# ------------------------------------------------------------- case generation
def _records(rng: random.Random, n: int) -> list[dict[str, Any]]:
    ids = rng.sample(range(1, 1000), n)
    out = []
    for i in ids:
        dec = rng.choice([0, 1, 2, 2])
        amount = str(rng.randrange(1, 900)) + (
            f".{rng.randrange(0, 10**dec):0{dec}d}" if dec else ""
        )
        out.append(
            {
                "id": i,
                "title": rng.choice(TITLES),
                "owner": rng.choice(OWNERS),
                "project": rng.choice(PROJECTS),
                "status": rng.choice(["active", "active", "archived"]),
                "amount": amount,
                "confidential": rng.random() < 0.3,
                "created": f"2026-{rng.randrange(1, 10):02d}-{rng.randrange(1, 29):02d}",
            }
        )
    return out


def _user(rng: random.Random, role: str | None = None) -> dict[str, Any]:
    return {
        "name": rng.choice(OWNERS),
        "role": role or rng.choice(["admin", "member", "member", "viewer"]),
        "projects": sorted(rng.sample(PROJECTS, rng.randrange(1, 3))),
    }


def _params(rng: random.Random, cfg: Config) -> dict[str, Any]:
    """Random options, only those disclosed by ``cfg``."""
    p: dict[str, Any] = {}
    if "STATUS" in cfg and rng.random() < 0.5:
        p["include_archived"] = rng.random() < 0.7
    if "CONF" in cfg and rng.random() < 0.5:
        p["purpose"] = rng.choice(["audit", "report"])
    if "SINCE" in cfg and rng.random() < 0.5:
        p["since"] = f"2026-{rng.randrange(2, 9):02d}-{rng.randrange(1, 29):02d}"
    if "LIMIT" in cfg and rng.random() < 0.5:
        p["max_records"] = rng.randrange(1, 4)
    return p


Case = tuple[list[Any], bool]  # ([records, user, params], boundary)

KINDS: dict[str, list[tuple[str, bool]]] = {
    "FORMAT": [("quoting", True), ("plain", False), ("empty", True), ("plain", False)],
    "PERM": [("member_mixed", False), ("owner_outside", True), ("viewer", True), ("admin", False)],
    "STATUS": [("include_admin", False), ("include_member", True), ("default", False)],
    "CONF": [("admin_audit", True), ("admin_report", True), ("member", False)],
    "SINCE": [("boundary", True), ("plain", False)],
    "OBLIG": [("plain", False)],
    "LIMIT": [("truncate", False), ("exact", True), ("absent", True)],
}


def module_case(module: str, rng: random.Random, cfg: Config, hint: int | None = None) -> Case:
    kinds = KINDS[module]
    kind, boundary = kinds[hint % len(kinds)] if hint is not None else rng.choice(kinds)
    recs = _records(rng, rng.randrange(3, 7))
    user = _user(rng, "member" if module in ("SINCE", "OBLIG", "LIMIT") else None)
    params = _params(rng, cfg)
    if module == "FORMAT":
        user["role"] = rng.choice(["admin", "member"])
        if kind == "quoting":
            recs[0]["title"] = rng.choice(["Budget, draft", 'The "north" plan', 'Notes "final"'])
            recs[0].update(project=user["projects"][0], status="active", confidential=False)
        elif kind == "empty":
            user["role"] = "member"
            for r in recs:
                r["project"] = next(p for p in PROJECTS if p not in user["projects"])
                r["owner"] = next(o for o in OWNERS if o != user["name"])
    elif module == "PERM":
        user["role"] = {"viewer": "viewer", "admin": "admin"}.get(kind, "member")
        if kind == "owner_outside":
            outside = next(p for p in PROJECTS if p not in user["projects"])
            recs[0].update(owner=user["name"], project=outside, status="active", confidential=False)
    elif module == "STATUS":
        user["role"] = "admin" if kind == "include_admin" else "member"
        if kind != "default":
            params["include_archived"] = True
        for r in recs[:2]:
            r.update(status="archived", project=user["projects"][0], confidential=False)
    elif module == "CONF":
        user["role"] = "member" if kind == "member" else "admin"
        if kind == "admin_audit":
            params["purpose"] = "audit"
        elif kind == "admin_report":
            params["purpose"] = rng.choice(["report", "review"])
        for r in recs[:2]:
            r.update(confidential=True, status="active", project=user["projects"][0])
    elif module == "SINCE":
        recs[0].update(project=user["projects"][0], status="active", confidential=False)
        params["since"] = recs[0]["created"] if kind == "boundary" else "2026-05-01"
    elif module == "LIMIT":
        for r in recs:
            r.update(project=user["projects"][0], status="active", confidential=False)
        params.pop("since", None)
        if kind == "truncate":
            params["max_records"] = rng.randrange(1, 3)
        elif kind == "exact":
            params["max_records"] = len(recs)
        else:
            params.pop("max_records", None)
    return [recs, user, params], boundary


def interaction_case(rng: random.Random, cfg: Config) -> Case:
    recs = _records(rng, rng.randrange(5, 9))
    user = _user(rng, rng.choice(["admin", "member"]))
    params = _params(rng, cfg)
    for r in recs[:3]:
        r["project"] = user["projects"][0]
    return [recs, user, params], False


def _exported(args: list[Any], cfg: Config) -> int:
    try:
        recs, user, params = json.loads(json.dumps(args))
        text = ref_export(recs, user, params, cfg)
    except PermissionError:
        return 0
    if cfg["FORMAT"] == 1:
        return text.count("\n") - 1
    return len(json.loads(text)["records"])


PARTIAL_FAULTS = {"PERM": ("partial:PERM", "partial:PERM_REPLACE"), "STATUS": ("partial:STATUS",)}


def purpose_ok(purpose: str, module: str, args: list[Any], cfg: Config, keys: list[str]) -> bool:
    exp = observe(args, cfg, keys)
    if module == "FORMAT":
        if purpose == "changed":
            return _exported(args, cfg) > 0 or exp == {"json": {"records": []}}
        return True
    if module == "OBLIG" and cfg.get("FORMAT") == 1:
        return False  # no observable effect while the format is CSV
    if purpose == "meaningful":
        return exp != observe(args, _without(cfg, module), keys)
    if purpose == "exception":  # the exception applies: dropping it changes the result
        return exp != observe(args, cfg, keys, frozenset({"exception:CONF_NONE"}))
    if purpose == "changed":
        return exp != observe(args, _with_old(cfg, module), keys)
    if purpose == "kept":
        if exp != observe(args, _with_old(cfg, module), keys):
            return False
        faults = PARTIAL_FAULTS.get(module, ())
        return any(exp != observe(args, cfg, keys, frozenset({f})) for f in faults) or (
            exp != observe(args, _without(cfg, module), keys)
        )
    if purpose == "interaction":
        others = [m for m in cfg if m not in ("FORMAT", "OBLIG")]
        return sum(exp != observe(args, _without(cfg, m), keys) for m in others) >= 3
    raise KeyError(purpose)


def pick_cases(
    module: str,
    purpose: str,
    rng: random.Random,
    cfg: Config,
    n: int,
    avoid: set[str],
    keys: list[str],
    cycle_kinds: bool = True,
) -> list[Case]:
    picked: list[Case] = []
    tries = 0
    for _ in range(6000):
        if len(picked) >= n:
            break
        tries += 1
        hint = len(picked) if cycle_kinds and tries <= 150 else None
        args, boundary = (
            interaction_case(rng, cfg)
            if purpose == "interaction"
            else module_case(module, rng, cfg, hint)
        )
        key = json.dumps(args, sort_keys=True)
        if key in avoid or not purpose_ok(purpose, module, args, cfg, keys):
            continue
        avoid.add(key)
        picked.append((args, boundary))
        tries = 0
    if len(picked) < n:
        raise RuntimeError(f"could not generate {n} {purpose} cases for {module} under {cfg}")
    return picked


def _check(
    cid: str, module: str, category: str, case: Case, cfg: Config, keys: list[str]
) -> dict[str, Any]:
    args, boundary = case
    expect = observe(args, cfg, keys)
    stale = {}
    for m, v in sorted(cfg.items()):
        if v > 1:
            old_cfg = _with_old(cfg, m)
            old = observe(args, old_cfg, keys)
            if old != expect:
                stale[m] = old
    return {
        "id": cid,
        "module": module,
        "category": category,
        "boundary": boundary,
        "fn": "export_records",
        "args": args,
        "mode": "csv" if cfg["FORMAT"] == 1 else "json",
        "keys": keys,
        "expect": expect,
        "stale": stale,
    }


def build_checks(schedule: Schedule, seed: int, avoid: set[str]) -> list[dict[str, Any]]:
    final = config_at(schedule, len(schedule))
    cats = categories(schedule)
    rng = random.Random(seed * 7919 + 17)
    checks: list[dict[str, Any]] = []
    for module in [m for m in MODULES if m in final]:
        cat = cats[module]
        keys = keys_for(cat)
        purpose = "changed" if cat == "replaced" else "meaningful"
        n = 3 if cat == "deferred" else 4
        plan = [(purpose, n)] if cat != "exception" else [("meaningful", 2), ("exception", 2)]
        cases = [c for p, k in plan for c in pick_cases(module, p, rng, final, k, avoid, keys)]
        for i, case in enumerate(cases, 1):
            checks.append(_check(f"{module}-{cat}-{i}", module, cat, case, final, keys))
        if cat == "replaced" and module in PARTIAL_FAULTS:
            for i, case in enumerate(pick_cases(module, "kept", rng, final, 3, avoid, keys), 1):
                checks.append(
                    _check(f"{module}-kept_part-{i}", module, "kept_part", case, final, keys)
                )
    keys = keys_for("interaction")
    if len([m for m in final if m not in ("FORMAT", "OBLIG")]) < 3:
        return checks  # too few filtering rules for interaction checks (the smoke instance)
    for i, case in enumerate(
        pick_cases("*", "interaction", rng, final, 5, avoid, keys, cycle_kinds=False), 1
    ):
        checks.append(_check(f"interaction-{i}", "*", "interaction", case, final, keys))
    return checks


def build_snapshot_checks(
    schedule: Schedule, seed: int, avoid: set[str]
) -> dict[int, list[dict[str, Any]]]:
    rng = random.Random(seed * 104729 + 11)
    out: dict[int, list[dict[str, Any]]] = {}
    for stage in range(1, len(schedule)):
        cfg = config_at(schedule, stage)
        checks = []
        for module in [m for m in MODULES if m in cfg]:
            if module == "OBLIG" and cfg["FORMAT"] == 1:
                continue
            keys = keys_for("deferred" if module == "OBLIG" else "retained")
            cases = pick_cases(module, "meaningful", rng, cfg, 2, avoid, keys, cycle_kinds=False)
            for i, case in enumerate(cases, start=1):
                c = _check(f"s{stage}-{module}-{i}", module, "snapshot", case, cfg, keys)
                c["version"] = cfg[module]
                checks.append(c)
        out[stage] = checks
    return out


# ------------------------------------------------------------- documents and tests
API_DOC = """## Package and API

Implement `exporter.export_records(records, user, params) -> str` in the package
/task/workspace/exporter/ (a starting skeleton is provided). Standard library only.

- `records`: a list of dicts with exactly these keys: "id" (int, unique in the list), "title"
  (str), "owner" (str), "project" (str), "status" ("active" or "archived"), "amount" (a
  decimal string such as "12.50"; keep it exactly as given), "confidential" (bool) and
  "created" (a date string "YYYY-MM-DD"). Do not modify the input.
- `user`: a dict with "name" (str), "role" (str) and "projects" (list of str).
- `params`: a dict of export options; it may be empty. Rules introduce the options; ignore keys
  that no rule in force mentions.
- Base behaviour, unless a rule says otherwise: every record is exported, in ascending id
  order, in the format that the format rule specifies.
"""


def stage_doc(schedule: Schedule, stage: int) -> str:
    n = len(schedule)
    intro = introduced_at(schedule)
    head = [f"# Stage {stage} of {n} requirements", ""]
    if stage == 1:
        head += [API_DOC, "## Rules introduced in stage 1", ""]
    else:
        head += [
            "Only the changes are listed. A CHANGED rule says which part of an earlier rule it "
            "replaces; every other earlier rule, obligation and exception stays in force.",
            "",
            "## Changes",
            "",
        ]
    for module, version in schedule[stage - 1]:
        tag = "NEW" if version == 1 else "CHANGED"
        head.append(f"- [{tag}] {RULES[module][version].format(s=intro[module])}")
    head += [
        "",
        "Visible tests for the rules introduced or changed in this stage only: "
        "/task/fixtures/current-tests/test_stage.py (replaced at each stage). Earlier rules still "
        "apply and are checked by the hidden evaluation.",
        "",
    ]
    return "\n".join(head)


def visible_tests(schedule: Schedule, stage: int, rng: random.Random, avoid: set[str]) -> str:
    cfg = config_at(schedule, stage)
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
    for module, version in schedule[stage - 1]:
        if module == "OBLIG" and cfg["FORMAT"] == 1:
            continue
        cat = "final_stage" if module == "LIMIT" else "retained"
        keys = keys_for(cat)
        purpose = "changed" if version > 1 else "meaningful"
        cases = pick_cases(module, purpose, rng, cfg, 2, avoid, keys, cycle_kinds=False)
        if module == "CONF":  # one example of the exception applying
            cases = cases[:1] + pick_cases(module, "exception", rng, cfg, 1, avoid, keys, False)
        for args, _ in cases:
            n += 1
            exp = observe(args, cfg, keys)
            call = f"exporter.export_records(*{args!r})"
            out.append(f"    def test_{module.lower()}_{n}(self):")
            if "raises" in exp:
                out.append(f"        with self.assertRaises(PermissionError):\n            {call}")
            elif "text" in exp:
                out.append(f"        self.assertEqual({call}, {exp['text']!r})")
            else:
                out.append(f"        out = json.loads({call})")
                for k, v in exp["json"].items():
                    out.append(f"        self.assertEqual(out[{k!r}], {v!r})")
            out.append("")
    if n == 0:
        out += ["    def test_nothing_new(self):", "        pass", ""]
    out += ["", 'if __name__ == "__main__":', "    unittest.main()", ""]
    return "\n".join(out)


SEED_INIT = '''"""Data-export package (implement the requirements in /task/fixtures/stage-*/)."""

from .core import export_records

__all__ = ["export_records"]
'''
SEED_CORE = '''"""Implementation goes here."""


def export_records(records, user, params):
    raise NotImplementedError
'''


def task_prompt(n_stages: int, smoke: bool = False) -> str:
    text = f"""Software task with requirements released in {n_stages} stages over time.

Implement the Python package `exporter` in /task/workspace/exporter/ (a skeleton is provided; you
may add modules and functions). Requirements are under /task/fixtures/stage-N/REQUIREMENTS.md;
only stage 1 is available now, and earlier requirement files stay readable after later stages
are released.

Visible tests: /task/fixtures/current-tests/test_stage.py covers only the rules introduced or
changed in the latest released stage, and is replaced at each stage. Rules from earlier stages
still apply but are not in the visible tests. To run them from your code:
  import subprocess, sys
  subprocess.run([sys.executable, "-m", "unittest", "discover", "-s",
                  "/task/fixtures/current-tests"], cwd="/task/workspace")

Staged requirements:
- Reply {{"action": "advance"}} to release the next stage when you are ready. Its requirements
  appear under /task/fixtures/stage-N/ and are added to your working context.
- Later stages list only changes. A rule marked CHANGED says which part of an earlier rule it
  replaces; every other earlier rule, obligation and exception, and every part of a rule that is
  not replaced, stays in force.
- A final answer is accepted only after all {n_stages} stages are released.

Finish with {{"action": "final", "answer": {{"summary": "..."}}}} once the package implements every
requirement in force at the end of the final stage. Your code in /task/workspace is evaluated
afterwards by separate hidden tests of every requirement in force at the end, including
requirements that have stayed unchanged since earlier stages.
"""
    return text + SMOKE_NOTE if smoke else text


@dataclass
class ExportTruth:
    schedule: Schedule
    final_config: Config
    checks: list[dict[str, Any]]
    categories: dict[str, str]
    snapshot_checks: dict[int, list[dict[str, Any]]]
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "coding",
            "generator_version": GENERATOR_VERSION,
            "schedule": [[list(x) for x in st] for st in self.schedule],
            "final_config": self.final_config,
            "categories": self.categories,
            "checks": self.checks,
            "snapshot_checks": {str(k): v for k, v in self.snapshot_checks.items()},
            "notes": self.notes,
        }


def generate_w2(spec: InstanceSpec) -> TaskInstance:
    schedule = schedule_for(spec.name)
    n = len(schedule)
    rng = random.Random(spec.seed)
    avoid: set[str] = set()
    stages = [
        {
            f"stage-{k}/REQUIREMENTS.md": stage_doc(schedule, k),
            "current-tests/test_stage.py": visible_tests(schedule, k, rng, avoid),
        }
        for k in range(1, n + 1)
    ]
    final = config_at(schedule, n)
    truth = ExportTruth(
        schedule=schedule,
        final_config=final,
        checks=build_checks(schedule, spec.seed, avoid),
        categories=categories(schedule),
        snapshot_checks=build_snapshot_checks(schedule, spec.seed, avoid),
        notes=f"final config {final}",
    )
    files: dict[str, str] = {}
    for st in stages:
        files.update(st)
    return TaskInstance(
        spec=spec,
        prompt=task_prompt(n, smoke=spec.name.startswith("w2-smoke")),
        files=files,
        truth=truth,
        stages=stages,
        stage_updates=[st[f"stage-{k}/REQUIREMENTS.md"] for k, st in enumerate(stages, start=1)],
        generator_version=GENERATOR_VERSION,
        kind="coding",
        workspace_seed={"exporter/__init__.py": SEED_INIT, "exporter/core.py": SEED_CORE},
        scorer_version=SCORER_VERSION,
    )


# ------------------------------------------------------------- evaluation
EVAL_MARKER = "@@CLM-EVAL@@"

# Runs inside a fresh sandbox container with a submission copy at /task/workspace and the checks
# at /task/fixtures/checks.json (read-only). Each check has a 5 s alarm.
EVAL_RUNNER = r"""
import json, signal, sys
sys.path.insert(0, '/task/workspace')
checks = json.load(open('/task/fixtures/checks.json'))
def _alarm(*_):
    raise TimeoutError('check timed out')
signal.signal(signal.SIGALRM, _alarm)
try:
    import exporter
    import_error = None
except BaseException as e:
    exporter, import_error = None, repr(e)[:300]
results = []
for c in checks:
    r = {'id': c['id']}
    if exporter is None:
        r.update(passed=False, stale=[], observed={'import_error': import_error})
        results.append(r)
        continue
    raw = None
    signal.alarm(5)
    try:
        f = getattr(exporter, c['fn'], None)
        if f is None:
            obs = {'raises': 'AttributeError', 'detail': 'missing function ' + c['fn']}
        else:
            raw = f(*json.loads(json.dumps(c['args'])))
            if type(raw) is not str:
                obs = {'type_error': type(raw).__name__}
            elif c['mode'] == 'csv':
                obs = {'text': raw}
            else:
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
                if type(parsed) is not dict:
                    obs = {'not_json_object': raw[:300]}
                else:
                    obs = {'json': {k: parsed.get(k) for k in c['keys']}}
    except PermissionError:
        obs = {'raises': 'PermissionError'}
    except BaseException as e:
        obs = {'raises': type(e).__name__, 'detail': str(e)[:200]}
    finally:
        signal.alarm(0)
    r['observed'] = obs
    r['passed'] = obs == c['expect']
    stale = []
    if not r['passed']:
        for m, old in (c.get('stale') or {}).items():
            if obs == old or ('text' in old and isinstance(raw, str) and raw == old['text']):
                stale.append(m)
    r['stale'] = sorted(stale)
    results.append(r)
print('@@CLM-EVAL@@' + json.dumps({'import_error': import_error, 'results': results}))
"""


def _parse(output: str | None) -> tuple[list[dict[str, Any]], str | None, str | None]:
    if output is None or EVAL_MARKER not in output:
        return [], None, "evaluator produced no result"
    payload = json.loads(output.rsplit(EVAL_MARKER, 1)[1].strip().splitlines()[0])
    return payload["results"], payload["import_error"], None


@dataclass
class ExportScore:
    completed: bool
    checks: int
    passed: int
    by_category: dict[str, dict[str, int]]
    stale: int
    stale_modules: dict[str, int]
    boundary: dict[str, int]
    type_errors: int
    retained_rule_failures: int  # failed retained + exception + kept_part checks
    retained_failed_modules: list[str]
    failed_modules: list[str]
    regressions: list[str]
    snapshots: dict[str, dict[str, Any]]
    import_error: str | None
    strict_success: bool
    outcome: str
    evaluation_error: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "components": self.components}

    @property
    def components(self) -> dict[str, float]:
        out = {"all_checks": self.passed / self.checks if self.checks else 0.0}
        for cat, v in self.by_category.items():
            out[cat] = v["passed"] / v["total"] if v["total"] else 0.0
        return out


def score_snapshots(
    truth: ExportTruth, outputs: dict[int, str | None]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for stage, output in sorted(outputs.items()):
        checks = {c["id"]: c for c in truth.snapshot_checks.get(stage, [])}
        results, import_error, error = _parse(output)
        mods: dict[str, dict[str, int]] = {}
        for c in checks.values():
            mods.setdefault(c["module"], {"total": 0, "passed": 0, "version": c["version"]})
            mods[c["module"]]["total"] += 1
        for r in results:
            if r["passed"] and r["id"] in checks:
                mods[checks[r["id"]]["module"]]["passed"] += 1
        out[str(stage)] = {"modules": mods, "import_error": import_error, "error": error}
    return out


def score_export(
    truth: ExportTruth,
    eval_output: str | None,
    completed: bool,
    error: str | None = None,
    snapshot_outputs: dict[int, str | None] | None = None,
) -> ExportScore:
    by: dict[str, dict[str, int]] = {}
    for c in truth.checks:
        by.setdefault(c["category"], {"total": 0, "passed": 0, "stale": 0})["total"] += 1
    results, import_error, parse_error = _parse(eval_output)
    if error is None:
        error = parse_error
    by_id = {c["id"]: c for c in truth.checks}
    passed = stale = type_errors = 0
    stale_mods: dict[str, int] = {}
    boundary = {"total": sum(1 for c in truth.checks if c["boundary"]), "passed": 0}
    failed_retained: set[str] = set()
    failed: set[str] = set()
    retained_failures = 0
    for r in results:
        c = by_id[r["id"]]
        cat = c["category"]
        if "type_error" in r.get("observed", {}):
            type_errors += 1
        if r["passed"]:
            passed += 1
            by[cat]["passed"] += 1
            boundary["passed"] += c["boundary"]
            continue
        failed.add(c["module"])
        if r["stale"]:
            stale += 1
            by[cat]["stale"] += 1
            for m in r["stale"]:
                stale_mods[m] = stale_mods.get(m, 0) + 1
        if cat in ("retained", "exception", "kept_part"):
            retained_failures += 1
            failed_retained.add(c["module"])
    snaps = score_snapshots(truth, snapshot_outputs or {})
    regressions = []
    for module in sorted(failed_retained):
        if truth.categories.get(module) not in ("retained", "exception"):
            continue
        version = truth.final_config[module]
        for snap in snaps.values():
            m = snap["modules"].get(module)
            if m and m["version"] == version and m["total"] and m["passed"] == m["total"]:
                regressions.append(module)
                break
    total = len(truth.checks)
    strict = completed and error is None and passed == total
    if error is not None:
        outcome = "evaluation_error"
    elif strict:
        outcome = "correct"
    elif not completed:
        outcome = "correct_unsubmitted" if passed == total else "no_submission"
    else:
        outcome = "failed_checks"
    return ExportScore(
        completed=completed,
        checks=total,
        passed=passed,
        by_category=by,
        stale=stale,
        stale_modules=stale_mods,
        boundary=boundary,
        type_errors=type_errors,
        retained_rule_failures=retained_failures,
        retained_failed_modules=sorted(failed_retained),
        failed_modules=sorted(failed),
        regressions=regressions,
        snapshots=snaps,
        import_error=import_error,
        strict_success=strict,
        outcome=outcome,
        evaluation_error=error,
        results=results,
    )


def checks_json(truth: ExportTruth) -> str:
    return json.dumps(truth.checks)


def snapshot_checks_json(truth: ExportTruth, stage: int) -> str:
    return json.dumps(truth.snapshot_checks.get(stage, []))
