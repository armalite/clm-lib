"""Tool interfaces for the native-tool transport (``native-tools/1``, programme 015).

Opt-in and separate from the single-action protocol: no historical configuration, prompt or
schema uses anything here.

Every tool declares two independent properties:

- ``category``: ``"task"`` for shared task tools that every arm of a comparison gets unchanged
  (the incident thread's search and page-read tools will be these), or ``"context"`` for
  context-management tools that only some arms get (``context_read`` / ``context_apply``).
- ``side_effects``: what invoking it may change. ``"none"`` (read-only), ``"workspace"`` (files in
  the run workspace, e.g. a general shell) or ``"context"`` (the working context itself).

A general shell is therefore a task tool with workspace side effects; ``context_read`` is a
context tool without side effects; ``context_apply`` is a context tool with context side effects.

This module defines interfaces and argument validation only. It does not decide whether several
calls in one response are executed, in what order, or how steps are counted: those are arm
policies (see ``counters.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ToolCategory = Literal["task", "context"]
SideEffects = Literal["none", "workspace", "context"]
CATEGORIES: tuple[str, ...] = ("task", "context")
SIDE_EFFECTS: tuple[str, ...] = ("none", "workspace", "context")
# The Messages API tool-name pattern.
TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    category: ToolCategory
    side_effects: SideEffects

    def __post_init__(self) -> None:
        if not TOOL_NAME.match(self.name):
            raise ValueError(f"invalid tool name {self.name!r}")
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown tool category {self.category!r}")
        if self.side_effects not in SIDE_EFFECTS:
            raise ValueError(f"unknown side-effect class {self.side_effects!r}")
        if self.input_schema.get("type") != "object":
            raise ValueError("a tool input schema must be a JSON object schema")
        if not self.description.strip():
            raise ValueError("a tool needs a description")

    def to_api(self) -> dict[str, Any]:
        """The Messages API tool definition. Category and side effects are runtime metadata and
        are never sent to the model."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class ToolOutcome:
    """What a tool implementation returns, before any output cap is applied.

    ``content`` is the tool's own result text (for a failed invocation, its error message).
    """

    content: str
    is_error: bool = False
    error_kind: str | None = None  # e.g. "invalid_arguments", "tool_exception", "not_found"


class Tool(Protocol):
    spec: ToolSpec

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome: ...


@dataclass
class ToolRegistry:
    """An ordered set of tools. The order is the order of the request's ``tools`` list, which is
    part of the cached request prefix, so it is fixed at construction."""

    specs: tuple[ToolSpec, ...] = ()
    _by_name: dict[str, ToolSpec] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for s in self.specs:
            if s.name in self._by_name:
                raise ValueError(f"duplicate tool name {s.name!r}")
            self._by_name[s.name] = s

    def get(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)

    def names(self, category: ToolCategory | None = None) -> tuple[str, ...]:
        return tuple(s.name for s in self.specs if category is None or s.category == category)

    def to_api(self) -> tuple[dict[str, Any], ...]:
        return tuple(s.to_api() for s in self.specs)


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def validate_arguments(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """Check tool arguments against the JSON-Schema subset tool definitions here use: ``type``,
    ``properties``, ``required``, ``additionalProperties: false``, ``items``, ``enum``,
    ``minimum``/``maximum`` and ``minLength``/``maxLength``. Returns problems (empty when valid).

    The provider does not guarantee schema-valid arguments (no strict mode is assumed), so every
    call is validated before it is executed. Unsupported keywords raise, so a schema can never be
    silently under-checked.
    """
    known = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "description",
    }
    unknown = set(schema) - known
    if unknown:
        raise ValueError(f"unsupported schema keywords at {path}: {sorted(unknown)}")
    problems: list[str] = []
    t = schema.get("type")
    if t is not None:
        allowed = _JSON_TYPES[t]
        # bool is an int subclass in Python, but not a JSON integer or number.
        bad_bool = isinstance(value, bool) and t in ("integer", "number")
        if not isinstance(value, allowed) or bad_bool:
            return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{path}: missing required {name!r}")
        for name, v in value.items():
            if name in props:
                problems += validate_arguments(props[name], v, f"{path}.{name}")
            elif schema.get("additionalProperties", True) is False:
                problems.append(f"{path}: unexpected property {name!r}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            problems += validate_arguments(schema["items"], v, f"{path}[{i}]")
    return problems
