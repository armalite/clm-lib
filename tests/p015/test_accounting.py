"""The accounting boundary: every provider attempt reserved and settled once, refusals before dispatch, failure
billing, and the usage split, including a real Pydantic AI AnthropicModel over an in-memory transport (no network:
see conftest). Ledgers here are temporary test ledgers; the active ledger is never opened."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.usage import RequestUsage

from clm_lib.budget import Ledger
from clm_lib.p015.accounting import AccountedModel, SpendRefused, split_usage
from clm_lib.p015.records import read_jsonl
from clm_lib.p015.session import GuardedSession, GuardedSettings

from . import harness as h


def attempts(run_dir: Path) -> list[dict[str, Any]]:
    return read_jsonl(run_dir / "attempts.jsonl")


def test_every_attempt_is_reserved_and_settled_once(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "ledger.json", 100.0)
    via_session, session = h.run_session(h.CASES["edits"], tmp_path / "run", ledger)
    rows = attempts(tmp_path / "run")
    started = [r for r in rows if r["event"] == "started"]
    finished = [r for r in rows if r["event"] == "finished"]
    assert len(started) == len(finished) == len(via_session["calls"]) == len(ledger.entries) == 10
    assert [r["attempt"] for r in finished] == list(range(10))
    assert all(r["outcome"] == "ok" and r["category"] == "main" for r in finished)
    assert all(r["reserved_usd"] >= r["charged_usd"] > 0 for r in finished)
    assert ledger.spent_usd == pytest.approx(sum(r["charged_usd"] for r in finished), abs=1e-6)
    assert not ledger._open
    # The exact request Pydantic AI built is what was recorded and what the model received.
    assert [h.dump(r["messages"]) for r in started] == [c["messages"] for c in via_session["calls"]]
    # The financial attempt count is the harness's own; the library's step and call counts stay in its status.
    assert session.model.attempts == 10


def test_provider_failure_is_settled_at_the_reservation(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "ledger.json", 100.0)
    via_session, _ = h.run_session(h.CASES["provider_failure"], tmp_path / "run", ledger)
    assert via_session["runs"][-1]["ending"] == "provider_failure"
    finished = [r for r in attempts(tmp_path / "run") if r["event"] == "finished"]
    failed = finished[-1]
    assert (failed["outcome"], failed["error"], failed["billing"]) == (
        "error",
        "http_503",
        "unknown",
    )
    assert (
        failed["charged_usd"] == failed["reserved_usd"]
        and failed["cost_basis"] == "reservation_assumed"
    )


def test_unbilled_http_errors_are_settled_at_zero(tmp_path: Path) -> None:
    def respond(messages: Any, info: Any) -> ModelResponse:
        raise ModelHTTPError(429, "scripted", {"error": "rate limited"})

    ledger = Ledger.open(tmp_path / "ledger.json", 100.0)
    session = GuardedSession(
        FunctionModel(respond), run_dir=tmp_path / "run", run_id="r", ledger=ledger, price=h.PRICE
    )
    assert session.run_turn("go").ending == "provider_failure"
    (failed,) = [r for r in attempts(tmp_path / "run") if r["event"] == "finished"]
    assert (failed["billing"], failed["charged_usd"]) == ("none", 0.0)


def test_a_spend_refusal_is_its_own_ending_and_nothing_is_dispatched(tmp_path: Path) -> None:
    """After turn 1, the ledger allows no further spend: turn 2's first attempt is refused before dispatch."""
    ledger = Ledger.open(tmp_path / "ledger.json", 100.0)

    def stop_spending(turn: int) -> None:
        ledger.limit_usd = ledger.spent_usd - ledger.start_spent_usd

    via_session, _ = h.run_session(
        h.CASES["answers_retained"], tmp_path / "run", ledger, after_turn=stop_spending
    )
    direct = h.run_direct(h.CASES["answers_retained"])
    assert [r["ending"] for r in via_session["runs"]] == ["completed", "spend_refused"]
    assert via_session["runs"][1]["calls"] == 0  # the model was never called in turn 2
    assert via_session["calls"] == direct["calls"][: len(via_session["calls"])]  # turn 1 identical
    refused = attempts(tmp_path / "run")[-1]
    assert (refused["outcome"], refused["refusal"]) == ("refused", "budget_exhausted")


@pytest.mark.parametrize(
    ("mode", "authorised", "price", "settings", "kind"),
    [
        ("scripted", False, True, None, "not_scripted"),  # a provider model in scripted mode
        ("live", False, True, {"max_tokens": 100}, "not_authorised"),
        ("live", True, False, {"max_tokens": 100}, "no_price"),
        ("live", True, True, None, "unbounded_output"),
    ],
)
def test_live_requests_are_refused_without_authorisation_price_or_bound(
    tmp_path: Path,
    mode: str,
    authorised: bool,
    price: bool,
    settings: dict[str, Any] | None,
    kind: str,
) -> None:
    calls: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("must not be dispatched")

    model = mocked_anthropic(handler)
    session = GuardedSession(
        model,
        run_dir=tmp_path / "run",
        run_id="r",
        ledger=Ledger.open(tmp_path / "l.json", 100.0),
        price=h.PRICE if price else None,
        mode=mode,  # type: ignore[arg-type]
        live_authorised=authorised,
        model_settings=settings,  # type: ignore[arg-type]
    )
    outcome = session.run_turn("go")
    assert outcome.ending == "spend_refused" and isinstance(outcome.error, SpendRefused)
    assert outcome.error.kind == kind and calls == []


