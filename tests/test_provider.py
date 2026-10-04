"""Offline checks of the Anthropic adapter's payload and response parsing (fake client)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clm_lib.provider import ACTION_SCHEMA, AnthropicProvider, ModelRequest, ProviderError


class FakeMessages:
    def __init__(self, msg: Any) -> None:
        self.msg = msg
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.msg


def fake_message(stop: str = "end_turn") -> Any:
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=40,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=None,
        output_tokens_details=SimpleNamespace(thinking_tokens=25),
    )
    content = [
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text='{"action": "final"}'),
    ]
    return SimpleNamespace(
        usage=usage, content=content, stop_reason=stop, model="claude-opus-5-5", _request_id="req_1"
    )


def provider_with(msg: Any) -> tuple[AnthropicProvider, FakeMessages]:
    msgs = FakeMessages(msg)
    prov = AnthropicProvider(model="claude-opus-5-5", effort="low")
    prov._client = SimpleNamespace(messages=msgs)
    return prov, msgs


def test_payload_is_stateless_and_uses_structured_output() -> None:
    prov, msgs = provider_with(fake_message())
    req = ModelRequest("SYS", "USER", 2048, "action", ACTION_SCHEMA)
    resp = prov.complete(req)
    sent = msgs.kwargs
    assert sent == prov.payload(req)
    assert sent["messages"] == [{"role": "user", "content": "USER"}]  # no replayed history
    assert sent["system"] == "SYS" and sent["max_tokens"] == 2048
    assert sent["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": ACTION_SCHEMA},
    }
    assert "temperature" not in sent and "thinking" not in sent
    assert resp.text == '{"action": "final"}'
    assert resp.usage.thinking_tokens == 25 and resp.usage.cache_creation_input_tokens == 0
    assert resp.request_id == "req_1"


def test_refusal_is_an_error_with_billed_usage() -> None:
    prov, _ = provider_with(fake_message(stop="refusal"))
    with pytest.raises(ProviderError) as info:
        prov.complete(ModelRequest("S", "U", 10, "action"))
    assert info.value.kind == "refusal"
    assert info.value.response.usage.output_tokens == 40  # type: ignore[attr-defined]


def test_summary_requests_have_no_schema() -> None:
    prov = AnthropicProvider(model="m", effort=None)
    body = prov.payload(ModelRequest("S", "U", 10, "summary"))
    assert "output_config" not in body
