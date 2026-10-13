"""Experiment 011 component diagnostic: can Jev detect information wrongly lost or changed in a
proposed context rewrite?

One request per case. The state holds only what a real checker could see: the task text, the
current context, any already released update and the proposed replacement. Labels,
justifications, categories, families and provenance are never sent. Three separate noul
questions (``jev-edit-check/1``); a case is flagged if any probability reaches the reporting
threshold (a convention fixed before scoring, not an editing policy). Deterministic reference
checks (reference loss, new numbers) are reported alongside for comparison.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .budget import BudgetExhausted, Ledger
from .jev import JevError, JevPrice

QUESTION_VERSION = "jev-edit-check/1"
PAYLOAD_FIELDS = ("task", "current_context", "update", "proposed_context")
SCOPE = (
    "Compare state.current_context (the working context before the rewrite) with "
    "state.proposed_context (the replacement). state.task is the task the context serves and "
    "state.update is already released information that may justify changes (empty if none)."
)
QUESTIONS: dict[str, dict[str, Any]] = {
    "lost": {
        "question": (
            "Does the proposed context drop, or mark as finished, an obligation, requirement, "
            "exception, open question or unresolved item that the current context still treats "
            "as active, when neither the current context nor the update says it is finished, "
            "superseded or no longer needed?"
        ),
        "criteria": {
            "true": (
                "Yes if an item that is still active in the current context is missing from the "
                "proposed context, or is now described as done, closed, answered or no longer "
                "applicable, and neither the current context nor the update supports that. This "
                "includes losing an exception or condition attached to a requirement that is kept."
            ),
            "false": (
                "No if every active item is still present, possibly paraphrased, shortened or "
                "merged; if removed items were superseded, completed, duplicated or closed "
                "according to the current context or the update; or if historical information is "
                "kept as history. A shorter context is not by itself a loss."
            ),
        },
    },
    "changed": {
        "question": (
            "Does the proposed context state a value, number, limit, condition, status or "
            "relationship differently from the current context, without the update or the "
            "current context supporting the change?"
        ),
        "criteria": {
            "true": (
                "Yes if a value, threshold, amount, date, percentage, status, scope or "
                "which-applies-to-what relationship differs from the current context and the "
                "update does not justify the new version."
            ),
            "false": (
                "No if values, conditions and statuses mean the same (wording may differ), or if "
                "every difference is justified by the update or by the current context itself."
            ),
        },
    },
    "evidence": {
        "question": (
            "Does the proposed context remove the evidence reference supporting a claim it "
            "keeps, or attach a kept claim to a different reference than the current context did?"
        ),
        "criteria": {
            "true": (
                "Yes if a claim that remains in the proposed context had a source reference "
                "(file:line, document section, ticket, dashboard, link or citation) in the current "
                "context and that reference is now missing, or the claim now cites a different "
                "reference without the update supporting it."
            ),
            "false": (
                "No if kept claims keep their references (possibly reformatted or merged), if a "
                "claim and its reference were removed together, or if a reference changed in a "
                "way the update supports."
            ),
        },
    },
}


def payload_state(case: dict[str, Any]) -> dict[str, Any]:
    """Only checker-visible data; entries keep their order and ids."""
    entries = lambda xs: [{"id": e["id"], "role": e["role"], "body": e["body"]} for e in xs]  # noqa: E731
    return {
        "task": case["task"],
        "current_context": entries(case["current"]),
        "update": case.get("update", ""),
        "proposed_context": entries(case["proposed"]),
    }


def build_edit_request(case: dict[str, Any], model: str) -> dict[str, Any]:
    return {
        "state": payload_state(case),
        "model": model,
        "questions": {
            k: {
                "type": "noul",
                "instructions": {"question": q["question"], "scope": SCOPE},
                "criteria": q["criteria"],
            }
            for k, q in QUESTIONS.items()
        },
    }


# ---------------------------------------------------------------- deterministic reference checks
REF = re.compile(
    r"[\w./-]+\.(?:md|log|txt|py|csv|json)(?::[\d,\-]+)?"  # file or file:line
    r"|§\s?\d+(?:\.\d+)*"  # document sections
    r"|\b[A-Z]{2,}-\d+\b"  # tickets / change ids
    r"|\[[A-Z][a-z]+ \d{4}\]"  # [Author Year]
)
NUM = re.compile(r"\d+(?:\.\d+)?")


def refs(text: str) -> set[str]:
    return {m.group(0).replace(" ", "") for m in REF.finditer(text)}


def check_ref_loss(case: dict[str, Any]) -> list[str]:
    cur = "\n".join(e["body"] for e in case["current"])
    new = "\n".join(e["body"] for e in case["proposed"])
    return sorted(refs(cur) - refs(new))


def check_new_numbers(case: dict[str, Any]) -> list[str]:
    cur = "\n".join(e["body"] for e in case["current"]) + "\n" + case.get("update", "")
    new = "\n".join(e["body"] for e in case["proposed"])
    return sorted(set(NUM.findall(new)) - set(NUM.findall(cur)))


# ---------------------------------------------------------------- run
def run_edit_diagnostic(
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
    """One request per case; every attempt is reserved and settled in the ledger."""
    doc = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = [c for c in doc["cases"] if c["split"] == split]
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    out = out_root / f"{stamp}-edit-{split}"
    (out / "requests").mkdir(parents=True)
    results: list[dict[str, Any]] = []
    stopped = None
    for c in cases:
        body = build_edit_request(c, model)
        rec: dict[str, Any] = {"case_id": c["id"], "request": body, "attempts": []}
        for attempt in range(retries + 1):
            try:
                res = ledger.reserve(
                    price.max_cost(body) if price else 0.0, f"jev-edit-{stamp}", "jev-diagnostic"
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
                answers={k: resp.probabilities.get(k) for k in QUESTIONS},
            )
            break
        rec["charged_usd"] = round(sum(a["charged_usd"] for a in rec["attempts"]), 8)
        (out / "requests" / f"{c['id']}.json").write_text(
            json.dumps(rec, indent=1, ensure_ascii=False)
        )
        results.append({k: v for k, v in rec.items() if k not in ("request", "response")})
        if stopped:
            break
    import hashlib

    summary = {
        "cases_file": cases_path.name,
        "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "cases_version": doc["version"],
        "question_version": QUESTION_VERSION,
        "split": split,
        "model_requested": model,
        "stopped": stopped,
        "results": results,
        "score": score_edit_check(cases, results, doc["reporting_threshold"]),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    return out


# ---------------------------------------------------------------- scoring
def _metrics(flags: dict[str, bool | None], cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Case-level counts; an unanswered harmful case is a miss and an unanswered acceptable case a
    false flag (the predeclared gate convention)."""
    harm = [c for c in cases if c["label"] == "harmful"]
    ok = [c for c in cases if c["label"] == "acceptable"]
    detected = [c["id"] for c in harm if flags.get(c["id"]) is True]
    missed = [c["id"] for c in harm if flags.get(c["id"]) is not True]
    false_flags = [c["id"] for c in ok if flags.get(c["id"]) is not False]
    tp, fp = len(detected), len(false_flags)
    return {
        "harmful_detected": detected,
        "harmful_missed": missed,
        "acceptable_flagged": false_flags,
        "recall": f"{tp}/{len(harm)}",
        "false_flags": f"{fp}/{len(ok)}",
        "precision": f"{tp}/{tp + fp}" if tp + fp else None,
    }


