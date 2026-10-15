"""The task-tool bridge: what the model sees, validation, the output cap, failures and concurrency."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from clm_lib.budget import Ledger
from clm_lib.p015.records import read_jsonl
from clm_lib.p015.session import GuardedSession
from clm_lib.p015.tools import bridge
from clm_lib.tooling import ToolOutcome, ToolSpec

LOOKUP = ToolSpec(
    "lookup_host",
    "Look up a host's current state (synthetic fixture).",
    {
        "type": "object",
        "properties": {"host": {"type": "string", "minLength": 1}, "verbose": {"type": "boolean"}},
        "required": ["host"],
        "additionalProperties": False,
    },
    "task",
    "none",
)


@dataclass
class Fixture:
    spec: ToolSpec = LOOKUP
    calls: list[dict[str, Any]] = field(default_factory=list)
    delay: float = 0.0
    body: str = ""
    raise_error: bool = False
    windows: list[tuple[float, float]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        start = time.monotonic()
        with self.lock:
            self.calls.append(arguments)
        if self.raise_error:
            raise RuntimeError("host inventory unavailable")
        time.sleep(self.delay)
        with self.lock:
            self.windows.append((start, time.monotonic()))
        if arguments["host"] == "missing":
            return ToolOutcome("no such host", is_error=True, error_kind="not_found")
        return ToolOutcome(self.body or f"{arguments['host']}: healthy, load 0.4")


def scripted(
    *responses: ModelResponse,
) -> tuple[FunctionModel, list[AgentInfo], list[list[ModelMessage]]]:
    infos: list[AgentInfo] = []
    seen: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        infos.append(info)
        seen.append(messages)
        return responses[len(infos) - 1]

    return FunctionModel(respond), infos, seen


def session(tmp_path: Path, model: FunctionModel, fixture: Fixture) -> GuardedSession:
    return GuardedSession(
        model,
        run_dir=tmp_path / "run",
        run_id="t",
        ledger=Ledger.open(tmp_path / "l.json", 10.0),
        price=None,
        task_tools=[fixture],
    )


def use(*calls: tuple[str, dict[str, Any], str]) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(n, a, i) for n, a, i in calls])


def test_the_model_sees_the_spec_unchanged(tmp_path: Path) -> None:
    model, infos, _ = scripted(ModelResponse(parts=[TextPart("ok")]))
    session(tmp_path, model, Fixture()).run_turn("go")
    (tool,) = [t for t in infos[0].function_tools if t.name == "lookup_host"]
    assert (tool.name, tool.description, tool.parameters_json_schema) == (
        LOOKUP.name,
        LOOKUP.description,
        LOOKUP.input_schema,
    )


def test_invalid_arguments_get_a_retry_and_never_run_the_tool(tmp_path: Path) -> None:
    fixture = Fixture()
    model, _, seen = scripted(
        use(("lookup_host", {"host": "", "extra": 1}, "c1")),
        use(("lookup_host", {"host": "db-2"}, "c2")),
        ModelResponse(parts=[TextPart("db-2 is healthy")]),
    )
    outcome = session(tmp_path, model, fixture).run_turn("check db-2")
    assert outcome.ending == "completed" and fixture.calls == [{"host": "db-2"}]
    rebuilt = str(seen[1][0].parts[-1].content)  # type: ignore[union-attr]
    assert "invalid arguments: $.host: shorter than 1; $: unexpected property 'extra'" in rebuilt


def test_results_are_capped_and_recorded_without_annotation(tmp_path: Path) -> None:
    fixture = Fixture(body="x" * 10_000)
    model, _, seen = scripted(
        use(("lookup_host", {"host": "a"}, "c1")),
        use(("lookup_host", {"host": "missing"}, "c2")),
        ModelResponse(parts=[TextPart("done")]),
    )
    session(tmp_path, model, fixture).run_turn("go")
    rows = read_jsonl(tmp_path / "run" / "tools.jsonl")
    assert rows[0]["cap"]["truncated"] and rows[0]["cap"]["delivered_content_codepoints"] == 8_000
    assert (
        rows[0]["delivered"]
        == "x" * 8_000 + "\n[result truncated: 10000 code points produced, first 8000 shown]"
    )
    assert (rows[1]["delivered"], rows[1]["is_error"], rows[1]["error_kind"]) == (
        "no such host",
        True,
        "not_found",
    )
    rebuilt = str(seen[2][0].parts[-1].content)  # type: ignore[union-attr]
    assert (
        "[lookup_host]\\nno such host" in rebuilt
    )  # delivered as the tool's own text, nothing added


def test_a_raising_task_tool_ends_the_run_as_a_tool_failure(tmp_path: Path) -> None:
    model, _, _ = scripted(
        use(("lookup_host", {"host": "a"}, "c1")), ModelResponse(parts=[TextPart("x")])
    )
    outcome = session(tmp_path, model, Fixture(raise_error=True)).run_turn("go")
    assert outcome.ending == "tool_failure" and str(outcome.error) == "host inventory unavailable"
    assert read_jsonl(tmp_path / "run" / "tools.jsonl")[-1]["event"] == "raised"


def test_bridged_calls_still_run_concurrently(tmp_path: Path) -> None:
    """Pydantic AI runs synchronous tools of one response in worker threads; the bridge doesn't serialise them."""
    fixture = Fixture(delay=0.4)
    model, _, _ = scripted(
        use(("lookup_host", {"host": "a"}, "c1"), ("lookup_host", {"host": "b"}, "c2")),
        ModelResponse(parts=[TextPart("done")]),
    )
    session(tmp_path, model, fixture).run_turn("go")
    (_, e1), (s2, _) = sorted(fixture.windows)
    assert s2 < e1  # the second call started before the first finished


def test_only_read_only_task_tools_are_bridged() -> None:
    writer = ToolSpec("write_note", "Write a note.", {"type": "object"}, "task", "workspace")
    with pytest.raises(ValueError, match="read-only"):
        bridge(Fixture(spec=writer))
