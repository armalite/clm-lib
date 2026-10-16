"""Offline docthread sessions for the paper and summary arms, through the real p015 path (`ArmSession`, Pydantic AI,
the bridged task tools, accounting, records; for the paper arm, the real Docker sandbox), with the scripted
investigator (`plain_agent`). Integration evidence only: scripted success says nothing about any model's performance.

    python -m clm_lib.p015.docthread.arms_offline OUT_DIR --arm paper_clm_shell|token_tail_p015 [--policy ...]
        [--instance docthread-dev-1] [--fixed-ratio]

Measurements come from the session's own records (request sizes in characters, the arm's estimate from its trace,
notices, edits, rollbacks, summaries, refetches, evidence exposure, and attempts and cost by category).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
from pathlib import Path
from typing import Any

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from ...budget import Ledger, ModelPrice
from ..arms import Arm, ArmSession
from ..paper.capability import PaperSettings
from ..records import read_jsonl
from ..summary.capability import SummarySettings
from .fixture import DEV_INSTANCES, GENERATOR_VERSION, Fixture, instance
from .offline import SYNTHETIC_CHARS_PER_TOKEN, SyntheticUsageModel
from .plain_agent import PlainInvestigator, PlainPolicy
from .scoring import SCORER_VERSION, score_session
from .task import SYSTEM_PROMPT, TASK_VERSION, questions, user_message
from .tools import EvidenceService


@dataclasses.dataclass
class ArmPlan:
    arm: Arm = "paper_clm_shell"
    instance: str = "docthread-dev-1"
    policy: PlainPolicy = "tidy"
    synthetic_usage: bool = True
    label: str = "ordinary"  # "stress" when settings or faults are chosen to exercise a pressure or failure path
    paper: PaperSettings = dataclasses.field(default_factory=PaperSettings)
    summary: SummarySettings = dataclasses.field(default_factory=SummarySettings)
    fail_at_attempt: int | None = (
        None  # a scripted provider failure (HTTP 503) at this attempt (0-based, any category)
    )
    refuse_after_turn: int | None = None  # the ledger allows no more spend after this turn
    bad_edits: list[str] = dataclasses.field(default_factory=list)
    summary_faults: dict[int, str] = dataclasses.field(default_factory=dict)
    overrides: dict[int, dict[str, Any]] = dataclasses.field(default_factory=dict)


def _model(inv: PlainInvestigator, plan: ArmPlan) -> FunctionModel:
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


def run(plan: ArmPlan, out_dir: Path, run_id: str | None = None) -> dict[str, Any]:
    fx: Fixture = instance(plan.instance)
    desk = EvidenceService(fx)
    inv = PlainInvestigator(fx, plan.policy, plan.overrides, plan.bad_edits, plan.summary_faults)
    run_id = run_id or f"{plan.instance}-{plan.arm}-{plan.policy}"
    ledger = Ledger.open(
        out_dir / f"{run_id}-ledger.json", 100.0
    )  # a temporary test ledger, never the active one
    price = ModelPrice("scripted", 2.0, 2.5, 4.0, 0.2, 10.0, "offline placeholder", "offline")
    session = ArmSession(
        plan.arm,
        _model(inv, plan),
        run_dir=out_dir / run_id,
        run_id=run_id,
        ledger=ledger,
        price=price,
        paper=plan.paper,
        summary=plan.summary,
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
            "policy": f"scripted-plain-investigator:{plan.policy}",
            "synthetic_usage": plan.synthetic_usage,
            "label": plan.label,
            "bad_edits": len(plan.bad_edits),
            "summary_faults": plan.summary_faults,
        },
        score=score,
        measurements=measurements,
        calls_by_turn=calls_by_turn,
    )
    return {
        "arm": plan.arm,
        "instance": plan.instance,
        "policy": plan.policy,
        "label": plan.label,
        "synthetic_usage": plan.synthetic_usage,
        "endings": endings,
        "score": score,
        "measurements": measurements,
        "run_id": run_id,
    }


def measure(run_dir: Path, desk: EvidenceService, calls_by_turn: list[list[str]]) -> dict[str, Any]:
    attempts = read_jsonl(run_dir / "attempts.jsonl")
    trace = read_jsonl(run_dir / "trace.jsonl")
    tools = read_jsonl(run_dir / "tools.jsonl")
    started = [a for a in attempts if a["event"] == "started"]
    finished = [a for a in attempts if a["event"] == "finished"]
    main = [a for a in started if a["category"] == "main"]
    chars = [
        sum(len(str(p.get("content", ""))) for m in a["messages"] for p in m["parts"]) for a in main
    ]
    requests = [r for r in trace if r["type"] in ("paper_request", "summary_arm_request")]
    notices = [n for r in requests for n in r["notices"]]
    kinds: dict[str, int] = {}
    for r in trace:
        kinds[r["type"]] = kinds.get(r["type"], 0) + 1
    delivered = [len(t["delivered"]) for t in tools if t["event"] == "result"]
    commands = [r for r in trace if r["type"] == "paper_command"]
    by_category: dict[str, dict[str, Any]] = {}
    for a in finished:
        c = by_category.setdefault(a["category"], {"attempts": 0, "input_tokens": 0, "output_tokens": 0,
                                                   "cache_read_tokens": 0, "cache_write_tokens": 0,
                                                   "latency_s": 0.0, "charged_usd": 0.0, "refused": 0})  # fmt: skip
        c["attempts"] += 1
        if a["outcome"] == "refused":
            c["refused"] += 1
        rep = a.get("reported") or {}
        c["input_tokens"] += rep.get("input_tokens_total", 0)
        c["output_tokens"] += rep.get("output_tokens", 0)
        c["cache_read_tokens"] += rep.get("cache_read_tokens", 0)
        c["cache_write_tokens"] += rep.get("cache_write_tokens", 0)
        c["latency_s"] = round(c["latency_s"] + a.get("latency_s", 0.0), 3)
        c["charged_usd"] = round(c["charged_usd"] + a.get("charged_usd", 0.0), 6)
    return {
        "requests": len(main),
        "peak_request_chars": max(chars, default=0),
        "peak_estimated_tokens": max((r["estimated_tokens"] for r in requests), default=0),
        # The largest estimate seen before the arm's own management acted (a pressure check, a rollback or an
        # over-limit check), so pressure that was relieved is still visible.
        "peak_estimate_before_management": max(
            [r["estimated_tokens"] for r in requests]
            + [r["est_request_tokens"] for r in trace if r["type"] == "pressure"]
            + [r["from_tokens"] for r in trace if r["type"] == "paper_rollback"]
            + [
                r["estimated_tokens"]
                for r in trace
                if r["type"] in ("paper_over_limit", "context_limit") and "estimated_tokens" in r
            ],
            default=0,
        ),
        "final_chars_per_token": requests[-1]["chars_per_token"] if requests else None,
        "size_nudges": sum("-token budget." in n for n in notices),
        "editing_reminders": sum(
            "tokens of the" in n and "-token limit. Edit" in n for n in notices
        ),
        "persistent_nudges": sum("tokens are left before" in n for n in notices),
        "rollback_notices": sum("were rolled back" in n for n in notices),
        "over_limit_notices": sum("nothing more can be rolled back" in n for n in notices),
        "finalize_notices": sum("step(s) left in this turn" in n for n in notices),
        "pressure_requests": sum(1 for r in requests if r.get("pressure")),
        "commands": len(commands),
        "free_steps": sum(1 for r in commands if r["free_step"]),
        "edits_applied": kinds.get("paper_edit_applied", 0),
        "edits_rejected": kinds.get("paper_edit_rejected", 0),
        "rollbacks": kinds.get("paper_rollback", 0),
        "rollback_steps": sum(r["units"] for r in trace if r["type"] == "paper_rollback"),
        "summaries_applied": kinds.get("summary_applied", 0),
        "summaries_too_large": kinds.get("summary_too_large", 0),
        "summaries_empty": kinds.get("summary_empty", 0),
        "evidence_exposure_chars": sum(delivered),
        "tool_results": len(delivered),
        "refetches": desk.repeat_calls,
        "calls_per_turn": [len(c) for c in calls_by_turn],
        "attempts_by_category": by_category,
        "trace_records": dict(sorted(kinds.items())),
    }


def summariser_requests(run_dir: Path) -> list[dict[str, Any]]:
    """The summariser requests as sent (for checks that they hold only agent-visible material)."""
    out = []
    for a in read_jsonl(run_dir / "attempts.jsonl"):
        if a["event"] == "started" and a["category"] == "summary":
            out.append(a)
    return out


def matrix() -> list[tuple[str, ArmPlan]]:
    """The offline session matrix reported in the results repository (experiment 015). Stress sessions are labelled:
    their policy or faults are chosen to exercise a pressure or failure path, not to resemble an agent."""
    from .plain_agent import BAD_EDITS

    out: list[tuple[str, ArmPlan]] = []
    for inst in ("docthread-dev-1", "docthread-dev-2"):
        tag = inst.rsplit("-", 1)[1]
        policies: tuple[PlainPolicy, ...] = ("tidy", "keep")
        for policy in policies:
            for synthetic in (True, False):
                name = f"paper-dev{tag}-{policy}-{'synthetic' if synthetic else 'fixed'}"
                out.append((name, ArmPlan("paper_clm_shell", inst, policy, synthetic)))
        for synthetic in (True, False):
            name = f"summary-dev{tag}-{'synthetic' if synthetic else 'fixed'}"
            out.append((name, ArmPlan("token_tail_p015", inst, "none", synthetic)))
    out += [
        ("paper-dev1-never-synthetic-stress", ArmPlan("paper_clm_shell", policy="never", label="stress")),
        (
            "paper-dev1-never-readme-stress",
            ArmPlan("paper_clm_shell", policy="never", label="stress", paper=PaperSettings(profile="readme")),
        ),
        (
            "paper-dev1-keep-synthetic-readme",
            ArmPlan("paper_clm_shell", policy="keep", label="alternative", paper=PaperSettings(profile="readme")),
        ),
        ("paper-dev1-never-3-rollbacks-stress",
         ArmPlan("paper_clm_shell", policy="never", label="stress", paper=PaperSettings(max_rollbacks=3))),
        ("paper-dev1-bad-edits-stress", ArmPlan("paper_clm_shell", label="stress", bad_edits=list(BAD_EDITS.values()))),
        ("paper-dev1-provider-failure", ArmPlan("paper_clm_shell", fail_at_attempt=20, label="failure")),
        ("paper-dev1-spend-refusal", ArmPlan("paper_clm_shell", refuse_after_turn=5, label="failure")),
        ("summary-dev1-faulty-summaries-stress",
         ArmPlan("token_tail_p015", policy="none", label="stress", summary_faults={1: "too_long"})),
        ("summary-dev1-overflow-stress",
         ArmPlan("token_tail_p015", policy="none", label="stress", summary_faults={1: "too_long", 2: "empty"})),
        ("summary-dev1-summariser-failure",
         ArmPlan("token_tail_p015", policy="none", label="failure", fail_at_attempt=9)),
        ("summary-dev1-spend-refusal",
         ArmPlan("token_tail_p015", policy="none", refuse_after_turn=5, label="failure")),
    ]  # fmt: skip
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--arm", default="paper_clm_shell", choices=["paper_clm_shell", "token_tail_p015"]
    )
    parser.add_argument("--policy", default=None, choices=["tidy", "keep", "never", "none"])
    parser.add_argument("--instance", default="docthread-dev-1", choices=sorted(DEV_INSTANCES))
    parser.add_argument(
        "--fixed-ratio", action="store_true", help="use the arm's scripted-model estimates"
    )
    parser.add_argument(
        "--matrix", action="store_true", help="run the whole offline session matrix"
    )
    args = parser.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.matrix:
        rows = []
        for name, plan in matrix():
            r = run(plan, args.out_dir, run_id=name)
            rows.append({"session": name, **{k: v for k, v in r.items() if k != "score"},
                         "points": r["score"]["points"], "recall_points": r["score"]["recall_points"]})  # fmt: skip
            print(name, r["endings"], r["score"]["points"], flush=True)
        (args.out_dir / "matrix.json").write_text(
            json.dumps(rows, indent=1) + "\n", encoding="utf-8"
        )
        return 0
    policy: PlainPolicy = args.policy or ("tidy" if args.arm == "paper_clm_shell" else "none")
    result = run(ArmPlan(args.arm, args.instance, policy, not args.fixed_ratio), args.out_dir)
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
