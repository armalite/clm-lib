"""A scripted docthread investigator, for offline integration checks only (never model-performance evidence).

It is a Pydantic AI `FunctionModel` that reads the request it is given (guarded-clm's rebuilt request: the task with
the current question, the working context and the runtime status) and acts in a fixed, natural order:

- turn 1: the intake journal for the incident hour until the first customer rejection, then the rejecting
  component's journal up to its first rejection;
- turn 2: every component's change log;
- turn 3: the rejecting component's queue statistics;
- turn 4: that component's journal pages after the first rejection, up to the explicit evidence line;
- turn 6: the playbook's contents, symptom catalogue and matching section;
- turns 5, 7 and 8 (memory questions): answer from the working context if the fact is still visible there,
  otherwise read the output that held it again.

It answers with the true values, or with values given in `overrides` for faulty-answer checks. It knows where facts are
because it is scripted from the fixture; a real model would not.

Policies (what it does with its working context):
- `tidy`: in every response that reads something, it first deletes the previous steps' entries (`edit_context`
  listed before the read in the same response), so the context stays small;
- `keep`: it never edits unless the guard has held an output back, in which case it deletes the previous steps'
  entries and calls again.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from .fixture import HOURS, INCIDENT_HOUR, ROOT_PAGES, Fixture, canonical_call, mentions
from .scoring import answer_block, true_answers
from .task import TASK_V1, Question, questions

Policy = Literal["tidy", "keep"]
Call = tuple[str, dict[str, Any]]


def plan(fx: Fixture) -> dict[int, list[Call]]:
    root, intake = fx.root(), fx.names["intake"]
    first, ev = fx.layout["first_rejection_page"], fx.layout["evidence_page"]
    journal = [
        ("read_journal", {"component": intake, "hour": INCIDENT_HOUR, "page": p})
        for p in range(1, fx.layout["intake_first_rejected_page"] + 1)
    ]
    journal += [
        ("read_journal", {"component": root, "hour": INCIDENT_HOUR, "page": p})
        for p in range(1, first + 1)
    ]
    assert ev <= ROOT_PAGES and set(HOURS)
    return {
        1: journal,
        2: [("list_changes", {"component": c}) for c in fx.components],
        3: [("queue_stats", {"queue": f"q.{root}", "hour": INCIDENT_HOUR})],
        4: [
            ("read_journal", {"component": root, "hour": INCIDENT_HOUR, "page": p})
            for p in range(first + 1, ev + 1)
        ],
        6: [
            ("read_playbook", {"section": s})
            for s in ("contents", "symptoms", fx.mechanism.symptom_section)
        ],
    }


def _parse_key(key: str) -> Call:
    tool, rest = key.split("(", 1)
    args: dict[str, Any] = {}
    for part in rest.rstrip(")").split(", "):
        k, v = part.split("=", 1)
        args[k] = int(v) if v.isdigit() else v
    return tool, args


class Investigator:
    def __init__(
        self,
        fx: Fixture,
        policy: Policy = "tidy",
        overrides: dict[int, dict[str, Any]] | None = None,
        task_version: str = TASK_V1,
    ) -> None:
        self.fx, self.policy, self.overrides = fx, policy, overrides or {}
        self.questions: list[Question] = questions(fx, task_version)
        self.plans = plan(fx)
        self.turn = 0
        self.position = 0
        self.calls = 0
        self.last_call: Call | None = None

    def model(self) -> FunctionModel:
        return FunctionModel(self.respond, model_name="scripted-investigator")

    # ------------------------------------------------------------------ reading the rebuilt request
    @staticmethod
    def _prompt(messages: list[ModelMessage]) -> str:
        request = messages[-1] if isinstance(messages[-1], ModelRequest) else messages[0]
        return next(
            str(p.content)
            for p in request.parts
            if isinstance(p, UserPromptPart) and str(p.content).startswith("<task>")
        )

    def _current_turn(self, prompt: str) -> int:
        task = prompt.split("</task>", 1)[0]
        current = task.split("Current request:\n", 1)[1] if "Current request:\n" in task else task
        for q in reversed(self.questions):
            if q.text in current:
                return q.number
        raise AssertionError("no turn question found in the request")

    @staticmethod
    def _entries(prompt: str) -> list[dict[str, Any]]:
        body = prompt.split("<working_context>\n", 1)[1].split("</working_context>", 1)[0]
        return [
            json.loads(line.replace("\\u003c", "<")) for line in body.splitlines() if line.strip()
        ]

    # ------------------------------------------------------------------ acting
    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        prompt = self._prompt(messages)
        number = self._current_turn(prompt)
        if number != self.turn:
            self.turn, self.position, self.last_call = number, 0, None
        entries = self._entries(prompt)
        steps = [e for e in entries if e["id"].startswith("s") and not e["id"].startswith("s0")]
        latest = max(
            (e for e in steps if e["id"].endswith(".obs")),
            key=lambda e: int(e["id"][1:].split(".")[0]),
            default=None,
        )
        held = (
            self.last_call is not None
            and latest is not None
            and f"Held back: {self.last_call[0]}(" in latest["body"]
        )
        turn = self.questions[number - 1]
        calls = self.plans.get(number)
        if calls is None:  # a memory turn: answer from context, or read the source again
            fname = turn.asks[0].key
            visible = any(mentions(e["body"], self.fx.facts[fname]) for e in entries)
            source = _parse_key(self.fx.sources[fname][-1])
            if not visible and self.last_call != source:
                return self._act(source, steps, tidy=self.policy == "tidy")
            if held:
                return self._act(source, steps, tidy=True)
            return self._answer(turn)
        if held:
            assert self.last_call is not None
            return self._act(self.last_call, steps, tidy=True)
        if self.last_call is not None:
            self.position += 1
        if self.position >= len(calls):
            return self._answer(turn)
        return self._act(calls[self.position], steps, tidy=self.policy == "tidy")

    def _act(self, call: Call, steps: list[dict[str, Any]], *, tidy: bool) -> ModelResponse:
        self.calls += 1
        self.last_call = call
        parts: list[Any] = []
        old = [e["id"] for e in steps]
        if tidy and old:
            parts.append(
                ToolCallPart(
                    "edit_context", {"ops": [{"op": "delete", "ids": old}]}, f"e{self.calls}"
                )
            )
        parts.append(ToolCallPart(call[0], dict(call[1]), f"c{self.calls}"))
        return ModelResponse(parts=parts)

    def _answer(self, turn: Question) -> ModelResponse:
        values = true_answers(self.fx, turn) | self.overrides.get(turn.number, {})
        self.last_call = None
        return ModelResponse(
            parts=[
                TextPart(f"Scripted answer for question {turn.number}.\n\n{answer_block(values)}")
            ]
        )


__all__ = ["Investigator", "canonical_call", "plan"]
