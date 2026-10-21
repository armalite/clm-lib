"""`ArmSession`: the p015 runner path for the paper and summary arms (`paper_clm_shell`, `paper_clm_tools`,
`token_tail_p015`).

It mirrors `GuardedSession` (which hosts guarded-clm and is unchanged): one Pydantic AI `Agent` per session, the
same shared task tools through the same bridge, the same accounting wrapper, recorder and export, the same system
prompt and output type, one `agent.run` per user turn with the previous turn's `all_messages()` as history and the
same `conversation_id`. Only the context capability differs:

- `paper_clm_shell`: `PaperShellContext`, with its `bash` tool and a per-session sandbox (`paper.shell`);
- `paper_clm_tools`: `PaperToolsContext` (`papertools`), with its `context_read` and `context_apply` tools and no
  sandbox of its own;
- `token_tail_p015`: `TokenTailSummary`, whose summariser calls go through a second accounting wrapper (category
  `summary`) sharing the ledger, the recorder and the attempt numbering.

Endings are those of `GuardedSession`, with the arms' own errors: `context_limit` (`ArmContextLimit`), `step_limit`
(`ArmStepLimit`), `call_cap`, `summary_call_cap` (`SummaryCallCap`), `request_incompatible` (`ArmRequestIncompatible`:
`paper-clm-tools/2` could not build a request the provider's documented rules allow; nothing was dispatched) and
`sandbox_unavailable` (the shell could not start an isolated container; nothing ran on the host).
"""

from __future__ import annotations

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
from .accounting import (
    AccountedModel,
    AccountingBoundViolated,
    AttemptCounter,
    LaunchStopped,
    Mode,
    OutputTruncated,
    RetryPolicy,
    SpendGate,
    SpendRefused,
)
from .arm_errors import ArmContextLimit, ArmRequestIncompatible, ArmStepLimit, SummaryCallCap
from .loop import SessionLoop
from .paper.capability import PaperSettings, PaperShellContext
from .paper.shell import ShellSandbox
from .papertools import EDIT_TOOL, READ_TOOL, PaperToolsContext, ToolsSettings
from .provenance import harness_provenance, versions
from .records import SessionRecorder
from .session import ContinuationUnsupported, TurnOutcome
from .summary.capability import SummarySettings, TokenTailSummary
from .tools import bridge

Arm = Literal["paper_clm_shell", "paper_clm_tools", "token_tail_p015"]
ARMS: tuple[Arm, ...] = ("paper_clm_shell", "paper_clm_tools", "token_tail_p015")


# Why the paper and summary arms cannot continue after a limit-ended turn (checked offline, 2026-10-19).
CONTINUATION_BLOCKER = (
    "the arm keeps its own copy of the conversation and takes the messages each request adds by position (the "
    "length of the request it last sent). A limit-ended turn stops between a tool result and the next model call, "
    "so the next turn's question follows a tool-result request; Pydantic AI merges those two requests when it next "
    "cleans the history, and the arm's next turn then loses or repeats messages (or, from the earlier completed "
    "turn's history, never sees the new question). Continuing needs a reviewed change to the arm's bookkeeping."
)
# paper_clm_tools keeps no copy of its own, but continuing after a limit-ended turn is not part of its version 1.
TOOLS_CONTINUATION_BLOCKER = (
    "continuing after a limit-ended turn is not part of paper-clm-tools/1; it has not been specified or "
    "verified"
)


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
        tools: ToolsSettings | None = None,
        sandbox_image: str = "python:3.12-slim",
        system_prompt: str | Sequence[str] = (),
        task_tools: Sequence[TaskTool] = (),
        output_type: Any = str,
        model_settings: ModelSettings | None = None,
        conversation_id: str = "session",
        mode: Mode = "scripted",
        live_authorised: bool = False,
        evidence: str = EVIDENCE_LABEL,
        gate: SpendGate | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.arm = arm
        self.recorder = SessionRecorder(run_dir, run_id=run_id, evidence=evidence)
        counter = AttemptCounter()
        accounting: dict[str, Any] = {
            "ledger": ledger,
            "price": price,
            "run_id": run_id,
            "sink": self.recorder,
            "mode": mode,
            "live_authorised": live_authorised,
            "counter": counter,
            "gate": gate,
            "retry": retry,
        }  # fmt: skip
        self.model = AccountedModel(model, category="main", **accounting)
        self.capability: PaperShellContext | PaperToolsContext | TokenTailSummary
        if arm == "paper_clm_shell":
            settings = paper or PaperSettings()
            box = run_dir / "sandbox"
            (box / "mirror").mkdir(parents=True)
            (box / "work").mkdir()
            sandbox = ShellSandbox(box / "mirror", box / "work", image=sandbox_image,
                                   timeout_s=settings.command_timeout_s)  # fmt: skip
            self.capability = PaperShellContext(sandbox, settings, on_record=self.recorder.trace)
            self.cap = settings.cap
        elif arm == "paper_clm_tools":
            tsettings = tools or ToolsSettings()
            self.capability = PaperToolsContext(tsettings, on_record=self.recorder.trace)
            self.cap = tsettings.cap
        elif arm == "token_tail_p015":
            ssettings = summary or SummarySettings()
            self.summary_model = AccountedModel(model, category="summary", **accounting)
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
        self.loop = SessionLoop()  # one event loop for all turns (p015-session-loop/1)
        self.recorder.write_meta(
            arm=arm,
            settings=self.capability.describe(),
            call_cap=self.cap,
            mode=mode,
            conversation_id=conversation_id,
            model={"system": model.system, "model_name": model.model_name},
            price=dataclasses.asdict(price) if price is not None else None,
            task_tools=[t.spec.to_api() for t in task_tools],
            framework_tools={
                "paper_clm_shell": ["bash"],
                "paper_clm_tools": [READ_TOOL, EDIT_TOOL],
            }.get(arm, []),
            provenance={"harness": harness_provenance(), "versions": versions()},
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
        if isinstance(exc, ArmContextLimit):
            return "context_limit"
        if isinstance(exc, ArmStepLimit):
            return "step_limit"
        if isinstance(exc, ArmRequestIncompatible):
            return "request_incompatible"
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
        if isinstance(cap, PaperToolsContext):
            t = cap.state
            return {
                "steps": t.steps,
                "free_steps": t.free_steps,
                "calls": t.calls,
                "retries_used": t.retries,
                "final_turn": t.final_turn,
                "edits_staged_total": t.edits_staged,
                "edits_committed_total": t.edits_committed,
                "edits_rejected_total": t.edits_rejected,
                "rollbacks_total": t.rollbacks_total,
            }
        s = cap.state
        return {
            "steps": s.steps,
            "calls": s.calls,
            "summary_calls": s.summary_calls,
            "summaries_total": s.summaries_total,
            "summary_calls_total": s.summary_calls_total,
        }

    def close(self) -> None:
        """Close the session's model clients on its event loop, then the loop. Turns are refused afterwards."""
        self.loop.close(
            self.model, *([self.summary_model] if hasattr(self, "summary_model") else [])
        )

    def continue_after_limit(self) -> int:
        """Not supported (p015-turn-endings/2): see `CONTINUATION_BLOCKER` (`TOOLS_CONTINUATION_BLOCKER`)."""
        blocker = (
            TOOLS_CONTINUATION_BLOCKER if self.arm == "paper_clm_tools" else CONTINUATION_BLOCKER
        )
        raise ContinuationUnsupported(f"{self.arm}: {blocker}")

    def __enter__(self) -> ArmSession:
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
        try:
            result = self.loop.run(
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
