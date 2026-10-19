"""`PaperShellContext` (`paper-clm-shell/1` and `/2`): the paper's shell-based context editing, as a Pydantic AI
capability.

The model has one extra tool, `bash`, which runs one command in a fresh sandbox container (`shell.ShellSandbox`).
Before each command, the editable conversation is written to the mirror file (`mirror.render`). If the command
changed the file, the edit gate (`fit`) decides: the file must parse back into a valid conversation (`mirror.parse`),
its last block (the reply holding the running command) must be unchanged, and the result must fit the limit. An
accepted edit replaces the conversation from the next request on; a rejected one changes nothing. Either way, the
command's result ends with a one-line receipt.

The capability owns the model-visible message list: each request it sends is the pinned first request (system prompt
and first question) followed by the editable conversation, and Pydantic AI adopts that list as its history. Per
request it also adds transient notices (not mirrored, not kept): size nudges, the adaptive persistent nudge, rollback
and over-limit notices, and the finalize notice.

Accounting, per run (one user turn): every response is a model call (`call_cap`); every response is a task step
(`max_steps`) except an edit-only one (exactly one `bash` call, whose command changed the mirror, exited 0 and printed
nothing). Over the limit, the newest steps of this turn are rolled back until `retry_margin_tokens` are free, up to
`max_rollbacks` times; then one final request over the limit is allowed, and if the next is still over, the turn
ends (`ArmContextLimit`).

Versions (`PaperSettings.version`):
- `paper-clm-shell/1` (the default; what smoke-dev2 ran): a rolled-back step takes its edit receipt with it.
- `paper-clm-shell/2` (2026-10-19): when a rolled-back step held a rejected edit, that edit's rejection reason is
  repeated, once, beside the rollback notice ("from the rolled-back step: edit rejected, nothing changed: ..."), so
  that the model is still told why its edit was not applied (the README's property; the sources do not say how
  rollback treats a receipt, so this is a documented adaptation). Nothing else of the step returns: not its command,
  output or the deleted text. The feedback is part of the request the rollback measures, so it counts towards the
  limit like any other content. Every other rule is the same in both versions.

Settings, sources and every adaptation are listed in clm-lib-test-results, experiment 015,
`paper-clm-shell-fidelity.md`. This is an independent implementation from the paper and the public README; it is
not, and is not claimed to be, byte-identical to the reference harness.
"""

from __future__ import annotations

import collections
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ...output_cap import cap_exec
from ..arm_errors import ArmContextLimit, ArmStepLimit
from ..estimate import ESTIMATE_VERSION, RatioEstimator, request_chars
from .mirror import MIRROR_VERSION, MirrorError, parse, render
from .shell import MIRROR_FILE, ShellSandbox

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestContext

PAPER_VERSION = "paper-clm-shell/1"  # the default, and the historical version
PAPER_V2 = "paper-clm-shell/2"  # rejection feedback kept through rollback
PAPER_VERSIONS = (PAPER_VERSION, PAPER_V2)
TOOL = "bash"
NOTE = "[context]"


PROFILES = ("paper", "readme")


