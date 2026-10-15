"""Bridge from clm-lib's shared task-tool interface (`tooling.py`) to Pydantic AI tools (decision G5).

What the model sees is exactly the `ToolSpec`: its name, description and argument schema (`Tool.from_schema`).

- **Validation:** arguments are checked against the schema by `tooling.validate_arguments` in Pydantic AI's
  `args_validator` hook, before the tool runs. Invalid arguments raise `ModelRetry`, so Pydantic AI returns its
  usual retry prompt to the model. The retry text is ours ("invalid arguments: ..."), not pydantic's validation
  wording; that is the one model-visible transformation.
- **Results:** the tool's text is capped by `tool-output-cap/1` (8,000 code points by default; framing-free single-text
  results keep their head and gain a truncation notice). Nothing else is added: no ids, scorer data or debug text.
- **Errors:** a `ToolOutcome` with `is_error` is returned as its text, like any result. An exception raised by the
  implementation propagates unchanged and, as Pydantic AI does for any tool, ends the run; the recorder notes it
  first so the ending can be classified as a tool failure.
- **Execution:** the function is a plain synchronous callable, so Pydantic AI runs it in a worker thread,
  concurrently with other calls of the same response, as it does for any synchronous tool. Nothing is serialised.
- **Scope:** only read-only task tools (`category == "task"`, `side_effects == "none"`) are bridged, because the
  capability's guard runs a tool before deciding whether its output fits (guarded-clm SPEC 13.1).
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic_ai import ModelRetry, RunContext, Tool

from ..output_cap import DEFAULT_CAP, cap_text
from ..tooling import Tool as TaskTool
from ..tooling import validate_arguments


class ToolSink(Protocol):
    def tool_result(self, entry: dict[str, Any]) -> None: ...

    def tool_raised(self, name: str, exc: BaseException) -> None: ...


def bridge(
    task_tool: TaskTool, *, sink: ToolSink | None = None, cap: int = DEFAULT_CAP
) -> Tool[Any]:
    spec = task_tool.spec
    if spec.category != "task" or spec.side_effects != "none":
        raise ValueError(
            f"{spec.name}: only read-only task tools can be bridged (got {spec.category}/{spec.side_effects})"
        )

    def validate(ctx: RunContext[Any], **kwargs: Any) -> None:
        del ctx
        problems = validate_arguments(spec.input_schema, kwargs)
        if problems:
            raise ModelRetry("invalid arguments: " + "; ".join(problems))

    def run(**kwargs: Any) -> str:
        try:
            outcome = task_tool.invoke(kwargs)
        except BaseException as exc:
            if sink is not None:
                sink.tool_raised(spec.name, exc)
            raise
        text, record = cap_text(outcome.content, cap)
        if sink is not None:
            sink.tool_result(
                {
                    "tool": spec.name,
                    "arguments": kwargs,
                    "is_error": outcome.is_error,
                    "error_kind": outcome.error_kind,
                    "delivered": text,
                    "cap": record.to_dict(),
                }
            )
        return text

    return Tool.from_schema(
        run,
        name=spec.name,
        description=spec.description,
        json_schema=spec.input_schema,
        takes_ctx=False,
        args_validator=validate,
    )
