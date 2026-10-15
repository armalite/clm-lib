"""Native tool-use transport ``native-tools/1`` (programme 015). Opt-in.

The single-action protocol (``provider.ModelRequest``: one JSON action per call, no native tool
calls) is unchanged and remains the only path used by historical configurations. Nothing in the
runner uses this module yet.

What this module does:

- builds Messages API requests that carry a message history and native tool definitions;
- preserves the model's response content blocks exactly as returned (text, ``thinking`` with its
  signature, ``redacted_thinking``, ``tool_use``), in order, so they can be passed back unchanged
  where an arm replays message history;
- extracts the tool calls the model actually emitted (zero, one or several), with their IDs,
  names and arguments, in emission order;
- classifies each response as ``tool_calls``, ``completion`` or ``malformed``, with explicit
  reasons, and each call as valid or invalid;
- builds tool-result messages that pair exactly one result with every emitted call, in the same
  order, and validates the pairing of a whole history.

What it deliberately does not do (arm policy, decided later per arm):

- whether several calls in one response may run, and in what order: a call that an arm declines
  to execute still gets an explicit result (``not_executed_result``), so a batch is never
  silently reduced to its first call or rewritten;
- how the working context is represented. Paper arms keep a message history; lite arms rebuild
  each request from their working context. Both just hand this module a list of messages;
- step accounting, limits and output caps (``counters.py``, ``output_cap.py``).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .provider import ProviderError, Usage, classify_anthropic_error, usage_from_sdk
from .tooling import ToolOutcome, ToolRegistry, validate_arguments

TRANSPORT_VERSION = "native-tools/1"

# Content block types this transport understands in a response. Anything else (for example a
# server-tool block) makes the response malformed for this transport; it is still preserved.
KNOWN_BLOCKS = ("text", "thinking", "redacted_thinking", "tool_use")
NORMAL_STOPS = ("end_turn", "stop_sequence")


class PairingError(ValueError):
    """Tool results do not pair one-to-one, in order, with the emitted tool calls."""


# ------------------------------------------------------------------ requests
@dataclass(frozen=True)
class NativeRequest:
    """One stateless Messages request with native tools.

    ``messages`` is the complete history the arm wants the model to see, as API message dicts
    (``{"role": "user" | "assistant", "content": str | [blocks]}``). The transport never adds,
    drops or reorders messages. ``purpose`` is ``"main"`` (the task model) or ``"summary"``.
    ``cache_breakpoints`` are message indices whose last content block gets ``cache_control``
    when ``cache_ttl`` is set; placement is an arm decision.
    """

    system_blocks: tuple[str, ...]
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    max_tokens: int
    purpose: Literal["main", "summary"]
    tool_choice: dict[str, Any] | None = None
    cache_breakpoints: tuple[int, ...] = ()
    cache_ttl: str | None = None

    def __post_init__(self) -> None:
        if not self.system_blocks or any(not b for b in self.system_blocks):
            raise ValueError("system blocks must be present and non-empty")
        if not self.messages:
            raise ValueError("a request needs at least one message")
        if self.purpose not in ("main", "summary"):
            raise ValueError(f"unknown purpose {self.purpose!r}")
        for i in self.cache_breakpoints:
            if not 0 <= i < len(self.messages):
                raise ValueError(f"cache breakpoint {i} is outside the message list")


def native_body(
    req: NativeRequest,
    model: str,
    effort: str | None = None,
    thinking: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The Messages API body. Inputs are deep-copied, so callers' history is never mutated."""
    messages: list[dict[str, Any]] = copy.deepcopy(list(req.messages))
    if req.cache_ttl is not None:
        for i in req.cache_breakpoints:
            content = messages[i]["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
                messages[i]["content"] = content
            content[-1]["cache_control"] = {"type": "ephemeral", "ttl": req.cache_ttl}
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": req.max_tokens,
        "system": [{"type": "text", "text": t} for t in req.system_blocks],
        "messages": messages,
    }
    if req.tools:
        body["tools"] = copy.deepcopy(list(req.tools))
    if req.tool_choice is not None:
        body["tool_choice"] = dict(req.tool_choice)
    if thinking is not None:
        body["thinking"] = dict(thinking)
    if effort:
        body["output_config"] = {"effort": effort}
    return body


# ----------------------------------------------------------------- responses
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Any  # exactly as emitted; normally a dict, but never coerced
    index: int  # position among this response's tool calls
    block_index: int  # position in the response's content blocks


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False

    def to_block(self) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": self.content,
        }
        if self.is_error:
            block["is_error"] = True
        return block


@dataclass(frozen=True)
class NativeResponse:
    content: tuple[dict[str, Any], ...]  # provider content blocks, verbatim, in order
    stop_reason: str | None
    model: str
    usage: Usage
    request_id: str | None = None
    usage_source: str = "provider"  # "provider" | "scripted"

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        out: list[ToolCall] = []
        for bi, b in enumerate(self.content):
            if b.get("type") == "tool_use":
                out.append(
                    ToolCall(
                        str(b.get("id", "")), str(b.get("name", "")), b.get("input"), len(out), bi
                    )
                )
        return tuple(out)

    @property
    def text(self) -> str:
        return "".join(str(b.get("text", "")) for b in self.content if b.get("type") == "text")

    def assistant_message(self) -> dict[str, Any]:
        """The response as an assistant history message: every block, unchanged and in order
        (thinking signatures included). Arms that replay history must use this."""
        return {"role": "assistant", "content": copy.deepcopy(list(self.content))}


