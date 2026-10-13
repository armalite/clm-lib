"""Solutions for the W3 register workload (export-gen/2), used only by tests.

``INDEPENDENT_SOURCE`` is a second implementation written from the requirement texts and
structured differently from ``clm_lib.w3.ref_export`` (a list of record predicates and output
builders instead of one loop). It cross-checks the reference host-side and is the known-correct
sandbox submission. Faulty submissions are the reference with a fault set or a modified
configuration (``faulty_core``).
"""

from __future__ import annotations

import inspect

from clm_lib import w3

INDEPENDENT_SOURCE = """
import csv
import io
import json


def _visible(cfg, vals, user, params, rec):
    role, mine = user["role"], rec["owner"] == user["name"]
    if role == "viewer" and cfg.get("VIEWER") == 2:
        scope_ok = mine
    elif "SCOPE" in cfg and role != "admin":
        scope_ok = rec["project"] in user["projects"] or (cfg["SCOPE"] == 2 and mine)
    else:
        scope_ok = True
    if not scope_ok:
        return False
    if "STATUS" in cfg and rec["status"] == "archived":
        if params.get("include_archived") is not True:
            return False
        if cfg["STATUS"] == 2 and role != "admin":
            return False
    if "CONF" in cfg and rec["confidential"]:
        allowed = {"audit", vals["p2"]} if cfg["CONF"] == 2 else {"audit"}
        if not (role == "admin" and params.get("purpose") in allowed):
            return False
    if "SINCE" in cfg and "since" in params:
        date = rec.get("updated") if cfg["SINCE"] == 2 and rec.get("updated") else rec["created"]
        if date < params["since"]:
            return False
    if "REGION" in cfg:
        code = vals["region"]
        if cfg["REGION"] == 2:
            hit = rec["region"].lower() == code
            has = any(x.lower() == code for x in user["regions"])
        else:
            hit, has = rec["region"] == code, code in user["regions"]
        if hit and not has:
            return False
    return True


def _title(cfg, t):
    if cfg.get("TITLE") == 2:
        return " ".join(t.split())
    if cfg.get("TITLE") == 1:
        return t.strip()
    return t


def export_for(cfg, vals, records, user, params):
    if cfg.get("VIEWER") == 1 and user["role"] == "viewer":
        raise PermissionError("viewer")
    keep = sorted((r for r in records if _visible(cfg, vals, user, params, r)), key=lambda r: r["id"])
    if "LIMIT" in cfg and "max_records" in params:
        n = params["max_records"]
        if cfg["LIMIT"] == 2 and n > vals["cap"]:
            n = vals["cap"]
        keep = keep[:n]
    fields = ["id", "title", "owner", "amount"]
    if cfg.get("FORMAT", 1) == 1:
        buf = io.StringIO()
        out = csv.writer(buf, lineterminator="\\n")
        out.writerow(fields)
        for r in keep:
            out.writerow([r["id"], _title(cfg, r["title"]), r["owner"], r["amount"]])
        return buf.getvalue()
    if cfg["FORMAT"] == 3:
        fields.append("project")
    doc = {"records": [{f: (_title(cfg, r[f]) if f == "title" else r[f]) for f in fields} for r in keep]}
    if "BY" in cfg and not (cfg["BY"] == 2 and user["role"] == "admin"):
        doc["exported_by"] = user["name"]
    if "CC" in cfg and cfg.get("CONF") == 2 and params.get("purpose") != "audit":
        count = len([r for r in keep if r["confidential"]])
        if count:
            doc["confidential_count"] = count
    return json.dumps(doc, indent=1, sort_keys=True)
"""


def independent_core(cfg: dict[str, int], vals: dict[str, object]) -> str:
    """exporter/core.py for the independent implementation under ``cfg``."""
    return (
        INDEPENDENT_SOURCE
        + f"\nCFG = {cfg!r}\nVALS = {vals!r}\n\n"
        + "def export_records(records, user, params):\n"
        + "    return export_for(CFG, VALS, records, user, params)\n"
    )


def faulty_core(
    cfg: dict[str, int], vals: dict[str, object], faults: frozenset[str] = frozenset()
) -> str:
    """exporter/core.py: the reference rules for ``cfg`` with deliberate ``faults``."""
    return (
        "from typing import Any\n\n"
        + inspect.getsource(w3.ref_export)
        + f"\nCFG = {cfg!r}\nVALS = {vals!r}\nFAULTS = frozenset({sorted(faults)!r})\n\n"
        + "def export_records(records, user, params):\n"
        + "    return ref_export(records, user, params, CFG, VALS, FAULTS)\n"
    )
