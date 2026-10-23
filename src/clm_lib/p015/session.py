"""`GuardedSession`: the opt-in programme 015 runner path (decision G1).

One Pydantic AI `Agent` per session, with guarded-clm's `clm_context()` exactly as the library builds it
(`ClmStepLimit`, then `ClmContext`), kept for every turn. Each turn calls `agent.run` with the same
`conversation_id`, the previous turn's `result.all_messages()` as `message_history` (the library's documented
requirement), and `UsageLimits(request_limit=call_cap)`. The capability keeps the conversation's working context and
estimator across turns; its step and call counts are per run. Nothing is reset between turns, and nothing is added to
the prompt, instructions or output type for scoring.

Endings are classified so that a financial refusal is never mistaken for a context-management result (G7):
`completed`, `spend_refused`, `budget_exceeded`, `context_limit`, `step_limit`, `call_cap`, `output_truncated` (a
response stopped at `max_tokens`, `p015-truncation/1`), `provider_failure`,
`tool_failure` (a bridged task tool raised), `model_behaviour` (Pydantic AI's `UnexpectedModelBehavior`, e.g. a tool's
retry limit), `harness_defect` (an accounting-bound violation) and `unclassified_error` (any other exception, for
example one raised by a framework tool that is not bridged, so the harness cannot attribute it).

Request layout (`p015-request-layout/1`, 2026-10-23): `request_layout` and `cache_point` are passed to `clm_context()`
unchanged on every turn. Their defaults are the library's (`"single"`, no cache point), so a configuration that
doesn't state them behaves as before. `guarded_option_problems` refuses an invalid combination at planning and again
before dispatch: an unknown layout or TTL, a cache point without a segmented layout or with caching off, or a cache
point whose TTL breaks the provider's order (a longer TTL may not follow a shorter one). The library tells its rebuilt
requests and runtime notes apart by `ModelRequest.metadata`; the runner passes `result.all_messages()` (or a limited
run's captured messages) on unchanged, so that metadata survives every turn.

Continuation (`p015-turn-endings/2`, 2026-10-19): after a turn that ended at the main model's step or call limit,
`continue_after_limit()` lets the next turn start from that run's own messages (its last processed request, then the
last step's response and tool results). guarded-clm folds that step into the working context at the next request,
as it does every step, and its new-turn rule protects the unanswered question. Nothing is reset or synthesised.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from guarded_clm import (
    REQUEST_LAYOUTS,
    BudgetExceededError,
    ContextLimitError,
    ContextLimits,
    StepLimitError,
    clm_context,
)
from guarded_clm.steps import lm_call_cap
from pydantic_ai import Agent, UsageLimits, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_core import to_jsonable_python

from ..budget import Ledger, ModelPrice
from ..tooling import Tool as TaskTool
from . import EVIDENCE_LABEL
from .accounting import (
    AccountedModel,
    AccountingBoundViolated,
    LaunchStopped,
    Mode,
    OutputTruncated,
    RetryPolicy,
    SpendGate,
    SpendRefused,
)
from .loop import SessionLoop
from .provenance import harness_provenance, library_provenance
from .records import SessionRecorder
from .tools import bridge

OBSOLETE_GUARDED_SETTINGS = {
    "strict_ratio_learning": (
        "strict_ratio_learning was removed in guarded-clm 0.2.0.dev1 (revision 3f18473), whose implausible-ratio "
        "backstop is always on. Remove the field (whatever its value) and use a configuration made for that "
        "revision, such as configs/p015/proposed-comparison-0.2.0.dev1.json"
    ),
}


def obsolete_setting_problems(guarded: dict[str, Any] | None) -> list[str]:
    """Settings for the guarded arm that no longer exist. They are refused, never ignored or translated."""
    return [
        f"arm_settings.guarded_clm.{k}: {why}"
        for k, why in OBSOLETE_GUARDED_SETTINGS.items()
        if k in (guarded or {})
    ]


REQUEST_LAYOUT_VERSION = "p015-request-layout/1"
CACHE_TTLS = ("5m", "1h")
_TTL_ORDER = {"5m": 0, "1h": 1}


def guarded_option_problems(
    guarded: dict[str, Any] | None, model: dict[str, Any] | None
) -> list[str]:
    """An invalid `request_layout`/`cache_point` combination, given the model's cache settings (`p015-model/1`).

    The provider caches tools, then the system prompt, then messages, and a breakpoint with a longer TTL may not follow
    one with a shorter TTL. The tool and instruction breakpoints come before the context's cache point, the automatic
    one (on the last block) after it."""
    g, m = guarded or {}, model or {}
    layout, point = g.get("request_layout", "single"), g.get("cache_point")
    out = []
    if layout not in REQUEST_LAYOUTS:
        out.append(
            f"arm_settings.guarded_clm.request_layout {layout!r} is not one of {', '.join(REQUEST_LAYOUTS)}"
        )
    if point is None:
        return out
    if point not in CACHE_TTLS:
        return [
            *out,
            f"arm_settings.guarded_clm.cache_point {point!r} is not one of 5m, 1h or null",
        ]
    if layout == "single":
        out.append("arm_settings.guarded_clm.cache_point needs a segmented request_layout")
    ttl = m.get("cache_ttl", "5m")
    if ttl is None:
        out.append(
            "arm_settings.guarded_clm.cache_point is set but model.cache_ttl is null (caching off)"
        )
        return out
    breakpoints = set(m.get("cache_breakpoints", ("automatic", "tool_definitions", "instructions")))
    if breakpoints & {"tool_definitions", "instructions"} and _TTL_ORDER[ttl] < _TTL_ORDER[point]:
        out.append(f"arm_settings.guarded_clm.cache_point {point} follows {ttl} tool or instruction breakpoints; "
                   "a longer TTL may not follow a shorter one")  # fmt: skip
    if "automatic" in breakpoints and _TTL_ORDER[ttl] > _TTL_ORDER[point]:
        out.append(f"the automatic {ttl} breakpoint follows arm_settings.guarded_clm.cache_point {point}; "
                   "a longer TTL may not follow a shorter one")  # fmt: skip
    return out


@dataclasses.dataclass(frozen=True)
class GuardedSettings:
    """`clm_context()`'s arguments. The defaults are the library's. `strict_ratio_learning` was removed with
    guarded-clm 0.2.0.dev1 (its implausible-ratio backstop is always on), so it is not a setting here; a
    configuration that still names it is refused with a migration error (`obsolete_setting_problems`)."""

    budget_tokens: int = 32_000
    max_steps: int = 60
    call_cap: int | None = None
    end_over_budget: bool = False
    max_output_chars: int = 8_000
    max_file_bytes: int = 256_000
    max_entries: int = 200
    max_body_chars: int = 24_000
    max_total_chars: int = 120_000
    request_layout: Literal["single", "segmented", "segmented_task_last"] = (
        "single"  # the library's default
    )
    cache_point: Literal["5m", "1h"] | None = None  # see guarded_option_problems

    @property
    def cap(self) -> int:
        return self.call_cap if self.call_cap is not None else lm_call_cap(self.max_steps)

    def capability(self, on_record: Any) -> Any:
        return clm_context(
            budget_tokens=self.budget_tokens,
            max_steps=self.max_steps,
            call_cap=self.call_cap,
            end_over_budget=self.end_over_budget,
            max_output_chars=self.max_output_chars,
            limits=ContextLimits(
                max_file_bytes=self.max_file_bytes,
                max_entries=self.max_entries,
                max_body_chars=self.max_body_chars,
                max_total_chars=self.max_total_chars,
            ),
            on_record=on_record,
            request_layout=self.request_layout,
            cache_point=self.cache_point,
        )


@dataclasses.dataclass
class TurnOutcome:
    turn: int
    ending: str
    output: Any = None
    error: BaseException | None = None
    messages: list[ModelMessage] | None = None

    @property
    def error_text(self) -> str | None:
        return None if self.error is None else f"{type(self.error).__name__}: {self.error}"


# p015-turn-endings/2: the endings after which the next scheduled turn may run (main-model step or call exhaustion).
CONTINUABLE_ENDINGS = ("step_limit", "call_cap")


class ContinuationUnsupported(RuntimeError):
    """The next turn cannot safely follow a limit-ended turn through the runner's existing interface."""


