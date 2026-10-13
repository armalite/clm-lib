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

import hashlib
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
    v2 = doc["version"] == "jev-edit-diag/2"  # v2: evidence contract and four questions
    qs = QUESTIONS_V2 if v2 else QUESTIONS
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    out = out_root / f"{stamp}-{'edit2' if v2 else 'edit'}-{split}"
    (out / "requests").mkdir(parents=True)
    results: list[dict[str, Any]] = []
    stopped = None
    for c in cases:
        meta: dict[str, Any] = {}
        if v2:
            body, meta = build_edit_request_v2(c, model)
        else:
            body = build_edit_request(c, model)
        rec: dict[str, Any] = {"case_id": c["id"], "request": body, "attempts": []}
        if v2:
            rec["evidence"] = meta
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
                answers={k: resp.probabilities.get(k) for k in qs},
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
        "question_version": QUESTION_VERSION_V2 if v2 else QUESTION_VERSION,
        **(
            {
                "questions_sha256": questions_v2_sha256(),
                "evidence_version": EVIDENCE_VERSION,
                "verdict_version": VERDICT_VERSION,
            }
            if v2
            else {}
        ),
        "split": split,
        "model_requested": model,
        "stopped": stopped,
        "results": results,
        "score": (score_edit_check_v2 if v2 else score_edit_check)(
            cases, results, doc["reporting_threshold"]
        ),
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


# ================================================================ v2: evidence-complete checking
EVIDENCE_VERSION = "edit-evidence/1"
QUESTION_VERSION_V2 = "jev-edit-check/2"
VERDICT_VERSION = "edit-verdict/2"
PAYLOAD_FIELDS_V2 = (
    "task",
    "current_context",
    "update",
    "execution_output",
    "cited_sources",
    "evidence_notes",
    "proposed_context",
)
MAX_ENTRY_CHARS = 6000  # the runtime's output cap per stream
MAX_UPDATE_CHARS = 4000
MAX_EXEC_CHARS = 6000
MAX_REF_LINES = 20
MAX_LINE_CHARS = 300
MAX_CITED_CHARS = 8000
CITE = re.compile(
    r"([\w./-]+\.(?:md|log|txt|py|csv|json|yaml|yml|toml|cfg|ini)):(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)"
)


def _cut(text: str, limit: int, what: str, notes: list[str]) -> str:
    if len(text) <= limit:
        return text
    notes.append(f"{what} truncated to {limit} of {len(text)} characters")
    return text[:limit] + f" [... truncated: {len(text) - limit} characters omitted]"