@dataclasses.dataclass(frozen=True)
class PaperSettings:
    """`profile` selects the source for the rules the paper and the README describe differently (2026-10-16 review,
    clm-lib-test-results 015 `paper-clm-shell-fidelity.md`):

    - `paper` (default): the paper's experimental settings (Appendix E). One editing reminder, given when the request
      comes within `reminder_before_tokens` (2,048) of the limit; and over the limit, the last step is rolled back and
      the request retried, up to 6 times (BrowseComp-Plus).
    - `readme`: the harness README's defaults. Nudges at 25, 50 and 75% of the budget plus the adaptive persistent
      nudge, and rollback of the newest steps until `retry_margin_tokens` are free, up to 50 times.

    Fields left as None take the profile's value.
    """

    budget_tokens: int = 32_000
    reserve_tokens: int = 2_048
    max_steps: int = 60
    call_cap: int | None = None  # 2 * max_steps + 24 by default
    edit_gate: str = "fit"
    profile: str = "paper"
    reminders: str | None = None  # "single" (paper) or "escalating" (README)
    reminder_before_tokens: int = 2_048
    rollback: str | None = None  # "last-step" (paper) or "to-margin" (README)
    max_rollbacks: int | None = None  # 6 (paper) or 50 (README)
    nudge_fractions: tuple[float, ...] = (0.25, 0.50, 0.75)  # of the budget (README)
    persistent_floor_fraction: float = 0.10  # of the limit (README)
    persistent_output_mult: float = 2.0
    persistent_min_fraction: float = 0.50  # of the limit (README)
    retry_margin_tokens: int = 2_048
    finalize_steps: int = 3
    max_output_chars: int = 8_000
    command_timeout_s: int = 30
    version: str = PAPER_VERSION

    def __post_init__(self) -> None:
        if self.version not in PAPER_VERSIONS:
            raise ValueError(
                f"unknown paper arm version {self.version!r}; expected one of {PAPER_VERSIONS}"
            )
        if self.edit_gate != "fit":
            raise ValueError("only the paper's default `fit` edit gate is implemented")
        if self.profile not in PROFILES:
            raise ValueError(f"unknown profile {self.profile!r}; expected one of {PROFILES}")
        paper = self.profile == "paper"
        defaults = {
            "reminders": "single" if paper else "escalating",
            "rollback": "last-step" if paper else "to-margin",
            "max_rollbacks": 6 if paper else 50,
        }
        for name, value in defaults.items():
            if getattr(self, name) is None:
                object.__setattr__(self, name, value)
        if self.reminders not in ("single", "escalating") or self.rollback not in (
            "last-step",
            "to-margin",
        ):
            raise ValueError(f"unknown reminders {self.reminders!r} or rollback {self.rollback!r}")

    @property
    def limit(self) -> int:
        return self.budget_tokens - self.reserve_tokens

    @property
    def cap(self) -> int:
        return self.call_cap if self.call_cap is not None else 2 * self.max_steps + 24

    @property
    def rollback_limit(self) -> int:
        assert self.max_rollbacks is not None
        return self.max_rollbacks


def instructions(settings: PaperSettings) -> str:
    return (
        f"Context management. Your conversation context is limited to about {settings.limit:,} tokens. Before each "
        f"bash command runs, the conversation so far (every message after the system prompt and the first question) "
        f"is written to {MIRROR_FILE}, one block per message, each headed [[CTX_TURN i role=user|assistant|tool]]. "
        "An assistant block holds your text and your tool calls (<<call id=... tool=...>>, the JSON arguments, "
        "<<end call>>); a tool block holds the results (<<result id=... tool=...>>, the result, <<end result>>). "
        "You can change the file with any shell command: delete blocks, shorten them or rewrite them. When the "
        "command finishes, a changed file becomes your context from the next request on, if it is a valid "
        "conversation and fits the limit: assistant blocks and user or tool blocks alternate, starting with an "
        "assistant block; every call is answered in the next block; and the last block, which holds the running "
        "command, is unchanged. Otherwise the edit is not applied, and the command's result says why. The bash tool "
        "runs one command per call, in a sandbox without network access; files in /work persist between commands."
    )


@dataclasses.dataclass
class PaperState:
    """Per conversation. The run counters are reset by `for_run`."""

    pinned: ModelRequest | None = None
    messages: list[ModelMessage] = dataclasses.field(
        default_factory=list
    )  # editable, model-visible
    sent_len: int = 0
    last_chars: int = 0
    params: ModelRequestParameters | None = None
    model: Model | None = None
    current_response: ModelResponse | None = None
    recent_outputs: collections.deque[int] = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=3)
    )
    nudged: set[float] = dataclasses.field(default_factory=set)
    # per run
    run_messages: set[int] = dataclasses.field(
        default_factory=set
    )  # ids of messages added this run
    steps: int = 0
    calls: int = 0
    free_steps: int = 0
    rollbacks: int = 0
    final_over_used: bool = False
    rejections: dict[str, str] = dataclasses.field(
        default_factory=dict
    )  # paper-clm-shell/2: bash call id -> its edit's rejection reason, this run
    carried: list[str] = dataclasses.field(
        default_factory=list
    )  # paper-clm-shell/2: reasons from steps rolled back for this request
    # per conversation totals
    edits_accepted: int = 0
    edits_rejected: int = 0
    rollbacks_total: int = 0


