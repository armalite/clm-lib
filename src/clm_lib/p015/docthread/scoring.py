"""Objective scoring for docthread (`docthread-score/1`).

**Correctness** is per field, from the reply's last fenced code block (opened with three backticks, with or without
a `json` tag) whose content is a JSON object. A turn whose reply has no such block scores 0 on every field.

| kind | correct when |
| --- | --- |
| name, id, code | the string equals the truth, ignoring surrounding space and letter case |
| time | the journal time with milliseconds (`HH:MM:SS.mmm`), optionally preceded by the date (space or `T`) and followed by `Z` or ` UTC` |
| count, seconds | the number equals the truth; thousands separators and a trailing unit (`jobs`; `s`, `sec`, `seconds`) are allowed |
| quote | at least 24 characters, a substring of one line of the failing component's journal for the incident hour, and containing the changed setting at its new value |

**Diagnostics are reported separately from correctness** and never change a score:

- `status`, for a field that isn't correct:
  - `missing`: no answer, or the key is absent or empty;
  - `malformed`: the wrong type, or no parsable number;
  - `distractor`: one of the fixture's planted look-alikes;
  - `other_evidence`: the value appears somewhere in the evidence but isn't the answer;
  - `not_in_evidence`: the value appears nowhere in the evidence;
  - for quotes: `too_short`, `not_a_journal_line` (not a substring of any journal line), `wrong_line` (a real line
    without the cause).

  No status claims why an answer is wrong.
- `carried_in_turn`, for recall values: the first earlier question whose final reply already contained the fact.
- `refetched`, for recall values: whether answering the question itself called a tool output that holds the fact
  again.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .fixture import Fixture, mentions
from .task import Ask, Question, questions

SCORER_VERSION = "docthread-score/1"
_NUMBER = re.compile(
    r"^\s*([0-9][0-9,]*(?:\.0+)?)\s*(?:jobs?|s|sec|secs|seconds)?\s*$", re.IGNORECASE
)
_TIME = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[ T])?(\d{2}:\d{2}:\d{2}\.\d{3})(?:Z| UTC)?$")
MIN_QUOTE = 24


def extract_answer(reply: str) -> dict[str, Any] | None:
    """The reply's answer: the object in its last fenced block that parses as JSON (None without one)."""
    blocks: list[str] = []
    inside: list[str] | None = None
    for raw in (reply or "").splitlines():
        line = raw.strip()
        if inside is None and line.startswith("```"):
            inside = []
        elif inside is not None and line == "```":
            blocks.append("\n".join(inside))
            inside = None
        elif inside is not None:
            inside.append(raw)
    for block in reversed(blocks):
        try:
            value = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
        return None
    return None


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        m = _NUMBER.match(value)
        if m:
            return int(float(m.group(1).replace(",", "")))
    return None


def _quote(value: str) -> str:
    """The quoted text without the decoration people add around quotes."""
    text = value.strip()
    pairs = {'"': '"', "'": "'", "`": "`", "\u201c": "\u201d"}
    while len(text) >= 2 and pairs.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


def judge(field: Ask, got: Any, fx: Fixture, corpus: str) -> tuple[bool, str]:
    """(correct, status)."""
    truth = fx.facts[field.key]
    if got is None or (isinstance(got, str) and not got.strip()):
        return False, "missing"
    if field.check == "quote":
        if not isinstance(got, str):
            return False, "malformed"
        q = _quote(got)
        if len(q) < MIN_QUOTE:
            return False, "too_short"
        if any(q in line for line in fx.evidence_lines):
            return (True, "correct") if fx.signature.search(q) else (False, "wrong_line")
        journals = (line for lines in fx.journals.values() for line in lines)
        return (
            (False, "wrong_line")
            if any(q in line for line in journals)
            else (False, "not_a_journal_line")
        )
    if field.check in ("count", "seconds"):
        n = _number(got)
        if n is None:
            return False, "malformed"
        if n == int(truth):
            return True, "correct"
        value = str(n)
    elif not isinstance(got, str):
        return False, "malformed"
    elif field.check == "time":
        m = _TIME.match(got.strip())
        if m and m.group(1) == truth:
            return True, "correct"
        value = m.group(1) if m else got.strip()
    else:
        if got.strip().lower() == truth.lower():
            return True, "correct"
        value = got.strip()
    if any(value.lower() == d.lower() for d in fx.distractors.get(field.key, [])):
        return False, "distractor"
    return (False, "other_evidence") if mentions(corpus, value) else (False, "not_in_evidence")


def score_session(
    fx: Fixture, replies: list[str | None], calls_by_question: list[list[str]] | None = None
) -> dict[str, Any]:
    """Score the eight questions. `replies[i]` is question i+1's final reply (None, or absent, if it didn't finish);
    `calls_by_question[i]` lists the canonical keys of the tool calls made while answering it (for `refetched`)."""
    corpus = "\n".join(fx.render_all().values())
    padded = list(replies) + [None] * (8 - len(replies))
    per_question = []
    points = recall_points = recall_out_of = 0
    for q in questions(fx):
        reply = padded[q.number - 1]
        parsed = extract_answer(reply) if reply is not None else None
        values: dict[str, Any] = {}
        for ask in q.asks:
            given = None if parsed is None else parsed.get(ask.key)
            ok, status = (False, "missing") if parsed is None else judge(ask, given, fx, corpus)
            result: dict[str, Any] = {"correct": ok, "status": status, "given": given}
            if ask.recall_from is not None:
                recall_out_of += 1
                recall_points += ok
                result["recall_from"] = ask.recall_from
                result["carried_in_turn"] = carried_in(fx, ask.key, padded[: q.number - 1])
                if calls_by_question is not None and len(calls_by_question) >= q.number:
                    result["refetched"] = bool(
                        set(calls_by_question[q.number - 1]) & set(fx.sources[ask.key])
                    )
            points += ok
            values[ask.key] = result
        per_question.append(
            {
                "question": q.number,
                "finished": reply is not None,
                "parsed": parsed is not None,
                "values": values,
            }
        )
    return {
        "scorer": SCORER_VERSION,
        "fixture": fx.name,
        "questions": per_question,
        "points": points,
        "out_of": sum(len(q.asks) for q in questions(fx)),
        "recall_points": recall_points,
        "recall_out_of": recall_out_of,
    }


def carried_in(fx: Fixture, key: str, earlier: list[str | None]) -> int | None:
    """The first earlier question whose final reply already contained the fact, if any."""
    return next(
        (n for n, reply in enumerate(earlier, start=1) if reply and mentions(reply, fx.facts[key])),
        None,
    )


def answer_block(values: dict[str, Any]) -> str:
    return "```json\n" + json.dumps(values, indent=2) + "\n```"


def true_answers(fx: Fixture, q: Question) -> dict[str, Any]:
    """Correct values for a question (a quote is the evidence line's detail text)."""
    out: dict[str, Any] = {}
    for ask in q.asks:
        value = fx.facts[ask.key]
        if ask.check == "quote":
            value = value.split(" | ", 5)[5]
        out[ask.key] = int(value) if ask.check in ("count", "seconds") else value
    return out
