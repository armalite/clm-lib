"""Experiment 012: Jev screening of structured document-search results (``result-screen/1``).

Only structured search results are handled: blocks printed by the handbook ``search`` tool,

    <<search-result SR-003 query="..." k=5 returned=5 saved=/task/workspace/search-results/SR-003.txt>>
    === HB-07 §2 | <title> — <heading> | <document meta>
    <passage text>
    ...
    <<end search-result SR-003>>

``show()`` output (``<<passages ...>>``), saved originals and documents are never screened, so
recovery reads always come back unfiltered. Arbitrary command output is never screened.

Presentation is identical in both conditions up to the screening step: when the existing
output cap cuts a search block, the block is cut back to its last complete passage and a notice
names the passages not shown (from the saved original). Screening then sees exactly the
passages condition A sees, and never more.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCREEN_VERSION = "result-screen/1"
QUESTION_VERSION = "jev-screen-question/1"
MIN_CHARS = 2000  # presented passage characters (headers + text) for a block to be eligible
MIN_PASSAGES = 3
THRESHOLD = 0.5  # frozen after the development diagnostic (draft value 0.3; see the 012 protocol)
MIN_KEEP = 2
MAX_TASK_CHARS = 4000
MAX_INTENT_CHARS = 500
BLOCK = re.compile(
    r'<<search-result (?P<rid>SR-\d+) query="(?P<query>[^"]*)" k=(?P<k>\d+) returned=(?P<n>\d+) '
    r"saved=(?P<saved>\S+)>>\n(?P<body>.*?)(?:<<end search-result (?P=rid)>>\n?|\Z)",
    re.S,
)
NOTICE_PREFIXES = ("[output cap:", f"[{SCREEN_VERSION}:")
PASSAGES = re.compile(r"<<passages \(unscreened\): (?P<ids>[^>]*)>>")

SCOPE = (
    "state.task is the agent's task, state.query the search query it ran, state.step_intent what "
    "it said it was looking for, and state.passages every passage the search returned (one "
    "passage can be an exception, carve-out or newer version of another). Judge only the "
    "passage in instructions.passage. Do not judge whether the passage is true."
)
QUESTION = {
    "question": (
        "Is this passage needed, or could it change the answer, for what the agent searched for "
        "(state.query and state.step_intent) in this task?"
    ),
    "criteria": {
        "true": (
            "Yes if the passage states the rule, amount, limit or condition the search is about, "
            "or states an exception, carve-out, addition, effective date or version status "
            "(current or superseded) for that same subject that could change which rule or "
            "amount applies."
        ),
        "false": (
            "No if the passage is about a different subject, budget or process than the search, "
            "so that it could not change the answer. General administrative text (submitting "
            "claims, receipts, reviews, contacts) counts as no unless the search is about it."
        ),
    },
}


@dataclass
class SearchBlock:
    rid: str
    query: str
    k: int
    returned: int
    saved: str
    start: int
    end: int
    complete: bool
    passages: list[dict[str, str]] = field(default_factory=list)  # id, header, text
    notices: list[str] = field(default_factory=list)  # runtime notices already in the block

    @property
    def chars(self) -> int:
        return sum(len(p["header"]) + 1 + len(p["text"]) + 1 for p in self.passages)


def parse_blocks(stdout: str) -> list[SearchBlock]:
    out = []
    for m in BLOCK.finditer(stdout):
        body = m.group("body")
        passages: list[dict[str, str]] = []
        notices: list[str] = []
        for line in body.split("\n"):
            if line.startswith(NOTICE_PREFIXES):
                notices.append(line)
            elif line.startswith("=== "):
                pid = line[4:].split(" | ", 1)[0].strip()
                passages.append({"id": pid, "header": line, "text": ""})
            elif passages and line:
                passages[-1]["text"] = (passages[-1]["text"] + "\n" + line).lstrip("\n")
        complete = m.group(0).rstrip("\n").endswith(f"<<end search-result {m.group('rid')}>>")
        out.append(
            SearchBlock(
                m.group("rid"),
                m.group("query"),
                int(m.group("k")),
                int(m.group("n")),
                m.group("saved"),
                m.start(),
                m.end(),
                complete,
                passages,
                notices,
            )
        )
    return out


def render_block(b: SearchBlock, passages: list[dict[str, str]], notice: str = "") -> str:
    lines = [
        f'<<search-result {b.rid} query="{b.query}" k={b.k} returned={b.returned} saved={b.saved}>>'
    ]
    for p in passages:
        lines += [p["header"], p["text"]]
    lines += b.notices
    if notice:
        lines.append(notice)
    lines.append(f"<<end search-result {b.rid}>>")
    return "\n".join(lines) + "\n"


def saved_original(ws: Path, b: SearchBlock) -> list[dict[str, Any]] | None:
    """The tool's saved JSON for a block, if present and well formed (host-side read)."""
    p = ws / "search-results" / f"{b.rid}.json"
    try:
        if p.is_symlink() or p.stat().st_size > 200_000:
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        return [x for x in d["passages"] if isinstance(x, dict) and "id" in x]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def repair_truncated(stdout: str, ws: Path) -> tuple[str, list[dict[str, Any]]]:
    """Both conditions: a block cut by the output cap keeps only complete passages, plus a
    notice naming the passages not shown. Returns (text, records)."""
    recs = []
    for b in reversed(parse_blocks(stdout)):
        if b.complete:
            continue
        shown = b.passages[:-1] if b.passages else []  # the last one may be cut mid-text
        saved = saved_original(ws, b) or []
        missing = [x["id"] for x in saved[len(shown) :]]
        notice = (
            f"[output cap: {len(missing) or 'the remaining'} passages of {b.rid} not shown"
            + (f" ({', '.join(missing)})" if missing else "")
            + f"; complete result: {b.saved}]"
        )
        stdout = stdout[: b.start] + render_block(b, shown, notice) + stdout[b.end :]
        recs.append({"rid": b.rid, "shown": len(shown), "not_shown": missing})
    return stdout, recs