def test_token_counting_and_streaming_are_refused(tmp_path: Path) -> None:
    model = AccountedModel(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("x")])),
        ledger=Ledger.open(tmp_path / "l.json", 1.0),
        price=None,
        run_id="r",
    )
    with pytest.raises(RuntimeError, match="token counting"):
        asyncio.run(model.count_tokens([], None, None))  # type: ignore[arg-type]


def test_split_usage_never_double_counts_cache_tokens() -> None:
    u = split_usage(
        RequestUsage(
            input_tokens=1_050, cache_read_tokens=900, cache_write_tokens=50, output_tokens=7
        )
    )
    assert (
        u.input_tokens,
        u.cache_read_input_tokens,
        u.cache_creation_input_tokens,
        u.output_tokens,
    ) == (100, 900, 50, 7)


# ---------------------------------------------------------------------- a real AnthropicModel, offline


def mocked_anthropic(handler: Any) -> AnthropicModel:
    """Pydantic AI's AnthropicModel with an Anthropic client whose HTTP transport is in memory (no network; SDK
    retries off). The key is a placeholder, not a credential."""
    client = anthropic.AsyncAnthropic(
        api_key="offline-placeholder",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return AnthropicModel("claude-sonnet-5", provider=AnthropicProvider(anthropic_client=client))


def anthropic_message(
    content: list[dict[str, Any]], stop: str, usage: dict[str, int]
) -> dict[str, Any]:
    return {
        "id": "msg_offline",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage,
    }


def test_real_provider_path_with_cache_usage_offline(tmp_path: Path) -> None:
    """The capability calibrates on provider usage through Pydantic AI's actual Anthropic usage mapping; the ledger
    prices uncached input, cache reads, cache writes and output separately, from the same figures."""
    sent: list[dict[str, Any]] = []
    responses = [
        anthropic_message(
            [{"type": "tool_use", "id": "toolu_1", "name": "alerts", "input": {}}],
            "tool_use",
            {
                "input_tokens": 300,
                "cache_read_input_tokens": 1_200,
                "cache_creation_input_tokens": 400,
                "output_tokens": 20,
            },
        ),
        anthropic_message(
            [{"type": "text", "text": "Three alerts are open."}],
            "end_turn",
            {
                "input_tokens": 120,
                "cache_read_input_tokens": 1_600,
                "cache_creation_input_tokens": 90,
                "output_tokens": 12,
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(sent) - 1])

    ledger = Ledger.open(tmp_path / "ledger.json", 100.0)
    session = GuardedSession(
        mocked_anthropic(handler),
        run_dir=tmp_path / "run",
        run_id="mock",
        ledger=ledger,
        price=h.PRICE,
        settings=GuardedSettings(),
        system_prompt=h.SYSTEM,
        framework_tools=[h.alerts],
        mode="live",
        live_authorised=True,
        model_settings={"max_tokens": 1_000},
    )
    outcome = session.run_turn("What is open?")
    assert outcome.ending == "completed" and outcome.output == "Three alerts are open."
    # The provider received one rebuilt user message per request, with the capability's sections.
    assert all(
        len(body["messages"]) == 1 and body["messages"][0]["role"] == "user" for body in sent
    )
    first_text = json.dumps(sent[1]["messages"][0]["content"])
    assert (
        "<working_context>" in first_text
        and "s1.obs" in first_text
        and "<runtime_status>" in first_text
    )
    assert {t["name"] for t in sent[0]["tools"]} == {"alerts", "edit_context"}
    finished = [r for r in attempts(tmp_path / "run") if r["event"] == "finished"]
    first = finished[0]["reported"]
    assert first == {
        "input_tokens_total": 1_900,
        "input_tokens_uncached": 300,
        "cache_read_tokens": 1_200,
        "cache_write_tokens": 400,
        "output_tokens": 20,
    }
    expected = (300 * 2.0 + 400 * 2.5 + 1_200 * 0.2 + 20 * 10.0) / 1e6
    assert finished[0]["charged_usd"] == pytest.approx(expected, abs=1e-6)
    assert finished[0]["usage_source"] == "provider-model"
    # The capability's second request is anchored on the first call's total reported input (1,900 tokens, cache
    # reads and writes included), not on its 300 uncached tokens.
    status = json.loads(json.dumps(sent[1]["messages"][0]["content"]))[-1]["text"].split(
        "<runtime_status>\n"
    )[1]
    size = int(status.splitlines()[1].split("about ")[1].split(" of")[0].replace(",", ""))
    assert size > 1_900
    trace = read_jsonl(tmp_path / "run" / "trace.jsonl")
    assert [r["type"] for r in trace] == ["lite_guard_check"]
