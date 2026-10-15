"""Offline docthread sessions through the real p015 path (GuardedSession, Pydantic AI, bridged task tools,
accounting, records), with the scripted investigator. Integration evidence only: scripted success says nothing about
any model's performance.

    python -m clm_lib.p015.docthread.offline OUT_DIR [--policy tidy|keep] [--instance docthread-dev-1]

Measurements come from the session's own records: request sizes (characters) and the capability's size estimate
(its status line), reported input tokens when synthetic usage is on, cumulative evidence exposure (characters of
delivered tool output), peak request, PRESSURE and OVER LIMIT notices, held-back outputs, refetches and edits.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from ...budget import Ledger, ModelPrice
from ..records import read_jsonl
from ..session import GuardedSession, GuardedSettings
from .agent import Investigator, Policy
from .fixture import DEV_INSTANCES, GENERATOR_VERSION, Fixture, instance
from .scoring import SCORER_VERSION, score_session
from .task import SYSTEM_PROMPT, TASK_VERSION, questions, user_message
from .tools import EvidenceService

SYNTHETIC_CHARS_PER_TOKEN = 2.6  # declared, synthetic: not a measurement of any tokenizer


class SyntheticUsageModel(FunctionModel):
    """Reports synthetic provider-like input tokens (characters / 2.6) from a model whose `system` is not a scripted
    one, so guarded-clm calibrates its estimator on them, as it would on a provider."""

    @property
    def system(self) -> str:
        return "scripted-synthetic-usage"


@dataclasses.dataclass
class Plan:
    instance: str = "docthread-dev-1"
    policy: Policy = "tidy"
    synthetic_usage: bool = True
    settings: GuardedSettings = dataclasses.field(default_factory=GuardedSettings)
    fail_at_attempt: int | None = (
        None  # a scripted provider failure (HTTP 503) at this attempt (0-based)
    )
    refuse_after_turn: int | None = None  # the ledger allows no more spend after this turn
    overrides: dict[int, dict[str, Any]] = dataclasses.field(default_factory=dict)


def _model(inv: Investigator, plan: Plan) -> FunctionModel:
    attempts = [0]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        index = attempts[0]
        attempts[0] += 1
        if plan.fail_at_attempt is not None and index == plan.fail_at_attempt:
            raise ModelHTTPError(503, "scripted-investigator", {"error": "scripted overload"})
        response = inv.respond(messages, info)
        if plan.synthetic_usage:
            chars = sum(len(str(getattr(p, "content", ""))) for m in messages for p in m.parts)
            chars += len(info.instructions or "") + len(
                json.dumps([t.parameters_json_schema for t in info.function_tools])
            )
            response.usage = RequestUsage(
                input_tokens=math.ceil(chars / SYNTHETIC_CHARS_PER_TOKEN), output_tokens=40
            )
        return response

    cls = SyntheticUsageModel if plan.synthetic_usage else FunctionModel
    return cls(respond, model_name="scripted-investigator")


def run(plan: Plan, out_dir: Path, run_id: str | None = None) -> dict[str, Any]:
    fx: Fixture = instance(plan.instance)
    desk = EvidenceService(fx)
    inv = Investigator(fx, plan.policy, plan.overrides)
    run_id = run_id or f"{plan.instance}-{plan.policy}"
    ledger = Ledger.open(
        out_dir / f"{run_id}-ledger.json", 100.0
    )  # a temporary test ledger, never the active one
    price = ModelPrice("scripted", 2.0, 2.5, 4.0, 0.2, 10.0, "offline placeholder", "offline")
    session = GuardedSession(
        _model(inv, plan),
        run_dir=out_dir / run_id,
        run_id=run_id,
        ledger=ledger,
        price=price,
        settings=plan.settings,
        system_prompt=SYSTEM_PROMPT,
        task_tools=desk.tools(),
        conversation_id=f"{plan.instance}-conversation",
    )
    replies: list[str | None] = []
    calls_by_turn: list[list[str]] = []
    endings: list[str] = []
    for q in questions(fx):
        start = len(desk.order)
        outcome = session.run_turn(user_message(q))
        calls_by_turn.append(desk.order[start:])
        endings.append(outcome.ending)
        replies.append(outcome.output if outcome.ending == "completed" else None)
        if outcome.ending != "completed":
            break
        if plan.refuse_after_turn == q.number:
            ledger.limit_usd = ledger.spent_usd - ledger.start_spent_usd
    score = score_session(fx, replies, calls_by_turn)
    measurements = measure(out_dir / run_id, desk, calls_by_turn)
    session.recorder.write_meta(
        task={
            "generator": GENERATOR_VERSION,
            "task": TASK_VERSION,
            "scorer": SCORER_VERSION,
            "instance": plan.instance,
            "seed": fx.seed,
            "mechanism": fx.mechanism.key,
            "policy": f"scripted-investigator:{plan.policy}",
            "synthetic_usage": plan.synthetic_usage,
        },
        score=score,
        measurements=measurements,
        calls_by_turn=calls_by_turn,
    )
    return {
        "instance": plan.instance,
        "policy": plan.policy,
        "synthetic_usage": plan.synthetic_usage,
        "endings": endings,
        "score": score,
        "measurements": measurements,
    }


_SIZE = re.compile(r"request size: about ([\d,]+) of ([\d,]+) tokens")


def measure(run_dir: Path, desk: EvidenceService, calls_by_turn: list[list[str]]) -> dict[str, Any]:
    attempts = read_jsonl(run_dir / "attempts.jsonl")
    trace = read_jsonl(run_dir / "trace.jsonl")
    tools = read_jsonl(run_dir / "tools.jsonl")
    requests: list[dict[str, Any]] = []
    for a in attempts:
        if a["event"] != "started":
            continue
        parts = [p for m in a["messages"] for p in m["parts"]]
        prompt = next((p["content"] for p in parts if p.get("part_kind") == "user-prompt"), "")
        status = prompt.split("<runtime_status>\n", 1)[-1]
        m = _SIZE.search(status)
        requests.append(
            {
                "chars": sum(len(str(p.get("content", ""))) for p in parts),
                "estimated_tokens": int(m.group(1).replace(",", "")) if m else None,
                "pressure": "PRESSURE:" in status,
                "over_limit": "OVER LIMIT:" in status,
            }
        )
    reported = [
        a["reported"]["input_tokens_total"]
        for a in attempts
        if a["event"] == "finished" and a["outcome"] == "ok"
    ]
    kinds: dict[str, int] = {}
    for r in trace:
        kinds[r["type"]] = kinds.get(r["type"], 0) + 1
    delivered = [len(t["delivered"]) for t in tools if t["event"] == "result"]
    held = [r for r in trace if r["type"] == "lite_held_back"]
    return {
        "requests": len(requests),
        "peak_request_chars": max((r["chars"] for r in requests), default=0),
        "peak_estimated_tokens": max((r["estimated_tokens"] or 0 for r in requests), default=0),
        "peak_reported_tokens": max(reported, default=None),
        "requests_with_pressure": sum(r["pressure"] for r in requests),
        "requests_over_limit": sum(r["over_limit"] for r in requests),
        "evidence_exposure_chars": sum(delivered),
        "tool_results": len(delivered),
        "largest_tool_result_chars": max(delivered, default=0),
        "held_back": len(held),
        "refetches": desk.repeat_calls,
        "calls_per_turn": [len(c) for c in calls_by_turn],
        "trace_records": dict(sorted(kinds.items())),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--policy", default="tidy", choices=["tidy", "keep"])
    parser.add_argument("--instance", default="docthread-dev-1", choices=sorted(DEV_INSTANCES))
    parser.add_argument(
        "--fixed-ratio", action="store_true", help="use the capability's scripted-model estimates"
    )
    args = parser.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    result = run(Plan(args.instance, args.policy, not args.fixed_ratio), args.out_dir)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "score"}
            | {"points": result["score"]["points"]},
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
