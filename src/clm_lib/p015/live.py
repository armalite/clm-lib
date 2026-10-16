"""The shared model configuration for the 015 comparison (`p015-model/1`) and the live model factory.

**Nothing here is called live in this repository's tests.** The live factory is exercised only over an in-memory
HTTP transport. A live request still needs the launcher's authorisation, a dated price and `mode="live"`
(`accounting.py`), and the separately authorised development smoke has not been run.

One configuration for all three arms, from official provider documentation (sources and dates in
clm-lib-test-results, experiment 015, `arm-settings.md`):

- **Model:** `claude-sonnet-5`, the exact ID.
- **Sampling:** not sent. Claude Sonnet 5 rejects non-default `temperature`, `top_p` and `top_k`, so the paper's
  0.7 / 0.95 cannot be applied (a recorded adaptation). Pydantic AI omits sampling unless it is set.
- **Thinking:** `adaptive` (the model's default; `thinking` is not sent) or `disabled`. Effort: explicit.
- **Output:** `max_tokens`, which bounds thinking and response together.
- **Caching:** ordinary automatic prompt caching (`anthropic_cache`) plus explicit breakpoints after the tool
  definitions and the instructions, with the same TTL in every arm. Each session's system prompt starts with a
  fixed-length `run-ref` line, so independent sessions never share cached prefixes, while a session reuses its own.
  This is not Suffix Cache Reuse.
- **Retries:** the SDK's are off (`max_retries=0`); the bounded `p015-retry/1` policy runs in the accounting wrapper,
  with every attempt reserved and recorded.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
from typing import Any, Literal

from .accounting import RETRY_VERSION, RetryPolicy

MODEL_CONFIG_VERSION = "p015-model/1"
RUN_REF = "run-ref: {tag}\n\n"  # 32 hexadecimal characters, so the line's length is the same for every session
CREDENTIAL_SOURCES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")


@dataclasses.dataclass(frozen=True)
class ModelConfig:
    model: str = "claude-sonnet-5"
    max_tokens: int = 8_192
    thinking: Literal["adaptive", "disabled"] = "adaptive"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    cache_ttl: Literal["5m", "1h"] | None = "5m"  # None: no caching
    timeout_s: float = 600.0
    max_retries: int = 2  # p015-retry/1 retries of one logical call (in the accounting wrapper)
    session_tag: bool = True

    def model_settings(self) -> dict[str, Any]:
        """Pydantic AI model settings, identical for every arm and for the summariser."""
        out: dict[str, Any] = {"max_tokens": self.max_tokens, "anthropic_effort": self.effort}
        if self.thinking == "disabled":
            out["anthropic_thinking"] = {"type": "disabled"}
        if self.cache_ttl is not None:
            out |= {
                "anthropic_cache": self.cache_ttl,
                "anthropic_cache_tool_definitions": self.cache_ttl,
                "anthropic_cache_instructions": self.cache_ttl,
            }
        return out

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(max_retries=self.max_retries)

    def describe(self) -> dict[str, Any]:
        return {
            "version": MODEL_CONFIG_VERSION,
            **dataclasses.asdict(self),
            "model_settings": self.model_settings(),
            "retry": {"version": RETRY_VERSION, **dataclasses.asdict(self.retry_policy())},
            "sdk_max_retries": 0,
            "sampling": "not sent",
        }


def new_session_tag() -> str:
    return secrets.token_hex(16)


def tagged_system_prompt(system_prompt: str, tag: str | None) -> str:
    """The shared system prompt, led by the session's `run-ref` line when a tag is given."""
    if tag is None:
        return system_prompt
    if len(tag) != 32 or any(c not in "0123456789abcdef" for c in tag):
        raise ValueError("a session tag is 32 lowercase hexadecimal characters")
    return RUN_REF.format(tag=tag) + system_prompt


def credential_source_present() -> bool:
    """Whether a credential source exists (names only; no value is read beyond its presence)."""
    return any(os.environ.get(name) for name in CREDENTIAL_SOURCES)


def live_model(
    config: ModelConfig, *, http_client: Any | None = None, api_key: str | None = None
) -> Any:
    """A Pydantic AI `AnthropicModel` whose Anthropic client never retries by itself. `http_client` (an
    `httpx.AsyncClient`) and `api_key` exist for offline tests over an in-memory transport."""
    import anthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    kwargs: dict[str, Any] = {"max_retries": 0, "timeout": config.timeout_s}
    if http_client is not None:
        kwargs["http_client"] = http_client
    if api_key is not None:
        kwargs["api_key"] = api_key
    client = anthropic.AsyncAnthropic(**kwargs)
    return AnthropicModel(config.model, provider=AnthropicProvider(anthropic_client=client))
