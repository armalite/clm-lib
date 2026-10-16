"""The shared model configuration (`p015-model/1`) on the wire, the bounded retry policy (`p015-retry/1`), and the
SDK's own retries being off. Offline: an in-memory HTTP transport, network blocked; the key is a placeholder."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from clm_lib.budget import Ledger, ModelPrice
from clm_lib.p015.accounting import AccountedModel, RetryPolicy
from clm_lib.p015.live import ModelConfig, live_model, tagged_system_prompt

PRICE = ModelPrice("claude-sonnet-5", 2.0, 2.5, 4.0, 0.2, 10.0, "test", "test")
TAG = "0123456789abcdef0123456789abcdef"


def message(text: str) -> dict[str, Any]:
    return {
        "id": "msg_offline", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 5, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
    }  # fmt: skip


def run_live(
    tmp_path: Path, config: ModelConfig, replies: list[tuple[int, dict[str, Any]]]
) -> tuple[Any, list[Any]]:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        status, body = replies[min(len(sent), len(replies)) - 1]
        return httpx.Response(status, json=body)

    model = live_model(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                       api_key="offline-placeholder")  # fmt: skip
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    accounted = AccountedModel(model, ledger=Ledger.open(tmp_path / "l.json", 10.0), price=PRICE, run_id="t",
                               mode="live", live_authorised=True, retry=config.retry_policy(), sleep=sleep)  # fmt: skip

    def lookup(component: str) -> str:
        """Look something up.

        Args:
            component: The component.
        """
        return component

    agent: Agent[None, str] = Agent(
        accounted,
        system_prompt=tagged_system_prompt("The task.", TAG),
        tools=[lookup],
        model_settings=config.model_settings(),
    )  # type: ignore[arg-type]
    try:
        result: Any = asyncio.run(agent.run("question"))
    except Exception as exc:
        result = exc
    return (result, slept), [json.loads(r.content) for r in sent]


def test_the_wire_request_carries_the_shared_configuration(tmp_path: Path) -> None:
    (result, _), bodies = run_live(tmp_path, ModelConfig(), [(200, message("answer"))])
    assert result.output == "answer" and len(bodies) == 1
    body = bodies[0]
    assert body["model"] == "claude-sonnet-5" and body["max_tokens"] == 8_192
    assert not {"temperature", "top_p", "top_k"} & set(body)  # sampling is never sent
    assert "thinking" not in body  # adaptive thinking is the model's default
    assert body["output_config"]["effort"] == "high"
    assert body["cache_control"]["type"] == "ephemeral"  # automatic caching
    assert body["tools"][-1]["cache_control"]["type"] == "ephemeral"
    system = body["system"]
    assert isinstance(system, list) and system[-1]["cache_control"]["type"] == "ephemeral"
    assert system[0]["text"].startswith(f"run-ref: {TAG}\n\nThe task.")
    disabled = ModelConfig(thinking="disabled", cache_ttl=None)
    (_, _), bodies = run_live(tmp_path / "d", disabled, [(200, message("answer"))])
    assert bodies[0]["thinking"] == {"type": "disabled"} and "cache_control" not in bodies[0]


def test_sdk_retries_are_off_and_the_shared_policy_retries_with_records(tmp_path: Path) -> None:
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    # Without the shared policy, a 529 is one HTTP request and ends the call: the SDK does not retry by itself.
    (result, _), bodies = run_live(tmp_path / "a", ModelConfig(max_retries=0), [(529, overloaded)])
    assert isinstance(result, ModelHTTPError) and result.status_code == 529 and len(bodies) == 1
    # With p015-retry/1 (2 retries): two 529s, then success - three recorded attempts, with the planned delays.
    (result, slept), bodies = run_live(tmp_path / "b", ModelConfig(), [(529, overloaded), (529, overloaded),
                                                                        (200, message("ok"))])  # fmt: skip
    assert result.output == "ok" and len(bodies) == 3 and slept == [2.0, 8.0]
    entries = json.loads((tmp_path / "b" / "l.json").read_text())["entries"]
    assert [e["status"] for e in entries] == ["error:http_529", "error:http_529", "ok"]
    # A non-retryable error is not retried.
    bad = {"type": "error", "error": {"type": "invalid_request_error", "message": "no"}}
    (result, _), bodies = run_live(tmp_path / "c", ModelConfig(), [(400, bad)])
    assert isinstance(result, ModelHTTPError) and len(bodies) == 1


def test_retry_policy_bounds() -> None:
    policy = RetryPolicy(max_retries=2)
    err = ModelHTTPError(429, "m", {}, headers={"retry-after": "30"})  # type: ignore[call-arg]
    assert policy.retryable(err) and policy.delay(err, 0) == 30.0
    assert (
        policy.delay(ModelHTTPError(503, "m", {}), 2) == 32.0
        and policy.delay(ModelHTTPError(503, "m", {}), 5) == 60
    )
    assert not policy.retryable(ModelHTTPError(400, "m", {})) and not policy.retryable(
        ValueError("x")
    )
    with pytest.raises(ValueError):
        tagged_system_prompt("x", "short")


def test_scripted_models_still_refuse_live_mode_without_authorisation(tmp_path: Path) -> None:
    from clm_lib.p015.accounting import SpendRefused

    model = AccountedModel(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("x")])),
                           ledger=Ledger.open(tmp_path / "l.json", 1.0), price=PRICE, run_id="t", mode="live")  # fmt: skip
    agent: Agent[None, str] = Agent(model)
    with pytest.raises(SpendRefused):
        asyncio.run(agent.run("q"))