def eligible(b: SearchBlock) -> bool:
    return b.complete and len(b.passages) >= MIN_PASSAGES and b.chars >= MIN_CHARS


def build_screen_request(
    task: str, query: str, intent: str, passages: list[dict[str, str]], model: str
) -> dict[str, Any]:
    """One request for one block: every passage is in state, and each question judges one
    passage whose text is also in its instructions."""
    state = {
        "task": task[:MAX_TASK_CHARS],
        "query": query,
        "step_intent": intent[:MAX_INTENT_CHARS],
        "passages": [
            {"id": p["id"], "source": p["header"][4:], "text": p["text"]} for p in passages
        ],
    }
    questions = {
        f"p{i}": {
            "type": "noul",
            "instructions": {
                "question": QUESTION["question"],
                "passage_id": p["id"],
                "passage": p["header"][4:] + "\n" + p["text"],
                "scope": SCOPE,
            },
            "criteria": QUESTION["criteria"],
        }
        for i, p in enumerate(passages)
    }
    return {"state": state, "model": model, "questions": questions}


def select(
    passages: list[dict[str, str]],
    probs: list[float | None],
    threshold: float = THRESHOLD,
    min_keep: int = MIN_KEEP,
) -> tuple[list[int], list[int]]:
    """(kept, missing): indices kept in original order. A missing answer keeps its passage;
    at least ``min_keep`` passages are kept (highest probabilities, then original rank)."""
    missing = [i for i, p in enumerate(probs) if p is None]
    kept = {i for i, p in enumerate(probs) if p is None or p >= threshold}
    score = [2.0 if p is None else p for p in probs]  # a missing answer is already kept
    ranked = sorted(range(len(passages)), key=lambda i: (-score[i], i))
    for i in ranked:
        if len(kept) >= min(min_keep, len(passages)):
            break
        kept.add(i)
    return sorted(kept), missing


STOP = set(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
        "what",
        "which",
        "who",
        "how",
        "does",
        "do",
        "today",
        "per",
    ]
)


def term_select(query: str, passages: list[dict[str, str]], min_keep: int = MIN_KEEP) -> list[int]:
    """Deterministic reference: keep passages sharing a non-stopword query term (4+ letters),
    with at least ``min_keep`` kept (most shared terms, then original rank)."""
    terms = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in STOP and len(w) >= 4}
    hits = [
        len(terms & set(re.findall(r"[a-z0-9]+", (p["header"] + " " + p["text"]).lower())))
        for p in passages
    ]
    kept = {i for i, h in enumerate(hits) if h > 0}
    for i in sorted(range(len(passages)), key=lambda i: (-hits[i], i)):
        if len(kept) >= min(min_keep, len(passages)):
            break
        kept.add(i)
    return sorted(kept)


