"""Experiment 012 component diagnostic: ``result-screen/1`` on labelled search results.

``clm-lib jev-screen-diagnostic --cases <file> --split dev|frozen`` sends one TypeSafe request
per eligible case, built by the same ``build_screen_request`` the runtime uses, applies the same
``select`` policy, and scores it against the cases' labels. No main-model calls. Labels, support
sets and case metadata never reach the request (only task, query, intent and passages).

Scores (eligible cases only):
- support-passage recall: kept ``support`` passages / all ``support`` passages;
- complete-set survival: among cases whose result holds a complete valid support set, the
  share where at least one such set is still complete after screening;
- omitted share: omitted characters / eligible characters, also by label;
- the same numbers for the deterministic query-term selector on the same inputs.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .budget import BudgetExhausted, Ledger
from .jev import JevError, JevPrice
from .jev_screen import (
    MIN_KEEP,
    QUESTION,
    SCOPE,
    SCREEN_VERSION,
    THRESHOLD,
    build_screen_request,
    select,
    term_select,
)

GATE = {
    "eligible_question_share_min": 0.5,
    "support_recall_min": 0.95,
    "omitted_share_min": 0.20,
    "complete_set_losses_max": 1,
}


def _chars(p: dict[str, str]) -> int:
    return len(p["header"]) + 1 + len(p["text"]) + 1


def score_selection(case: dict[str, Any], kept: list[int]) -> dict[str, Any]:
    ps, labels = case["passages"], case["labels"]
    kept_ids = {ps[i]["id"] for i in kept}
    support = [p["id"] for p in ps if labels[p["id"]] == "support"]
    present = [s for s in case["support_sets_present"]]
    survives = any(all(x in kept_ids for x in s) for s in present) if present else None
    omitted = {"support": 0, "context": 0, "irrelevant": 0}
    for p in ps:
        if p["id"] not in kept_ids:
            omitted[labels[p["id"]]] += _chars(p)
    return {
        "kept": sorted(kept_ids, key=[p["id"] for p in ps].index),
        "support_total": len(support),
        "support_kept": sum(1 for x in support if x in kept_ids),
        "complete_set_present": bool(present),
        "complete_set_survives": survives,
        "chars": sum(_chars(p) for p in ps),
        "omitted_chars": sum(omitted.values()),
        "omitted_by_label": omitted,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    st = sum(r["support_total"] for r in rows)
    sk = sum(r["support_kept"] for r in rows)
    present = [r for r in rows if r["complete_set_present"]]
    lost = [r["case_id"] for r in present if not r["complete_set_survives"]]
    ch = sum(r["chars"] for r in rows)
    om = sum(r["omitted_chars"] for r in rows)
    by = {
        k: sum(r["omitted_by_label"][k] for r in rows) for k in ("support", "context", "irrelevant")
    }
    return {
        "cases": len(rows),
        "support_recall": f"{sk}/{st}",
        "support_recall_value": sk / st if st else None,
        "complete_set_cases": len(present),
        "complete_set_lost": lost,
        "omitted_share": om / ch if ch else 0.0,
        "omitted_chars": om,
        "eligible_chars": ch,
        "omitted_by_label": by,
    }


def run_screen_diagnostic(
    advisor: Any,
    price: JevPrice | None,
    ledger: Ledger,
    cases_path: Path,
    split: str,
    out_root: Path,
    model: str,
    retries: int = 1,
    sleep: Any = time.sleep,
) -> Path:
    doc = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = [c for c in doc["cases"] if c["split"] == split]
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    out = out_root / f"{stamp}-screen-{split}"
    (out / "requests").mkdir(parents=True)
    results: list[dict[str, Any]] = []
    stopped = None
    for c in cases:
        if not c["eligible"]:
            results.append({"case_id": c["id"], "eligible": False})
            continue
        body = build_screen_request(c["task"], c["query"], c["intent"], c["passages"], model)
        rec: dict[str, Any] = {
            "case_id": c["id"],
            "eligible": True,
            "request": body,
            "attempts": [],
        }
        resp = None
        for attempt in range(retries + 1):
            try:
                res = ledger.reserve(
                    price.max_cost(body) if price else 0.0, f"jev-screen-{stamp}", "jev-diagnostic"
                )
            except BudgetExhausted as exc:
                stopped = f"budget: {exc}"
                break
            try:
                resp, latency = advisor.call(body)
            except JevError as exc:
                entry = ledger.settle(
                    res,
                    actual_usd=0.0 if exc.billing == "none" else None,
                    status=f"error:{exc.kind}",
                    note=json.dumps({"provider": "typesafe", "error": exc.kind}),
                )
                rec["attempts"].append(
                    {"error": str(exc)[:300], "charged_usd": entry["charged_usd"]}
                )
                if exc.retryable and attempt < retries:
                    sleep(min(10.0, 2.0**attempt))
                    continue
                break
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
            rec["attempts"].append({"charged_usd": entry["charged_usd"]})
            rec.update(
                response=resp.raw,
                model=resp.model,
                latency_s=round(latency, 4),
                usage={"input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens},
            )
            break
        probs = [
            resp.probabilities.get(f"p{i}") if resp else None for i in range(len(c["passages"]))
        ]
        rec["probabilities"] = probs
        if resp is None:
            rec["outcome"] = "unanswered (shown unscreened)"
            kept = list(range(len(c["passages"])))
        else:
            kept, missing = select(c["passages"], probs, THRESHOLD, MIN_KEEP)
            rec["missing_answers"] = len(missing)
        rec["jev"] = score_selection(c, kept)
        rec["term"] = score_selection(c, term_select(c["query"], c["passages"], MIN_KEEP))
        (out / "requests" / f"{c['id'].replace(':', '_')}.json").write_text(
            json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        results.append({k: v for k, v in rec.items() if k not in ("request", "response")})
        if stopped:
            break
    scored = [r for r in results if r.get("eligible")]
    jev = aggregate([{**r["jev"], "case_id": r["case_id"]} for r in scored])
    term = aggregate([{**r["term"], "case_id": r["case_id"]} for r in scored])
    by_q = {(c["instance"], c["question"]) for c in cases}
    el_q = {(c["instance"], c["question"]) for c in cases if c["eligible"]}
    gate = {
        **GATE,
        "eligible_question_share": len(el_q) / len(by_q) if by_q else 0.0,
        "support_recall": jev["support_recall_value"],
        "omitted_share": jev["omitted_share"],
        "complete_set_losses": len(jev["complete_set_lost"]),
        "errors": sum(1 for r in scored if r.get("outcome")),
        "missing_answers": sum(r.get("missing_answers", 0) for r in scored),
    }
    gate["passed"] = (
        gate["eligible_question_share"] >= GATE["eligible_question_share_min"]
        and (gate["support_recall"] or 0) >= GATE["support_recall_min"]
        and gate["omitted_share"] >= GATE["omitted_share_min"]
        and gate["complete_set_losses"] <= GATE["complete_set_losses_max"]
        and gate["errors"] == 0
        and stopped is None
    )
    lat = [r["latency_s"] for r in scored if "latency_s" in r]
    summary = {
        "cases_file": cases_path.name,
        "cases_version": doc["version"],
        "screen_version": SCREEN_VERSION,
        "question": QUESTION,
        "scope": SCOPE,
        "threshold": THRESHOLD,
        "min_keep": MIN_KEEP,
        "split": split,
        "model_requested": model,
        "stopped": stopped,
        "results": results,
        "jev": jev,
        "term_selector": term,
        "gate": gate,
        "latency_s": {
            "total": round(sum(lat), 4),
            "mean": round(sum(lat) / len(lat), 4) if lat else None,
        },
        "tokens": {
            k: sum(r.get("usage", {}).get(k, 0) for r in scored)
            for k in ("input_tokens", "output_tokens")
        },
        "cost_usd": round(sum(a["charged_usd"] for r in scored for a in r["attempts"]), 8),
    }
    import hashlib

    summary["cases_sha256"] = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    (out / "summary.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    return out