def unanswered_calls(messages: Sequence[ModelMessage]) -> list[str]:
    """Tool calls with no result in the request that follows them: such a history is not provider-valid."""
    out = []
    for i, m in enumerate(messages):
        if not isinstance(m, ModelResponse):
            continue
        ids = {p.tool_call_id for p in m.parts if isinstance(p, ToolCallPart)}
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        answered = (
            {p.tool_call_id for p in nxt.parts if isinstance(p, ToolReturnPart)}
            if isinstance(nxt, ModelRequest)
            else set()
        )
        out += sorted(ids - answered)
    return out


class GuardedSession:
    def __init__(
        self,
        model: Model,
        *,
        run_dir: Path,
        run_id: str,
        ledger: Ledger,
        price: ModelPrice | None,
        settings: GuardedSettings | None = None,
        system_prompt: str | Sequence[str] = (),
        instructions: str | None = None,
        task_tools: Sequence[TaskTool] = (),
        framework_tools: Sequence[Any] = (),
        output_type: Any = str,
        model_settings: ModelSettings | None = None,
        conversation_id: str = "session",
        mode: Mode = "scripted",
        live_authorised: bool = False,
        evidence: str = EVIDENCE_LABEL,
        gate: SpendGate | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.settings = settings or GuardedSettings()
        self.recorder = SessionRecorder(run_dir, run_id=run_id, evidence=evidence)
        self.model = AccountedModel(
            model,
            ledger=ledger,
            price=price,
            run_id=run_id,
            sink=self.recorder,
            mode=mode,
            live_authorised=live_authorised,
            gate=gate,
            retry=retry,
        )
        tools = [*(bridge(t, sink=self.recorder) for t in task_tools), *framework_tools]
        self.agent: Agent[None, Any] = Agent(
            self.model,
            system_prompt=system_prompt,
            instructions=instructions,
            tools=tools,
            capabilities=[self.settings.capability(self.recorder.trace)],
            output_type=output_type,
            model_settings=model_settings,
        )
        self.conversation_id = conversation_id
        self.history: list[ModelMessage] | None = None
        self.turns = 0
        self.limited_messages: list[ModelMessage] | None = (
            None  # the last run's messages, if it hit a limit
        )
        self.loop = SessionLoop()  # one event loop for all turns (p015-session-loop/1)
        self.recorder.write_meta(
            settings=dataclasses.asdict(self.settings),
            call_cap=self.settings.cap,
            model_settings=dict(model_settings or {}),
            mode=mode,
            conversation_id=conversation_id,
            model={"system": model.system, "model_name": model.model_name},
            price=dataclasses.asdict(price) if price is not None else None,
            task_tools=[t.spec.to_api() for t in task_tools],
            framework_tools=[
                getattr(t, "name", getattr(t, "__name__", repr(t))) for t in framework_tools
            ],
            provenance={"guarded_clm": library_provenance(), "harness": harness_provenance()},
        )

    def classify(self, exc: BaseException) -> str:
        if isinstance(exc, LaunchStopped):
            return "stopped"
        if isinstance(exc, SpendRefused):
            return "spend_refused"
        if isinstance(exc, AccountingBoundViolated):
            return "harness_defect"
        if isinstance(exc, OutputTruncated):
            return "output_truncated"
        if isinstance(exc, BudgetExceededError):
            return "budget_exceeded"
        if isinstance(exc, ContextLimitError):
            return "context_limit"
        if isinstance(exc, StepLimitError):
            return "step_limit"
        if isinstance(exc, UsageLimitExceeded):
            return "call_cap"
        if isinstance(exc, ModelAPIError):
            return "provider_failure"
        if self.recorder.raised_by_tool(exc):
            return "tool_failure"
        if isinstance(exc, UnexpectedModelBehavior):
            return "model_behaviour"
        return "unclassified_error"

    def close(self) -> None:
        """Close the session's model clients on its event loop, then the loop. Turns are refused afterwards."""
        self.loop.close(self.model)

    def continue_after_limit(self) -> int:
        """Let the next turn follow a turn that ended at the main model's step or call limit (p015-turn-endings/2).

        The limit is raised before guarded-clm's own request hook, so the limited run's messages end with the last
        step's response and its tool results, not yet folded into the working context. Passing them as the next
        turn's history lets the library fold that step in as usual, and its new-turn rule then records the
        unanswered question as a protected entry. Passing the earlier completed turn's history instead would make
        the library record that turn's answer a second time, under the limited turn's number. Returns how many
        messages are kept."""
        if self.limited_messages is None:
            raise ContinuationUnsupported("the last turn did not end at a step or call limit")
        dangling = unanswered_calls(self.limited_messages)
        if dangling:
            raise ContinuationUnsupported(
                f"the limited run left tool calls without results: {dangling}"
            )
        self.history, self.limited_messages = self.limited_messages, None
        self.recorder.trace(
            {"type": "turn_continued", "after_turn": self.turns, "kept_messages": len(self.history)}
        )
        return len(self.history)

    def __enter__(self) -> GuardedSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def run_turn(self, prompt: Any) -> TurnOutcome:
        """One user turn. A turn that ends early does not end the session object; the caller decides."""
        if self.loop.closed:
            raise RuntimeError(
                "the session is closed; start a new session (with a new model) instead"
            )
        self.turns += 1
        captured: list[list[ModelMessage]] = []

        async def run() -> Any:
            with capture_run_messages() as messages:
                captured.append(messages)
                return await self.agent.run(
                    prompt,
                    message_history=self.history,
                    conversation_id=self.conversation_id,
                    usage_limits=UsageLimits(request_limit=self.settings.cap),
                )

        self.limited_messages = None
        try:
            result = self.loop.run(run())
        except Exception as exc:
            outcome = TurnOutcome(self.turns, self.classify(exc), error=exc)
            if outcome.ending in CONTINUABLE_ENDINGS and captured:
                self.limited_messages = list(captured[0])
            self.recorder.turn(
                {
                    "turn": self.turns,
                    "prompt": to_jsonable_python(prompt),
                    "ending": outcome.ending,
                    "error": outcome.error_text,
                }
            )
            return outcome
        self.history = result.all_messages()
        outcome = TurnOutcome(self.turns, "completed", output=result.output, messages=self.history)
        self.recorder.turn(
            {
                "turn": self.turns,
                "prompt": to_jsonable_python(prompt),
                "ending": "completed",
                "error": None,
                "output": to_jsonable_python(result.output),
                "all_messages": to_jsonable_python(self.history),
            }
        )
        return outcome
