"""Solutions for the W2 export task (export-gen/1), used only by tests.

``INDEPENDENT_SOURCE`` is a second implementation written from the requirement text and structured
differently from ``clm_lib.w2.ref_export`` (a filter pipeline instead of one loop). It cross-checks
the reference host-side and is the known-correct sandbox submission. Faulty submissions are the
reference function with a fault set or a modified configuration (see ``faulty_core``).
"""

from __future__ import annotations

import inspect

from clm_lib import w2

INDEPENDENT_SOURCE = """
import csv
import io
import json


def _allowed_by_role(cfg, user, rec):
    v = cfg.get("PERM")
    if v is None or user["role"] == "admin":
        return True
    in_project = rec["project"] in user["projects"]
    if v >= 2:
        return in_project or rec["owner"] == user["name"]
    return in_project


def _status_ok(cfg, user, params, rec):
    v = cfg.get("STATUS")
    if v is None or rec["status"] == "active":
        return True
    wants = params.get("include_archived") is True
    if v >= 2:
        return wants and user["role"] == "admin"
    return wants


def _confidential_ok(cfg, user, params, rec):
    if not cfg.get("CONF") or not rec["confidential"]:
        return True
    return user["role"] == "admin" and params.get("purpose") == "audit"


def _date_ok(cfg, params, rec):
    if not cfg.get("SINCE") or "since" not in params:
        return True
    return rec["created"] >= params["since"]


def export_for(cfg, records, user, params):
    if cfg.get("PERM") and user["role"] == "viewer":
        raise PermissionError("viewer")
    keep = [
        r
        for r in records
        if _allowed_by_role(cfg, user, r)
        and _status_ok(cfg, user, params, r)
        and _confidential_ok(cfg, user, params, r)
        and _date_ok(cfg, params, r)
    ]
    keep.sort(key=lambda r: r["id"])
    cut = False
    if cfg.get("LIMIT") and "max_records" in params:
        cut = len(keep) > params["max_records"]
        keep = keep[: params["max_records"]]
    fields = ("id", "title", "owner", "amount")
    if cfg["FORMAT"] == 1:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\\n")
        writer.writerow(fields)
        writer.writerows([[r[f] for f in fields] for r in keep])
        return buf.getvalue()
    doc = {"records": [dict((f, r[f]) for f in fields) for r in keep]}
    if cfg.get("OBLIG"):
        doc["exported_by"] = user["name"]
    if cfg.get("LIMIT"):
        doc["truncated"] = cut
    return json.dumps(doc, indent=2)
"""


def independent_core(cfg: dict[str, int]) -> str:
    """exporter/core.py for the independent implementation under ``cfg``."""
    return (
        INDEPENDENT_SOURCE
        + f"\nCFG = {cfg!r}\n\n"
        + "def export_records(records, user, params):\n"
        + "    return export_for(CFG, records, user, params)\n"
    )


def faulty_core(cfg: dict[str, int], faults: frozenset[str] = frozenset()) -> str:
    """exporter/core.py: the reference rules for ``cfg`` with deliberate ``faults``."""
    return (
        "import csv\nimport io\nimport json\nfrom typing import Any\n\nConfig = dict\n\n"
        + inspect.getsource(w2.ref_export)
        + f"\nCFG = {cfg!r}\nFAULTS = frozenset({sorted(faults)!r})\n\n"
        + "def export_records(records, user, params):\n"
        + "    return ref_export(records, user, params, CFG, FAULTS)\n"
    )


INIT = "from .core import export_records  # noqa: F401\n"


def write_core_code(core: str) -> str:
    """Agent-side Python that installs ``core`` as the package implementation."""
    return (
        f"open('exporter/core.py', 'w').write({core!r})\n"
        f"open('exporter/__init__.py', 'w').write({INIT!r})\nprint('written')\n"
    )
