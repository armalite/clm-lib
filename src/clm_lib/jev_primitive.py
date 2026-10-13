"""Jev as a primitive inside model-written code (``jev-primitive/1``; experiment 014, planned).

Model-authored code running in the sandbox calls ``clm_jev.judge(question, items, ...)``. The call
writes a request file into the workspace mailbox ``/task/workspace/.clm-jev/`` and waits. While
the same execution is still running, the host executor polls the mailbox, validates the request,
asks Jev through the runtime's recorded attempt loop (ledger reservation and settlement per
attempt), and writes the response file atomically. The waiting code reads it and continues: it
decides itself what the probabilities mean and how to edit ``context.json``. The runtime then
validates and accepts the edit as for any CLM edit. No extra model turn is involved.

Boundaries:
- the sandbox keeps no network and no credentials; only the host talks to Jev;
- requests carry only text the code supplies (which it can already see); the host adds
  nothing, reads no files on the code's behalf, and never runs code from a request;
- requests are size-, item- and count-limited; every request and response is recorded;
- an execution that ends, times out or is killed leaves unanswered requests unprocessed (no
  Jev call is made for them), and the mailbox is emptied after every execution.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "jev-primitive/1"
MAILBOX = ".clm-jev"
CLIENT_FILENAME = "clm_jev.py"
ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
DEFAULT_CRITERIA = {
    "true": "Yes if, for this item, the answer to the question is yes.",
    "false": "No if, for this item, the answer to the question is no.",
}
SCOPE = (
    "state.background is shared information supplied by the agent's own code (it may be empty), "
    "and state.items are the items it asks about. Judge only the item in instructions.item, "
    "against instructions.question. Answer the question as asked; do not judge anything else."
)

# The module written to /task/workspace/clm_jev.py when the primitive is enabled. Standard
# library only; it talks to the host only through files in the workspace mailbox.
CLIENT_SOURCE = r'''"""clm_jev: ask Jev narrow yes/no questions from your own code (jev-primitive/1).

    import clm_jev
    r = clm_jev.judge("Does the update change the rule stated in this entry?",
                      {"n1": "...entry text...", "n2": "..."},
                      background="...the update text...")
    # r == {"status": "ok", "answers": {"n1": 0.93, "n2": 0.04}, "model": "...", "error": None}
    # r["status"] == "error": r["error"] says why; answers are None. Nothing is retried for you.

Up to 20 items per call; each answer is the probability that the question is true for that item.
The answers are judgments that can be wrong. Your code decides what to do with them.
"""
import json, os, time, uuid

_BOX = "/task/workspace/.clm-jev"


def judge(question, items, background="", criteria=None, timeout=25.0):
    items = {str(k): str(v) for k, v in dict(items).items()}
    os.makedirs(_BOX, exist_ok=True)
    rid = uuid.uuid4().hex[:16]
    req = {"version": "jev-primitive/1", "id": rid, "question": str(question),
           "background": str(background), "items": [{"key": k, "text": v} for k, v in items.items()]}
    if criteria:
        req["criteria"] = {"true": str(criteria["true"]), "false": str(criteria["false"])}
    tmp = os.path.join(_BOX, ".req-" + rid + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(req, fh)
    os.replace(tmp, os.path.join(_BOX, "req-" + rid + ".json"))
    path = os.path.join(_BOX, "resp-" + rid + ".json")
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except ValueError:
                pass
        time.sleep(0.05)
    return {"status": "error", "error": "no response from the runtime within the timeout",
            "answers": {k: None for k in items}, "model": None}
'''


@dataclass
class PrimitiveLimits:
    max_calls_per_execution: int = 4
    max_calls_per_run: int = 30
    max_items: int = 20
    max_item_chars: int = 4000
    max_background_chars: int = 4000
    max_question_chars: int = 500
    max_request_bytes: int = 100_000


def validate(raw: bytes, lim: PrimitiveLimits) -> tuple[dict[str, Any] | None, str]:
    """The parsed request, or (None, reason). Only the documented fields are accepted."""
    if len(raw) > lim.max_request_bytes:
        return None, f"request is {len(raw)} bytes; limit {lim.max_request_bytes}"
    try:
        req = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "request is not valid UTF-8 JSON"
    if not isinstance(req, dict) or req.get("version") != VERSION:
        return None, f"request must be an object with version {VERSION!r}"
    extra = set(req) - {"version", "id", "question", "background", "items", "criteria"}
    if extra:
        return None, f"unknown request fields: {sorted(extra)}"
    q, bg, items = req.get("question"), req.get("background", ""), req.get("items")
    if not isinstance(q, str) or not q.strip() or len(q) > lim.max_question_chars:
        return (
            None,
            f"question must be a non-empty string of at most {lim.max_question_chars} chars",
        )
    if not isinstance(bg, str) or len(bg) > lim.max_background_chars:
        return None, f"background must be a string of at most {lim.max_background_chars} chars"
    if not isinstance(items, list) or not 1 <= len(items) <= lim.max_items:
        return None, f"items must be a list of 1 to {lim.max_items} items"
    keys = []
    for it in items:
        if not isinstance(it, dict) or set(it) != {"key", "text"}:
            return None, "each item must have exactly key and text"
        if not isinstance(it["key"], str) or not it["key"] or len(it["key"]) > 64:
            return None, "item keys must be non-empty strings of at most 64 chars"
        if not isinstance(it["text"], str) or len(it["text"]) > lim.max_item_chars:
            return None, f"item text must be a string of at most {lim.max_item_chars} chars"
        keys.append(it["key"])
    if len(set(keys)) != len(keys):
        return None, "item keys must be unique"
    crit = req.get("criteria")
    if crit is not None and (
        not isinstance(crit, dict)
        or set(crit) != {"true", "false"}
        or not all(isinstance(v, str) and 0 < len(v) <= 1000 for v in crit.values())
    ):
        return None, "criteria must be {'true': str, 'false': str} (each at most 1000 chars)"
    return req, ""


def build_body(req: dict[str, Any], model: str) -> dict[str, Any]:
    """The TypeSafe request: the code's background and items, one noul question per item."""
    crit = req.get("criteria") or DEFAULT_CRITERIA
    return {
        "state": {"background": req.get("background", ""), "items": req["items"]},
        "model": model,
        "questions": {
            f"i{n}": {
                "type": "noul",
                "instructions": {
                    "question": req["question"],
                    "item_key": it["key"],
                    "item": it["text"],
                    "scope": SCOPE,
                },
                "criteria": crit,
            }
            for n, it in enumerate(req["items"])
        },
    }


