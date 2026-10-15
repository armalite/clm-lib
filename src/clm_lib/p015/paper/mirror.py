"""The paper arm's context mirror: the editable conversation as a text file, and back.

The editable conversation is every message after the pinned first request (system prompt and first user task).
Each message is one block, headed `[[CTX_TURN n role=user|assistant|tool]]`, in order. Inside a block:

- `user`: the user's text;
- `assistant`: the reply text, then one `<<call id=... tool=...>>` section per tool call holding its JSON arguments,
  closed by `<<end call>>`;
- `tool`: one `<<result id=... tool=...>>` (or `<<retry ...>>` for a validation or retry message) section per
  result, closed by `<<end result>>` (or `<<end retry>>`).

`parse` turns an edited file back into messages. A block whose text is unchanged keeps its original message object
(so provider metadata survives); a changed block is rebuilt from its text, and reasoning (thinking) parts are not
carried into a rebuilt reply. The result must be a valid conversation: replies and requests alternate, starting with
a reply after the pinned request; every tool call is answered by exactly one result in the next block, and no
result lacks a call. Anything else is rejected with a reason, and the previous context is kept.

The block format is this project's own choice: the public sources name the file and the one-block-per-message
`[[CTX_TURN i role=...]]` header, but not the encoding of tool calls inside a block.
"""

from __future__ import annotations

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
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

MIRROR_VERSION = "ctx-turn-mirror/1"
_HEADER = re.compile(r"^\[\[CTX_TURN (\d+) role=(user|assistant|tool)\]\][ \t]*$", re.MULTILINE)
_CALL = re.compile(r"<<call id=(\S+) tool=(\S+)>>\n(.*?)\n<<end call>>", re.DOTALL)
_RESULT = re.compile(r"<<(result|retry) id=(\S+) tool=(\S+)>>\n(.*?)\n<<end \1>>", re.DOTALL)


class MirrorError(ValueError):
    """The edited mirror is not a valid conversation."""


def role_of(message: ModelMessage) -> str:
    if isinstance(message, ModelResponse):
        return "assistant"
    if any(isinstance(p, ToolReturnPart | RetryPromptPart) for p in message.parts):
        return "tool"
    return "user"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def body(message: ModelMessage) -> str:
    out: list[str] = []
    if isinstance(message, ModelResponse):
        text = "".join(p.content for p in message.parts if isinstance(p, TextPart)).strip()
        if text:
            out.append(text)
        for p in message.parts:
            if isinstance(p, ToolCallPart):
                out.append(
                    f"<<call id={p.tool_call_id} tool={p.tool_name}>>\n{p.args_as_json_str()}\n<<end call>>"
                )
        return "\n".join(out)
    for q in message.parts:
        if isinstance(q, ToolReturnPart):
            out.append(
                f"<<result id={q.tool_call_id} tool={q.tool_name}>>\n{q.model_response_str()}\n<<end result>>"
            )
        elif isinstance(q, RetryPromptPart):
            out.append(
                f"<<retry id={q.tool_call_id} tool={q.tool_name or 'retry'}>>\n{q.model_response()}\n<<end retry>>"
            )
        elif isinstance(q, UserPromptPart):
            out.append(_text(q.content))
    return "\n".join(out)


def render(messages: Sequence[ModelMessage]) -> str:
    blocks = [
        f"[[CTX_TURN {i} role={role_of(m)}]]\n{body(m)}" for i, m in enumerate(messages, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _rebuild(role: str, text: str) -> ModelMessage:
    if role == "user":
        if not text.strip():
            raise MirrorError("an empty user block")
        return ModelRequest(parts=[UserPromptPart(text.strip())])
    if role == "assistant":
        calls = [(m.group(1), m.group(2), m.group(3)) for m in _CALL.finditer(text)]
        prose = _CALL.sub("", text).strip()
        parts: list[Any] = [TextPart(prose)] if prose else []
        for cid, tool, args in calls:
            try:
                value = json.loads(args)
            except json.JSONDecodeError as exc:
                raise MirrorError(f"call {cid}: arguments are not JSON") from exc
            if not isinstance(value, dict):
                raise MirrorError(f"call {cid}: arguments must be an object")
            parts.append(ToolCallPart(tool, value, cid))
        if not parts:
            raise MirrorError("an empty assistant block")
        return ModelResponse(parts=parts)
    results = list(_RESULT.finditer(text))
    if not results or _RESULT.sub("", text).strip():
        raise MirrorError("a tool block must hold only result sections")
    tool_parts: list[Any] = []
    for m in results:
        kind, cid, tool, content = m.group(1), m.group(2), m.group(3), m.group(4)
        if kind == "result":
            tool_parts.append(ToolReturnPart(tool, content, cid))
        else:
            tool_parts.append(RetryPromptPart(content=content, tool_name=tool, tool_call_id=cid))
    return ModelRequest(parts=tool_parts)


def parse(text: str, originals: Sequence[ModelMessage]) -> list[ModelMessage]:
    headers = list(_HEADER.finditer(text))
    if text[: headers[0].start()].strip() if headers else text.strip():
        raise MirrorError("text before the first [[CTX_TURN ...]] header")
    unused: dict[tuple[str, str], list[ModelMessage]] = {}
    for m in originals:
        unused.setdefault((role_of(m), body(m)), []).append(m)
    out: list[ModelMessage] = []
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        role, block = h.group(2), text[h.end() : end].strip("\n").rstrip()
        same = unused.get((role, block))
        out.append(same.pop(0) if same else _rebuild(role, block))
    problems = conversation_problems(out)
    if problems:
        raise MirrorError("; ".join(problems))
    return out


def conversation_problems(messages: Sequence[ModelMessage]) -> list[str]:
    """Problems with an editable conversation (empty when valid). It follows the pinned first request, so it must
    start with a reply; replies and requests alternate; calls and results pair one to one, block to block."""
    problems: list[str] = []
    for i, m in enumerate(messages):
        want = ModelResponse if i % 2 == 0 else ModelRequest
        if not isinstance(m, want):
            problems.append(
                f"block {i + 1}: expected {'an assistant' if want is ModelResponse else 'a user or tool'} block"
            )
            break
        if isinstance(m, ModelResponse):
            calls = [p.tool_call_id for p in m.parts if isinstance(p, ToolCallPart)]
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            answered = (
                []
                if nxt is None
                else [
                    p.tool_call_id
                    for p in nxt.parts
                    if isinstance(p, ToolReturnPart | RetryPromptPart)
                ]
            )
            if calls and nxt is not None and sorted(calls) != sorted(answered):
                problems.append(f"block {i + 1}: tool calls {calls} are answered by {answered}")
            if not calls and answered:
                problems.append(f"block {i + 2}: results {answered} answer no call")
    return problems
