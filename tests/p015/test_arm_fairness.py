"""The three core arms show the model the same task: the same system prompt, user turns, task-tool names,
descriptions and schemas, argument validation, tool results and output contract. Only the context-management part
differs (instructions and the arm's own tool). Scripted, network blocked; no sandbox command runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from clm_lib.budget import Ledger, ModelPrice
from clm_lib.p015.arms import ArmSession
from clm_lib.p015.docthread.fixture import instance
from clm_lib.p015.docthread.task import SYSTEM_PROMPT, questions, user_message
from clm_lib.p015.docthread.tools import EvidenceService
from clm_lib.p015.records import read_jsonl
from clm_lib.p015.session import GuardedSession

PRICE = ModelPrice("scripted", 2.0, 2.5, 4.0, 0.2, 10.0, "offline placeholder", "offline")
TASK_TOOLS = {"read_journal", "list_changes", "queue_stats", "read_playbook"}
ARM_TOOLS = {"guarded": {"edit_context"}, "paper_clm_shell": {"bash"}, "token_tail_p015": set()}


def scripted(component: str) -> FunctionModel:
    steps = iter(
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_journal", {"component": "nope", "hour": "11:00", "page": 0}, "c1"
                    )
                ]
            ),
            ModelResponse(parts=[ToolCallPart("list_changes", {"component": component}, "c2")]),
            ModelResponse(parts=[TextPart('Done.\n\n```json\n{"rejecting_component": "x"}\n```')]),
        ]
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return next(steps)

    return FunctionModel(respond, model_name="scripted")


@pytest.fixture(scope="module")
def arms(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("fair")
    fx = instance("docthread-dev-1")
    prompt = user_message(questions(fx)[0])
    dirs = {}
    for arm in ("guarded", "paper_clm_shell", "token_tail_p015"):
        common: dict[str, Any] = dict(
            run_dir=out / arm, run_id=arm, ledger=Ledger.open(out / f"{arm}.json", 100.0), price=PRICE,
            system_prompt=SYSTEM_PROMPT, task_tools=EvidenceService(fx).tools(), conversation_id="c",
        )  # fmt: skip
        session: Any = (
            GuardedSession(scripted(fx.components[0]), **common)
            if arm == "guarded"
            else ArmSession(arm, scripted(fx.components[0]), **common)
        )  # type: ignore[arg-type]
        assert session.run_turn(prompt).ending == "completed"
        dirs[arm] = out / arm
    return dirs


def first_request(run_dir: Path) -> dict[str, Any]:
    return next(a for a in read_jsonl(run_dir / "attempts.jsonl") if a["event"] == "started")


def test_task_tools_are_identical_and_only_the_arm_tool_differs(arms: dict[str, Path]) -> None:
    specs = {}
    for arm, run_dir in arms.items():
        params = first_request(run_dir)["parameters"]
        tools = {t["name"]: t for t in params["function_tools"]}
        assert set(tools) == TASK_TOOLS | ARM_TOOLS[arm], arm
        specs[arm] = {n: tools[n] for n in TASK_TOOLS}
        assert params["output_tools"] == [] and params["allow_text_output"] is True, arm
    assert specs["guarded"] == specs["paper_clm_shell"] == specs["token_tail_p015"]


def test_system_prompt_and_first_question_are_identical(arms: dict[str, Path]) -> None:
    fx = instance("docthread-dev-1")
    prompt = user_message(questions(fx)[0])
    for arm, run_dir in arms.items():
        text = json.dumps(first_request(run_dir)["messages"], ensure_ascii=False)
        assert json.dumps(SYSTEM_PROMPT, ensure_ascii=False)[1:-1] in text, arm
        assert json.dumps(prompt.strip(), ensure_ascii=False)[1:-1] in text, arm
        session = json.loads((run_dir / "session.json").read_text())
        assert (
            session["task_tools"]
            == json.loads((arms["guarded"] / "session.json").read_text())["task_tools"]
        )


def test_argument_handling_and_results_are_identical(arms: dict[str, Path]) -> None:
    delivered = {}
    retries = {}
    for arm, run_dir in arms.items():
        tools = read_jsonl(run_dir / "tools.jsonl")
        delivered[arm] = [(t["tool"], t["arguments"], t["delivered"], t["is_error"]) for t in tools]
        sent = [
            json.dumps(a["messages"], ensure_ascii=False)
            for a in read_jsonl(run_dir / "attempts.jsonl")
            if a["event"] == "started"
        ]
        retries[arm] = "invalid arguments: " in sent[1]
    assert delivered["guarded"] == delivered["paper_clm_shell"] == delivered["token_tail_p015"]
    fx = instance("docthread-dev-1")
    assert delivered["guarded"] == [
        ("list_changes", {"component": fx.components[0]}, fx.change_log(fx.components[0]), False)
    ]
    assert all(
        retries.values()
    )  # the invalid read was refused by the shared validator, with its text, in every arm
