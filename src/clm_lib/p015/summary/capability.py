"""`TokenTailSummary` (`token-tail-p015/1`): clm-lib's `token-tail/1` summary strategy on the p015 path.

What is kept from `token-tail/1` (clm-lib `baseline.SummaryPolicy` and the historical runner, used unchanged where
they apply):

- **Trigger:** before a request, if its estimate reaches `pressure_ratio` (0.70) of the budget, summarise; if it is
  still over the hard limit (budget minus `recovery_reserve_tokens`, 1,000), summarise again (forced); if nothing is
  eligible, or the request is still over the hard limit, the turn ends (`ArmContextLimit`).
- **Tail selection:** `SummaryPolicy.split`, over the conversation's units, with the allowances as fractions of the
  budget (tail 0.15, newest 0.25), and an entry's tokens as its rendered length over the current ratio, plus one.
- **The summariser request:** the `token-tail/1` user layout (`<task>`, then `<transcript_to_summarise>` with one
  escaped JSON line per entry, then the length instruction); the length from `SummaryPolicy.char_limit` with the
  room left under the target (0.50 of the budget); acceptance by `SummaryPolicy.accept_tokens` (the first attempt
  must relieve pressure, the retry need only fit the hard limit); one retry.
- **Replacement:** the summary replaces the older section; it is not appended to it. An earlier summary is the
  first entry of the next summariser transcript, so its facts are carried forward.

What changed for p015 (hence the new version):

- The working context is the Pydantic AI conversation, not a list of entries. A unit is one reply and the request
  that follows it (its tool results, or the next question), so a tail never splits a call from its result.
- The summary is placed in the pinned first request (system prompt and first question), after the question, as
  `<summary_of_earlier_work>`. If the current turn's question was in the summarised section, it is restated after the
  summary until the turn ends.
- The summariser's system text is a docthread-neutral version of `SUMMARY_SYSTEM_TOKEN_TAIL` (no local files,
  `file:line` references or evidence stages), and it also asks for the questions and answers so far.
- The agent's instructions describe the automatic summaries without the workspace sentence (there is no workspace).
- The historical runner's spill of an oversized latest output to a file is not part of the strategy and is not
  adapted: shared task-tool outputs are capped at 8,000 code points for every arm.

Summariser calls go through their own `AccountedModel` (category `summary`), so their tokens, cache figures,
latency and cost are recorded apart from the agent's calls, and they obey the same authorisation and ledger rules.
They have their own per-turn cap (`summary_call_cap`) and do not count as task steps or towards the agent's call cap.
The summariser sees only what the agent could see: the first question and the conversation being replaced.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext

from ...baseline import TOKEN_TAIL, SummaryPolicy
from ...context import Entry
from ...prompts import render_entries
from ..arm_errors import ArmContextLimit, ArmStepLimit, SummaryCallCap
from ..estimate import ESTIMATE_VERSION, RatioEstimator, request_chars
from ..paper.mirror import body, role_of

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestContext

SUMMARY_VERSION = "token-tail-p015/1"
NOTE = "[context]"

SUMMARY_SYSTEM_P015 = """You compress an agent's working transcript. The agent is
answering a sequence of questions about a production incident with read-only tools, and will keep
working after your summary replaces the entries below. The transcript may include an earlier
summary: carry its facts forward. Preserve, exactly and verbatim where relevant:
- every concrete value, identifier, timestamp, count, setting name and quoted line found so far;
- which sources were read, and which were relevant or irrelevant, and why;
- the questions asked so far and the answers given;
- hypotheses confirmed, hypotheses ruled out, approaches that failed;
- unresolved questions and the agent's next planned steps.
Drop repetition and raw log dumps. Use compact bullet points. Plain text only. Stay within the
requested length; it is set so the agent has room to continue."""


@dataclasses.dataclass(frozen=True)
class SummarySettings:
    budget_tokens: int = 32_000
    pressure_ratio: float = 0.70
    recovery_reserve_tokens: int = 1_000
    tail_ratio: float = 0.15
    newest_ratio: float = 0.25
    target_ratio: float = 0.50
    max_chars: int = 3_000
    min_chars: int = 600
    retries: int = 1
    max_steps: int = 60
    call_cap: int | None = None  # 2 * max_steps + 24 by default (agent calls only)
    summary_call_cap: int = 24  # per turn; provisional (protocol draft, open item)
    finalize_steps: int = 3

    @property
    def hard(self) -> int:
        return self.budget_tokens - self.recovery_reserve_tokens

    @property
    def cap(self) -> int:
        return self.call_cap if self.call_cap is not None else 2 * self.max_steps + 24

    def policy(self) -> SummaryPolicy:
        b = self.budget_tokens
        return SummaryPolicy(
            max_chars=self.max_chars,
            retries=self.retries,
            policy=TOKEN_TAIL,
            tail_tokens=int(self.tail_ratio * b),
            newest_tokens=int(self.newest_ratio * b),
            target_tokens=int(self.target_ratio * b),
            min_chars=self.min_chars,
        )


def instructions(settings: SummarySettings) -> str:
    tail = int(settings.tail_ratio * settings.budget_tokens)
    return (
        "CONTEXT MANAGEMENT (automatic):\n"
        f"- When the request grows past {round(100 * settings.pressure_ratio)}% of the budget, the runtime asks a "
        "summariser to replace older parts of this conversation with a summary. The most recent messages are kept "
        f"verbatim up to about {tail} tokens (the newest message is kept even if larger, when it fits); older "
        "messages, and recent messages too large for that allowance, are summarised. Summaries are sized to leave "
        "room for further work."
    )


def summary_user(task: str, entries: list[Entry], max_chars: int) -> str:
    """The `token-tail/1` summariser user message (clm-lib `prompts.summary_user`), unchanged."""
    return (
        f"<task>\n{task.strip()}\n</task>\n\n"
        f"<transcript_to_summarise>\n{render_entries(entries)}\n</transcript_to_summarise>\n\n"
        f"Write the replacement summary now, at most {max_chars} characters."
    )


@dataclasses.dataclass
class SummaryState:
    pinned: ModelRequest | None = None
    messages: list[ModelMessage] = dataclasses.field(default_factory=list)
    sent_len: int = 0
    last_chars: int = 0
    params: ModelRequestParameters | None = None
    model: Model | None = None
    summary: str | None = None
    summary_index: int = 0
    restate: str | None = None  # the current turn's question, when it was summarised
    carry: list[Entry] = dataclasses.field(
        default_factory=list
    )  # restated questions of earlier turns
    run_messages: set[int] = dataclasses.field(default_factory=set)
    steps: int = 0
    calls: int = 0
    summary_calls: int = 0
    summaries_total: int = 0
    summary_calls_total: int = 0


class TokenTailSummary(AbstractCapability[Any]):
    def __init__(
        self,
        summary_model: Model,
        settings: SummarySettings | None = None,
        *,
        model_settings: ModelSettings | None = None,
        on_record: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.id = "token_tail_p015"
        self.description = "Automatic summaries of older conversation (token-tail-p015/1)."
        self.defer_loading = False
        self.settings = settings or SummarySettings()
        self.policy = self.settings.policy()
        self.summary_model = summary_model
        self.model_settings = model_settings
        self.on_record = on_record
        self.state = SummaryState()
        self.estimator = RatioEstimator()

    def record(self, entry: dict[str, Any]) -> None:
        if self.on_record is not None:
            self.on_record({"variant": SUMMARY_VERSION, **entry})

    def describe(self) -> dict[str, Any]:
        return {
            "variant": SUMMARY_VERSION,
            "estimate": ESTIMATE_VERSION,
            "settings": dataclasses.asdict(self.settings),
            "hard_limit": self.settings.hard,
            "call_cap": self.settings.cap,
            "policy": dataclasses.asdict(self.policy),
            "summary_system": SUMMARY_SYSTEM_P015,
        }

    async def for_run(self, ctx: RunContext[Any]) -> TokenTailSummary:
        del ctx
        st = self.state
        if st.restate is not None:
            st.carry.append(Entry("q", "user", st.restate))
            st.restate = None
        st.run_messages = set()
        st.steps = st.calls = st.summary_calls = 0
        return self

    def get_instructions(self) -> str | None:
        return instructions(self.settings)

    # ------------------------------------------------------------------ the request

    def _pinned(self) -> ModelRequest:
        st = self.state
        assert st.pinned is not None
        if st.summary is None and st.restate is None:
            return st.pinned
        extra: list[Any] = []
        if st.summary is not None:
            extra.append(
                UserPromptPart(
                    f"<summary_of_earlier_work>\n{st.summary}\n</summary_of_earlier_work>"
                )
            )
        if st.restate is not None:
            extra.append(UserPromptPart(f"The question you are working on now:\n\n{st.restate}"))
        return dataclasses.replace(st.pinned, parts=[*st.pinned.parts, *extra])

    def _build(
        self, messages: list[ModelMessage], notices: list[str]
    ) -> tuple[list[ModelMessage], int, int]:
        st = self.state
        request: list[ModelMessage] = [self._pinned(), *messages]
        if notices:
            last = request[-1]
            assert isinstance(last, ModelRequest)
            request[-1] = dataclasses.replace(
                last, parts=[*last.parts, UserPromptPart("\n".join(notices))]
            )
        chars = request_chars(request, st.params)
        assert st.model is not None
        return request, chars, self.estimator.tokens(st.model, chars)

    def _units(self) -> list[list[ModelMessage]]:
        msgs = self.state.messages
        return [msgs[i : i + 2] for i in range(0, len(msgs), 2)]

    def _unit_entries(self, units: list[list[ModelMessage]]) -> list[Entry]:
        return [
            Entry(f"u{i}", "unit", "\n".join(f"[{role_of(m)}] {body(m)}" for m in u))
            for i, u in enumerate(units)
        ]

    def _entry_tokens(self, entry: Entry) -> int:
        assert self.state.model is not None
        return int(len(render_entries([entry])) / self.estimator.ratio(self.state.model)) + 1

    def _transcript(self, older: list[list[ModelMessage]]) -> list[Entry]:
        """The summariser's transcript: the earlier summary, earlier restated questions, then the older messages."""
        st = self.state
        out: list[Entry] = []
        if st.summary is not None:
            out.append(Entry(f"sum{st.summary_index}", "summary", st.summary))
        out += st.carry
        if st.restate is not None:
            out.append(Entry("q", "user", st.restate))
        n = 0
        for unit in older:
            for m in unit:
                n += 1
                out.append(Entry(f"m{n}", role_of(m), body(m)))
        return out

    async def _summarise(self, forced: bool) -> bool:
        st, s, policy = self.state, self.settings, self.policy
        units = self._units()
        entries = self._unit_entries(units)
        if not policy.eligible(tuple(entries), self._entry_tokens):
            return False
        older_entries, tail_entries = policy.split(tuple(entries), self._entry_tokens)
        older = units[: len(older_entries)]
        tail = [m for u in units[len(older_entries) :] for m in u]
        assert len(tail_entries) == len(units) - len(older)
        question = self._run_question(older)
        hard, pressure_tokens = s.hard, s.pressure_ratio * s.budget_tokens
        assert st.pinned is not None and st.model is not None
        task = "\n".join(str(p.content) for p in st.pinned.parts if isinstance(p, UserPromptPart))
        saved = (st.summary, st.restate)
        st.summary, st.restate = "", question if question is not None else st.restate
        base = self._build(tail, [])[2]
        st.summary, st.restate = saved
        room = policy.target_tokens - base
        transcript = self._transcript(older)
        for attempt in range(policy.retries + 1):
            limit = policy.char_limit(attempt, room, self.estimator.ratio(st.model))
            if st.summary_calls >= s.summary_call_cap:
                self.record({"type": "summary_call_cap", "summary_calls": st.summary_calls})
                raise SummaryCallCap(
                    f"the summariser-call cap of {s.summary_call_cap} per turn was reached"
                )
            st.summary_calls += 1
            st.summary_calls_total += 1
            request = ModelRequest(
                parts=[
                    SystemPromptPart(SUMMARY_SYSTEM_P015),
                    UserPromptPart(summary_user(task, transcript, limit)),
                ]
            )
            response = await self.summary_model.request(
                [request], self.model_settings, ModelRequestParameters()
            )
            text = "".join(p.content for p in response.parts if isinstance(p, TextPart)).strip()
            record = {
                "summary_attempt": attempt,
                "forced": forced,
                "requested_max_chars": limit,
                "older_units": len(older),
                "kept_units": len(units) - len(older),
                "summary_chars": len(text),
            }
            if not text:
                self.record({"type": "summary_empty", **record})
                continue
            saved = (st.summary, st.restate)
            st.summary, st.restate = text, question if question is not None else st.restate
            est = self._build(tail, [])[2]
            if est <= policy.accept_tokens(attempt, pressure_tokens, hard):
                st.summary_index += 1
                st.summaries_total += 1
                st.messages = tail
                st.carry = []
                self.record({"type": "summary_applied", "est_request_tokens": est, **record})
                return True
            st.summary, st.restate = saved
            self.record({"type": "summary_too_large", "est_request_tokens": est, **record})
        self.record(
            {"type": "context_limit", "reason": "summary did not fit after the bounded retry"}
        )
        raise ArmContextLimit("summary did not fit the acceptance limit after the bounded retry")

    def _run_question(self, older: list[list[ModelMessage]]) -> str | None:
        """The current turn's question, if it is in the section being summarised."""
        found = None
        for unit in older:
            for m in unit:
                if isinstance(m, ModelRequest) and id(m) in self.state.run_messages:
                    texts = [str(p.content) for p in m.parts if isinstance(p, UserPromptPart)]
                    if texts:
                        found = "\n".join(texts)
        return found

    # ------------------------------------------------------------------ the hooks

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        st, s = self.state, self.settings
        st.params, st.model = request_context.model_request_parameters, ctx.model
        messages = list(request_context.messages)
        if st.pinned is None:
            first = messages[0]
            assert isinstance(first, ModelRequest)
            st.pinned, new = first, messages[1:]
        else:
            new = messages[st.sent_len :]
        for m in new:
            st.messages.append(m)
            st.run_messages.add(id(m))
        if st.steps >= s.max_steps:
            self.record({"type": "step_limit", "steps": st.steps, "max_steps": s.max_steps})
            raise ArmStepLimit(f"the task-step limit of {s.max_steps} was reached")
        if st.calls >= s.cap:
            self.record({"type": "call_cap", "calls": st.calls, "call_cap": s.cap})
            raise UsageLimitExceeded(f"the model-call cap of {s.cap} was reached")
        _, _, est = self._build(st.messages, [])
        pressure = est >= s.pressure_ratio * s.budget_tokens
        if pressure:
            self.record({"type": "pressure", "est_request_tokens": est})
            if await self._summarise(forced=False):
                est = self._build(st.messages, [])[2]
        if est > s.hard:
            if not await self._summarise(forced=True):
                self.record(
                    {
                        "type": "context_limit",
                        "est_request_tokens": est,
                        "reason": "nothing to summarise",
                    }
                )
                raise ArmContextLimit(f"request ~{est} tokens > hard limit {s.hard}")
            est = self._build(st.messages, [])[2]
            if est > s.hard:
                self.record({"type": "context_limit", "est_request_tokens": est})
                raise ArmContextLimit(
                    f"request ~{est} tokens > hard limit {s.hard} after a summary"
                )
        left = min(s.max_steps - st.steps, s.cap - st.calls)
        notices = (
            [f"{NOTE} {left} step(s) left in this turn: finish now and give your final answer."]
            if left <= s.finalize_steps
            else []
        )
        request, chars, est = self._build(st.messages, notices)
        request_context.messages = request
        st.sent_len, st.last_chars = len(request), chars
        self.record(
            {
                "type": "summary_arm_request",
                "estimated_tokens": est,
                "chars": chars,
                "messages": len(request),
                "pressure": pressure,
                "has_summary": st.summary is not None,
                "notices": notices,
                "steps": st.steps,
                "calls": st.calls,
                "summary_calls": st.summary_calls,
                "chars_per_token": self.estimator.ratio(ctx.model),
            }
        )
        return request_context

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        del request_context
        st = self.state
        st.calls += 1
        st.steps += 1
        self.estimator.observe(ctx.model, st.last_chars, response.usage.input_tokens)
        return response
