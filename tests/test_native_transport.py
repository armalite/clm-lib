"""native-tools/1 transport and tool interfaces (programme 015), with scripted providers only.

These tests check representation, pairing and classification offline. They say nothing about
compatibility with the live API, which has not been called.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from typing import Any

import anthropic
import pytest
from anthropic.types import Message

from clm_lib.provider import (
    AnthropicProvider,
    ModelRequest,
    ProviderError,
    classify_anthropic_error,
)
from clm_lib.tooling import ToolOutcome, ToolRegistry, ToolSpec, validate_arguments
from clm_lib.transport import (
    TRANSPORT_VERSION,
    AnthropicNativeProvider,
    NativeRequest,
    NativeResponse,
    PairingError,
    ScriptedNativeProvider,
    ToolResult,
    interpret,
    not_executed_result,
    outcome_result,
    results_message,
    validate_history,
)

SEARCH = ToolSpec(
    "search",
    "Search the incident thread's documents.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "k": {"type": "integer", "minimum": 1},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "task",
    "none",
)
READ_PAGE = ToolSpec(
    "read_page",
    "Read one page by id.",
    {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
    "task",
    "none",
)
CONTEXT_APPLY = ToolSpec(
    "context_apply",
    "Apply an edit to the working context.",
    {"type": "object", "properties": {"edit": {"type": "string"}}, "required": ["edit"]},
    "context",
    "context",
)
SHELL = ToolSpec(
    "bash",
    "Run one shell command in the sandbox.",
    {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    "task",
    "workspace",
)
REG = ToolRegistry((SEARCH, READ_PAGE, CONTEXT_APPLY))


def tool_use(i: str, name: str, args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": i, "name": name, "input": args}


def resp(content: list[dict[str, Any]], stop: str | None) -> NativeResponse:
    from clm_lib.provider import Usage

    return NativeResponse(tuple(content), stop, "scripted", Usage())


def request(messages: list[dict[str, Any]], **kw: Any) -> NativeRequest:
    return NativeRequest(("system text",), tuple(messages), REG.to_api(), 1024, "main", **kw)


# ------------------------------------------------------------- tool interfaces
def test_tool_categories_and_side_effects_are_distinct_and_not_sent() -> None:
    assert REG.names("task") == ("search", "read_page")
    assert REG.names("context") == ("context_apply",)
    assert SHELL.category == "task" and SHELL.side_effects == "workspace"
    api = REG.to_api()
    assert [t["name"] for t in api] == ["search", "read_page", "context_apply"]
    assert all(set(t) == {"name", "description", "input_schema"} for t in api)
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry((SEARCH, SEARCH))
    with pytest.raises(ValueError, match="category"):
        ToolSpec("x", "d", {"type": "object"}, "other", "none")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="name"):
        ToolSpec("bad name", "d", {"type": "object"}, "task", "none")


def test_argument_validation() -> None:
    s = SEARCH.input_schema
    assert validate_arguments(s, {"query": "db", "k": 3}) == []
    assert validate_arguments(s, {}) == ["$: missing required 'query'"]
    assert validate_arguments(s, {"query": "db", "k": True}) == ["$.k: expected integer, got bool"]
    assert validate_arguments(s, {"query": "", "x": 1}) == [
        "$.query: shorter than 1",
        "$: unexpected property 'x'",
    ]
    assert validate_arguments(s, {"query": "db", "k": 0}) == ["$.k: below 1"]
    with pytest.raises(ValueError, match="unsupported"):
        validate_arguments({"type": "object", "oneOf": []}, {})


# ------------------------------------------------------------- responses
def test_single_call_is_represented_faithfully() -> None:
    content = [
        {"type": "thinking", "thinking": "", "signature": "sig-1"},
        {"type": "text", "text": "Searching."},
        tool_use("toolu_a", "search", {"query": "disk full"}),
    ]
    r = resp(content, "tool_use")
    it = interpret(r, REG)
    assert it.kind == "tool_calls" and it.problems == ()
    (c,) = r.tool_calls
    assert (c.id, c.name, c.arguments, c.index, c.block_index) == (
        "toolu_a",
        "search",
        {"query": "disk full"},
        0,
        2,
    )
    assert it.calls[0].valid
    # The assistant message keeps every block, thinking signature included, unchanged.
    msg = r.assistant_message()
    assert msg == {"role": "assistant", "content": content}
    msg["content"][0]["signature"] = "mutated"
    assert r.content[0]["signature"] == "sig-1"
    assert r.text == "Searching."


def test_multiple_calls_keep_emission_order_and_none_is_dropped() -> None:
    content = [
        tool_use("t1", "search", {"query": "a"}),
        {"type": "text", "text": "and"},
        tool_use("t2", "read_page", {"id": "p7"}),
        tool_use("t3", "search", {"query": "b"}),
    ]
    r = resp(content, "tool_use")
    it = interpret(r, REG)
    assert it.kind == "tool_calls"
    assert [c.call.id for c in it.calls] == ["t1", "t2", "t3"]
    assert [c.call.index for c in it.calls] == [0, 1, 2]
    assert [c.call.block_index for c in it.calls] == [0, 2, 3]
    # An arm that runs only one call must still answer the others explicitly.
    calls = r.tool_calls
    results = [
        outcome_result(calls[0], ToolOutcome("3 hits")),
        not_executed_result(calls[1], "policy: one call per response"),
        not_executed_result(calls[2], "policy: one call per response"),
    ]
    msg = results_message(calls, results, ("[status] step 4",))
    assert [b.get("tool_use_id") for b in msg["content"]] == ["t1", "t2", "t3", None]
    assert msg["content"][1]["is_error"] is True
    assert "is_error" not in msg["content"][0]
    assert msg["content"][3] == {"type": "text", "text": "[status] step 4"}


def test_results_must_pair_one_to_one_in_order() -> None:
    calls = resp(
        [tool_use("t1", "search", {"query": "a"}), tool_use("t2", "search", {"query": "b"})],
        "tool_use",
    ).tool_calls
    ok = [ToolResult("t1", "x"), ToolResult("t2", "y")]
    results_message(calls, ok)
    with pytest.raises(PairingError, match="2 tool calls but 1"):
        results_message(calls, ok[:1])
    with pytest.raises(PairingError, match="expected"):
        results_message(calls, list(reversed(ok)))
    with pytest.raises(PairingError, match="expected"):
        results_message(calls, [ToolResult("t1", "x"), ToolResult("t9", "y")])


def test_completion_without_tool_call() -> None:
    it = interpret(resp([{"type": "text", "text": "Final answer: ..."}], "end_turn"), REG)
    assert (it.kind, it.problems, it.calls) == ("completion", (), ())


@pytest.mark.parametrize(
    ("content", "stop", "problem"),
    [
        ([tool_use("t1", "search", {"query": "a"})], "max_tokens", "truncated_tool_call"),
        ([{"type": "text", "text": "cut"}], "max_tokens", "truncated_output"),
        ([{"type": "text", "text": "?"}], "tool_use", "tool_use_stop_without_calls"),
        ([tool_use("t1", "search", {"query": "a"})], "end_turn", "tool_calls_with_stop:end_turn"),
        (
            [tool_use("t1", "search", {}), tool_use("t1", "search", {})],
            "tool_use",
            "duplicate_tool_call_id",
        ),
        ([tool_use("", "search", {"query": "a"})], "tool_use", "tool_call_without_id"),
        ([{"type": "server_tool_use", "id": "s"}], "end_turn", "unexpected_block:server_tool_use"),
        ([{"type": "text", "text": "x"}], "pause_turn", "unexpected_stop_reason:pause_turn"),
        ([{"type": "text", "text": "x"}], None, "unexpected_stop_reason:None"),
    ],
)
def test_malformed_responses_are_explicit(
    content: list[dict[str, Any]], stop: str | None, problem: str
) -> None:
    it = interpret(resp(content, stop), REG)
    assert it.kind == "malformed"
    assert problem in it.problems


def test_invalid_calls_are_flagged_per_call_not_dropped() -> None:
    r = resp(
        [
            tool_use("t1", "search", {"query": "a"}),
            tool_use("t2", "delete_everything", {}),
            tool_use("t3", "read_page", "p7"),
            tool_use("t4", "read_page", {"page": "p7"}),
        ],
        "tool_use",
    )
    it = interpret(r, REG)
    assert it.kind == "tool_calls"
    assert [c.problems for c in it.calls] == [
        (),
        ("unknown_tool:delete_everything",),
        ("arguments_not_an_object",),
        ("$: missing required 'id'", "$: unexpected property 'page'"),
    ]
    assert r.tool_calls[2].arguments == "p7"  # never coerced


# ------------------------------------------------------------- history
def test_history_pairing_validation() -> None:
    a1 = {
        "role": "assistant",
        "content": [
            tool_use("t1", "search", {"query": "a"}),
            tool_use("t2", "search", {"query": "b"}),
        ],
    }
    good = [
        {"role": "user", "content": "task"},
        a1,
        {
            "role": "user",
            "content": [
                ToolResult("t1", "x").to_block(),
                ToolResult("t2", "y").to_block(),
                {"type": "text", "text": "note"},
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    assert validate_history(good) == []
    swapped = copy.deepcopy(good)
    swapped[2]["content"][:2] = swapped[2]["content"][1::-1]
    assert any("do not answer" in p for p in validate_history(swapped))
    text_first = copy.deepcopy(good)
    text_first[2]["content"].insert(0, {"type": "text", "text": "x"})
    assert any("after other content" in p for p in validate_history(text_first))
    missing = copy.deepcopy(good)
    missing[2]["content"] = missing[2]["content"][:1]
    assert any("do not answer" in p for p in validate_history(missing))
    assert validate_history(good[:2]) == ["history ends with unanswered calls ['t1', 't2']"]
    reused = [*copy.deepcopy(good[:3]), copy.deepcopy(a1)]
    assert any("duplicate tool call id" in p for p in validate_history(reused))
    assert any("expected 'user'" in p for p in validate_history([a1]))


# ------------------------------------------------------------- requests / providers
def test_payload_carries_tools_history_and_cache_breakpoints_without_mutation() -> None:
    history = [
        {"role": "user", "content": "task text"},
        {"role": "assistant", "content": [tool_use("t1", "search", {"query": "a"})]},
        {"role": "user", "content": [ToolResult("t1", "hits").to_block()]},
    ]
    before = copy.deepcopy(history)
    req = request(history, cache_breakpoints=(0, 2), cache_ttl="5m")
    body = AnthropicNativeProvider("claude-sonnet-5", effort="high").payload(req)
    assert history == before  # the arm's history is never mutated
    assert body["tools"] == list(REG.to_api())
    assert body["output_config"] == {"effort": "high"}
    assert "thinking" not in body and "tool_choice" not in body
    assert body["messages"][0]["content"] == [
        {"type": "text", "text": "task text", "cache_control": {"type": "ephemeral", "ttl": "5m"}}
    ]
    assert body["messages"][2]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert "cache_control" not in str(body["messages"][1])
    off = AnthropicNativeProvider("claude-sonnet-5").payload(
        request(history, cache_breakpoints=(0,))
    )
    assert "cache_control" not in str(off) and "output_config" not in off
    with pytest.raises(ValueError, match="outside"):
        request(history, cache_breakpoints=(3,))
    with pytest.raises(ValueError, match="purpose"):
        NativeRequest(("s",), tuple(history), (), 10, "action")  # type: ignore[arg-type]


class _FakeMessages:
    def __init__(self, out: Any) -> None:
        self.out = out
        self.bodies: list[dict[str, Any]] = []

    def create(self, **body: Any) -> Any:
        self.bodies.append(body)
        if isinstance(self.out, BaseException):
            raise self.out
        return self.out


class _FakeClient:
    def __init__(self, out: Any) -> None:
        self.messages = _FakeMessages(out)


def _sdk_message(stop: str = "tool_use") -> Message:
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "two lookups"},
                {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"query": "x"}},
                {"type": "tool_use", "id": "toolu_2", "name": "read_page", "input": {"id": "p1"}},
            ],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
                "cache_creation": {"ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 0},
            },
        }
    )


def test_anthropic_native_adapter_preserves_blocks_and_usage_offline() -> None:
    """The adapter's parsing of an SDK Message object (no network; a fake client)."""
    p = AnthropicNativeProvider("claude-sonnet-5")
    p._client = _FakeClient(_sdk_message())
    r = p.complete(request([{"role": "user", "content": "task"}]))
    assert [b["type"] for b in r.content] == ["thinking", "text", "tool_use", "tool_use"]
    assert r.content[0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    assert [c.id for c in r.tool_calls] == ["toolu_1", "toolu_2"]
    u = r.usage
    assert (
        u.input_tokens,
        u.output_tokens,
        u.cache_read_input_tokens,
        u.cache_creation_input_tokens,
    ) == (10, 5, 3, 2)
    assert (u.cache_creation_5m, u.cache_creation_1h) == (2, 0)
    assert r.usage_source == "provider"
    assert interpret(r, REG).kind == "tool_calls"
    assert p.describe()["transport"] == TRANSPORT_VERSION
    p._client = _FakeClient(_sdk_message("refusal"))
    with pytest.raises(ProviderError) as ei:
        p.complete(request([{"role": "user", "content": "task"}]))
    assert ei.value.kind == "refusal" and ei.value.billing == "billed"
    assert ei.value.response.usage.output_tokens == 5  # type: ignore[attr-defined]


def _sdk_error(cls: type[Exception], status: int | None = None) -> Exception:
    exc = cls.__new__(cls)
    exc.message = f"{cls.__name__} message"  # type: ignore[attr-defined]
    if status is not None:
        exc.status_code = status  # type: ignore[attr-defined]
    return exc


@pytest.mark.parametrize(
    ("cls", "status", "expected"),
    [
        (anthropic.AuthenticationError, 401, ("auth", False, "none")),
        (anthropic.BadRequestError, 400, ("bad_request", False, "none")),
        (anthropic.RateLimitError, 429, ("rate_limit", True, "none")),
        (anthropic.InternalServerError, 500, ("server", True, "unknown")),
        (anthropic.APITimeoutError, None, ("timeout", True, "unknown")),
        (anthropic.APIConnectionError, None, ("connection", True, "unknown")),
        (RuntimeError, None, ("other", False, "unknown")),
    ],
)
def test_both_adapters_classify_errors_identically(
    cls: type[Exception], status: int | None, expected: tuple[str, bool, str]
) -> None:
    exc = _sdk_error(cls, status) if cls is not RuntimeError else RuntimeError("boom")
    got = classify_anthropic_error(exc)
    assert (got.kind, got.retryable, got.billing) == expected
    seen = []
    for p, req in (
        (AnthropicProvider("m"), ModelRequest("s", "u", 10, "action")),
        (AnthropicNativeProvider("m"), request([{"role": "user", "content": "u"}])),
    ):
        p._client = _FakeClient(exc)
        with pytest.raises(ProviderError) as ei:
            p.complete(req)  # type: ignore[arg-type]
        seen.append((ei.value.kind, ei.value.retryable, ei.value.billing))
    assert seen == [expected, expected]


def test_scripted_native_provider_records_requests_and_errors() -> None:
    err = ProviderError("server", "500", retryable=True, billing="unknown")
    p = ScriptedNativeProvider(
        [
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 7},
            }
        ],
        fail_with={0: err},
    )
    req = request([{"role": "user", "content": "task"}])
    with pytest.raises(ProviderError):
        p.complete(req)
    r = p.complete(req)
    assert r.usage.input_tokens == 7 and r.usage_source == "scripted"
    assert len(p.requests) == 2 and p.payloads[1]["messages"] == [
        {"role": "user", "content": "task"}
    ]
    with pytest.raises(ProviderError, match="exhausted"):
        p.complete(req)


# ------------------------------------------------------------- historical path
def test_historical_runtime_does_not_load_the_native_transport() -> None:
    code = (
        "import sys, clm_lib.cli, clm_lib.runner, clm_lib.export; "
        "new = ['clm_lib.transport', 'clm_lib.tooling', 'clm_lib.counters', "
        "'clm_lib.output_cap', 'clm_lib.native_call']; "
        "print([m for m in new if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_single_action_payload_is_unchanged() -> None:
    req = ModelRequest("SYS", "USER", 2048, "action", json_schema={"type": "object"})
    assert AnthropicProvider("claude-opus-5-5").payload(req) == {
        "model": "claude-opus-5-5",
        "max_tokens": 2048,
        "system": "SYS",
        "messages": [{"role": "user", "content": "USER"}],
        "output_config": {
            "effort": "low",
            "format": {"type": "json_schema", "schema": {"type": "object"}},
        },
    }
