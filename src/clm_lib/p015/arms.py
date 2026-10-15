"""`ArmSession`: the p015 runner path for the paper and summary arms (`paper_clm_shell`, `token_tail_p015`).

It mirrors `GuardedSession` (which hosts guarded-clm and is unchanged): one Pydantic AI `Agent` per session, the
same shared task tools through the same bridge, the same accounting wrapper, recorder and export, the same system
prompt and output type, one `agent.run` per user turn with the previous turn's `all_messages()` as history and the
same `conversation_id`. Only the context capability differs:

- `paper_clm_shell`: `PaperShellContext`, with its `bash` tool and a per-session sandbox (`paper.shell`);
- `token_tail_p015`: `TokenTailSummary`, whose summariser calls go through a second accounting wrapper (category
  `summary`) sharing the ledger, the recorder and the attempt numbering.

Endings are those of `GuardedSession`, with the arms' own errors: `context_limit` (`ArmContextLimit`), `step_limit`
(`ArmStepLimit`), `call_cap`, `summary_call_cap` (`SummaryCallCap`) and `sandbox_unavailable` (the shell could not
start an isolated container; nothing ran on the host).
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_core import to_jsonable_python

from ..budget import Ledger, ModelPrice
from ..executor import ExecutorUnavailable
from ..tooling import Tool as TaskTool
from . import EVIDENCE_LABEL
from .accounting import AccountedModel, AccountingBoundViolated, AttemptCounter, Mode, SpendRefused
from .arm_errors import ArmContextLimit, ArmStepLimit, SummaryCallCap
from .paper.capability import PaperSettings, PaperShellContext
from .paper.shell import ShellSandbox
from .provenance import harness_provenance, versions
from .records import SessionRecorder
from .session import TurnOutcome
from .summary.capability import SummarySettings, TokenTailSummary
from .tools import bridge

Arm = Literal["paper_clm_shell", "token_tail_p015"]
ARMS: tuple[Arm, ...] = ("paper_clm_shell", "token_tail_p015")


class ArmSession:
    def __init__(
        self,
        arm: Arm,
        model: Model,
        *,
        run_dir: Path,
        run_id: str,
        ledger: Ledger,
        price: ModelPrice | None,
        paper: PaperSettings | None = None,
        summary: SummarySettings | None = None,
        sandbox_image: str = "python:3.12-slim",
        system_prompt: str | Sequence[str] = (),
        task_tools: Sequence[TaskTool] = (),
        output_type: Any = str,
        model_settings: ModelSettings | None = None,
        conversation_id: str = "session",
        mode: Mode = "scripted",
        live_authorised: bool = False,
        evidence: str = EVIDENCE_LABEL,
    ) -> None:
        self.arm = arm
        self.recorder = SessionRecorder(run_dir, run_id=run_id, evidence=evidence)
        counter = AttemptCounter()
        accounting = dict(ledger=ledger, price=price, run_id=run_id, sink=self.recorder, mode=mode,
                          live_authorised=live_authorised, counter=counter)  # fmt: skip
        self.model = AccountedModel(model, category="main", **accounting)  # type: ignore[arg-type]
        self.capability: PaperShellContext | TokenTailSummary
        if arm == "paper_clm_shell":
            settings = paper or PaperSettings()
            box = run_dir / "sandbox"
            (box / "mirror").mkdir(parents=True)
            (box / "work").mkdir()
            sandbox = ShellSandbox(box / "mirror", box / "work", image=sandbox_image,
                                   timeout_s=settings.command_timeout_s)  # fmt: skip
            self.capability = PaperShellContext(sandbox, settings, on_record=self.recorder.trace)
            self.cap = settings.cap
        elif arm == "token_tail_p015":
            ssettings = summary or SummarySettings()
            self.summary_model = AccountedModel(model, category="summary", **accounting)  # type: ignore[arg-type]
            self.capability = TokenTailSummary(
                self.summary_model,
                ssettings,
                model_settings=model_settings,
                on_record=self.recorder.trace,
            )
            self.cap = ssettings.cap
        else:
            raise ValueError(f"unknown arm {arm!r}; expected one of {ARMS}")
        self.agent: Agent[None, Any] = Agent(
            self.model,
            system_prompt=system_prompt,
            tools=[bridge(t, sink=self.recorder) for t in task_tools],
            capabilities=[self.capability],
            output_type=output_type,
            model_settings=model_settings,
        )
        self.conversation_id = conversation_id
        self.history: list[ModelMessage] | None = None
        self.turns = 0
        self.recorder.write_meta(
            arm=arm,
            settings=self.capability.describe(),
            call_cap=self.cap,
            mode=mode,
            conversation_id=conversation_id,
            model={"system": model.system, "model_name": model.model_name},
            price=dataclasses.asdict(price) if price is not None else None,
            task_tools=[t.spec.to_api() for t in task_tools],
            framework_tools=["bash"] if arm == "paper_clm_shell" else [],
            provenance={"harness": harness_provenance(), "versions": versions()},
        )

    def classify(self, exc: BaseException) -> str:
        if isinstance(exc, SpendRefused):
            return "spend_refused"
        if isinstance(exc, AccountingBoundViolated):
            return "harness_defect"
        if isinstance(exc, ArmContextLimit):
            return "context_limit"
        if isinstance(exc, ArmStepLimit):
            return "step_limit"
        if isinstance(exc, SummaryCallCap):
            return "summary_call_cap"
        if isinstance(exc, UsageLimitExceeded):
            return "call_cap"
        if isinstance(exc, ExecutorUnavailable):
            return "sandbox_unavailable"
        if isinstance(exc, ModelAPIError):
            return "provider_failure"
        if self.recorder.raised_by_tool(exc):
            return "tool_failure"
        if isinstance(exc, UnexpectedModelBehavior):
            return "model_behaviour"
        return "unclassified_error"

    def counters(self) -> dict[str, Any]:
        cap = self.capability
        if isinstance(cap, PaperShellContext):
            p = cap.state
            return {
                "steps": p.steps,
                "free_steps": p.free_steps,
                "calls": p.calls,
                "rollbacks": p.rollbacks,
                "edits_accepted_total": p.edits_accepted,
                "edits_rejected_total": p.edits_rejected,
                "rollbacks_total": p.rollbacks_total,
            }
        s = cap.state
        return {
            "steps": s.steps,
            "calls": s.calls,
            "summary_calls": s.summary_calls,
            "summaries_total": s.summaries_total,
            "summary_calls_total": s.summary_calls_total,
        }

    def run_turn(self, prompt: Any) -> TurnOutcome:
        """One user turn. A turn that ends early does not end the session object; the caller decides."""
        self.turns += 1
        try:
            result = asyncio.run(
                self.agent.run(
                    prompt,
                    message_history=self.history,
                    conversation_id=self.conversation_id,
                    usage_limits=UsageLimits(request_limit=self.cap),
                )
            )
        except Exception as exc:
            outcome = TurnOutcome(self.turns, self.classify(exc), error=exc)
            self.recorder.turn(
                {
                    "turn": self.turns,
                    "prompt": to_jsonable_python(prompt),
                    "ending": outcome.ending,
                    "error": outcome.error_text,
                    "counters": self.counters(),
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
                "counters": self.counters(),
            }
        )
        return outcome
