"""The paper arm's part-level context mirror (`ctx-part-mirror/1`, used by `paper-clm-shell/3`).

The editable context is every message part after the pinned first request (system prompt and first question), up to
but not including the reply that holds the running command, which cannot change while it runs. Each part is one
block, in order, headed `[[CTX_BLOCK n kind=... ]]`:

- `kind=user`: a user message (a later question, or a note added earlier);
- `kind=assistant`: reply text;
- `kind=tool_call tool=... id=...`: one tool call's JSON arguments;
- `kind=tool_result tool=... id=...`: one tool result's text (or a tool's retry message).

Reasoning (thinking) parts are not shown; they stay with their reply while it is unchanged.

**What an edit can do** (the model decides; the runtime only checks structure and rebuilds):
- keep a block by keeping its header line, with its text unchanged or rewritten. A tool result's text can be replaced
  by any text, for example a summary, and stays the native result of its call; a tool call's text must stay a JSON
  object;
- delete a block by removing its header and text. A tool call and its result are deleted together, whichever of the
  two is removed;
- add a note under a `[[CTX_BLOCK note]]` header; text before the first header is a note too. A note becomes user
  text in the next message from the user or the tools, after that message's tool results; a note before every
  kept block opens the context;
- the blocks kept must stay in their order.

**What is rebuilt:** each message keeps its unchanged parts as they were; a message whose parts all went is dropped,
and two messages of the same kind that become neighbours are merged. A reply that changed loses its reasoning parts
(a replayed signature must belong to the reply as written). The result must start with a reply (it follows the pinned
request), and every tool call must still have its result next to it. Anything else is rejected with a reason, and the
previous context is kept. The context may begin with user text (a note placed before every kept block, or a
question whose earlier replies were deleted); the arm then sends it within the request that holds the pinned first
question, after the question, so that no two requests are sent in a row.

Header attributes other than the number are informative and are not read back.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

PART_MIRROR_VERSION = "ctx-part-mirror/1"
_HEADER = re.compile(r"^\[\[CTX_BLOCK (\d+|note)\b[^\]\n]*\]\][ \t]*$", re.MULTILINE)


class PartMirrorError(ValueError):
    """The edited file is not an acceptable context; nothing changes."""


@dataclasses.dataclass(frozen=True)
class Block:
    number: int
    message: int  # index in the editable messages
    part: int  # index in that message's parts
    kind: str  # user | assistant | tool_call | tool_result
    text: str
    tool: str | None = None
    call_id: str | None = None


def _part_text(part: Any) -> str:
    content = part.content
    if isinstance(content, str):
        return content
    if isinstance(part, UserPromptPart):
        return "".join(c for c in content if isinstance(c, str))
    return json.dumps(content, ensure_ascii=False, default=str)


def blocks(messages: Sequence[ModelMessage]) -> list[Block]:
    """The editable parts, numbered from 1. Reasoning and other non-text parts are not blocks."""
    out: list[Block] = []
    for m, message in enumerate(messages):
        for p, part in enumerate(message.parts):
            if isinstance(part, TextPart):
                block = Block(0, m, p, "assistant", part.content)
            elif isinstance(part, ToolCallPart):
                block = Block(
                    0, m, p, "tool_call", part.args_as_json_str(), part.tool_name, part.tool_call_id
                )
            elif isinstance(part, ToolReturnPart):
                block = Block(
                    0,
                    m,
                    p,
                    "tool_result",
                    part.model_response_str(),
                    part.tool_name,
                    part.tool_call_id,
                )
            elif isinstance(part, RetryPromptPart) and part.tool_name:
                block = Block(
                    0, m, p, "tool_result", part.model_response(), part.tool_name, part.tool_call_id
                )
            elif isinstance(part, RetryPromptPart):
                block = Block(0, m, p, "user", part.model_response())
            elif isinstance(part, UserPromptPart):
                block = Block(0, m, p, "user", _part_text(part))
            else:
                continue
            out.append(dataclasses.replace(block, number=len(out) + 1))
    return out


def render(messages: Sequence[ModelMessage]) -> str:
    lines = []
    for b in blocks(messages):
        attrs = f" kind={b.kind}" + (f" tool={b.tool} id={b.call_id}" if b.call_id else "")
        lines.append(f"[[CTX_BLOCK {b.number}{attrs}]]\n{b.text}")
    return "\n\n".join(lines) + ("\n" if lines else "")


def _replace(part: Any, kind: str, text: str) -> Any:
    if kind == "tool_call":
        try:
            args = json.loads(text)
        except json.JSONDecodeError:
            args = None
        if not isinstance(args, dict):
            raise PartMirrorError(
                f"the {part.tool_name} call's text must stay a JSON object of arguments"
            )
        return dataclasses.replace(part, args=args)
    return dataclasses.replace(part, content=text)


def _with_parts(message: ModelMessage, parts: list[Any]) -> ModelMessage:
    if isinstance(message, ModelRequest):
        return dataclasses.replace(message, parts=parts)
    return dataclasses.replace(message, parts=parts)


def parse(text: str, messages: Sequence[ModelMessage]) -> tuple[list[ModelMessage], list[set[int]]]:
    """The edited file as messages, and for each new message the indices of the original messages it came from.
    Raises `PartMirrorError` (nothing changes) for an edit the rules do not allow."""
    view = blocks(messages)
    by_number = {b.number: b for b in view}
    headers = list(_HEADER.finditer(text))
    sections: list[tuple[str, str]] = []
    lead = text[: headers[0].start()] if headers else text
    if lead.strip():
        sections.append(("note", lead.strip()))
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        sections.append((h.group(1), text[h.end() : end].strip("\n").rstrip()))

    kept: dict[int, str] = {}
    notes_after: dict[int, list[str]] = {}  # block number (0 = the start) -> notes placed after it
    last = 0
    for key, body in sections:
        if key == "note":
            if not body.strip():
                raise PartMirrorError("a note is empty")
            notes_after.setdefault(last, []).append(body.strip())
            continue
        n = int(key)
        if n not in by_number:
            raise PartMirrorError(f"there is no block {n}; the file had blocks 1 to {len(view)}")
        if n in kept:
            raise PartMirrorError(f"block {n} appears twice")
        if n < last:
            raise PartMirrorError(
                f"block {n} is out of order: kept blocks must stay in their order"
            )
        if not body.strip():
            raise PartMirrorError(f"block {n} would be empty; delete it instead")
        kept[n] = body
        last = n

    # A tool call and its result go together.
    gone = {by_number[n].call_id for n in by_number if n not in kept and by_number[n].call_id}
    for n in list(kept):
        b = by_number[n]
        if b.call_id in gone:
            if kept[n] != b.text.rstrip():
                raise PartMirrorError(
                    f"block {n} was changed, but its paired {'result' if b.kind == 'tool_call' else 'call'} "
                    "was deleted; a tool call and its result are kept or deleted together"
                )
            del kept[n]

    # The new parts of each message; notes go into the request holding or following their anchor.
    parts: list[list[Any]] = [[] for _ in messages]
    changed = [False] * len(messages)
    for b in view:
        part = messages[b.message].parts[b.part]
        if b.number not in kept:
            changed[b.message] = True
            continue
        if kept[b.number] != b.text.rstrip():
            part = _replace(part, b.kind, kept[b.number])
            changed[b.message] = True
        parts[b.message].append((b.part, part))
    for m, message in enumerate(messages):  # reasoning and other non-block parts stay in place
        listed = {b.part for b in view if b.message == m}
        for p, part in enumerate(message.parts):
            if p not in listed:
                parts[m].append((p, part))
        parts[m].sort(key=lambda x: x[0])
    leading = [UserPromptPart(text) for text in notes_after.pop(0, [])]
    for anchor, texts in notes_after.items():
        start = by_number[anchor].message
        target = next(
            (m for m in range(start, len(messages)) if isinstance(messages[m], ModelRequest)), None
        )
        if target is None:
            raise PartMirrorError("a note has no following user or tool message to go into")
        changed[target] = True
        parts[target].extend(
            (len(messages[target].parts) + i, UserPromptPart(t)) for i, t in enumerate(texts)
        )

    rebuilt: list[tuple[ModelMessage, set[int]]] = (
        [(ModelRequest(parts=leading), set())] if leading else []
    )  # a note before every kept block opens the context
    for m, message in enumerate(messages):
        new = [p for _, p in parts[m]]
        if isinstance(message, ModelResponse):
            if changed[m]:
                new = [p for p in new if not isinstance(p, ThinkingPart)]
            if not any(not isinstance(p, ThinkingPart) for p in new):
                continue  # a reply with nothing left is dropped (with its reasoning)
        elif not new:
            continue
        current = message if not changed[m] else _with_parts(message, new)
        if rebuilt and type(rebuilt[-1][0]) is type(current):
            prev, origin = rebuilt[-1]
            merged_parts = [*prev.parts, *current.parts]
            if isinstance(current, ModelResponse):
                merged_parts = [p for p in merged_parts if not isinstance(p, ThinkingPart)]
            else:  # results first, so the provider sees them straight after their calls
                merged_parts = sorted(
                    merged_parts, key=lambda p: not isinstance(p, ToolReturnPart | RetryPromptPart)
                )
            rebuilt[-1] = (_with_parts(prev, merged_parts), origin | {m})
        else:
            rebuilt.append((current, {m}))
    out = [msg for msg, _ in rebuilt]
    problems = structure_problems(out)
    if problems:
        raise PartMirrorError("; ".join(problems))
    return out, [origin for _, origin in rebuilt]


def structure_problems(messages: Sequence[ModelMessage]) -> list[str]:
    """Problems with an editable conversation that follows the pinned request (empty when valid)."""
    problems: list[str] = []
    if messages and isinstance(messages[-1], ModelResponse):
        problems.append(
            "the context must end with a user or tool message, because your current reply follows it; keep the "
            "results of your last kept reply, or delete that reply's text too"
        )
    for i, m in enumerate(messages):
        if isinstance(m, ModelResponse):
            calls = {p.tool_call_id for p in m.parts if isinstance(p, ToolCallPart)}
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            answered = (
                {
                    p.tool_call_id
                    for p in nxt.parts
                    if isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_name
                }
                if isinstance(nxt, ModelRequest)
                else set()
            )
            if calls - answered:
                problems.append(f"tool calls {sorted(calls - answered)} would have no result")
    calls_all = {p.tool_call_id for m in messages if isinstance(m, ModelResponse) for p in m.parts
                 if isinstance(p, ToolCallPart)}  # fmt: skip
    for m in messages:
        if isinstance(m, ModelRequest):
            orphans = {p.tool_call_id for p in m.parts
                       if isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_name} - calls_all  # fmt: skip
            if orphans:
                problems.append(f"tool results {sorted(orphans)} would answer no call")
    return problems
