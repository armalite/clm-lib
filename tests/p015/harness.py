"""Comparison harness for the p015 path: the same scripted conversation run (A) directly through guarded-clm and
(B) through clm-lib's GuardedSession. Synthetic data only.

A transcript is every request the model saw (messages, instructions, request parameters including tool
definitions, settings, reported usage), every capability trace record, and how each turn ended. The harness's only
normalisation is the scrubbing of timestamps, run ids, conversation ids, toolset ids and capability ids, exactly as
guarded-clm's own transcript tests do. Prompt wording, estimates, entry order, revisions, guard decisions, counters,
records and errors are compared as they are.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import json
import math
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from guarded_clm import ContextLimits, clm_context
from pydantic_ai import Agent, ModelRetry, UsageLimits
from pydantic_ai import _utils as pydantic_ai_utils
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from pydantic_core import to_jsonable_python

from clm_lib.budget import Ledger, ModelPrice
from clm_lib.p015.records import read_jsonl
from clm_lib.p015.session import GuardedSession, GuardedSettings

EDIT = "edit_context"
SYSTEM = "You are a careful on-call assistant. Use the tools to answer."
VOLATILE = frozenset({"timestamp", "run_id", "conversation_id", "toolset_id", "capability_id"})
FROZEN_TIME = datetime.datetime(2026, 10, 15, 0, 0, 0, 654321, tzinfo=datetime.UTC)
PRICE = ModelPrice("scripted", 2.0, 2.5, 4.0, 0.2, 10.0, "test", "test")


class _FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:
        return FROZEN_TIME.astimezone(tz) if tz is not None else FROZEN_TIME.replace(tzinfo=None)


@contextlib.contextmanager
def frozen_clock() -> Iterator[None]:
    """Pydantic AI's timestamps count towards the capability's size estimates; freeze them (as guarded-clm's own
    transcript tests do) so both runs see identical serialised lengths."""
    namespace = pydantic_ai_utils.__dict__
    original = namespace["datetime"]
    namespace["datetime"] = _FrozenDatetime
    try:
        yield
    finally:
        namespace["datetime"] = original


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def dump(value: Any) -> Any:
    return scrub(json.loads(json.dumps(to_jsonable_python(value))))


# ---------------------------------------------------------------------- synthetic tools


def alerts() -> str:
    """List the open alerts."""
    return "3 open alerts: disk-pressure on db-2, p99 latency on api-7, cert expiry on edge-1"


def trace_page(n: int) -> str:
    """Read one page of the request trace.

    Args:
        n: The page number, from 1.
    """
    return trace_text(n)


def trace_text(n: int) -> str:
    rows = [
        f"{n:02d}{i:03d}|host-{(n * 17 + i) % 53:02d}|{(n * 977 + i * 7351) % 10000:04d}us|ok"
        for i in range(110)
    ]
    return f"trace page {n}\n" + "\n".join(rows)


def runbook(topic: str) -> str:
    """Look up a runbook entry.

    Args:
        topic: The topic.
    """
    return f"runbook[{topic}]: drain the node, then restart the service"


def probe(attempt: int) -> str:
    """Probe a service; the first attempt is rejected.

    Args:
        attempt: The attempt number, from 0.
    """
    if attempt < 1:
        raise ModelRetry("probe again with attempt=1")
    return "probe ok"


def pager() -> str:
    """Page the on-call engineer (its backend is down)."""
    raise RuntimeError("pager backend unavailable")


TOOLS: list[Callable[..., Any]] = [alerts, trace_page, runbook, probe, pager]
DENSE = [trace_text(n) for n in range(1, 13)]


# ---------------------------------------------------------------------- the scripted model


Step = ModelResponse | BaseException


def call(tool: str, args: dict[str, Any], call_id: str, thought: str = "") -> ModelResponse:
    parts: list[Any] = [TextPart(thought)] if thought else []
    return ModelResponse(parts=[*parts, ToolCallPart(tool, args, call_id)])


def calls(*items: tuple[str, dict[str, Any], str], thought: str = "") -> ModelResponse:
    parts: list[Any] = [TextPart(thought)] if thought else []
    return ModelResponse(parts=[*parts, *(ToolCallPart(t, a, i) for t, a, i in items)])


def edit(call_id: str, *ops: dict[str, Any], thought: str = "") -> ModelResponse:
    return call(EDIT, {"ops": list(ops)}, call_id, thought)


def text(content: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content)])


def request_chars(messages: Sequence[ModelMessage], info: AgentInfo) -> int:
    stripped = [
        dataclasses.replace(m, instructions=None) if isinstance(m, ModelRequest) else m
        for m in messages
    ]
    tools = [
        {"name": t.name, "description": t.description, "parameters": t.parameters_json_schema}
        for t in [*info.function_tools, *info.output_tools]
    ]
    return (
        len(ModelMessagesTypeAdapter.dump_json(stripped))
        + len(info.instructions or "")
        + len(json.dumps(tools))
    )


def dense_chars(messages: Sequence[ModelMessage]) -> int:
    prompt = "".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )
    return sum(prompt.count(e) * len(e) for e in {json.dumps(d)[1:-1] for d in DENSE})


class Script:
    """Plays scripted responses and records what the model saw. `usage=None`: FunctionModel's own usage (the
    capability's fixed-ratio path). `(other, dense)`: synthetic provider-like input tokens from a model whose
    `system` is not a scripted one, so the capability calibrates on them. A step may be an exception to raise."""

    def __init__(
        self, steps: list[Step], *, usage: tuple[float, float] | None = (2.4, 1.6)
    ) -> None:
        self.steps, self.usage = steps, usage
        self.seen: list[dict[str, Any]] = []

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        index = len(self.seen)
        self.seen.append(
            {
                "messages": dump(messages),
                "instructions": info.instructions,
                "parameters": dump(info.model_request_parameters),
                "settings": dump(info.model_settings),
            }
        )
        step = self.steps[index]
        if isinstance(step, BaseException):
            raise step
        response = dataclasses.replace(step, parts=list(step.parts))
        if self.usage is not None:
            other, dense = self.usage
            total = request_chars(messages, info)
            heavy = min(dense_chars(messages), total)
            response.usage = RequestUsage(
                input_tokens=math.ceil(heavy / dense + (total - heavy) / other), output_tokens=9
            )
        self.seen[-1]["reported_input_tokens"] = (
            response.usage.input_tokens if self.usage is not None else None
        )
        return response

    def model(self) -> FunctionModel:
        if self.usage is None:
            return FunctionModel(self.respond, model_name="scripted")
        return ProviderLikeModel(self.respond, model_name="scripted")


class ProviderLikeModel(FunctionModel):
    @property
    def system(self) -> str:
        return "scripted-provider-like"  # not "function"/"test": the capability calibrates on its usage


@dataclasses.dataclass
class Case:
    name: str
    steps: Callable[[], list[Step]]
    prompts: list[str]
    settings: GuardedSettings = dataclasses.field(default_factory=GuardedSettings)
    usage: tuple[float, float] | None = (2.4, 1.6)


# ---------------------------------------------------------------------- the two ways of running a case


def _limits(s: GuardedSettings) -> ContextLimits:
    return ContextLimits(
        max_file_bytes=s.max_file_bytes,
        max_entries=s.max_entries,
        max_body_chars=s.max_body_chars,
        max_total_chars=s.max_total_chars,
    )


@frozen_clock()
def run_direct(case: Case) -> dict[str, Any]:
    script = Script(case.steps(), usage=case.usage)
    records: list[dict[str, Any]] = []
    s = case.settings
    capability = clm_context(
        budget_tokens=s.budget_tokens,
        max_steps=s.max_steps,
        call_cap=s.call_cap,
        end_over_budget=s.end_over_budget,
        max_output_chars=s.max_output_chars,
        limits=_limits(s),
        on_record=records.append,
    )
    agent = Agent(script.model(), system_prompt=SYSTEM, tools=TOOLS, capabilities=[capability])
    runs: list[dict[str, Any]] = []
    history: list[ModelMessage] | None = None
    for prompt in case.prompts:
        before = len(script.seen)
        try:
            result = asyncio.run(
                agent.run(
                    prompt,
                    message_history=history,
                    conversation_id="conv",
                    usage_limits=UsageLimits(request_limit=s.cap),
                )
            )
        except Exception as error:
            runs.append(
                {
                    "prompt": prompt,
                    "calls": len(script.seen) - before,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            break
        history = result.all_messages()
        runs.append(
            {
                "prompt": prompt,
                "calls": len(script.seen) - before,
                "output": dump(result.output),
                "all_messages": dump(history),
            }
        )
    return {"calls": script.seen, "records": dump(records), "runs": runs}


@frozen_clock()
def run_session(
    case: Case, run_dir: Path, ledger: Ledger, *, after_turn: Callable[[int], None] | None = None
) -> tuple[dict[str, Any], GuardedSession]:
    script = Script(case.steps(), usage=case.usage)
    session = GuardedSession(
        script.model(),
        run_dir=run_dir,
        run_id=case.name,
        ledger=ledger,
        price=PRICE,
        settings=case.settings,
        system_prompt=SYSTEM,
        framework_tools=TOOLS,
        conversation_id="conv",
    )
    runs: list[dict[str, Any]] = []
    for prompt in case.prompts:
        before = len(script.seen)
        outcome = session.run_turn(prompt)
        if outcome.error is not None:
            runs.append(
                {
                    "prompt": prompt,
                    "calls": len(script.seen) - before,
                    "error": outcome.error_text,
                    "ending": outcome.ending,
                }
            )
            break
        runs.append(
            {
                "prompt": prompt,
                "calls": len(script.seen) - before,
                "output": dump(outcome.output),
                "all_messages": dump(outcome.messages),
                "ending": outcome.ending,
            }
        )
        if after_turn is not None:
            after_turn(outcome.turn)
    records = dump(read_jsonl(run_dir / "trace.jsonl"))
    return {"calls": script.seen, "records": records, "runs": runs}, session


def without_endings(transcript: dict[str, Any]) -> dict[str, Any]:
    """The session's ending classification is extra information the direct run doesn't have."""
    return {
        **transcript,
        "runs": [{k: v for k, v in r.items() if k != "ending"} for r in transcript["runs"]],
    }


# ---------------------------------------------------------------------- the cases

LONG = "Every alert in the review needs its cause, its owner and the change that triggered it. " * 6


def _read_and_answer() -> list[Step]:
    return [call("alerts", {}, "c1", thought="Check the alerts."), text("Three alerts are open.")]


def _edits() -> list[Step]:
    return [
        call("alerts", {}, "c1"),
        edit("e1", {"op": "add_note", "id": "n1", "body": "db-2 disk pressure"}),
        edit("e2", {"op": "replace", "id": "n1", "body": "db-2 disk pressure, owner storage"}),
        edit(
            "e3", {"op": "replace", "id": "n1", "body": "db-2 disk pressure, owner storage"}
        ),  # unchanged
        edit("e4", {"op": "delete", "ids": ["s1.obs", "nope"]}),  # rejected: unknown id
        edit("e5", {"op": "delete", "ids": ["s1.obs"]}),
        text("answer one"),
        edit("e6", {"op": "delete", "ids": ["t1.answer"]}),  # refused: protected
        edit(
            "e7",
            {"op": "replace", "id": "t1.answer", "body": "x"},
            {"op": "add_note", "id": "n2", "body": "y"},
        ),
        text("answer two"),
    ]


def _multiple_calls() -> list[Step]:
    return [
        calls(
            ("alerts", {}, "c1"), ("runbook", {"topic": "disk"}, "c2"), thought="Two reads at once."
        ),
        calls(
            ("runbook", {"topic": "latency"}, "c3"),
            (EDIT, {"ops": [{"op": "delete", "ids": ["s1.obs"]}]}, "e1"),
            ("alerts", {}, "c4"),
        ),
        text("done"),
    ]


def _guard_burst() -> list[Step]:
    return [
        call("alerts", {}, "c1"),
        calls(*(("trace_page", {"n": n}, f"p{n}") for n in range(1, 4))),
        calls(*(("trace_page", {"n": n}, f"p{n}") for n in range(4, 10)), ("alerts", {}, "c2")),
        calls(
            (EDIT, {"ops": [{"op": "delete", "ids": ["s2.obs", "s3.obs"]}]}, "e1"),
            ("trace_page", {"n": 7}, "p7b"),
            ("trace_page", {"n": 8}, "p8b"),
        ),
        text("pages reviewed"),
    ]


def _pressure_rearm_reset() -> list[Step]:
    return [
        call("alerts", {}, "c1", thought=LONG * 8),
        call("alerts", {}, "c2", thought=LONG * 8),
        call("alerts", {}, "c3", thought=LONG * 8),
        edit("e1", {"op": "delete", "ids": ["s1.act", "s2.act", "s3.act"]}),
        call("alerts", {}, "c4", thought=LONG * 8),
        call("alerts", {}, "c5", thought=LONG * 8),
        call("alerts", {}, "c6", thought=LONG * 8),
        text("answer one"),
        # Turn 2 starts over the limit again: the allowance was reset by the new turn, so it uses the reserve.
        edit("e2", {"op": "delete", "ids": ["s5.act", "s6.act", "s7.act"]}),
        text("answer two"),
    ]


def _answers_retained() -> list[Step]:
    return [
        call("alerts", {}, "c1"),
        text("first answer"),
        text("second answer"),
        text("third answer"),
    ]


def _step_limit() -> list[Step]:
    steps: list[Step] = []
    for i in range(1, 3):
        steps += [
            call("alerts", {}, f"c{i}"),
            edit(f"e{i}", {"op": "add_note", "id": f"n{i}", "body": str(i)}),
        ]
    return steps + [call("alerts", {}, f"d{i}") for i in range(1, 8)]


def _call_cap() -> list[Step]:
    return [edit(f"e{i}", {"op": "add_note", "id": f"n{i}", "body": str(i)}) for i in range(1, 9)]


def _tool_failure() -> list[Step]:
    return [
        call("probe", {"attempt": 0}, "c1"),
        call("probe", {"attempt": 1}, "c2"),
        call("pager", {}, "c3"),
        text("not reached"),
    ]


def _provider_failure() -> list[Step]:
    return [
        call("alerts", {}, "c1"),
        ModelHTTPError(503, "scripted", {"error": "overloaded"}),
        text("not reached"),
    ]


CASES: dict[str, Case] = {
    c.name: c
    for c in [
        Case("read_and_answer_scripted", _read_and_answer, ["What is open?"], usage=None),
        Case("read_and_answer", _read_and_answer, ["What is open?"]),
        Case("edits", _edits, ["Tidy as you go.", "Now try the earlier answer."]),
        Case("multiple_calls", _multiple_calls, ["Read in parallel."]),
        Case(
            "guard_burst",
            _guard_burst,
            ["Review the trace."],
            GuardedSettings(budget_tokens=12_000),
            (2.4, 1.2),
        ),
        Case(
            "pressure_rearm_reset",
            _pressure_rearm_reset,
            ["Think at length.", "And again."],
            GuardedSettings(budget_tokens=9_000),
        ),
        Case("answers_retained", _answers_retained, ["first", "second", "third"]),
        Case("step_limit", _step_limit, ["Keep going."], GuardedSettings(max_steps=5, call_cap=40)),
        Case("call_cap", _call_cap, ["Edit away."], GuardedSettings(call_cap=4)),
        Case("tool_failure", _tool_failure, ["Probe and page."]),
        Case("provider_failure", _provider_failure, ["Go."]),
    ]
}