def notice_text(
    b: SearchBlock, kept: list[int], total_chars: int, kept_chars: int, recover: str = "show"
) -> str:
    omitted = [p["id"] for i, p in enumerate(b.passages) if i not in kept]
    if not omitted:
        return f"[{SCREEN_VERSION}: all {len(kept)} passages kept]"
    return (
        f"[{SCREEN_VERSION}: kept {len(kept)} of {len(b.passages)} passages ({kept_chars:,} of "
        f"{total_chars:,} chars); omitted {', '.join(omitted) or 'none'}; unscreened: {recover}({omitted!r}) "
        f"or {b.saved}]"
    )


@dataclass
class ScreenOutcome:
    text: str
    records: list[dict[str, Any]]


def screen_stdout(
    stdout: str,
    task: str,
    intent: str,
    model: str,
    call: Callable[[dict[str, Any]], tuple[Any, str | None]] | None,
    ws: Path,
    threshold: float = THRESHOLD,
    min_keep: int = MIN_KEEP,
    recover: str = "show",
) -> ScreenOutcome:
    """Present (both conditions) and, with ``call``, screen (condition B) every search block.
    ``call(body) -> (response or None, error)`` is the runtime's recorded Jev attempt loop."""
    text, repairs = repair_truncated(stdout, ws)
    records: list[dict[str, Any]] = [{"kind": "cap_repair", **r} for r in repairs]
    for b in reversed(parse_blocks(text)):
        rec: dict[str, Any] = {
            "kind": "block",
            "rid": b.rid,
            "query": b.query,
            "k": b.k,
            "passages": len(b.passages),
            "chars": b.chars,
            "eligible": eligible(b),
            "screened": False,
        }
        saved = saved_original(ws, b)
        ok = saved is not None and [x["id"] for x in saved[: len(b.passages)]] == [
            p["id"] for p in b.passages
        ]
        rec["provenance_ok"] = ok
        if call is None or not rec["eligible"]:
            records.append(rec)
            continue
        if not ok:
            rec["skipped"] = "saved original missing or different"
            records.append(rec)
            continue
        body = build_screen_request(task, b.query, intent, b.passages, model)
        resp, err = call(body)
        rec["request"] = body
        if resp is None:
            notice = f"[{SCREEN_VERSION}: screening unavailable ({(err or 'service error')[:80]}); nothing omitted]"
            text = text[: b.start] + render_block(b, b.passages, notice) + text[b.end :]
            rec.update(error=err, notice_chars=len(notice))
            records.append(rec)
            continue
        probs = [resp.probabilities.get(f"p{i}") for i in range(len(b.passages))]
        kept, missing = select(b.passages, probs, threshold, min_keep)
        kp = [b.passages[i] for i in kept]
        kept_chars = sum(len(p["header"]) + 1 + len(p["text"]) + 1 for p in kp)
        notice = notice_text(b, kept, b.chars, kept_chars, recover)
        text = text[: b.start] + render_block(b, kp, notice) + text[b.end :]
        rec.update(
            screened=True,
            probabilities=probs,
            kept=[b.passages[i]["id"] for i in kept],
            omitted=[p["id"] for i, p in enumerate(b.passages) if i not in kept],
            missing_answers=[b.passages[i]["id"] for i in missing],
            kept_chars=kept_chars,
            notice_chars=len(notice),
            model=resp.model,
        )
        records.append(rec)
    return ScreenOutcome(text, records)


def recovery_reads(stdout: str, code: str) -> dict[str, Any]:
    """Unscreened reads in a step (both conditions): show() calls with their ids, reads of saved
    originals and direct document reads (from the code text)."""
    ids = []
    for m in PASSAGES.finditer(stdout):
        ids += [x.strip() for x in m.group("ids").split(",") if x.strip()]
    return {
        "show_calls": len(PASSAGES.findall(stdout)),
        "show_ids": ids,
        "saved_reads": int("search-results/" in code and "open(" in code),
        "doc_reads": int("fixtures/handbook" in code),  # any direct document access (grep, open)
    }
