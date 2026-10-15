"""The docthread task tools: four read-only shared task tools over one fixture (`tooling.Tool` implementations).

`EvidenceService` serves one session. It logs every call under a canonical key; a refetch is any call whose key
was already logged (the same tool with the same normalised arguments). Refetches are measured, never refused. Outputs are the fixture's text only; an invalid request gets a
short neutral error naming the valid choices, with no answer or scorer information.

The tools are bridged to the model by `clm_lib.p015.tools.bridge`, which validates arguments against the schemas
below and applies the shared 8,000-code-point output cap (fixture outputs stay under it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...tooling import ToolOutcome, ToolSpec
from .fixture import HOURS, Fixture, canonical_call


@dataclass
class _ServiceTool:
    spec: ToolSpec
    service: EvidenceService

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        return self.service.fetch(self.spec.name, arguments)


class EvidenceService:
    def __init__(self, fixture: Fixture) -> None:
        self.fixture = fixture
        self.order: list[str] = []  # every call's canonical key, in call order

    @property
    def repeat_calls(self) -> int:
        return len(self.order) - len(set(self.order))

    def fetch(self, tool: str, args: dict[str, Any]) -> ToolOutcome:
        fx = self.fixture
        args = dict(args)
        for name, value in args.items():
            if isinstance(
                value, str
            ):  # names are matched exactly; playbook sections ignore letter case
                args[name] = value.strip().lower() if name == "section" else value.strip()
        self.order.append(canonical_call(tool, args))
        if tool == "read_journal":
            if args["component"] not in fx.components:
                return ToolOutcome(f"no component named {args['component']!r}", True, "not_found")
            pages = fx.pages(args["component"], args["hour"])
            if not 1 <= args["page"] <= pages:
                return ToolOutcome(
                    f"page {args['page']} does not exist; this journal has {pages} pages",
                    True,
                    "not_found",
                )
            return ToolOutcome(fx.journal_page(args["component"], args["hour"], args["page"]))
        if tool == "list_changes":
            return ToolOutcome(fx.change_log(args["component"]))
        if tool == "queue_stats":
            return ToolOutcome(fx.queue_table(args["queue"], args["hour"]))
        if tool == "read_playbook":
            text = fx.playbook_section(args["section"])
            if text is None:
                return ToolOutcome(
                    f"no playbook section {args['section']!r}; read 'contents' for the list",
                    True,
                    "not_found",
                )
            return ToolOutcome(text)
        raise KeyError(tool)

    def tools(self) -> list[_ServiceTool]:
        fx = self.fixture
        components = fx.components
        hour = {
            "type": "string",
            "enum": list(HOURS),
            "description": "The hour's start, UTC (each covers one hour).",
        }
        where = f"Components: {', '.join(components)}. Hours (UTC, {fx.date}): {', '.join(HOURS)}."
        specs = [
            ToolSpec(
                "read_journal",
                "Returns the job-event journal of one component for one hour, a page at a time (earliest events "
                f"first; each page's first line gives the page count for that hour). {where}",
                {
                    "type": "object",
                    "properties": {
                        "component": {
                            "type": "string",
                            "enum": components,
                            "description": "The component.",
                        },
                        "hour": hour,
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "The page number, from 1.",
                        },
                    },
                    "required": ["component", "hour", "page"],
                    "additionalProperties": False,
                },
                "task",
                "none",
            ),
            ToolSpec(
                "list_changes",
                "Every configuration change applied to one component on the incident day, latest change at the top. "
                f"Components: {', '.join(components)}.",
                {
                    "type": "object",
                    "properties": {
                        "component": {
                            "type": "string",
                            "enum": components,
                            "description": "The component.",
                        }
                    },
                    "required": ["component"],
                    "additionalProperties": False,
                },
                "task",
                "none",
            ),
            ToolSpec(
                "queue_stats",
                "Throughput, rejections, depth and waiting times of one queue during one hour (a row per ten minutes, "
                "then totals for the hour). "
                f"Queues: {', '.join(fx.queues)}. Hours (UTC, {fx.date}): {', '.join(HOURS)}.",
                {
                    "type": "object",
                    "properties": {
                        "queue": {"type": "string", "enum": fx.queues, "description": "The queue."},
                        "hour": hour,
                    },
                    "required": ["queue", "hour"],
                    "additionalProperties": False,
                },
                "task",
                "none",
            ),
            ToolSpec(
                "read_playbook",
                "Fetch one named section of the operations playbook; ask for 'contents' to see what sections exist.",
                {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "minLength": 1,
                            "description": "The section name.",
                        }
                    },
                    "required": ["section"],
                    "additionalProperties": False,
                },
                "task",
                "none",
            ),
        ]
        return [_ServiceTool(spec, self) for spec in specs]


__all__ = ["EvidenceService", "canonical_call"]
