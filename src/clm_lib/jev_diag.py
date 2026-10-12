"""Experiment 008 component diagnostic: Jev on labelled (update, entry) cases.

Uses the same request builder and fixed question as the agent integration (``jev.py``).
Cases sharing an update are sent together, one noul question per entry, as in a review action.
Labels were written before any Jev answer existed. The reporting threshold (0.5) is fixed in the
case file before scoring; it is a reporting convention, never an editing policy.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .budget import BudgetExhausted, Ledger
from .jev import JevError, JevPrice, ReviewItem, build_request


def load_cases(path: Path, split: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc, [c for c in doc["cases"] if c["split"] == split]


def run_diagnostic(
    advisor: Any,
    price: JevPrice | None,
    ledger: Ledger,
    cases_path: Path,
    split: str,
    out_root: Path,
    model: str,
) -> Path:
    """One request per distinct update; every attempt recorded (no retries here)."""
    doc, cases = load_cases(cases_path, split)
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    out = out_root / f"{stamp}-{split}"
    (out / "requests").mkdir(parents=True)
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in cases:
        groups.setdefault(c["update_id"], []).append(c)
    results: list[dict[str, Any]] = []
    for n, (uid, group) in enumerate(sorted(groups.items()), start=1):
        body = build_request(
            group[0]["update"], [ReviewItem(c["id"], c["entry"]) for c in group], model
        )
        reserve = price.max_cost(body) if price else 0.0
        try:
            res = ledger.reserve(reserve, f"jev-diagnostic-{stamp}", "jev-diagnostic")
        except BudgetExhausted as exc:
            results.append({"update_id": uid, "error": f"budget: {exc}"})
            break
        rec: dict[str, Any] = {"update_id": uid, "request": body}
        try:
            resp, latency = advisor.call(body)
        except JevError as exc:
            entry = ledger.settle(
                res,
                actual_usd=0.0 if exc.billing == "none" else None,
                status=f"error:{exc.kind}",
                note=json.dumps({"provider": "typesafe", "error": exc.kind}),
            )
            rec.update(error=str(exc)[:300], charged_usd=entry["charged_usd"])
        else:
            cost = price.cost(resp.input_tokens, resp.output_tokens) if price else 0.0
            entry = ledger.settle(
                res,
                actual_usd=cost,
                status="ok",
                note=json.dumps(
                    {
                        "provider": "typesafe",
                        "model": resp.model,
                        "input_tokens": resp.input_tokens,
                        "output_tokens": resp.output_tokens,
                    }
                ),
            )
            rec.update(
                response=resp.raw,
                model=resp.model,
                latency_s=round(latency, 4),
                usage={"input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens},
                cost_usd=round(cost, 8),
                charged_usd=entry["charged_usd"],
                answers={c["id"]: resp.probabilities.get(f"e{i}") for i, c in enumerate(group)},
            )
        (out / "requests" / f"{n:03d}.json").write_text(json.dumps(rec, indent=1))
        results.append({k: v for k, v in rec.items() if k not in ("request", "response")})
    sha = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    summary = {
        "cases_file": str(cases_path),
        "cases_sha256": sha,
        "cases_version": doc["version"],
        "split": split,
        "model_requested": model,
        "results": results,
        "score": score(cases, results, doc["reporting_threshold"]),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return out


def score(
    cases: list[dict[str, Any]], results: list[dict[str, Any]], threshold: float
) -> dict[str, Any]:
    """Counts at the pre-fixed threshold; ambiguous cases and unanswered cases are separate."""
    answers: dict[str, float | None] = {}
    for r in results:
        answers.update(r.get("answers") or {})
    out: dict[str, Any] = {
        "threshold": threshold,
        "affected_flagged": [],
        "affected_missed": [],
        "unaffected_flagged": [],
        "unaffected_clear": [],
        "ambiguous": {},
        "unanswered": [],
    }
    for c in cases:
        p = answers.get(c["id"])
        if p is None:
            out["unanswered"].append(c["id"])
        elif c["label"] == "ambiguous":
            out["ambiguous"][c["id"]] = p
        elif c["label"] == "affected":
            out["affected_flagged" if p >= threshold else "affected_missed"].append(c["id"])
        else:
            out["unaffected_flagged" if p >= threshold else "unaffected_clear"].append(c["id"])
    tp, fn = len(out["affected_flagged"]), len(out["affected_missed"])
    fp = len(out["unaffected_flagged"])
    out["recall"] = round(tp / (tp + fn), 4) if tp + fn else None
    out["precision"] = round(tp / (tp + fp), 4) if tp + fp else None
    lat = [r["latency_s"] for r in results if "latency_s" in r]
    out["requests"] = len(results)
    out["errors"] = sum(1 for r in results if "error" in r)
    out["latency_s"] = {"total": round(sum(lat), 4), "max": max(lat) if lat else None}
    out["typesafe_tokens"] = {
        "input": sum((r.get("usage") or {}).get("input_tokens", 0) for r in results),
        "output": sum((r.get("usage") or {}).get("output_tokens", 0) for r in results),
    }
    out["cost_usd"] = round(sum(r.get("charged_usd", 0.0) for r in results), 8)
    out["models"] = sorted({r["model"] for r in results if "model" in r})
    return out
