"""Stateless model providers: one real adapter (Anthropic) and a scripted double.

Every call is a fresh request built by the runtime. Adapters never keep or
replay conversation history. The exact payload sent is available through
``payload()`` so traces record what the model saw (headers are never recorded).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["execute", "final"]},
        "thought": {"type": "string"},
        "code": {"type": "string"},
        "answer": {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string"},
                "required_value": {"type": "string"},
                "remedy": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["root_cause", "required_value", "remedy", "evidence_refs"],
            "additionalProperties": False,
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


# Staged tasks add an "advance" action (release the next evidence stage); other tasks keep
# ACTION_SCHEMA unchanged so earlier experiments' requests stay reproducible.
STAGED_ACTION_SCHEMA: dict[str, Any] = {
    **ACTION_SCHEMA,
    "properties": {
        **ACTION_SCHEMA["properties"],
        "action": {"type": "string", "enum": ["execute", "final", "advance"]},
    },
}


# Coding tasks: staged actions with a final answer that only summarises the submitted work.
CODING_ACTION_SCHEMA: dict[str, Any] = {
    **STAGED_ACTION_SCHEMA,
    "properties": {
        **STAGED_ACTION_SCHEMA["properties"],
        "answer": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class ModelRequest:
    system: str
    user: str
    max_tokens: int
    purpose: str  # "action" | "repair" | "summary" | "smoke"
    json_schema: dict[str, Any] | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    thinking_tokens: int | None = None  # included in output_tokens when reported

    def to_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "thinking_tokens": self.thinking_tokens,
        }


@dataclass
class ModelResponse:
    text: str
    usage: Usage
    stop_reason: str | None
    model: str
    request_id: str | None = None
    usage_source: str = "provider"  # "provider" | "scripted-estimate"


class ProviderError(Exception):
    """A provider call failed.

    ``kind`` is one of: auth, permission, bad_request, not_found, rate_limit,
    server, timeout, connection, refusal, other. ``billing`` says whether tokens
    may have been billed: "none" (rejected before generation: 4xx, 429, credential
    failure), "unknown" (5xx, timeout, connection loss, unexpected errors; charged at
    the full reservation), or "billed" (usage returned, e.g. refusal).
    """

    def __init__(self, kind: str, message: str, *, retryable: bool, billing: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.retryable = retryable
        self.billing = billing


class Provider(Protocol):
    name: str
    model: str
    is_scripted: bool

    def payload(self, req: ModelRequest) -> dict[str, Any]: ...

    def complete(self, req: ModelRequest) -> ModelResponse: ...

    def describe(self) -> dict[str, Any]: ...


@dataclass
class AnthropicProvider:
    """Anthropic Messages API adapter.

    Credentials are resolved by the SDK's own chain (``ANTHROPIC_API_KEY``,
    ``ANTHROPIC_AUTH_TOKEN``, or an ``ant auth login`` profile). This class never
    reads or stores credential values. SDK-internal retries are disabled so every
    attempt is visible to the runtime's accounting.
    """

    model: str
    base_url: str | None = None
    timeout_s: float = 120.0
    effort: str | None = "low"
    structured_output: bool = True
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
            "model": self.model,
            "base_url": self.base_url or "(SDK default / ANTHROPIC_BASE_URL)",
            "effort": self.effort,
            "structured_output": self.structured_output,
            "timeout_s": self.timeout_s,
            "sdk_retries": 0,
            "sampling": "provider defaults (temperature/top_p not settable on this model family)",
        }

    def payload(self, req: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": req.max_tokens,
            "system": req.system,
            "messages": [{"role": "user", "content": req.user}],
        }
        output_config: dict[str, Any] = {}
        if self.effort:
            output_config["effort"] = self.effort
        if req.json_schema is not None and self.structured_output:
            output_config["format"] = {"type": "json_schema", "schema": req.json_schema}
        if output_config:
            body["output_config"] = output_config
        return body

    def complete(self, req: ModelRequest) -> ModelResponse:
        import anthropic

        client = self._get_client()
        try:
            msg = client.messages.create(**self.payload(req))
        except anthropic.AuthenticationError as exc:
            raise ProviderError("auth", _safe(exc), retryable=False, billing="none") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError("permission", _safe(exc), retryable=False, billing="none") from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError("not_found", _safe(exc), retryable=False, billing="none") from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError("bad_request", _safe(exc), retryable=False, billing="none") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("rate_limit", _safe(exc), retryable=True, billing="none") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderError(
                "timeout", "request timed out", retryable=True, billing="unknown"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(
                "connection", "connection error", retryable=True, billing="unknown"
            ) from exc
        except anthropic.APIStatusError as exc:
            # A 5xx after dispatch may still have consumed tokens: treat as potentially billed.
            server = exc.status_code >= 500
            raise ProviderError(
                "server" if server else "other",
                _safe(exc),
                retryable=server,
                billing="unknown" if server else "none",
            ) from exc
        except Exception as exc:  # credential-chain failures (e.g. expired profile) and similar
            text = f"{type(exc).__name__}: {exc}"
            auth = any(
                k in text.lower() for k in ("credential", "oauth", "token", "identity", "auth")
            )
            # Credential-chain failures happen before any request is sent; anything else
            # unexpected might have happened after dispatch, so its cost is unknown.
            raise ProviderError(
                "auth" if auth else "other",
                text[:300],
                retryable=False,
                billing="none" if auth else "unknown",
            ) from exc
        u = msg.usage
        details = getattr(u, "output_tokens_details", None)
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
            thinking_tokens=getattr(details, "thinking_tokens", None) if details else None,
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        if msg.stop_reason == "refusal":
            err = ProviderError(
                "refusal", "model declined the request", retryable=False, billing="billed"
            )
            err.response = ModelResponse(text, usage, msg.stop_reason, msg.model, msg._request_id)  # type: ignore[attr-defined]
            raise err
        return ModelResponse(
            text, usage, msg.stop_reason, msg.model, getattr(msg, "_request_id", None)
        )


def _safe(exc: Exception) -> str:
    msg = getattr(exc, "message", None) or str(exc)
    return str(msg)[:300]


ScriptStep = str | dict[str, Any] | Callable[[ModelRequest], "str | dict[str, Any]"]


@dataclass
class ScriptedProvider:
    """Deterministic test double. Refused by live entry points (``is_scripted``)."""

    script: list[ScriptStep]
    model: str = "scripted"
    name: str = "scripted"
    is_scripted: bool = True
    requests: list[ModelRequest] = field(default_factory=list)
    fail_with: dict[int, ProviderError] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "note": "scripted test double"}

    def payload(self, req: ModelRequest) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": req.max_tokens,
            "system": req.system,
            "messages": [{"role": "user", "content": req.user}],
        }

    def complete(self, req: ModelRequest) -> ModelResponse:
        index = len(self.requests)
        self.requests.append(req)
        if index in self.fail_with:
            raise self.fail_with[index]
        if not self.script:
            raise ProviderError("other", "script exhausted", retryable=False, billing="none")
        step = self.script.pop(0)
        out = step(req) if callable(step) else step
        text = out if isinstance(out, str) else json.dumps(out)
        usage = Usage(
            input_tokens=(len(req.system) + len(req.user)) // 3,
            output_tokens=max(1, len(text) // 3),
        )
        return ModelResponse(text, usage, "end_turn", self.model, usage_source="scripted-estimate")