def _read_regular(path: Path, limit: int) -> bytes | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > limit + 1:
        return None
    with open(path, "rb") as fh:
        return fh.read(limit + 1)


@dataclass
class JevBroker:
    """Host side of one execution's mailbox. ``call(body) -> (response or None, error)`` is the
    runtime's recorded Jev attempt loop; ``record(rec)`` stores each exchange."""

    workspace: Path
    call: Callable[[dict[str, Any]], tuple[Any, str | None]]
    record: Callable[[dict[str, Any]], None]
    model: str
    limits: PrimitiveLimits
    run_calls_used: int = 0
    handled: set[str] = field(default_factory=set)
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    stop: Exception | None = None

    @property
    def box(self) -> Path:
        return self.workspace / MAILBOX

    def __call__(self) -> None:
        """Poll once: answer every new, well-formed request file."""
        box = self.box
        if not box.is_dir() or box.is_symlink():
            return
        for path in sorted(box.glob("req-*.json")):
            rid = path.name[4:-5]
            if rid in self.handled:
                continue
            self.handled.add(rid)
            self._handle(rid, path)

    def _respond(self, rid: str, resp: dict[str, Any]) -> None:
        if not ID_RE.match(rid):
            return  # never write a response named by an unexpected id
        tmp = self.box / f".resp-{rid}.tmp"
        tmp.write_text(json.dumps(resp), encoding="utf-8")
        os.replace(tmp, self.box / f"resp-{rid}.json")

    def _handle(self, rid: str, path: Path) -> None:
        t0 = time.monotonic()
        rec: dict[str, Any] = {"version": VERSION, "id": rid}
        raw = _read_regular(path, self.limits.max_request_bytes)
        req, err = (
            (None, "request file unreadable or not a regular file")
            if raw is None
            else validate(raw, self.limits)
        )
        keys = [it["key"] for it in req["items"]] if req else []
        resp: dict[str, Any]
        if req is None:
            resp = {
                "status": "error",
                "error": f"invalid request: {err}",
                "answers": {},
                "model": None,
            }
            rec.update(status="invalid", error=err, request_bytes=len(raw or b""))
        elif self.stop is not None:
            resp = {
                "status": "error",
                "error": "the run's budget is exhausted",
                "answers": dict.fromkeys(keys),
                "model": None,
            }
            rec.update(status="refused_budget", request=req)
        elif self.limits.max_calls_per_execution <= sum(
            1 for e in self.exchanges if e.get("called")
        ):
            resp = {
                "status": "error",
                "error": f"limit of {self.limits.max_calls_per_execution} calls per execution reached",
                "answers": dict.fromkeys(keys),
                "model": None,
            }
            rec.update(status="limit_execution", request=req)
        elif self.run_calls_used >= self.limits.max_calls_per_run:
            resp = {
                "status": "error",
                "error": f"limit of {self.limits.max_calls_per_run} calls per run reached",
                "answers": dict.fromkeys(keys),
                "model": None,
            }
            rec.update(status="limit_run", request=req)
        else:
            body = build_body(req, self.model)
            rec.update(request=req, body=body, called=True)
            self.run_calls_used += 1
            try:
                out, cerr = self.call(body)
            except Exception as exc:  # e.g. the runtime's budget stop: answer, then re-raise later
                self.stop = exc
                out, cerr = None, "the run's budget is exhausted"
            if out is None:
                resp = {
                    "status": "error",
                    "error": cerr or "service error",
                    "answers": dict.fromkeys(keys),
                    "model": None,
                }
                rec.update(status="error", error=cerr)
            else:
                answers = {
                    it["key"]: out.probabilities.get(f"i{n}") for n, it in enumerate(req["items"])
                }
                resp = {"status": "ok", "answers": answers, "model": out.model, "error": None}
                rec.update(
                    status="ok",
                    answers=answers,
                    model=out.model,
                    missing=[k for k, v in answers.items() if v is None],
                )
        self._respond(rid, resp)
        rec.update(response=resp, host_s=round(time.monotonic() - t0, 4))
        self.exchanges.append(rec)
        self.record(rec)

    def finish(self) -> list[str]:
        """After the execution: list request files left unanswered (the execution ended first;
        no Jev call is made for them), then empty the mailbox."""
        left = []
        box = self.box
        if box.is_dir() and not box.is_symlink():
            for p in sorted(box.iterdir()):
                if p.name.startswith("req-") and p.name[4:-5] not in self.handled:
                    left.append(p.name[4:-5])
                if p.is_file() or p.is_symlink():
                    p.unlink()
        return left
