"""A scripted docthread investigator for the paper and summary arms, for offline integration checks only (never
model-performance evidence).

It reads the plain Pydantic AI request those arms send (the pinned first request, then the conversation; a summary
or restated question in the first request; `[context]` notices at the end) and follows the same fixed plan as
`agent.Investigator` (`agent.plan`), answering with the true values (or `overrides`). Memory turns (5, 7, 8) answer
from what is visible in the request, or read the source output again.

Policies for the paper arm (what it does with the mirror file, through `bash`):
- `tidy`: before each read, an edit-only step that deletes every earlier call and its result (user questions and
  final answers stay), so the context stays small;
- `keep`: edits only after a persistent nudge, a rollback or an over-limit notice;
- `never` (stress): never edits, so the arm's rollback and over-limit handling are exercised.
`bad_edits` lists commands used, in order, in place of the first edits (to exercise the edit gate's rejections).

For the summary arm the policy is `none` (it has no context tool). It also plays the summariser: a request whose
system text is the summariser's gets a deterministic extractive summary (the calls made and the answers given, never
other tool-output facts), within the requested length, unless `summary_faults` says otherwise.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ..paper.shell import MIRROR_FILE
from ..summary.capability import SUMMARY_SYSTEM_P015
from .agent import Call, _parse_key, plan
from .fixture import Fixture, mentions
from .scoring import answer_block, true_answers
from .task import Question, questions

PlainPolicy = Literal["tidy", "keep", "never", "none"]

# Deletes every earlier call block and its result block; keeps user blocks, final answers and the last block (the
# running command). Prints nothing, so a successful run is an edit-only step.
TIDY_COMMAND = (
    "python3 - <<'PY'\n"
    "import re\n"
    f"p = {MIRROR_FILE!r}\n"
    "t = open(p).read()\n"
    r"h = list(re.finditer(r'^\[\[CTX_TURN \d+ role=(\w+)\]\][ \t]*$', t, re.M))" "\n"
    "b = [(x.group(1), t[x.start():(h[i + 1].start() if i + 1 < len(h) else len(t))]) for i, x in enumerate(h)]\n"
    "keep, i = [], 0\n"
    "while i < len(b):\n"
    "    if i + 2 < len(b) and b[i][0] == 'assistant' and '<<call id=' in b[i][1] and b[i + 1][0] == 'tool':\n"
    "        i += 2\n"
    "        continue\n"
    "    keep.append(b[i][1])\n"
    "    i += 1\n"
    "open(p, 'w').write(''.join(keep))\n"
    "PY"
)  # fmt: skip

BAD_EDITS = {
    "not_a_mirror": f"printf 'notes\\n' > {MIRROR_FILE}",
    "drop_running_command": (
        "python3 - <<'PY'\n"
        f"p = {MIRROR_FILE!r}\n"
        "t = open(p).read()\n"
        "open(p, 'w').write(t[: t.rindex('[[CTX_TURN ')])\n"
        "PY"
    ),
    "broken_arguments": f"sed -i 's/{{\"component\"/{{component/' {MIRROR_FILE}",
    "orphan_result": (
        "python3 - <<'PY'\n"
        f"p = {MIRROR_FILE!r}\n"
        "t = open(p).read()\n"
        "i = t.index('role=assistant]]')\n"
        "s = t.rindex('[[CTX_TURN ', 0, i)\n"
        "e = t.index('[[CTX_TURN ', i)\n"
        "open(p, 'w').write(t[:s] + t[e:])\n"
        "PY"
    ),
}


def _texts(message: ModelMessage) -> list[str]:
    out: list[str] = []
    for p in message.parts:
        if isinstance(p, UserPromptPart | SystemPromptPart):
            out.append(str(p.content))
        elif isinstance(p, ToolReturnPart):
            out.append(p.model_response_str())
        elif isinstance(p, TextPart):
            out.append(p.content)
        elif isinstance(p, ToolCallPart):
            out.append(p.args_as_json_str())
    return out


class PlainInvestigator:
    def __init__(
        self,
        fx: Fixture,
        policy: PlainPolicy = "tidy",
        overrides: dict[int, dict[str, Any]] | None = None,
        bad_edits: list[str] | None = None,
        summary_faults: dict[int, str] | None = None,
    ) -> None:
        self.fx, self.policy, self.overrides = fx, policy, overrides or {}
        self.bad_edits = list(bad_edits or [])
        self.summary_faults = (
            summary_faults or {}
        )  # summariser call number (from 1) -> "too_long" | "empty"
        self.questions: list[Question] = questions(fx)
        self.plans = plan(fx)
        self.turn = 0
        self.position = 0
        self.calls = 0
        self.summaries = 0
        self.last_id: str | None = None
        self.last_kind: str | None = None
        self.memory_read = False

    def model(self) -> FunctionModel:
        return FunctionModel(self.respond, model_name="scripted-investigator")

    # ------------------------------------------------------------------ reading the request
    def _current(self, messages: list[ModelMessage]) -> int:
        prompts = [
            str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart)
        ]
        visible = [q.number for q in self.questions if any(q.text in t for t in prompts)]
        if not visible:
            raise AssertionError("no question visible in the request")
        return max(visible)

    @staticmethod
    def _notices(messages: list[ModelMessage]) -> list[str]:
        last = messages[-1]
        return [
            str(p.content)
            for p in last.parts
            if isinstance(p, UserPromptPart) and str(p.content).startswith("[context]")
        ]

    @staticmethod
    def _has_old_results(messages: list[ModelMessage]) -> bool:
        return any(isinstance(p, ToolReturnPart) for m in messages[1:] for p in m.parts)

    # ------------------------------------------------------------------ acting
    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        first = messages[0]
        if any(
            isinstance(p, SystemPromptPart) and p.content == SUMMARY_SYSTEM_P015
            for p in first.parts
        ):
            return self._summarise(messages)
        number = self._current(messages)
        if number != self.turn:
            self.turn, self.position, self.last_id, self.last_kind, self.memory_read = (
                number,
                0,
                None,
                None,
                False,
            )
        notices = "\n".join(self._notices(messages))
        rolled = "rolled back" in notices
        pressed = rolled or "only about" in notices or "over the" in notices or "within" in notices
        answered = {p.tool_call_id for p in messages[-1].parts if isinstance(p, ToolReturnPart)}
        if self.last_id is not None and self.last_id in answered:
            if self.last_kind == "read":
                if number in self.plans:
                    self.position += 1
                else:
                    self.memory_read = True
        elif self.last_kind == "edit" and not rolled:
            pass  # an edit whose result was removed by its own edit cannot happen (the running block stays)
        previous = self.last_kind
        self.last_id, self.last_kind = None, None
        wants_edit = self.policy == "tidy" or (self.policy == "keep" and pressed)
        if wants_edit and previous != "edit" and self._has_old_results(messages):
            return self._edit()
        turn = self.questions[number - 1]
        calls = self.plans.get(number)
        if calls is None:
            fname = turn.asks[0].key
            text = "\n".join(t for m in messages for t in _texts(m))
            if mentions(text, self.fx.facts[fname]) or self.memory_read:
                return self._answer(turn)
            return self._read(_parse_key(self.fx.sources[fname][-1]))
        if self.position >= len(calls):
            return self._answer(turn)
        return self._read(calls[self.position])

    def _edit(self) -> ModelResponse:
        self.calls += 1
        command = self.bad_edits.pop(0) if self.bad_edits else TIDY_COMMAND
        self.last_id, self.last_kind = f"b{self.calls}", "edit"
        return ModelResponse(parts=[ToolCallPart("bash", {"command": command}, self.last_id)])

    def _read(self, call: Call) -> ModelResponse:
        self.calls += 1
        self.last_id, self.last_kind = f"c{self.calls}", "read"
        return ModelResponse(parts=[ToolCallPart(call[0], dict(call[1]), self.last_id)])

    def _answer(self, turn: Question) -> ModelResponse:
        values = true_answers(self.fx, turn) | self.overrides.get(turn.number, {})
        return ModelResponse(
            parts=[
                TextPart(f"Scripted answer for question {turn.number}.\n\n{answer_block(values)}")
            ]
        )

    # ------------------------------------------------------------------ the summariser
    def _summarise(self, messages: list[ModelMessage]) -> ModelResponse:
        self.summaries += 1
        user = next(str(p.content) for p in messages[0].parts if isinstance(p, UserPromptPart))
        m = re.search(r"at most (\d+) characters", user)
        limit = int(m.group(1)) if m else 600
        transcript = user.split("<transcript_to_summarise>\n", 1)[1].split(
            "\n</transcript_to_summarise>", 1
        )[0]
        lines: list[str] = []
        calls: list[str] = []
        for raw in transcript.splitlines():
            if not raw.strip():
                continue
            entry = json.loads(raw)
            if entry["role"] == "summary":
                lines.append(entry["body"])
            for c in re.finditer(
                r"<<call id=\S+ tool=(\S+)>>\n(.*?)\n<<end call>>", entry["body"], re.DOTALL
            ):
                calls.append(f"{c.group(1)}{c.group(2)}")
            for a in re.finditer(r"```json\n(.*?)\n```", entry["body"], re.DOTALL):
                lines.append("answered: " + " ".join(a.group(1).split()))
        if calls:
            lines.append("read: " + "; ".join(calls))
        text = "\n".join(lines) or "nothing yet"
        fault = self.summary_faults.get(self.summaries)
        if fault == "empty":
            text = ""
        elif fault == "too_long":
            text = (text + "\n") * (
                1 + 200_000 // max(len(text), 1)
            )  # far over any acceptance limit
        else:
            text = text[:limit]
        return ModelResponse(parts=[TextPart(text)])