ResponseKind = Literal["tool_calls", "completion", "malformed"]


@dataclass(frozen=True)
class CallCheck:
    call: ToolCall
    problems: tuple[str, ...]  # empty: the call names a known tool with valid arguments

    @property
    def valid(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class Interpretation:
    """How the runtime reads one response.

    - ``tool_calls``: one or more calls, all pairable (unique non-empty IDs). Individual calls may
      still be invalid (unknown tool, invalid arguments); each gets an error result.
    - ``completion``: no tool call and a normal stop.
    - ``malformed``: anything else, with ``problems`` naming why (for example a call cut off by
      ``max_tokens``, a ``tool_use`` stop without calls, duplicate IDs, an unknown block type).
      Calls in a malformed response are reported but must not be executed.
    """

    kind: ResponseKind
    problems: tuple[str, ...]
    calls: tuple[CallCheck, ...]


def interpret(resp: NativeResponse, registry: ToolRegistry) -> Interpretation:
    problems: list[str] = []
    for b in resp.content:
        t = b.get("type")
        if t not in KNOWN_BLOCKS:
            problems.append(f"unexpected_block:{t}")
    calls = resp.tool_calls
    ids = [c.id for c in calls]
    if any(not i for i in ids):
        problems.append("tool_call_without_id")
    if len(set(ids)) != len(ids):
        problems.append("duplicate_tool_call_id")
    stop = resp.stop_reason
    if stop == "max_tokens":
        problems.append("truncated_tool_call" if calls else "truncated_output")
    elif stop == "tool_use":
        if not calls:
            problems.append("tool_use_stop_without_calls")
    elif stop in NORMAL_STOPS:
        if calls:
            problems.append(f"tool_calls_with_stop:{stop}")
    else:
        problems.append(f"unexpected_stop_reason:{stop}")
    checks: list[CallCheck] = []
    for c in calls:
        cp: list[str] = []
        spec = registry.get(c.name)
        if spec is None:
            cp.append(f"unknown_tool:{c.name}")
        elif not isinstance(c.arguments, dict):
            cp.append("arguments_not_an_object")
        else:
            cp += validate_arguments(spec.input_schema, c.arguments)
        checks.append(CallCheck(c, tuple(cp)))
    if problems:
        return Interpretation("malformed", tuple(problems), tuple(checks))
    return Interpretation("tool_calls" if calls else "completion", (), tuple(checks))


# --------------------------------------------------------------- tool results
def outcome_result(call: ToolCall, outcome: ToolOutcome, content: str | None = None) -> ToolResult:
    """A result for an executed call. ``content`` overrides the outcome's text (for example the
    capped text from ``output_cap``)."""
    return ToolResult(call.id, outcome.content if content is None else content, outcome.is_error)


def not_executed_result(call: ToolCall, reason: str) -> ToolResult:
    """An explicit error result for a call that was not executed (invalid, or declined by the
    arm's policy). Every emitted call is answered; none is silently dropped."""
    return ToolResult(call.id, f"[not executed: {reason}]", True)


def results_message(
    calls: tuple[ToolCall, ...] | list[ToolCall],
    results: list[ToolResult],
    trailing_text: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The user message answering one response: exactly one result per emitted call, in the
    calls' order, then any runtime text blocks (for example a status notice)."""
    if len(results) != len(calls):
        raise PairingError(f"{len(calls)} tool calls but {len(results)} results")
    for c, r in zip(calls, results, strict=True):
        if c.id != r.tool_use_id:
            raise PairingError(f"result for {r.tool_use_id!r} where {c.id!r} was expected")
    if not calls:
        raise PairingError("no tool calls to answer")
    content: list[dict[str, Any]] = [r.to_block() for r in results]
    content += [{"type": "text", "text": t} for t in trailing_text if t]
    return {"role": "user", "content": content}


def validate_history(messages: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[str]:
    """Problems with a history's tool pairing (empty when valid).

    Rules: roles alternate starting with ``user``; every ``tool_use`` in an assistant message is
    answered in the immediately following user message by exactly one ``tool_result``; those
    results come first in that message and in the calls' order; no result answers an unknown or
    already answered call; tool-call IDs are unique across the history.
    """
    problems: list[str] = []
    seen: set[str] = set()
    pending: list[str] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        want = "user" if i % 2 == 0 else "assistant"
        if role != want:
            problems.append(f"message {i}: role {role!r}, expected {want!r}")
            continue
        content = m.get("content")
        blocks = content if isinstance(content, list) else []
        if role == "assistant":
            if pending:
                problems.append(f"message {i}: calls {pending} were never answered")
            pending = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    tid = str(b.get("id", ""))
                    if tid in seen:
                        problems.append(f"message {i}: duplicate tool call id {tid!r}")
                    seen.add(tid)
                    pending.append(tid)
            continue
        answered: list[str] = []
        for j, b in enumerate(blocks):
            if b.get("type") != "tool_result":
                continue
            if j != len(answered):
                problems.append(f"message {i}: tool_result after other content")
            answered.append(str(b.get("tool_use_id", "")))
        if answered != pending:
            problems.append(f"message {i}: results {answered} do not answer calls {pending}")
        pending = []
    if pending:
        problems.append(f"history ends with unanswered calls {pending}")
    return problems


# ------------------------------------------------------------------ providers
class NativeProvider(Protocol):
    name: str
    model: str
    is_scripted: bool

    def payload(self, req: NativeRequest) -> dict[str, Any]: ...

    def complete(self, req: NativeRequest) -> NativeResponse: ...

    def describe(self) -> dict[str, Any]: ...


def _block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return copy.deepcopy(block)
    dumped: dict[str, Any] = block.model_dump(mode="json", exclude_none=True)
    return dumped


@dataclass
class AnthropicNativeProvider:
    """Messages API adapter for ``native-tools/1``.

    Same credential handling, timeout and disabled SDK retries as ``AnthropicProvider``, and the
    same error classification (``classify_anthropic_error``). Response blocks are kept as plain
    dicts (``model_dump``, ``None`` fields dropped) so they can be saved and replayed.

    Not yet exercised against the live API: compatibility of the replayed blocks is unverified
    until an authorised smoke call. ``thinking`` and ``effort`` are passed through unchanged; no
    model setting is assumed here.
    """

    model: str
    base_url: str | None = None
    timeout_s: float = 120.0
    effort: str | None = None
    thinking: dict[str, Any] | None = None
    name: str = "anthropic"
    is_scripted: bool = False
    _client: Any = field(default=None, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            kwargs: dict[str, Any] = {"timeout": self.timeout_s, "max_retries": 0}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "transport": TRANSPORT_VERSION,
            "model": self.model,
            "base_url": self.base_url or "(SDK default / ANTHROPIC_BASE_URL)",
            "effort": self.effort,
            "thinking": self.thinking,
            "timeout_s": self.timeout_s,
            "sdk_retries": 0,
        }

    def payload(self, req: NativeRequest) -> dict[str, Any]:
        return native_body(req, self.model, self.effort, self.thinking)

    def complete(self, req: NativeRequest) -> NativeResponse:
        client = self._get_client()
        try:
            msg = client.messages.create(**self.payload(req))
        except Exception as exc:
            raise classify_anthropic_error(exc) from exc
        resp = NativeResponse(
            tuple(_block_dict(b) for b in msg.content),
            msg.stop_reason,
            msg.model,
            usage_from_sdk(msg.usage),
            getattr(msg, "_request_id", None),
        )
        if msg.stop_reason == "refusal":
            err = ProviderError(
                "refusal", "model declined the request", retryable=False, billing="billed"
            )
            err.response = resp  # type: ignore[attr-defined]
            raise err
        return resp


NativeScriptStep = dict[str, Any] | Callable[[NativeRequest], dict[str, Any]]


@dataclass
class ScriptedNativeProvider:
    """Deterministic test double for ``native-tools/1``. Refused by live entry points.

    Each script step is ``{"content": [blocks], "stop_reason": ..., "usage": {...}}`` (usage
    fields as in ``provider.Usage``; omitted ones are 0), or a callable returning one. A step with
    ``"stop_reason": "refusal"`` raises a billed refusal, as the real adapter does.
    ``fail_with`` maps a 0-based attempt index to the ProviderError raised at that attempt.
    """

    script: list[NativeScriptStep]
    model: str = "scripted"
    name: str = "scripted"
    is_scripted: bool = True
    requests: list[NativeRequest] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    fail_with: dict[int, ProviderError] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "transport": TRANSPORT_VERSION, "note": "scripted double"}

    def payload(self, req: NativeRequest) -> dict[str, Any]:
        return native_body(req, self.model)

    def complete(self, req: NativeRequest) -> NativeResponse:
        index = len(self.requests)
        self.requests.append(req)
        self.payloads.append(self.payload(req))
        if index in self.fail_with:
            raise self.fail_with[index]
        if not self.script:
            raise ProviderError("other", "script exhausted", retryable=False, billing="none")
        step = self.script.pop(0)
        out = step(req) if callable(step) else step
        usage = Usage(**out.get("usage", {}))
        resp = NativeResponse(
            tuple(copy.deepcopy(out["content"])),
            out.get("stop_reason", "end_turn"),
            self.model,
            usage,
            out.get("request_id"),
            # Tests of the accounting bound check may script "provider"-sourced usage.
            usage_source=out.get("usage_source", "scripted"),
        )
        if resp.stop_reason == "refusal":
            err = ProviderError(
                "refusal", "model declined the request", retryable=False, billing="billed"
            )
            err.response = resp  # type: ignore[attr-defined]
            raise err
        return resp