def _line_numbers(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        out.extend(range(lo, max(lo, hi) + 1))
    return out


def _resolve(path: str, sources: dict[str, str]) -> tuple[str | None, str | None]:
    """Exact path, else a unique suffix match among the released files."""
    if path in sources:
        return path, None
    hits = [p for p in sources if p.endswith("/" + path)]
    if len(hits) == 1:
        return hits[0], None
    return None, ("ambiguous short path" if hits else "no released file with this path")


def select_evidence(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """edit-evidence/1: the checker's state, assembled deterministically from runtime-visible
    inputs (current revision, released update, the proposing execution's captured stdout, and
    released source files for newly cited path:lines references). Returns (state, metadata)."""
    notes: list[str] = []

    def entries(xs: list[dict[str, Any]], which: str) -> list[dict[str, str]]:
        return [
            {
                "id": e["id"],
                "role": e["role"],
                "body": _cut(e["body"], MAX_ENTRY_CHARS, f"{which} entry {e['id']}", notes),
            }
            for e in xs
        ]

    current = entries(case["current"], "current_context")
    proposed = entries(case["proposed"], "proposed_context")
    update = _cut(case.get("update", ""), MAX_UPDATE_CHARS, "update", notes)
    stdout = case.get("exec_output", "")
    if not stdout:
        notes.append("the proposing execution printed nothing (no execution output)")
    stdout = _cut(stdout, MAX_EXEC_CHARS, "execution_output", notes)
    cur_text = "\n".join(e["body"] for e in case["current"])
    new_text = "\n".join(e["body"] for e in case["proposed"])
    sources: dict[str, str] = case.get("sources", {})
    cited: list[dict[str, Any]] = []
    used = 0
    seen: set[str] = set()
    for m in CITE.finditer(new_text):
        ref = m.group(0)
        if ref in seen or ref in cur_text:
            continue
        seen.add(ref)
        path, err = _resolve(m.group(1), sources)
        if path is None:
            notes.append(f"cited reference {ref} could not be resolved ({err})")
            continue
        lines = sources[path].splitlines()
        nums = _line_numbers(m.group(2))
        if len(nums) > MAX_REF_LINES:
            notes.append(
                f"cited reference {ref} spans {len(nums)} lines; only the first {MAX_REF_LINES} are included"
            )
            nums = nums[:MAX_REF_LINES]
        got: list[str] = []
        for n in nums:
            if 1 <= n <= len(lines):
                got.append(f"{path}:{n}: {lines[n - 1][:MAX_LINE_CHARS]}")
            else:
                notes.append(f"cited reference {ref}: line {n} does not exist in {path}")
        size = sum(len(x) + 1 for x in got)
        if used + size > MAX_CITED_CHARS:
            notes.append(
                f"cited reference {ref} omitted: cited-source limit of {MAX_CITED_CHARS} characters reached"
            )
            continue
        used += size
        cited.append({"reference": ref, "lines": got})
    state = {
        "task": case["task"],
        "current_context": current,
        "update": update,
        "execution_output": stdout,
        "cited_sources": cited,
        "evidence_notes": notes,
        "proposed_context": proposed,
    }
    meta = {
        "evidence_version": EVIDENCE_VERSION,
        "cited_references": [c["reference"] for c in cited],
        "notes": list(notes),
        "complete": not [n for n in notes if "no execution output" not in n],
    }
    return state, meta


SCOPE_V2 = (
    "Compare state.current_context (the full working context before the rewrite, including tool "
    "observations still in it) with state.proposed_context (the replacement). The supporting "
    "material is state.update (already released information), state.execution_output (what the "
    "code that wrote the proposal printed), state.cited_sources (the exact source lines for "
    "references newly cited in the proposal) and state.current_context itself. "
    "state.evidence_notes lists anything missing or truncated. state.task is the task the "
    "context serves."
)
# v2 wording names the whole supplied material wherever v1 named only the update (dev-run fix).
SUPPLIED = "the supplied material (current context, update, execution output or cited sources)"
QUESTIONS_V2: dict[str, dict[str, Any]] = {
    "lost": {
        "question": (
            "Does the proposed context drop, or mark as finished, an obligation, requirement, "
            "exception, open question or unresolved item that the current context still treats "
            f"as active, when nothing in {SUPPLIED} says it is finished, superseded or no longer "
            "needed?"
        ),
        "criteria": {
            "true": (
                "Yes if an item that is still active in the current context is missing from the "
                "proposed context, or is now described as done, closed, answered or no longer "
                f"applicable, and nothing in {SUPPLIED} supports that. This includes losing an "
                "exception or condition attached to a requirement that is kept."
            ),
            "false": (
                "No if every active item is still present, possibly paraphrased, shortened or "
                "merged; if removed items were superseded, completed, duplicated or closed "
                f"according to {SUPPLIED}; or if historical information is kept as history. A "
                "shorter context is not by itself a loss."
            ),
        },
    },
    "changed": {
        "question": (
            "Does the proposed context state a value, number, limit, condition, status or "
            f"relationship differently from the current context, without {SUPPLIED} supporting "
            "the change?"
        ),
        "criteria": {
            "true": (
                "Yes if a value, threshold, amount, date, percentage, status, scope or "
                "which-applies-to-what relationship that the proposed context states differs from "
                "what the current context states, and the supplied material does not justify the "
                "new version, or the supplied material contradicts it."
            ),
            "false": (
                "No if values, conditions and statuses mean the same (wording may differ); if every "
                "difference is justified by the supplied material; or if content was only removed "
                "(removal is judged by a separate question, not as a change). New content that the "
                "supplied material supports is not an unsupported change."
            ),
        },
    },
    "evidence": {
        "question": QUESTIONS["evidence"]["question"],
        "criteria": {
            "true": (
                "Yes if a claim that remains in the proposed context had a source reference "
                "(file:line, document section, ticket, dashboard, link or citation) in the current "
                "context and that reference is now missing, or the claim now cites a different "
                f"reference without {SUPPLIED} supporting it."
            ),
            "false": (
                "No if kept claims keep their references (possibly reformatted or merged), if a "
                "claim and its reference were removed together, or if a reference changed in a "
                f"way {SUPPLIED} supports."
            ),
        },
    },
    "insufficient": {
        "question": (
            "Is the supplied material insufficient to check whether the proposed context's new or "
            "changed content is supported?"
        ),
        "criteria": {
            "true": (
                "Yes if the proposed context adds or changes content whose support would have to "
                "come from material that is not supplied: for example state.evidence_notes reports "
                "the relevant material as missing or truncated, or a new claim has no checkable "
                "source in the current context, update, execution output or cited sources."
            ),
            "false": (
                "No if every addition or change can be checked against the supplied material "
                "(whether or not it turns out to be supported), or if the proposal only removes, "
                "merges or rephrases existing content."
            ),
        },
    },
}
HARM_KEYS = ("lost", "changed", "evidence")


def questions_v2_sha256() -> str:
    """Fingerprint of the exact v2 scope and question texts sent to Jev."""
    blob = json.dumps({"scope": SCOPE_V2, "questions": QUESTIONS_V2}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def build_edit_request_v2(
    case: dict[str, Any], model: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    state, meta = select_evidence(case)
    body = {
        "state": state,
        "model": model,
        "questions": {
            k: {
                "type": "noul",
                "instructions": {"question": q["question"], "scope": SCOPE_V2},
                "criteria": q["criteria"],
            }
            for k, q in QUESTIONS_V2.items()
        },
    }
    return body, meta


def verdict_v2(answers: dict[str, float | None] | None, threshold: float) -> str:
    """edit-verdict/2: flagged > uncertain > clear; unanswered if nothing came back."""
    a = answers or {}
    harm = [v for k in HARM_KEYS if (v := a.get(k)) is not None]
    ins = a.get("insufficient")
    if not harm and ins is None:
        return "unanswered"
    if harm and max(harm) >= threshold:
        return "flagged"
    if ins is not None and ins >= threshold:
        return "uncertain"
    return "clear"


def check_new_numbers_v2(case: dict[str, Any]) -> list[str]:
    """D-num given the same supplied material as Jev (current, update, execution output, cited)."""
    state, _ = select_evidence(case)
    support = "\n".join(
        [e["body"] for e in state["current_context"]]
        + [state["update"], state["execution_output"]]
        + [line for c in state["cited_sources"] for line in c["lines"]]
    )
    new = "\n".join(e["body"] for e in case["proposed"])
    return sorted(set(NUM.findall(new)) - set(NUM.findall(support)))


def _outcomes(
    verdicts: dict[str, str], cases: list[dict[str, Any]], label: str
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"flagged": [], "uncertain": [], "clear": [], "unanswered": []}
    for c in cases:
        if c["label"] == label:
            out[verdicts[c["id"]]].append(c["id"])
    return out


def score_edit_check_v2(
    cases: list[dict[str, Any]], results: list[dict[str, Any]], threshold: float
) -> dict[str, Any]:
    by_id = {r["case_id"]: r for r in results}
    ans = {c["id"]: (by_id.get(c["id"]) or {}).get("answers") for c in cases}
    verdicts = {c["id"]: verdict_v2(ans[c["id"]], threshold) for c in cases}
    harm = _outcomes(verdicts, cases, "harmful")
    ok = _outcomes(verdicts, cases, "acceptable")
    gap = {
        c["id"]: {"verdict": verdicts[c["id"]], "answers": ans[c["id"]]}
        for c in cases
        if c["label"] in ("insufficient", "ambiguous")
    }
    n_h = sum(1 for c in cases if c["label"] == "harmful")
    n_a = sum(1 for c in cases if c["label"] == "acceptable")
    labelled = [c for c in cases if c["label"] in ("harmful", "acceptable")]

    def ref_metrics(flag: dict[str, bool]) -> dict[str, Any]:
        return _metrics({k: v for k, v in flag.items()}, labelled)

    out: dict[str, Any] = {
        "threshold": threshold,
        "verdict_version": VERDICT_VERSION,
        "harmful": {k: v for k, v in harm.items()},
        "acceptable": {k: v for k, v in ok.items()},
        "summary": {
            "harmful_flagged": f"{len(harm['flagged'])}/{n_h}",
            "harmful_uncertain": f"{len(harm['uncertain'])}/{n_h}",
            "harmful_clear": f"{len(harm['clear'])}/{n_h}",
            "acceptable_clear": f"{len(ok['clear'])}/{n_a}",
            "acceptable_flagged": f"{len(ok['flagged'])}/{n_a}",
            "acceptable_uncertain": f"{len(ok['uncertain'])}/{n_a}",
            "acceptable_needing_inspection": f"{n_a - len(ok['clear'])}/{n_a}",
            "unanswered": sum(1 for v in verdicts.values() if v == "unanswered"),
        },
        "gap_cases": gap,
        "per_question": {
            k: _metrics(
                {
                    c["id"]: (
                        None
                        if (ans[c["id"]] or {}).get(k) is None
                        else (ans[c["id"]] or {})[k] >= threshold
                    )
                    for c in labelled
                },
                labelled,
            )
            for k in QUESTIONS_V2
        },
        "references": {
            "always_flag": ref_metrics({c["id"]: True for c in labelled}),
            "d_ref": ref_metrics({c["id"]: bool(check_ref_loss(c)) for c in labelled}),
            "d_num": ref_metrics({c["id"]: bool(check_new_numbers_v2(c)) for c in labelled}),
            "d_any": ref_metrics(
                {c["id"]: bool(check_ref_loss(c) or check_new_numbers_v2(c)) for c in labelled}
            ),
        },
        "deterministic_detail": {
            c["id"]: {"refs_lost": check_ref_loss(c), "new_numbers": check_new_numbers_v2(c)}
            for c in cases
        },
        "by_category": {},
    }
    for c in labelled:
        key = f"{c['label']}: {c['category']}"
        d = out["by_category"].setdefault(
            key, {"cases": 0, "flagged": 0, "uncertain": 0, "clear": 0, "unanswered": 0}
        )
        d["cases"] += 1
        d[verdicts[c["id"]]] += 1
    tp, fp, clear = len(harm["flagged"]), len(ok["flagged"]), len(ok["clear"])
    out["gate"] = (
        {
            "harmful_flagged_min": 14,
            "acceptable_flagged_max": 2,
            "acceptable_clear_min": 12,
            "harmful_flagged": tp,
            "acceptable_flagged": fp,
            "acceptable_clear": clear,
            "passed": tp >= 14 and fp <= 2 and clear >= 12,
        }
        if n_h == 16 and n_a == 16
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