def score_edit_check(
    cases: list[dict[str, Any]], results: list[dict[str, Any]], threshold: float
) -> dict[str, Any]:
    by_id = {r["case_id"]: r for r in results}
    verdict: dict[str, bool | None] = {}
    partial: list[str] = []
    per_q: dict[str, dict[str, bool | None]] = {k: {} for k in QUESTIONS}
    for c in cases:
        ans = (by_id.get(c["id"]) or {}).get("answers") or {}
        got = [v for v in ans.values() if v is not None]
        verdict[c["id"]] = (max(got) >= threshold) if got else None
        if got and len(got) < len(QUESTIONS):
            partial.append(c["id"])
        for k in QUESTIONS:
            v = ans.get(k)
            per_q[k][c["id"]] = None if v is None else v >= threshold
    labelled = [c for c in cases if c["label"] in ("harmful", "acceptable")]
    m = _metrics(verdict, labelled)
    harm_n = sum(1 for c in labelled if c["label"] == "harmful")
    ok_n = sum(1 for c in labelled if c["label"] == "acceptable")
    out: dict[str, Any] = {
        "threshold": threshold,
        "verdict_rule": "flagged if any question probability >= threshold",
        "jev": m,
        "per_question": {k: _metrics(v, labelled) for k, v in per_q.items()},
        "unanswered": [c["id"] for c in cases if verdict[c["id"]] is None],
        "partial": partial,
        "ambiguous": {
            c["id"]: (by_id.get(c["id"]) or {}).get("answers")
            for c in cases
            if c["label"] == "ambiguous"
        },
        "by_category": {},
        "references": {
            "always_flag": _metrics({c["id"]: True for c in labelled}, labelled),
            "d_ref": _metrics({c["id"]: bool(check_ref_loss(c)) for c in labelled}, labelled),
            "d_num": _metrics({c["id"]: bool(check_new_numbers(c)) for c in labelled}, labelled),
            "d_any": _metrics(
                {c["id"]: bool(check_ref_loss(c) or check_new_numbers(c)) for c in labelled},
                labelled,
            ),
        },
        "deterministic_detail": {
            c["id"]: {"refs_lost": check_ref_loss(c), "new_numbers": check_new_numbers(c)}
            for c in cases
        },
    }
    for c in labelled:
        key = f"{c['label']}: {c['category']}"
        d = out["by_category"].setdefault(key, {"cases": 0, "jev_flagged": 0, "d_any_flagged": 0})
        d["cases"] += 1
        d["jev_flagged"] += verdict[c["id"]] is True
        d["d_any_flagged"] += bool(check_ref_loss(c) or check_new_numbers(c))
    tp = len(m["harmful_detected"])
    fp = len(m["acceptable_flagged"])
    out["gate"] = (
        {
            "harmful_detected_min": 14,
            "acceptable_flagged_max": 2,
            "detected": tp,
            "of_harmful": harm_n,
            "flagged_acceptable": fp,
            "of_acceptable": ok_n,
            "passed": tp >= 14 and fp <= 2,
        }
        if harm_n == 16 and ok_n == 16
        else None
    )
    lat = [r["latency_s"] for r in results if "latency_s" in r]
    out["requests"] = len(results)
    out["attempts"] = sum(len(r["attempts"]) for r in results)
    out["errors"] = sum(1 for r in results for a in r["attempts"] if "error" in a)
    out["latency_s"] = {
        "total": round(sum(lat), 4),
        "mean": round(sum(lat) / len(lat), 4) if lat else None,
        "max": max(lat) if lat else None,
    }
    out["typesafe_tokens"] = {
        "input": sum((r.get("usage") or {}).get("input_tokens", 0) for r in results),
        "output": sum((r.get("usage") or {}).get("output_tokens", 0) for r in results),
    }
    out["cost_usd"] = round(sum(r.get("charged_usd", 0.0) for r in results), 8)
    out["models"] = sorted({r["model"] for r in results if "model" in r})
    return out