class PaperShellContext(AbstractCapability[Any]):
    def __init__(
        self,
        sandbox: ShellSandbox,
        settings: PaperSettings | None = None,
        *,
        on_record: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.id = "paper_clm_shell"
        self.description = "Shell-based editing of a mirrored conversation context."
        self.defer_loading = False
        self.settings = settings or PaperSettings()
        self.sandbox = sandbox
        self.on_record = on_record
        self.state = PaperState()
        self._last_rejection = ""  # the reason of the most recent rejected edit
        self.estimator = RatioEstimator()
        self.mirror_path = Path(sandbox.mirror_dir) / Path(MIRROR_FILE).name

    def record(self, entry: dict[str, Any]) -> None:
        if self.on_record is not None:
            self.on_record({"variant": self.settings.version, **entry})

    def describe(self) -> dict[str, Any]:
        return {
            "variant": self.settings.version,
            "mirror": MIRROR_VERSION,
            "estimate": ESTIMATE_VERSION,
            "settings": dataclasses.asdict(self.settings),
            "limit": self.settings.limit,
            "call_cap": self.settings.cap,
            "sandbox": {
                "image": self.sandbox.image,
                "timeout_s": self.sandbox.timeout_s,
                "memory": self.sandbox.memory,
                "network": "none",
                "mounts": ["/tmp/.live_ctx (mirror)", "/work (session scratch)"],
            },
        }

    async def for_run(self, ctx: RunContext[Any]) -> PaperShellContext:
        del ctx
        st = self.state
        st.run_messages = set()
        st.steps = st.calls = st.free_steps = st.rollbacks = 0
        st.final_over_used = False
        st.rejections, st.carried = {}, []
        return self

    def get_instructions(self) -> str | None:
        return instructions(self.settings)

    # ------------------------------------------------------------------ the shell tool

    def get_toolset(self) -> FunctionToolset[Any] | None:
        def bash(ctx: RunContext[Any], command: str) -> str:
            """Run one bash command in the sandbox and return its exit code and output.

            Args:
                command: The bash command to run.
            """
            return self.run_command(ctx.tool_call_id or "", command)

        toolset: FunctionToolset[Any] = FunctionToolset(id="paper_clm_shell_tools")
        toolset.add_function(bash, takes_ctx=True, name=TOOL, sequential=True)
        return toolset

    def _estimate(self, messages: list[ModelMessage]) -> tuple[int, int]:
        st = self.state
        chars = request_chars(messages, st.params)
        assert st.model is not None
        return chars, self.estimator.tokens(st.model, chars)

    def run_command(self, call_id: str, command: str) -> str:
        st, s = self.state, self.settings
        reply = st.current_response
        assert reply is not None and st.pinned is not None
        bash_ids = [
            p.tool_call_id
            for p in reply.parts
            if isinstance(p, ToolCallPart) and p.tool_name == TOOL
        ]
        if bash_ids and call_id != bash_ids[0]:
            self.record({"type": "paper_command_skipped", "call_id": call_id})
            return "not run: one bash command per response (only this response's first bash command runs)"
        before = [*st.messages, reply]
        text = render(before)
        self.mirror_path.write_text(text, encoding="utf-8")
        res = self.sandbox.run(command)
        output, cap = cap_exec(res, self.sandbox.capture_bytes, s.max_output_chars)
        try:
            after = self.mirror_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            after = ""
        changed = after != text
        receipt, outcome = None, "unchanged"
        if changed:
            outcome, receipt = self._edit(after, before, reply)
            if outcome == "rejected" and s.version == PAPER_V2:
                st.rejections[call_id] = self._last_rejection
        only_bash = len(
            [p for p in reply.parts if isinstance(p, ToolCallPart)]
        ) == 1 and bash_ids == [call_id]
        silent = (
            res.exit_code == 0
            and res.stdout_bytes == 0
            and res.stderr_bytes == 0
            and not res.timed_out
        )
        if only_bash:  # its step was not counted when the response arrived
            if changed and silent:
                st.free_steps += 1
            else:
                st.steps += 1
        self.record(
            {
                "type": "paper_command",
                "call_id": call_id,
                "exit_code": res.exit_code,
                "timed_out": res.timed_out,
                "stdout_bytes": res.stdout_bytes,
                "stderr_bytes": res.stderr_bytes,
                "duration_s": res.duration_s,
                "mirror_changed": changed,
                "edit": outcome,
                "free_step": only_bash and changed and silent,
                "cap": cap.to_dict(),
            }
        )
        return output if receipt is None else f"{output}\n{receipt}"

    def _edit(
        self, after: str, before: list[ModelMessage], reply: ModelResponse
    ) -> tuple[str, str]:
        st, s = self.state, self.settings
        assert st.pinned is not None

        def reject(reason: str) -> tuple[str, str]:
            st.edits_rejected += 1
            self._last_rejection = reason
            self.record(
                {"type": "paper_edit_rejected", "reason": reason, "blocks_before": len(before)}
            )
            return "rejected", f"{NOTE} edit rejected, nothing changed: {reason}."

        try:
            parsed = parse(after, before)
        except MirrorError as exc:
            return reject(str(exc))
        if not parsed or parsed[-1] is not reply:
            return reject("the last block, which holds the running command, must stay unchanged")
        _, tokens = self._estimate([st.pinned, *parsed])
        if tokens > s.limit:
            return reject(
                f"the edited context would be about {tokens:,} tokens, over the {s.limit:,}-token limit"
            )
        kept = {id(m) for m in before}
        st.messages = parsed[:-1]
        st.edits_accepted += 1
        self.record(
            {
                "type": "paper_edit_applied",
                "blocks_before": len(before),
                "blocks_after": len(parsed),
                "rebuilt_blocks": sum(1 for m in parsed if id(m) not in kept),
                "estimated_tokens": tokens,
            }
        )
        return "applied", (
            f"{NOTE} edit applied: {len(before)} blocks before, {len(parsed)} after; the context is now about "
            f"{tokens:,} of {s.limit:,} tokens."
        )

    # ------------------------------------------------------------------ the hooks

    def _absorb(self, messages: list[ModelMessage]) -> None:
        st = self.state
        if st.pinned is None:
            first = messages[0]
            assert isinstance(first, ModelRequest)
            st.pinned, new = first, messages[1:]
        else:
            new = messages[st.sent_len :]
        for m in new:
            if isinstance(m, ModelRequest):
                for p in m.parts:
                    if isinstance(p, ToolReturnPart):
                        assert st.model is not None
                        st.recent_outputs.append(
                            self.estimator.tokens(st.model, len(p.model_response_str()))
                        )
            st.messages.append(m)
            st.run_messages.add(id(m))

    def _build(self, notices: list[str]) -> tuple[list[ModelMessage], int, int]:
        st = self.state
        assert st.pinned is not None
        request: list[ModelMessage] = [st.pinned, *st.messages]
        if notices:
            last = request[-1]
            assert isinstance(last, ModelRequest)
            request[-1] = dataclasses.replace(
                last, parts=[*last.parts, UserPromptPart("\n".join(notices))]
            )
        chars, tokens = self._estimate(request)
        return request, chars, tokens

    def _rollable(self) -> bool:
        """Whether the newest step (a reply and its results) belongs to this turn and can be rolled back."""
        st = self.state
        return (
            len(st.messages) >= 2
            and isinstance(st.messages[-2], ModelResponse)
            and isinstance(st.messages[-1], ModelRequest)
            and {id(st.messages[-2]), id(st.messages[-1])} <= st.run_messages
            and any(
                isinstance(p, ToolReturnPart) or p.part_kind == "retry-prompt"
                for p in st.messages[-1].parts
            )
        )

    def _rollback(self, target: int) -> int:
        """README rule: remove the newest steps of this turn until the request is at most `target`."""
        st = self.state
        units = 0
        while self._rollable():
            if self._build(self._feedback())[2] <= target:
                break
            self._carry(st.messages[-2])
            del st.messages[-2:]
            units += 1
        return units

    def _carry(self, removed: ModelMessage) -> None:
        """paper-clm-shell/2: keep the rejection reason of an edit whose step is being rolled back (once)."""
        st = self.state
        if self.settings.version != PAPER_V2 or not isinstance(removed, ModelResponse):
            return
        for p in removed.parts:
            if isinstance(p, ToolCallPart) and p.tool_call_id in st.rejections:
                st.carried.append(st.rejections.pop(p.tool_call_id))

    def _feedback(self) -> list[str]:
        """The carried rejection reasons, in the existing receipt wording; empty in paper-clm-shell/1."""
        return [
            f"{NOTE} from the rolled-back step: edit rejected, nothing changed: {reason}."
            for reason in self.state.carried
        ]

    def _notices(self, tokens: int) -> list[str]:
        st, s = self.state, self.settings
        out: list[str] = []
        limit = s.limit
        if s.reminders == "single":
            # The paper: one editing reminder, 2,048 tokens before the limit; given once per crossing, and re-armed
            # when the request falls below it again (the repetition is not stated by the paper: our choice).
            point = limit - s.reminder_before_tokens
            if tokens < point:
                st.nudged.discard(0.0)
            elif 0.0 not in st.nudged:
                st.nudged.add(0.0)
                out.append(
                    f"{NOTE} the context is about {tokens:,} tokens, within {s.reminder_before_tokens:,} tokens of "
                    f"the {limit:,}-token limit. Edit {MIRROR_FILE} to make room."
                )
        else:
            budget = s.budget_tokens
            for f in list(st.nudged):
                if tokens < f * budget:
                    st.nudged.discard(f)  # re-armed once the context is back below it
            crossed = [f for f in s.nudge_fractions if tokens >= f * budget and f not in st.nudged]
            if crossed:
                st.nudged.update(crossed)
                out.append(
                    f"{NOTE} the context is about {tokens:,} tokens, {round(100 * tokens / budget)}% of the "
                    f"{budget:,}-token budget."
                )
            room = limit - tokens
            threshold = max(
                s.persistent_floor_fraction * limit,
                s.persistent_output_mult * max(st.recent_outputs, default=0),
            )
            if tokens >= s.persistent_min_fraction * limit and room < threshold:
                out.append(
                    f"{NOTE} only about {max(room, 0):,} tokens are left before the {limit:,}-token limit. Edit "
                    f"{MIRROR_FILE} to make room before reading more."
                )
        left = min(s.max_steps - st.steps, s.cap - st.calls)
        if left <= s.finalize_steps:
            out.append(
                f"{NOTE} {left} step(s) left in this turn: finish now and give your final answer."
            )
        return out

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        st, s = self.state, self.settings
        st.params, st.model = request_context.model_request_parameters, ctx.model
        self._absorb(list(request_context.messages))
        if st.steps >= s.max_steps:
            self.record({"type": "step_limit", "steps": st.steps, "max_steps": s.max_steps})
            raise ArmStepLimit(f"the task-step limit of {s.max_steps} was reached")
        if st.calls >= s.cap:
            self.record({"type": "call_cap", "calls": st.calls, "call_cap": s.cap})
            raise UsageLimitExceeded(f"the model-call cap of {s.cap} was reached")
        events: list[str] = []
        st.carried = []
        _, _, tokens = self._build([])
        over = tokens
        if tokens > s.limit and st.rollbacks < s.rollback_limit:
            units = 0
            if s.rollback == "to-margin":
                units = self._rollback(s.limit - s.retry_margin_tokens)
                if units:
                    st.rollbacks += 1
                    st.rollbacks_total += 1
            else:  # the paper: roll back the last step and retry, each counted, while still over the limit
                while tokens > s.limit and st.rollbacks < s.rollback_limit and self._rollable():
                    self._carry(st.messages[-2])
                    del st.messages[-2:]
                    units += 1
                    st.rollbacks += 1
                    st.rollbacks_total += 1
                    tokens = self._build(self._feedback())[
                        2
                    ]  # /2: measured with the carried feedback
            if units:
                after = self._build(self._feedback())[2]
                record: dict[str, Any] = {
                    "type": "paper_rollback",
                    "mode": s.rollback,
                    "units": units,
                    "from_tokens": over,
                    "to_tokens": after,
                }
                if s.version == PAPER_V2:
                    record["carried_rejections"] = len(st.carried)
                self.record(record)
                events.append(
                    f"{NOTE} the context was over the {s.limit:,}-token limit, so your last {units} step(s) were "
                    f"rolled back and their results removed. Edit {MIRROR_FILE} to make room before continuing."
                )
                events += self._feedback()
                tokens = after
        if tokens > s.limit:
            if st.final_over_used or tokens > s.budget_tokens:
                self.record(
                    {"type": "context_limit", "estimated_tokens": tokens, "limit_tokens": s.limit}
                )
                raise ArmContextLimit(
                    f"the request (about {tokens} tokens) is over the {s.limit}-token limit"
                )
            st.final_over_used = True
            self.record(
                {"type": "paper_over_limit", "estimated_tokens": tokens, "limit_tokens": s.limit}
            )
            events.append(
                f"{NOTE} the context is about {tokens:,} tokens, over the {s.limit:,}-token limit, and nothing more "
                f"can be rolled back. Edit {MIRROR_FILE} in this step; if the next request is still over the limit, "
                "the turn ends."
            )
        else:
            st.final_over_used = False
        notices = [*events, *self._notices(tokens)]
        request, chars, tokens = self._build(notices)
        request_context.messages = request
        st.sent_len, st.last_chars = len(request), chars
        self.record(
            {
                "type": "paper_request",
                "estimated_tokens": tokens,
                "chars": chars,
                "blocks": len(st.messages),
                "notices": notices,
                "steps": st.steps,
                "free_steps": st.free_steps,
                "calls": st.calls,
                "rollbacks": st.rollbacks,
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
        st.current_response = response
        self.estimator.observe(ctx.model, st.last_chars, response.usage.input_tokens)
        calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
        if not (len(calls) == 1 and calls[0].tool_name == TOOL):
            st.steps += 1  # a lone bash call is counted when its command has run (it may be a free edit step)
        if len(calls) > 1:
            self.record({"type": "parallel_calls", "calls": [c.tool_name for c in calls]})
        return response
