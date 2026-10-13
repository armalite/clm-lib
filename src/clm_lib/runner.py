"""Agent loop for the three run modes: summary baseline, CLM, guided helper demo.

Iteration (see SPEC §3.5):
 1. mirror accepted context to the workspace (CLM modes);
 2. assemble the request fresh from protected prefix + task + status + entries,
    apply context/call/cost limits, record request + revision + hash;
 3. call the provider; a valid final answer ends the task;
 4. run execute-actions in the sandbox;
 5. read back and validate the context file (CLM modes);
 6. accept atomically, then append this step's action, observation and receipt;
 7. continue from the accepted state.

Ordering rule: an accepted edit replaces the context as of the start of the step.
The step's own action and observation are appended after the edit, so the latest
command/result is always present in the next request. Receipts carry only ids
and counts, never removed content.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .baseline import SummaryPolicy
from .budget import BudgetExhausted, Ledger, ModelPrice, input_token_bound
from .config import Config
from .context import (
    CONTEXT_FILENAME,
    ContextLimits,
    ContextStore,
    EditOutcome,
    Entry,
    Revision,
    content_hash,
    serialize,
    total_chars,
    unified_diff,
)
from .executor import DockerExecutor, ExecResult, Executor, ExecutorUnavailable
from .handbook import HANDBOOK_ANSWER_SCHEMA, HANDBOOK_FIELDS, score_handbook
from .jev import QUESTION_VERSION, JevError, ReviewItem, build_request
from .jev_edit_check import (
    EVIDENCE_VERSION,
    HARM_KEYS,
    QUESTION_VERSION_V2,
    QUESTIONS_V2,
    build_edit_request_v2,
    questions_v2_sha256,
)
from .jev_primitive import CLIENT_FILENAME, CLIENT_SOURCE, MAILBOX, JevBroker, PrimitiveLimits
from .jev_primitive import VERSION as PRIMITIVE_VERSION
from .jev_screen import SCREEN_VERSION, recovery_reads, screen_stdout
from .prompts import (
    EDIT_CHECK_CLAUSE,
    EDIT_CHECK_JEV_CLAUSE,
    INITIATED_REVIEW_CLAUSE,
    INITIATED_REVIEW_JEV_CLAUSE,
    INITIATED_REVIEW_V2_CLAUSE,
    INITIATED_REVIEW_V2_JEV_CLAUSE,
    INITIATED_REVIEW_V3_CLAUSE,
    INITIATED_REVIEW_V3_JEV_CLAUSE,
    JEV_CLAUSE,
    JEV_PRIMITIVE_CLAUSE,
    PROMPT_VERSION,
    SEARCH_SCREEN_CLAUSE,
    SEARCH_SCREEN_SERVICE_CLAUSE,
    SUMMARY_SYSTEM_TOKEN_TAIL_CODING,
    SUMMARY_SYSTEM_TOKEN_TAIL_ROUNDS,
    UPDATE_REVIEW_CLAUSE,
    UPDATE_REVIEW_JEV_CLAUSE,
    render_entries,
    render_user,
    render_user_blocks,
    run_tag_block,
    summary_user_blocks,
    system_prompt,
)
from .provider import (
    ACTION_SCHEMA,
    CODING_ACTION_SCHEMA,
    STAGED_ACTION_SCHEMA,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
)
from .rounds import ROUNDS_ANSWER_SCHEMA, ROUNDS_FIELDS, score_rounds
from .tasks import (
    TaskInstance,
    no_answer_score,
    score_answer,
    scorer_version_for,
)
from .tracing import RunTrace, now_iso, sha256_text, snapshot_files
from .update_review import PAYLOAD_VERSION, build_payload

MODES = ("summary", "clm", "guided", "clm_direct", "clm_helpers", "clm_reuse")
# Experiment 007 CLM conditions: the same capabilities, with a direct-editing-only clause or a
# reusable-helpers-allowed clause (prompts.CLM_CLAUSE).
CLM_MODES = ("clm", "guided", "clm_direct", "clm_helpers", "clm_reuse")
INVALID_FOR_COMPARISON = {
    "infrastructure_error",
    "executor_unavailable",
    "accounting_bound_violated",
}


class _Stop(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    mode: str
    task: str
    status: str
    stop_reason: str
    score: Any  # tasks.Score (incident) or coding.CodingScore
    metrics: dict[str, Any]


@dataclass
class _Counters:
    # experiment 008 (host-mediated Jev context review)
    jev_reviews: int = 0
    jev_reviews_rejected: int = 0
    jev_attempts: int = 0
    jev_errors: int = 0
    jev_retries: int = 0
    provider_calls: int = 0
    action_calls: int = 0
    repair_calls: int = 0
    summary_calls: int = 0
    api_retries: int = 0
    provider_errors: int = 0
    executions: int = 0
    exec_timeouts: int = 0
    summaries_applied: int = 0
    summary_overflows: int = 0
    spills: int = 0
    edit_attempts: int = 0
    edits_accepted_changed: int = 0
    edits_unchanged_write: int = 0
    edits_rejected: int = 0
    pressure_steps: int = 0
    recovery_requests: int = 0
    premature_finals: int = 0


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    thinking_tokens: int = 0
    thinking_reported: bool = False
    est_input_tokens: int = 0
    cost_usd: float = 0.0
    reserved_usd_total: float = 0.0
    assumed_cost_usd: float = 0.0
    # Per call kind (action, repair, summary): calls, tokens, cost and provider latency.
    by_kind: dict[str, dict[str, float]] = field(default_factory=dict)


INCIDENT_FIELDS = ("root_cause", "required_value", "remedy", "evidence_refs")
# Experiment 007 rounds task: staged actions with the rounds answer contract.
ROUNDS_ACTION_SCHEMA: dict[str, Any] = {
    **STAGED_ACTION_SCHEMA,
    "properties": {**STAGED_ACTION_SCHEMA["properties"], "answer": ROUNDS_ANSWER_SCHEMA},
}
CODING_FIELDS = ("summary",)
# Experiment 012 v2 document-service task: the handbook answer contract plus the runtime's
# search and fetch actions.
SERVICE_ACTION_SCHEMA: dict[str, Any] = {
    **ACTION_SCHEMA,
    "properties": {
        **ACTION_SCHEMA["properties"],
        "action": {"type": "string", "enum": ["execute", "final", "search", "fetch"]},
        "query": {"type": "string"},
        "k": {"type": "integer"},
        "ids": {"type": "array", "items": {"type": "string"}},
        "answer": HANDBOOK_ANSWER_SCHEMA,
    },
}
# Experiment 012 handbook task: unstaged actions with the handbook answer contract.
HANDBOOK_ACTION_SCHEMA: dict[str, Any] = {
    **ACTION_SCHEMA,
    "properties": {**ACTION_SCHEMA["properties"], "answer": HANDBOOK_ANSWER_SCHEMA},
}


def with_review_action(schema: dict[str, Any]) -> dict[str, Any]:
    """Experiment 008: the action schema plus the optional review_context action."""
    props = dict(schema["properties"])
    props["action"] = {**props["action"], "enum": [*props["action"]["enum"], "review_context"]}
    props["revision"] = {"type": "integer"}
    props["entry_ids"] = {"type": "array", "items": {"type": "string"}}
    props["update"] = {"type": "string"}
    props["update_entry_id"] = {"type": "string"}
    return {**schema, "properties": props}


def with_begin_review_action(schema: dict[str, Any], v2: bool = False) -> dict[str, Any]:
    """Experiment 010: the action schema plus the begin_review action (both arms); v2 adds the
    review purpose (update or compaction)."""
    props = dict(schema["properties"])
    props["action"] = {**props["action"], "enum": [*props["action"]["enum"], "begin_review"]}
    props["new_information"] = {"type": "string"}
    if v2:
        props["purpose"] = {"type": "string", "enum": ["update", "compaction"]}
    return {**schema, "properties": props}


def with_confirm_edit_action(schema: dict[str, Any]) -> dict[str, Any]:
    """Experiment 011 pilot: the action schema plus confirm_edit (both checking conditions)."""
    props = dict(schema["properties"])
    props["action"] = {**props["action"], "enum": [*props["action"]["enum"], "confirm_edit"]}
    props["proposal"] = {"type": "string"}
    props["decision"] = {"type": "string", "enum": ["accept", "withdraw"]}
    return {**schema, "properties": props}


def parse_action(
    text: str,
    max_code_chars: int,
    allow_advance: bool = False,
    answer_fields: tuple[str, ...] = INCIDENT_FIELDS,
    allow_review: bool = False,
    allow_begin_review: bool = False,
    begin_review_v2: bool = False,
    begin_review_v3: bool = False,
    allow_confirm: bool = False,
    allow_service: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """Return (action, error). Only a whole-response JSON object is accepted."""
    raw = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", raw, flags=re.S)
    if fence:
        raw = fence.group(1).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"response is not a single JSON object ({exc.msg})"
    if not isinstance(obj, dict):
        return None, "response JSON must be an object"
    kind = obj.get("action")
    if kind == "advance" and allow_advance:
        return obj, ""
    if kind == "search" and allow_service:
        q, k = obj.get("query"), obj.get("k", 5)
        if not isinstance(q, str) or not q.strip() or len(q) > 300:
            return None, "search needs a non-empty string 'query' of at most 300 characters"
        if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 8:
            return None, "search 'k' must be an integer from 1 to 8"
        return obj, ""
    if kind == "fetch" and allow_service:
        ids = obj.get("ids")
        if (
            not isinstance(ids, list)
            or not ids
            or len(ids) > 10
            or not all(isinstance(i, str) for i in ids)
        ):
            return None, "fetch needs 'ids': a list of 1 to 10 passage or document ids"
        return obj, ""
    if kind == "confirm_edit" and allow_confirm:
        if not isinstance(obj.get("proposal"), str) or not obj["proposal"].strip():
            return None, "confirm_edit needs a string 'proposal' (for example 'P1')"
        if obj.get("decision") not in ("accept", "withdraw"):
            return None, "confirm_edit 'decision' must be 'accept' or 'withdraw'"
        return obj, ""
    if kind == "begin_review" and allow_begin_review and begin_review_v3:
        if obj.get("purpose") not in (None, "update", "compaction"):
            return None, "begin_review 'purpose' must be 'update' or 'compaction' if given"
        if not isinstance(obj.get("new_information", ""), str):
            return None, "begin_review 'new_information' must be a string"
        return obj, ""
    if kind == "begin_review" and allow_begin_review and begin_review_v2:
        if obj.get("purpose") not in ("update", "compaction"):
            return None, "begin_review needs 'purpose': 'update' or 'compaction'"
        if not isinstance(obj.get("new_information", ""), str):
            return None, "begin_review 'new_information' must be a string"
        return obj, ""
    if kind == "begin_review" and allow_begin_review:
        if not isinstance(obj.get("new_information"), str):
            return None, "begin_review needs a string 'new_information'"
        return obj, ""
    if kind == "review_context" and allow_review:
        ids = obj.get("entry_ids")
        if not isinstance(obj.get("revision"), int) or isinstance(obj.get("revision"), bool):
            return None, "review_context needs an integer 'revision'"
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            return None, "review_context needs a non-empty list of string 'entry_ids'"
        has_text, has_ref = (
            isinstance(obj.get("update"), str),
            isinstance(obj.get("update_entry_id"), str),
        )
        if has_text == has_ref:
            return None, "review_context needs exactly one of 'update' or 'update_entry_id'"
        return obj, ""
    if kind == "execute":
        code = obj.get("code")
        if not isinstance(code, str) or not code.strip():
            return None, "execute action needs a non-empty string 'code'"
        if len(code) > max_code_chars:
            return None, f"code is {len(code)} chars; limit {max_code_chars}"
        return obj, ""
    if kind == "final":
        ans = obj.get("answer")
        need = answer_fields
        if not isinstance(ans, dict) or any(k not in ans for k in need):
            return None, f"final action needs answer with fields {list(need)}"
        if "evidence_refs" in need and not isinstance(ans["evidence_refs"], list):
            return None, "evidence_refs must be a list of strings"
        if "incidents" in need and not isinstance(ans["incidents"], list):
            return None, "incidents must be a list of objects"
        if "answers" in need and not isinstance(ans["answers"], list):
            return None, "answers must be a list of objects"
        return obj, ""
    return None, "action must be 'execute' or 'final'"


def _fmt_observation(res: ExecResult, cap: int) -> str:
    head = f"exit_code={res.exit_code} duration={res.duration_s:.1f}s"
    if res.timed_out:
        head += " TIMED OUT"
    if res.error:
        head += f" error={res.error}"
    parts = [head]
    for label, text, total in (
        ("stdout", res.stdout, res.stdout_bytes),
        ("stderr", res.stderr, res.stderr_bytes),
    ):
        if not text and total == 0:
            continue
        shown = text[:cap]
        parts.append(f"--- {label} ---\n{shown}")
        if total > len(shown.encode()):
            parts.append(
                f"[{label} truncated: {total} bytes produced, first {len(shown)} chars shown]"
            )
    return "\n".join(parts)


@dataclass
class Runner:
    cfg: Config
    provider: Provider
    executor: Executor
    ledger: Ledger
    price: ModelPrice | None
    live: bool
    runs_dir: Path = field(default_factory=lambda: Path("runs"))
    sleep: Any = time.sleep
    # Experiment 008: optional Jev advisor (TypeSafeAdvisor or a test FakeAdvisor) and its price.
    jev: Any = None
    jev_price: Any = None

    def __post_init__(self) -> None:
        if self.live and self.jev is not None and getattr(self.jev, "name", "") == "fake":
            raise ValueError("a fake Jev advisor cannot be used for a live run")
        if self.live and self.provider.is_scripted:
            raise ValueError("a scripted provider cannot be used for a live run")
        if not self.live and not self.provider.is_scripted:
            raise ValueError("a real provider requires live=True (explicit opt-in to paid calls)")
        if self.live and self.price is None:
            raise ValueError(
                f"no dated price for model {self.provider.model}; cost cannot be bounded"
            )

    # ------------------------------------------------------------------ run
    def run(
        self,
        task: TaskInstance,
        mode: str,
        label: str = "",
        caching: bool | None = None,
        jev: bool | None = None,
        update_review: str | None = None,
        initiated_review: str | None = None,
        review_required: bool | None = None,
        review_update_source: str | None = None,
        edit_check: str | None = None,
        result_screen: str | None = None,
        jev_primitive: bool | None = None,
        extra_instruction: str | None = None,
    ) -> RunResult:
        """``caching`` overrides ``request.prompt_caching`` for this run (four-condition runs);
        ``jev`` overrides ``jev.enabled`` (experiment 008; CLM modes only). ``update_review``
        ("off", "control" or "jev") overrides ``[update_review]`` (experiment 009; CLM modes and
        rounds tasks only). ``initiated_review`` ("off", "control" or "jev") overrides
        ``[initiated_review]`` (experiment 010; CLM modes only); ``review_required`` overrides
        ``[initiated_review] required`` (010 v2: context edits need a prior review)."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        req = self.cfg.request
        on = req.prompt_caching if caching is None else caching
        if on and req.layout != "blocks/1":
            raise ValueError("prompt caching requires request.layout = 'blocks/1'")
        # blocks/1 run ids carry the caching condition so the four conditions stay distinct.
        cond = f"-cache{'on' if on else 'off'}" if req.layout == "blocks/1" else ""
        stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
        jev_on = self.cfg.jev.enabled if jev is None else jev
        if jev_on and mode not in CLM_MODES:
            raise ValueError("Jev context review is available to CLM modes only")
        if jev_on and self.jev is None:
            raise ValueError("Jev context review requested but no Jev advisor is configured")
        ur = self.cfg.update_review
        upd = update_review or (("jev" if ur.advice else "control") if ur.enabled else "off")
        if upd not in ("off", "control", "jev"):
            raise ValueError(f"update_review must be off, control or jev, not {upd!r}")
        if upd != "off":
            if mode not in CLM_MODES or task.kind != "rounds":
                raise ValueError("update review is available to CLM modes on rounds tasks only")
            if jev_on:
                raise ValueError("update review and the optional review action are exclusive")
            if upd == "jev" and self.jev is None:
                raise ValueError("update review advice requested but no Jev advisor is configured")
        ir = self.cfg.initiated_review
        irev = initiated_review or (("jev" if ir.advice else "control") if ir.enabled else "off")
        if irev not in ("off", "control", "jev"):
            raise ValueError(f"initiated_review must be off, control or jev, not {irev!r}")
        if irev != "off":
            if mode not in CLM_MODES:
                raise ValueError("initiated review is available to CLM modes only")
            if jev_on or upd != "off":
                raise ValueError(
                    "initiated review, update review and the optional review action are exclusive"
                )
            if irev == "jev" and self.jev is None:
                raise ValueError(
                    "initiated review advice requested but no Jev advisor is configured"
                )
        required = ir.required if review_required is None else review_required
        if required and irev == "off":
            raise ValueError("a required review needs initiated review (control or jev)")
        source = ir.update_source if review_update_source is None else review_update_source
        if source not in ("stated", "latest-round"):
            raise ValueError(f"review update source must be stated or latest-round, not {source!r}")
        if source == "latest-round" and (not required or task.kind != "rounds"):
            raise ValueError(
                "the latest-round update source needs a required review on rounds tasks"
            )
        ec = self.cfg.edit_check
        echk = edit_check or (("jev" if ec.advice else "control") if ec.enabled else "off")
        if echk not in ("off", "control", "jev"):
            raise ValueError(f"edit_check must be off, control or jev, not {echk!r}")
        if echk != "off":
            if mode not in CLM_MODES:
                raise ValueError("proposed-edit checking is available to CLM modes only")
            if jev_on or upd != "off" or irev != "off":
                raise ValueError("proposed-edit checking is exclusive with the review features")
            if echk == "jev" and self.jev is None:
                raise ValueError("edit-check advice requested but no Jev advisor is configured")
        rs = self.cfg.result_screen
        rscr = result_screen or ("jev" if rs.enabled else "off")
        if rscr not in ("off", "jev"):
            raise ValueError(f"result_screen must be off or jev, not {rscr!r}")
        if rscr != "off":
            if task.kind not in ("handbook", "docservice"):
                raise ValueError(
                    "result screening applies to structured search results (handbook tasks) only"
                )
            if jev_on or upd != "off" or irev != "off" or echk != "off":
                raise ValueError(
                    "result screening is exclusive with the review and edit-check features"
                )
            if self.jev is None:
                raise ValueError("result screening requested but no Jev advisor is configured")
        jprim = self.cfg.jev_primitive.enabled if jev_primitive is None else jev_primitive
        if jprim:
            if mode not in CLM_MODES:
                raise ValueError("the Jev primitive is available to CLM modes only")
            if jev_on or upd != "off" or irev != "off" or echk != "off" or rscr != "off":
                raise ValueError("the Jev primitive is exclusive with the other Jev features")
            if self.jev is None:
                raise ValueError("the Jev primitive requested but no Jev advisor is configured")
        irev_tags = (
            {"off": "", "control": "-pre3", "jev": "-pre3jev"}
            if source == "latest-round"
            else {"off": "", "control": "-pre", "jev": "-prejev"}
            if required
            else {"off": "", "control": "-rev", "jev": "-revjev"}
        )
        tag = (
            "-jev"
            if jev_on
            else {"off": "", "control": "-upd", "jev": "-updjev"}[upd]
            + irev_tags[irev]
            + {"off": "", "control": "-chk", "jev": "-chkjev"}[echk]
            + {"off": "", "jev": "-scr"}[rscr]
            + ("-jprim" if jprim else "")
        )
        run_id = f"{stamp}-{mode}{cond}{tag}-{task.spec.name}-{secrets.token_hex(2)}"
        if (
            extra_instruction
        ):  # a declared, per-run addition to the task text (recorded in run.json)
            task = replace(
                task, prompt=task.prompt.rstrip("\n") + "\n\n" + extra_instruction.strip() + "\n"
            )
        r = _Run(
            self,
            task,
            mode,
            run_id,
            label,
            on,
            jev_on,
            upd,
            irev,
            required,
            source,
            echk,
            rscr,
            jprim,
        )
        r.extra_instruction = extra_instruction or None
        return r.execute()


REVIEW_REQUIRED = "context edits in this run require a review first"
# Experiment 011 pilot: the pending proposal's full file (informational; the host holds it).
PROPOSAL_FILENAME = "proposed_context.json"
EC_LABELS = {
    "lost": "lost obligation or exception",
    "changed": "unsupported change",
    "evidence": "lost or reassigned evidence",
    "insufficient": "insufficient evidence to check",
}
V3_PAYLOAD_MAX_CHARS = 4000  # 010 v3: the round-files/1 length limit, as in experiment 009


def _entry_batches(entries: list[Entry], batch_size: int, max_chars: int) -> list[list[ReviewItem]]:
    """Jev request batches (experiments 009 and 010): at most batch_size questions and max_chars
    entry characters per request, in context order."""
    batches: list[list[ReviewItem]] = [[]]
    size = 0
    for e in entries:
        if batches[-1] and (len(batches[-1]) >= batch_size or size + len(e.body) > max_chars):
            batches.append([])
            size = 0
        batches[-1].append(ReviewItem(e.id, e.body))
        size += len(e.body)
    return batches


def _advice_lines(
    number: int,
    source: str,
    answers: dict[str, float | None],
    models: set[str],
    errors: list[str],
) -> list[str]:
    """The advice observation (experiments 008 and 009): every scored entry, in order."""
    answered = sum(1 for v in answers.values() if v is not None)
    lines = [
        f"Context review advice ({QUESTION_VERSION}; Jev {', '.join(sorted(models)) or 'unavailable'}; "
        f"revision {number}; update: {source}). Probability that the update requires "
        "reconsidering a current assertion or planned action in each entry. Advisory only: "
        "your context is unchanged.",
    ]
    lines += [
        f"  {eid}: {p:.2f}" if p is not None else f"  {eid}: no answer"
        for eid, p in answers.items()
    ]
    lines.append(f"Coverage: {answered} of {len(answers)} entries answered.")
    if errors:
        lines.append(f"Service problem: {errors[-1]}")
    return lines


class _Run:
    def __init__(
        self,
        runner: Runner,
        task: TaskInstance,
        mode: str,
        run_id: str,
        label: str,
        caching: bool = False,
        jev_on: bool = False,
        update_review: str = "off",
        initiated_review: str = "off",
        review_required: bool = False,
        review_update_source: str = "stated",
        edit_check: str = "off",
        result_screen: str = "off",
        jev_primitive: bool = False,
    ) -> None:
        self.r = runner
        # Experiment 014 (planned): Jev called from model-written code (jev-primitive/1).
        self.jprim = jev_primitive
        self.extra_instruction: str | None = None
        self.prim_log: list[dict[str, Any]] = []
        self.prim_calls = 0
        self.prim_unanswered: list[dict[str, Any]] = []
        # Experiment 012: "off" (A) or "jev" (B: screening of structured search results).
        self.rscr = result_screen
        self.screen_log: list[dict[str, Any]] = []
        self.recovery_log: list[dict[str, Any]] = []
        # Experiment 012 v2: the host-side document service (corpus in host memory only).
        self.service: Any = None
        self.service_log: list[dict[str, Any]] = []
        if task.kind == "docservice":
            from .docservice import DocService

            self.service = DocService(task.truth.corpus)
        # Experiment 011 pilot: "off" (A), "control" (B: proposals) or "jev" (C: proposals + Jev).
        self.echk = edit_check
        self.proposal: dict[str, Any] | None = None  # the pending proposal, if any
        self.proposals: list[dict[str, Any]] = []
        self.confirms: list[dict[str, Any]] = []
        self.ec_checks = 0
        self.code_reads: list[dict[str, Any]] = []
        # W3 register workload (export-gen/2): assessment points and the register score.
        self.register_task = task.generator_version == "export-gen/2"
        self.register_points: list[dict[str, Any]] = []
        self.register_score: dict[str, Any] | None = None
        self.jev_on = jev_on
        self.upd = update_review  # experiment 009: "off", "control" or "jev"
        self.updates: list[dict[str, Any]] = []
        self.irev = initiated_review  # experiment 010: "off", "control" or "jev"
        self.ireviews: list[dict[str, Any]] = []
        # 010 v2: context edits need a prior review; one authorisation per review.
        self.ireq = review_required
        self.isrc = review_update_source  # v3: "latest-round"
        self.edit_auth: dict[str, Any] | None = None
        self.auth_log: list[dict[str, Any]] = []
        self.blocked_edits: list[int] = []
        self.recovery_exempt_steps: list[int] = []
        self.reviews: list[dict[str, Any]] = []
        self.edit_log: list[dict[str, Any]] = []
        self.jev_seq = 0
        self.jev_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latency_s": 0.0}
        self.cfg = runner.cfg
        self.lim = runner.cfg.limits
        self.task = task
        self.mode = mode
        self.run_id = run_id
        self.label = label
        self.dir = runner.runs_dir / run_id
        self.trace = RunTrace(self.dir)
        self.ws = self.dir / "workspace"
        self.fx = self.dir / "fixtures"
        self.ws.mkdir()
        self.fx.mkdir()
        if self.jprim:  # jev-primitive/1: the client module and the mailbox, before step 1
            (self.ws / CLIENT_FILENAME).write_text(CLIENT_SOURCE, encoding="utf-8")
            (self.ws / MAILBOX).mkdir()
        self.climits = ContextLimits()
        self.store = ContextStore(self.climits)
        bl = self.cfg.baseline
        budget = self.lim.context_budget_tokens
        self.policy = SummaryPolicy(
            bl.tail_entries,
            bl.summary_max_chars,
            bl.summary_retries,
            policy=bl.policy,
            tail_tokens=int(bl.tail_ratio * budget),
            newest_tokens=int(bl.newest_ratio * budget),
            target_tokens=int(bl.target_ratio * budget),
            min_chars=bl.summary_min_chars,
        )
        self.c = _Counters()
        self.u = _Usage()
        self.cpt = self.lim.initial_chars_per_token
        self.step = 0
        self.summary_index = 0
        self.recovery_active = False
        self.stage = 1
        self.pressure_seen = False
        self.timeline: list[dict[str, Any]] = []
        self.last_reported_input = 0
        # step at which each management event first affected a request
        self.mgmt: dict[str, list[int]] = {"edit": [], "summary": [], "spill": [], "recovery": []}
        self.peak_req_est = 0
        self.peak_req_reported = 0
        self.peak_ctx_chars = 0
        self.helpers: dict[str, Any] = {"created": {}, "revised": [], "uses": []}
        self.answer: dict[str, Any] | None = None
        self.started = now_iso()
        self.t_start = time.monotonic()
        self.system = system_prompt(
            mode,
            exec_timeout=self.lim.exec_timeout_s,
            output_cap=self.lim.output_cap_chars,
            pressure_pct=int(self.lim.pressure_ratio * 100),
            tail=self.cfg.baseline.tail_entries,
            max_entries=self.climits.max_entries,
            max_body=self.climits.max_body_chars,
            summary_policy=self.policy.policy,
            tail_tokens=self.policy.tail_tokens,
            task_kind=task.kind,
        )
        if self.jev_on:
            self.system += JEV_CLAUSE.format(max_reviews=self.cfg.jev.max_reviews)
        if self.upd != "off":
            self.system += UPDATE_REVIEW_CLAUSE
            if self.upd == "jev":
                self.system += UPDATE_REVIEW_JEV_CLAUSE
        if self.irev != "off" and self.isrc == "latest-round":
            self.system += INITIATED_REVIEW_V3_CLAUSE
            if self.irev == "jev":
                self.system += INITIATED_REVIEW_V3_JEV_CLAUSE
        elif self.irev != "off" and self.ireq:
            self.system += INITIATED_REVIEW_V2_CLAUSE
            if self.irev == "jev":
                self.system += INITIATED_REVIEW_V2_JEV_CLAUSE
        elif self.irev != "off":
            self.system += INITIATED_REVIEW_CLAUSE
            if self.irev == "jev":
                self.system += INITIATED_REVIEW_JEV_CLAUSE
        if self.jprim:
            self.system += JEV_PRIMITIVE_CLAUSE.format(
                max_calls=self.cfg.jev_primitive.max_calls_per_execution
            )
        if self.rscr == "jev":
            self.system += (
                SEARCH_SCREEN_SERVICE_CLAUSE if task.kind == "docservice" else SEARCH_SCREEN_CLAUSE
            )
        if self.echk != "off":
            self.system += EDIT_CHECK_CLAUSE
            if self.echk == "jev":
                self.system += EDIT_CHECK_JEV_CLAUSE
        # Request layout, caching and run isolation (SPEC section 17).
        rq = self.cfg.request
        self.layout = rq.layout
        self.blocks_layout = rq.layout == "blocks/1"
        self.cache_ttl: str | None = rq.cache_ttl if caching else None
        # Random, fixed-length, carries no task information; never reused across runs.
        self.run_tag = secrets.token_hex(16) if rq.run_isolation == "run-tag/1" else None
        self.system_prefix = [run_tag_block(self.run_tag)] if self.run_tag else []
        self.prompt_system = self.system
        if self.blocks_layout:
            self.system = "".join([*self.system_prefix, self.prompt_system])
        self.last_render: tuple[str, list[str], list[int]] | None = None
        self.prev_blocks: dict[str, list[str]] = {}
        self.call_seq = 0
        self.first_call_cache: dict[str, int] | None = None

    # --------------------------------------------------------------- helpers
    @property
    def clm(self) -> bool:
        return self.mode in CLM_MODES

    def task_calls(self) -> int:
        """Calls counted against max_calls: all provider attempts, or all except summary calls
        when ``limits.summary_calls_in_max_calls`` is false (they then have their own cap)."""
        if self.lim.summary_calls_in_max_calls:
            return self.c.provider_calls
        return self.c.provider_calls - self.c.summary_calls

    def est_tokens(self, system: str, user: str) -> int:
        return int((len(system) + len(user)) / self.cpt) + 1

    def status_text(self, est: int, pressure: bool, recovery: bool) -> str:
        b = self.lim.context_budget_tokens
        entries = self.store.entries
        lines = [
            f"step {self.step}; model calls used {self.task_calls()}/{self.lim.max_calls}",
            f"request size ~{est} tokens of {b} budget ({100 * est // b}%); "
            f"pressure threshold {int(self.lim.pressure_ratio * 100)}%; "
            f"hard limit {b - self.lim.recovery_reserve_tokens} tokens",
            f"working context: {len(entries)} entries, {total_chars(entries)} chars "
            f"(~{self.cpt:.2f} chars/token)",
        ]
        if self.lim.status_shows_revision:
            lines.append(f"context revision {self.store.current.number}")
        if self.jev_on:
            lines.append(f"context reviews used {self.c.jev_reviews}/{self.cfg.jev.max_reviews}")
        if self.irev != "off":
            used = sum(1 for r in self.ireviews if r["status"] != "rejected")
            lines.append(f"context reviews used {used}/{self.cfg.initiated_review.max_reviews}")
        if self.ireq:
            a = self.edit_auth
            lines.append(
                f"context edit authorisation: available from the review at step {a['step']} "
                f"(revision {a['revision']})"
                if a
                else "context edit authorisation: none (start begin_review before editing "
                "context.json)"
            )
        if self.jprim:
            lines.append(
                f"jev primitive calls used {self.prim_calls}/"
                f"{self.cfg.jev_primitive.max_calls_per_run}"
            )
        if self.echk != "off":
            p = self.proposal
            lines.append(
                f"context proposal: {p['id']} pending (from step {p['step']}; confirm_edit "
                "accept or withdraw, or write context.json again)"
                if p
                else "context proposal: none pending"
            )
        if pressure and self.clm:
            lines.append(
                "PRESSURE: the request is above the pressure threshold. Manage your "
                "working context now by editing context.json; keep what you still need."
            )
        if recovery:
            lines.append(
                "OVER LIMIT: this request uses the recovery reserve. Your next step must "
                "shrink context.json below the hard limit, or the run ends."
                + self._recovery_alternative()
            )
        return "\n".join(lines)

    def assemble(
        self,
        pressure: bool = False,
        recovery: bool = False,
        entries: tuple[Entry, ...] | None = None,
    ) -> tuple[str, int]:
        ents = self.store.entries if entries is None else entries
        user = self.render(self.status_text(0, pressure, recovery), ents)
        est = self.est_tokens(self.system, user)
        user = self.render(self.status_text(est, pressure, recovery), ents)
        return user, self.est_tokens(self.system, user)

    def render(self, status: str, ents: tuple[Entry, ...]) -> str:
        if not self.blocks_layout:
            return render_user(self.task.prompt, status, ents)
        blocks, bps = render_user_blocks(self.task.prompt, status, ents)
        user = "".join(blocks)
        self.last_render = (user, blocks, bps)
        return user

    def action_request(
        self, user: str, purpose: str, schema: dict[str, Any], note: str = ""
    ) -> ModelRequest:
        """The action (or repair) request for ``user``, in this run's layout."""
        if not self.blocks_layout:
            return ModelRequest(
                self.system, user + note, self.lim.max_output_tokens, purpose, schema
            )
        assert self.last_render is not None and self.last_render[0] == user
        _, blocks, bps = self.last_render
        return ModelRequest.from_blocks(
            [*self.system_prefix, self.prompt_system],
            [*blocks, note] if note else blocks,
            self.lim.max_output_tokens,
            purpose,
            schema,
            bps,
            self.cache_ttl,
        )

    def request_settings(self) -> dict[str, Any]:
        rq = self.cfg.request
        return {
            "layout": self.layout,
            "prompt_caching": "on" if self.cache_ttl else "off",
            "cache_ttl": self.cache_ttl,
            "breakpoints": "task block + last working-context block (blocks/1)"
            if self.cache_ttl
            else None,
            "run_isolation": rq.run_isolation,
            "run_tag": self.run_tag,
        }

    def request_summary(self) -> dict[str, Any]:
        u = self.u
        total_in = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
        return {
            **self.request_settings(),
            "first_call_cache": self.first_call_cache,
            "by_kind": u.by_kind,
            "total_input_tokens_all_kinds": total_in,
            "cache_read_share_of_input": round(u.cache_read_input_tokens / total_in, 4)
            if total_in
            else 0.0,
            "provider_latency_s": round(sum(k["latency_s"] for k in u.by_kind.values()), 3),
        }

    def prefix_evidence(self, req: ModelRequest, kind: str) -> dict[str, Any]:
        """How much of this request's leading content repeats the previous request of the
        same family (action/repair vs summary), by blocks and characters. This is evidence
        for interpreting cache reads; it is not itself a cache measurement."""
        flat = (
            [*req.system_blocks, *req.user_blocks]
            if req.system_blocks is not None and req.user_blocks is not None
            else [req.system, req.user]
        )
        family = "summary" if kind == "summary" else "action"
        prev = self.prev_blocks.get(family)
        self.prev_blocks[family] = flat
        n_sys = len(req.system_blocks) if req.system_blocks is not None else 1
        out: dict[str, Any] = {
            "family": family,
            "blocks": len(flat),
            "system_blocks": n_sys,
            "breakpoint_blocks": [n_sys + i for i in req.cache_breakpoints]
            if req.cache_ttl
            else [],
            "previous": prev is not None,
        }
        if prev is not None:
            k = 0
            while k < min(len(prev), len(flat)) and prev[k] == flat[k]:
                k += 1
            out["common_blocks"] = k
            out["first_changed_block"] = k if k < len(flat) else None
            common_chars = sum(len(b) for b in flat[:k])
            if k < min(len(prev), len(flat)):  # partial match inside the first changed block
                a, b = prev[k], flat[k]
                j = 0
                while j < min(len(a), len(b)) and a[j] == b[j]:
                    j += 1
                common_chars += j
            out["common_prefix_chars"] = common_chars
        return out

    # ------------------------------------------------------------ provider
    def call(self, req: ModelRequest, kind: str, meta: dict[str, Any]) -> ModelResponse:
        retries = self.cfg.provider.api_retries
        for attempt in range(retries + 1):
            if kind == "summary" and not self.lim.summary_calls_in_max_calls:
                if self.c.summary_calls >= self.lim.max_summary_calls:
                    raise _Stop(
                        "max_summary_calls",
                        f"summary call cap {self.lim.max_summary_calls} reached",
                    )
            elif self.task_calls() >= self.lim.max_calls:
                raise _Stop(
                    "max_calls", f"call cap {self.lim.max_calls} reached before {kind} call"
                )
            payload = self.r.provider.payload(req)
            bound = input_token_bound(payload)
            reserve = (
                self.r.price.max_cost(bound, req.max_tokens, req.cache_ttl) if self.r.price else 0.0
            )
            prefix = self.prefix_evidence(req, kind)
            try:
                res = self.r.ledger.reserve(reserve, self.run_id, kind, bound)
            except BudgetExhausted as exc:
                self.trace.event("budget_stop", kind=kind, reserve_usd=reserve, reason=str(exc))
                raise _Stop("budget_exhausted", str(exc)) from exc
            est = self.est_tokens(req.system, req.user)
            rel, digest = self.trace.save_request(
                payload,
                {
                    "kind": kind,
                    "step": self.step,
                    "attempt": attempt,
                    "est_input_tokens": est,
                    "input_token_bound": bound,
                    "reserved_usd": reserve,
                    "context_revision": self.store.current.number,
                    "context_sha256": self.store.current.sha256,
                    "layout": self.layout,
                    "cache_ttl": req.cache_ttl,
                    "prefix": prefix,
                    **meta,
                },
            )
            self.trace.event(
                "request",
                kind=kind,
                step=self.step,
                attempt=attempt,
                file=rel,
                payload_sha256=digest,
                context_revision=self.store.current.number,
                context_sha256=self.store.current.sha256,
                est_input_tokens=est,
                reserved_usd=round(reserve, 6),
            )
            self.c.provider_calls += 1
            setattr(self.c, f"{kind}_calls", getattr(self.c, f"{kind}_calls") + 1)
            self.u.reserved_usd_total += reserve
            self.u.est_input_tokens += est
            self.peak_req_est = max(self.peak_req_est, est)
            t_call = time.monotonic()
            try:
                resp = self.r.provider.complete(req)
            except BaseException as exc:
                if isinstance(exc, ProviderError):
                    self._provider_error(exc, res, kind)
                    if exc.kind == "refusal":
                        raise _Stop("refused", "model refused the request") from exc
                    if exc.retryable and attempt < retries:
                        self.c.api_retries += 1
                        self.r.sleep(min(30.0, 2.0 * 4**attempt))
                        continue
                    raise _Stop("infrastructure_error", str(exc)[:300]) from exc
                # Interrupted or unexpected failure after dispatch: spend is unknown,
                # so the full reservation is charged.
                entry = self.r.ledger.settle(
                    res, actual_usd=None, status=f"interrupted:{type(exc).__name__}"
                )
                self.u.cost_usd += entry["charged_usd"]
                self.u.assumed_cost_usd += entry["charged_usd"]
                self.trace.event(
                    "provider_interrupted",
                    kind=kind,
                    error=type(exc).__name__,
                    charged_usd=entry["charged_usd"],
                    cost_basis=entry["cost_basis"],
                )
                raise
            latency = round(time.monotonic() - t_call, 3)
            cost = self.r.price.cost(resp.usage) if self.r.price else 0.0
            entry = self.r.ledger.settle(res, actual_usd=cost, status="ok", usage=resp.usage)
            self._add_usage(resp, cost, req)
            self._add_kind_usage(kind, resp, cost, latency)
            self.call_seq += 1
            if self.call_seq == 1:
                self.first_call_cache = {
                    "cache_read_input_tokens": resp.usage.cache_read_input_tokens,
                    "cache_creation_input_tokens": resp.usage.cache_creation_input_tokens,
                }
            reported = (
                resp.usage.input_tokens
                + resp.usage.cache_read_input_tokens
                + resp.usage.cache_creation_input_tokens
            )
            self.trace.event(
                "response",
                kind=kind,
                step=self.step,
                text=resp.text,
                stop_reason=resp.stop_reason,
                model=resp.model,
                request_id=resp.request_id,
                usage=resp.usage.to_dict(),
                usage_source=resp.usage_source,
                cost_usd=round(cost, 6),
                ledger_charged_usd=entry["charged_usd"],
                latency_s=latency,
                prefix=prefix,
                input_token_bound=bound,
                bound_held=reported <= bound and resp.usage.output_tokens <= req.max_tokens,
            )
            if resp.usage_source == "provider" and (
                reported > bound or resp.usage.output_tokens > req.max_tokens
            ):
                raise _Stop(
                    "accounting_bound_violated",
                    f"reported input {reported} (bound {bound}) / output "
                    f"{resp.usage.output_tokens} (max {req.max_tokens}); reservations unsafe",
                )
            return resp
        raise AssertionError("unreachable")

    def _provider_error(self, exc: ProviderError, res: Any, kind: str) -> None:
        """Settle a failed attempt: billed usage if returned, $0 only if provably unbilled."""
        self.c.provider_errors += 1
        billed_resp: ModelResponse | None = getattr(exc, "response", None)
        if billed_resp is not None and self.r.price:
            actual: float | None = self.r.price.cost(billed_resp.usage)
        elif exc.billing == "none":
            actual = 0.0
        else:
            actual = None  # potentially billed: charge the full reservation
        entry = self.r.ledger.settle(
            res,
            actual_usd=actual,
            status=f"error:{exc.kind}",
            usage=billed_resp.usage if billed_resp else None,
        )
        self.u.cost_usd += entry["charged_usd"]
        if actual is None:
            self.u.assumed_cost_usd += entry["charged_usd"]
        self.trace.event(
            "provider_error",
            kind=kind,
            error_kind=exc.kind,
            message=str(exc)[:500],
            retryable=exc.retryable,
            billing=exc.billing,
            charged_usd=entry["charged_usd"],
            cost_basis=entry["cost_basis"],
        )

    def _add_kind_usage(self, kind: str, resp: ModelResponse, cost: float, latency: float) -> None:
        k = self.u.by_kind.setdefault(
            kind,
            {
                "calls": 0,
                "input_tokens_uncached": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "latency_s": 0.0,
            },
        )
        u = resp.usage
        k["calls"] += 1
        k["input_tokens_uncached"] += u.input_tokens
        k["cache_read_input_tokens"] += u.cache_read_input_tokens
        k["cache_creation_input_tokens"] += u.cache_creation_input_tokens
        k["output_tokens"] += u.output_tokens
        k["cost_usd"] = round(k["cost_usd"] + cost, 6)
        k["latency_s"] = round(k["latency_s"] + latency, 3)

    def _add_usage(self, resp: ModelResponse, cost: float, req: ModelRequest) -> None:
        u = resp.usage
        self.u.input_tokens += u.input_tokens
        self.u.output_tokens += u.output_tokens
        self.u.cache_read_input_tokens += u.cache_read_input_tokens
        self.u.cache_creation_input_tokens += u.cache_creation_input_tokens
        if u.thinking_tokens is not None:
            self.u.thinking_tokens += u.thinking_tokens
            self.u.thinking_reported = True
        self.u.cost_usd += cost
        reported = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
        self.last_reported_input = reported
        self.peak_req_reported = max(self.peak_req_reported, reported)
        if resp.usage_source == "provider" and reported > 0 and req.purpose == "action":
            observed = (len(req.system) + len(req.user)) / reported
            self.cpt = round(0.5 * self.cpt + 0.5 * observed, 4)

    # ----------------------------------------------------- context pressure
    def entry_tokens(self, entry: Entry) -> int:
        return int(len(render_entries([entry])) / self.cpt) + 1

    def maybe_summarise(self, forced: bool) -> bool:
        if not self.policy.eligible(self.store.entries, self.entry_tokens):
            return False
        older, tail = self.policy.split(self.store.entries, self.entry_tokens)
        hard = self.lim.context_budget_tokens - self.lim.recovery_reserve_tokens
        pressure_tokens = self.lim.pressure_ratio * self.lim.context_budget_tokens
        # Room for the summary body: target request size minus everything else it will carry.
        _, base = self.assemble(entries=(Entry("sum0", "summary", ""), *tail))
        room = self.policy.target_tokens - base
        for attempt in range(self.policy.retries + 1):
            limit = self.policy.char_limit(attempt, room, self.cpt)
            req = self.policy.request(
                self.task.prompt,
                older,
                attempt,
                self.lim.max_output_tokens,
                limit,
                system=SUMMARY_SYSTEM_TOKEN_TAIL_CODING
                if self.task.kind == "coding"
                else SUMMARY_SYSTEM_TOKEN_TAIL_ROUNDS
                if self.task.kind == "rounds"
                else None,
            )
            if self.blocks_layout:
                ub, bps = summary_user_blocks(self.task.prompt, older, limit)
                req = ModelRequest.from_blocks(
                    [*self.system_prefix, req.system],
                    ub,
                    req.max_tokens,
                    "summary",
                    None,
                    bps,
                    self.cache_ttl,
                )
            resp = self.call(
                req,
                "summary",
                {
                    "summary_policy": self.policy.policy,
                    "older_entries": [e.id for e in older],
                    "kept_tail": [e.id for e in tail],
                    "summary_attempt": attempt,
                    "requested_max_chars": limit,
                    "forced": forced,
                },
            )
            text = resp.text.strip()
            if not text:
                continue
            candidate = tuple(SummaryPolicy.apply(text, tail, self.summary_index + 1))
            _, est = self.assemble(entries=candidate)
            if est <= self.policy.accept_tokens(attempt, pressure_tokens, hard):
                self.summary_index += 1
                self.store.commit(candidate, "summary", self.step)
                self.c.summaries_applied += 1
                self.mgmt["summary"].append(self.step)
                self.trace.event(
                    "summary_applied",
                    step=self.step,
                    policy=self.policy.policy,
                    attempt=attempt,
                    requested_max_chars=limit,
                    replaced=[e.id for e in older],
                    kept_tail=[e.id for e in tail],
                    summary_chars=len(text),
                    revision=self.store.current.number,
                    est_request_tokens=est,
                )
                self._save_revision()
                return True
            # Too large: keep the previous state and retry with a tighter limit.
            self.trace.event(
                "summary_too_large",
                step=self.step,
                policy=self.policy.policy,
                attempt=attempt,
                requested_max_chars=limit,
                summary_chars=len(text),
                est_request_tokens=est,
            )
        self.c.summary_overflows += 1
        raise _Stop(
            "context_overflow", "summary did not fit the acceptance limit after the bounded retry"
        )

    def spill_latest(self) -> bool:
        entries = list(self.store.entries)
        idx = len(entries) - 1
        if (
            self.lim.spill_past_receipt
            and entries
            and entries[-1].role == "receipt"
            and len(entries) >= 2
            and entries[-2].role == "observation"
        ):
            idx = len(entries) - 2  # the step's observation, followed by its edit receipt
        if not entries or entries[idx].role != "observation":
            return False
        last = entries[idx]
        if len(last.body) < self.lim.spill_min_chars:
            return False
        spill_dir = self.ws / "spill"
        spill_dir.mkdir(exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", last.id) + ".txt"
        (spill_dir / name).write_text(last.body, encoding="utf-8")
        entries[idx] = Entry(
            last.id,
            "observation",
            (
                f"[runtime: observation of {len(last.body)} chars moved to "
                f"/task/workspace/spill/{name} because the request exceeded the hard limit]"
            ),
        )
        self.store.commit(entries, "spill", self.step)
        self.c.spills += 1
        self.mgmt["spill"].append(self.step)
        self.trace.event(
            "spill", step=self.step, entry=last.id, chars=len(last.body), file=f"spill/{name}"
        )
        self._save_revision()
        return True

    def _register_point(self, point: str) -> None:
        """W3: the accepted revision live when stage ``self.stage`` has been processed (at the
        release of the next stage, or at the accepted final answer)."""
        if self.register_task:
            self.register_points.append(
                {
                    "stage": self.stage,
                    "revision": self.store.current.number,
                    "step": self.step,
                    "point": point,
                }
            )

    def _save_revision(self) -> None:
        rev = self.store.current
        self.trace.write_json(
            f"context/rev-{rev.number:04d}.json",
            {
                "number": rev.number,
                "parent": rev.parent,
                "source": rev.source,
                "step": rev.step,
                "sha256": rev.sha256,
                "entries": [e.to_dict() for e in rev.entries],
            },
        )
        if rev.parent is not None:
            parent = self.store.revisions[rev.parent]
            self.trace.write(f"context/rev-{rev.number:04d}.diff", unified_diff(parent, rev))
        self.peak_ctx_chars = max(self.peak_ctx_chars, total_chars(rev.entries))

    def prepare_request(self) -> tuple[str, int, bool, bool]:
        b = self.lim.context_budget_tokens
        hard = b - self.lim.recovery_reserve_tokens
        user, est = self.assemble()
        pressure = est >= self.lim.pressure_ratio * b
        self.pressure_seen = pressure  # before any management relieves it
        if pressure:
            self.c.pressure_steps += 1
            self.trace.event("pressure", step=self.step, est_request_tokens=est)
            if self.mode == "summary" and self.maybe_summarise(forced=False):
                user, est = self.assemble()
                pressure = est >= self.lim.pressure_ratio * b
        if est > hard and self.spill_latest():
            user, est = self.assemble(pressure=pressure)
        recovery = False
        if est > hard:
            if self.mode == "summary":
                if not self.maybe_summarise(forced=True):
                    raise _Stop("context_overflow", f"request ~{est} tokens > hard limit {hard}")
                user, est = self.assemble()
            else:
                if self.recovery_active or est > b:
                    raise _Stop(
                        "context_overflow",
                        f"request ~{est} tokens > limit after recovery opportunity",
                    )
                recovery = True
                self.c.recovery_requests += 1
                self.mgmt["recovery"].append(self.step)
        self.recovery_active = recovery
        user, est = self.assemble(pressure=pressure, recovery=recovery)
        if self.lim.recheck_after_notice and not recovery and est > hard:
            # The runtime's own PRESSURE notice pushed the request over the hard limit: handle it
            # like any other oversized request (spill, then the CLM recovery request).
            if self.spill_latest():
                user, est = self.assemble(pressure=pressure)
            if est > hard and self.clm and est <= b:
                recovery = self.recovery_active = True
                self.c.recovery_requests += 1
                self.mgmt["recovery"].append(self.step)
                user, est = self.assemble(pressure=pressure, recovery=True)
        if est > (b if recovery else hard):
            raise _Stop("context_overflow", f"request ~{est} tokens exceeds limit")
        return user, est, pressure, recovery

    # ---------------------------------------------------------- execution
    def _track_files(self, before: dict[str, str], after: dict[str, str], code: str) -> list[str]:
        """Snapshot changed workspace files; return *candidate* helper invocations.

        Candidates are inferred from the code text (file name or import statement).
        They are not evidence that a helper ran; see ``_record_helper_use``.
        """
        skip = {CONTEXT_FILENAME, f".{CONTEXT_FILENAME}.host-tmp"}
        changed = sorted(
            p
            for p in after
            if p not in skip and not p.startswith("spill/") and before.get(p) != after[p]
        )
        removed = sorted(p for p in before if p not in after and p not in skip)
        for p in changed:
            self.trace.write(f"files/step-{self.step:03d}/after/{p}", after[p])
            if p in before:
                self.trace.write(f"files/step-{self.step:03d}/before/{p}", before[p])
        candidates = []
        for p in sorted(before):  # only helpers that existed before this step
            if not p.endswith(".py") or p in skip:
                continue
            mod = Path(p).with_suffix("").as_posix().replace("/", ".")
            stem = Path(p).stem
            if (
                p in code
                or re.search(rf"\b(import|from)\s+{re.escape(mod)}\b", code)
                or re.search(rf"\bimport\s+{re.escape(stem)}\b", code)
                or re.search(rf"\bfrom\s+\S*{re.escape(stem)}\s+import\b", code)
            ):
                candidates.append(p)
        for p in changed:
            if not p.endswith(".py"):
                continue
            if p in before:
                self.helpers["revised"].append({"step": self.step, "file": p})
            else:
                self.helpers["created"].setdefault(p, self.step)
        if changed or removed:
            self.trace.event(
                "workspace_changes",
                step=self.step,
                changed=changed,
                removed=removed,
                candidate_helper_invocations=candidates,
            )
        return candidates

    def _record_helper_use(
        self,
        res: ExecResult,
        candidates: list[str],
        py_files: set[str],
        outcome: EditOutcome | None,
    ) -> None:
        """Combine candidate inference with the in-container execution record.

        Evidence tiers per workspace .py file:
        - candidate: named in the code text (inferred only);
        - loaded: imported or compiled (recorded, *not* counted as execution);
        - functions_executed: functions defined in the file began executing (PY_START);
        - wrote_context: code from the file was on the call stack when context.json was
          opened for writing or replaced (attribution of the write, not of its content).
        """
        rt: dict[str, Any] = dict(res.runtime_trace or {})
        ws = "/task/workspace/"
        calls: dict[str, int] = rt.get("calls", {}) or {}
        loaded = set(rt.get("imported", []) or []) | set(rt.get("compiled", []) or [])
        writes: list[dict[str, Any]] = rt.get("context_writes", []) or []
        per_file: dict[str, Any] = {}
        for p in sorted(py_files):
            full = ws + p
            funcs = {
                k.split("::", 1)[1]: v
                for k, v in calls.items()
                if k.split("::", 1)[0] == full and not k.endswith("::<module>")
            }
            module_body = f"{full}::<module>" in calls
            wrote = sorted(
                {
                    fr.split("::", 1)[1]
                    for w in writes
                    for fr in w.get("ws_frames", [])
                    if fr.split("::", 1)[0] == full
                }
            )
            if p in candidates or full in loaded or funcs or module_body or wrote:
                per_file[p] = {
                    "candidate": p in candidates,
                    "loaded": full in loaded,
                    "module_body_executed": module_body,
                    "functions_executed": funcs,
                    "wrote_context_from": wrote,
                }
        if not per_file:
            return
        use = {
            "step": self.step,
            "files": per_file,
            "exit_ok": res.exit_code == 0 and not res.timed_out,
            "runtime_record": res.runtime_trace is not None,
            "monitoring": bool(rt.get("monitoring")),
            "context_writes_unattributed": sum(1 for w in writes if not w.get("ws_frames")),
            "context_edit": outcome.status if outcome else "n/a",
        }
        self.helpers["uses"].append(use)
        self.trace.event("helper_use", **use)

    def helper_summary(self) -> dict[str, Any]:
        uses = self.helpers["uses"]
        executed: dict[str, list[int]] = {}
        attributed: dict[str, list[int]] = {}
        for u in uses:
            if not u["exit_ok"]:
                continue
            for p, f in u["files"].items():
                if f["functions_executed"]:
                    executed.setdefault(p, []).append(u["step"])
                if f["wrote_context_from"] and u["context_edit"] == "accepted":
                    attributed.setdefault(p, []).append(u["step"])
        return {
            "created": self.helpers["created"],
            "revised": self.helpers["revised"],
            "candidate_invocation_steps": sorted(
                {u["step"] for u in uses if any(f["candidate"] for f in u["files"].values())}
            ),
            "function_execution_steps": executed,
            "helper_written_accepted_edit_steps": attributed,
            "functions_executed_in_2plus_steps": any(len(v) >= 2 for v in executed.values()),
            "helper_written_accepted_edits_in_2plus_steps": any(
                len(v) >= 2 for v in attributed.values()
            ),
            "uses": uses,
            "evidence_note": (
                "candidate = named in code text (inferred); loaded = imported/compiled (not "
                "execution); function_execution = a function defined in the file began "
                "executing in a step that exited 0 (sys.monitoring PY_START); "
                "helper_written_accepted_edit = helper code was on the call stack when "
                "context.json was written and that step's edit was accepted. This attributes "
                "the write to the helper; it does not prove the helper alone chose the content."
            ),
        }

    def execute_step(self, action: dict[str, Any]) -> None:
        code: str = action["code"]
        thought = str(action.get("thought", ""))[:1000]
        script_rel = f"scripts/step-{self.step:03d}.py"
        self.trace.write(script_rel, code)
        self.trace.event(
            "execution_start", step=self.step, script=script_rel, code_sha256=sha256_text(code)
        )
        mirrored = self.store.mirror(self.ws) if self.clm else ""
        before = snapshot_files(self.ws)
        if self.register_task:
            # W3 (export-gen/2): the full pre-execution workspace, host-side, for offline replay.
            self.trace.write_json(f"workspace_pre/step-{self.step:03d}.json", before)
        broker = None
        if self.jprim:
            jp = self.cfg.jev_primitive
            broker = JevBroker(
                self.ws,
                self._jev_attempts,
                self._record_primitive,
                self.cfg.jev.model,
                PrimitiveLimits(jp.max_calls_per_execution, jp.max_calls_per_run, jp.max_items),
                run_calls_used=self.prim_calls,
            )
        try:
            if broker is None:
                res = self.r.executor.run(code, self.ws, self.fx)
            else:
                res = self.r.executor.run(code, self.ws, self.fx, broker=broker)  # type: ignore[call-arg]
        except ExecutorUnavailable as exc:
            raise _Stop("executor_unavailable", str(exc)) from exc
        if broker is not None:
            self.prim_calls = broker.run_calls_used
            for rid in broker.finish():  # the execution ended first; no Jev call was made
                self.prim_unanswered.append({"step": self.step, "id": rid})
                self.trace.event("jev_primitive_unanswered", step=self.step, id=rid)
        self.c.executions += 1
        if res.timed_out:
            self.c.exec_timeouts += 1
        after = snapshot_files(self.ws)
        self.trace.event(
            "execution_result",
            step=self.step,
            exit_code=res.exit_code,
            timed_out=res.timed_out,
            duration_s=res.duration_s,
            stdout=res.stdout,
            stderr=res.stderr,
            stdout_bytes=res.stdout_bytes,
            stderr_bytes=res.stderr_bytes,
            error=res.error,
            runtime_trace=res.runtime_trace,
        )
        if self.task.kind == "handbook":
            res = self._present_search(res, thought, code)
        candidates = self._track_files(before, after, code)
        self._note_reads(code)
        receipt: str | None = None
        outcome: EditOutcome | None = None
        if self.clm:
            # 011 pilot (B/C): hold a valid change as a proposal, except when the runtime
            # demands a shrinking edit in this very step (OVER LIMIT recovery).
            hold = self.echk != "off" and not self.recovery_active
            outcome = self.store.read_back(
                self.ws,
                mirrored,
                self.step,
                self._authorise_edit if self.ireq else None,
                hold=hold,
            )
            if outcome.status == "proposed":
                receipt = self._propose(outcome, res)
            else:
                receipt = self._record_edit(outcome)
                if self.echk != "off" and outcome.status == "accepted":
                    receipt = (receipt or "") + self._recovery_applied()
            if self.ireq:
                self._settle_edit_auth(outcome)
        py_files = {p for p in (*before, *after) if p.endswith(".py")}
        self._record_helper_use(res, candidates, py_files, outcome)
        act = f"[step {self.step}] execute\nthought: {thought}\ncode:\n{code}"
        obs = _fmt_observation(res, self.lim.output_cap_chars)
        new = [
            Entry(f"s{self.step}.act", "assistant", act),
            Entry(f"s{self.step}.obs", "observation", obs),
        ]
        if receipt:
            new.append(Entry(f"s{self.step}.rcpt", "receipt", receipt))
        self.store.append(new, "runtime", self.step)
        self._save_revision()
        if self.clm:
            self.store.mirror(self.ws)
            self._mirror_proposal()
        if broker is not None and broker.stop is not None:
            raise broker.stop  # the budget ran out during the execution; the step is recorded

    def _record_edit(self, out: EditOutcome) -> str | None:
        if out.attempted:
            self.c.edit_attempts += 1
        base = {
            "step": self.step,
            "status": out.status,
            "reason": out.reason,
            "before_revision": out.before.number if out.before else None,
        }
        if out.status == "not_written":
            return None
        if out.status == "unchanged_write":
            self.c.edits_unchanged_write += 1
            self.trace.event("context_edit", **base, changed=False)
            return None
        if out.status == "rejected":
            self.c.edits_rejected += 1
            self.trace.event("context_edit", **base, changed=False)
            try:
                raw = (self.ws / CONTEXT_FILENAME).read_bytes()[:200_000]
                self.trace.write(
                    f"context/rejected-step-{self.step:03d}.txt", raw.decode("utf-8", "replace")
                )
            except OSError:
                pass
            rev = out.before.number if out.before else 0
            return (
                f"context.json edit rejected at step {self.step}: {out.reason[:300]}. "
                f"Previous context (revision {rev}) kept."
            )
        assert out.before and out.after
        self.c.edits_accepted_changed += 1
        self.mgmt["edit"].append(self.step + 1)  # first affects the next request
        b_ids = [e.id for e in out.before.entries]
        a_ids = [e.id for e in out.after.entries]
        removed = [i for i in b_ids if i not in a_ids]
        added = [i for i in a_ids if i not in b_ids]
        rewritten = [
            e.id
            for e in out.after.entries
            if e.id in b_ids and e.body != next(x.body for x in out.before.entries if x.id == e.id)
        ]
        self.trace.event(
            "context_edit",
            **base,
            changed=True,
            after_revision=out.after.number,
            after_sha256=out.after.sha256,
            removed=removed,
            added=added,
            rewritten=rewritten,
            chars_before=total_chars(out.before.entries),
            chars_after=total_chars(out.after.entries),
        )
        self.edit_log.append(
            {
                "step": self.step,
                "before_revision": out.before.number,
                "after_revision": out.after.number,
                "removed": removed,
                "rewritten": rewritten,
                "added": added,
            }
        )
        self._save_revision()

        def ids(xs: list[str]) -> str:
            return ", ".join(xs[:12]) + (f" (+{len(xs) - 12} more)" if len(xs) > 12 else "")

        return (
            f"context.json edit accepted at step {self.step}: revision {out.before.number} -> "
            f"{out.after.number}; entries {len(b_ids)} -> {len(a_ids)}; body chars "
            f"{total_chars(out.before.entries)} -> {total_chars(out.after.entries)}; "
            f"removed [{ids(removed)}]; added [{ids(added)}]; rewritten [{ids(rewritten)}]"
        )

    # --------------------------------------------------------------- loop
    def loop(self) -> tuple[str, str]:
        coding = self.task.kind == "coding"
        rounds = self.task.kind == "rounds"
        service = self.task.kind == "docservice"
        handbook = self.task.kind == "handbook" or service
        schema = (
            CODING_ACTION_SCHEMA
            if coding
            else SERVICE_ACTION_SCHEMA
            if service
            else HANDBOOK_ACTION_SCHEMA
            if handbook
            else ROUNDS_ACTION_SCHEMA
            if rounds
            else STAGED_ACTION_SCHEMA
            if self.task.staged
            else ACTION_SCHEMA
        )
        fields = (
            CODING_FIELDS
            if coding
            else HANDBOOK_FIELDS
            if handbook
            else ROUNDS_FIELDS
            if rounds
            else INCIDENT_FIELDS
        )
        staged = self.task.staged
        if self.jev_on:
            schema = with_review_action(schema)
        if self.irev != "off":
            schema = with_begin_review_action(schema, v2=self.ireq)
        if self.echk != "off":
            schema = with_confirm_edit_action(schema)
        while True:
            self.step += 1
            user, est, pressure, recovery = self.prepare_request()
            self.timeline.append(
                {
                    "step": self.step,
                    "stage": self.stage,
                    "est_request_tokens": est,
                    "pressure_before_management": self.pressure_seen,
                    "pressure": pressure,
                    "recovery": recovery,
                }
            )
            req = self.action_request(user, "action", schema)
            resp = self.call(req, "action", {"pressure": pressure, "recovery": recovery})
            self.timeline[-1]["reported_input_tokens"] = self.last_reported_input
            self.timeline[-1]["cache_read_input_tokens"] = resp.usage.cache_read_input_tokens
            self.timeline[-1]["cache_creation_input_tokens"] = (
                resp.usage.cache_creation_input_tokens
            )
            self.timeline[-1]["uncached_input_tokens"] = resp.usage.input_tokens
            action, err = parse_action(
                resp.text,
                self.lim.max_code_chars,
                staged,
                fields,
                self.jev_on,
                self.irev != "off",
                self.ireq,
                self.isrc == "latest-round",
                self.echk != "off",
                service,
            )
            if action is None:
                self.trace.event(
                    "invalid_action", step=self.step, error=err, stop_reason=resp.stop_reason
                )
                note = (
                    f"\n\n<runtime_status>\nYour previous reply was rejected: {err}"
                    f"{' (output token limit reached)' if resp.stop_reason == 'max_tokens' else ''}. "
                    "Reply again with exactly one JSON object; keep code short.\n</runtime_status>\n"
                )
                repair = self.action_request(user, "repair", schema, note)
                resp = self.call(repair, "repair", {"error": err})
                action, err = parse_action(
                    resp.text,
                    self.lim.max_code_chars,
                    staged,
                    fields,
                    self.jev_on,
                    self.irev != "off",
                    self.ireq,
                    self.isrc == "latest-round",
                    self.echk != "off",
                    service,
                )
                if action is None:
                    self.trace.event("invalid_action", step=self.step, error=err, after_repair=True)
                    return "failed_protocol", f"invalid response after repair: {err}"
            self.timeline[-1]["action"] = action["action"]
            if action["action"] == "advance":
                self.advance_step(action)
                continue
            if action["action"] == "begin_review":
                self.begin_review_step(action)
                continue
            if action["action"] == "review_context":
                self.review_step(action)
                continue
            if action["action"] == "confirm_edit":
                self.confirm_step(action)
                continue
            if action["action"] in ("search", "fetch"):
                self.service_step(action)
                continue
            if action["action"] == "final":
                if staged and self.stage < self.task.n_stages:
                    self.premature_final_step(action)
                    continue
                self._end_proposal("unresolved_at_final")
                self.answer = action["answer"]
                self._register_point("final")
                self.trace.event("final_answer", step=self.step, answer=self.answer)
                return "completed", "final answer"
            self.execute_step(action)

    def advance_step(self, action: dict[str, Any]) -> None:
        """Release the next evidence stage (staged tasks only)."""
        thought = str(action.get("thought", ""))[:1000]
        if self.stage < self.task.n_stages:
            self._register_point("release")
            if getattr(self.task.truth, "snapshot_checks", None):
                # Host-side copy of the workspace at the end of this stage (evaluator-only).
                shutil.copytree(
                    self.ws,
                    self.dir / "evaluator" / "snapshots" / f"stage-{self.stage}",
                    ignore=self._submission_ignore(),
                )
            self.stage += 1
            if self.ireq and self.edit_auth:
                self._end_edit_auth("expired")  # a review covers the evidence available then
            expired = self._end_proposal("expired_at_stage_release")
            self.task.write_fixtures(self.fx, self.stage)
            released = self.task.stages[self.stage - 1]
            listing = "\n".join(
                f"  {p} ({t.count(chr(10))} lines)" for p, t in sorted(released.items())
            )
            obs = (
                f"Stage {self.stage} of {self.task.n_stages} released.\n"
                f"{self.task.stage_updates[self.stage - 1].strip()}\n\nNew files:\n{listing}"
            )
            self.trace.event(
                "stage_released", step=self.step, stage=self.stage, files=sorted(released)
            )
            if self.upd != "off":
                obs += self.update_review(released)
            if expired:
                obs += (
                    f"\n\nPending context proposal {expired} was discarded because a new stage "
                    "was released; the live context is unchanged. Write context.json again if you "
                    "still want a change."
                )
        else:
            obs = f"No further evidence: all {self.task.n_stages} stages are already released."
            self.trace.event("advance_noop", step=self.step)
        self.store.append(
            [
                Entry(
                    f"s{self.step}.act",
                    "assistant",
                    f"[step {self.step}] advance\nthought: {thought}",
                ),
                Entry(f"s{self.step}.obs", "observation", obs),
            ],
            "runtime",
            self.step,
        )
        self._save_revision()
        if self.clm:
            self._mirror_proposal()

    # ---------------------------------------------------- Jev context review (exp 008)
    def _validate_review(
        self, action: dict[str, Any]
    ) -> tuple[list[ReviewItem], str, str, int] | str:
        """(items, update text, update source, revision) or an error message."""
        j = self.cfg.jev
        if self.c.jev_reviews >= j.max_reviews:
            return f"review limit reached ({j.max_reviews} per task)"
        # Only the active context may be reviewed: entries the agent has deleted stay out of
        # Jev requests even though older revisions remain in the host's history.
        number = action["revision"]
        rev = self.store.current
        if number != rev.number:
            known = any(r.number == number for r in self.store.revisions)
            return (
                f"revision {number} {'is no longer current' if known else 'does not exist'}; "
                f"reviews use only the current context revision ({rev.number})"
            )
        ids: list[str] = action["entry_ids"]
        if len(ids) > j.max_entries:
            return f"{len(ids)} entries requested; at most {j.max_entries} per review"
        if len(set(ids)) != len(ids):
            return "entry_ids contains duplicates"
        by_id = {e.id: e for e in rev.entries}
        missing = [i for i in ids if i not in by_id]
        if missing:
            return f"entry ids not in revision {number}: {missing[:10]}"
        if "update_entry_id" in action:
            ref = action["update_entry_id"]
            if ref not in by_id:
                return f"update_entry_id {ref!r} is not in revision {number}"
            update, source = by_id[ref].body, f"entry {ref} of revision {number}"
        else:
            update, source = action["update"], "text supplied in the action"
        if not update.strip():
            return "the update is empty"
        if len(update) > j.max_update_chars:
            return f"the update is {len(update)} chars; at most {j.max_update_chars}"
        return [ReviewItem(i, by_id[i].body) for i in ids], update, source, number

    def _jev_attempts(self, body: dict[str, Any]) -> tuple[Any, str | None]:
        """Call Jev with the configured bounded retries; every attempt is in the ledger."""
        j = self.cfg.jev
        last = None
        for attempt in range(j.retries + 1):
            price = self.r.jev_price
            reserve = price.max_cost(body) if price else 0.0
            try:
                res = self.r.ledger.reserve(reserve, self.run_id, "jev")
            except BudgetExhausted as exc:
                raise _Stop("budget_exhausted", str(exc)) from exc
            self.jev_seq += 1
            self.c.jev_attempts += 1
            rec: dict[str, Any] = {"attempt": attempt, "request": body, "model_requested": j.model}
            try:
                resp, latency = self.r.jev.call(body)
            except JevError as exc:
                actual = 0.0 if exc.billing == "none" else None
                entry = self.r.ledger.settle(
                    res,
                    actual_usd=actual,
                    status=f"error:{exc.kind}",
                    note=json.dumps({"provider": "typesafe", "error": exc.kind}),
                )
                self.u.cost_usd += entry["charged_usd"]
                self.jev_usage["cost_usd"] += entry["charged_usd"]
                if actual is None:
                    self.u.assumed_cost_usd += entry["charged_usd"]
                self.c.jev_errors += 1
                rec.update(error=str(exc)[:300], charged_usd=entry["charged_usd"])
                self.trace.write_json(f"jev/{self.jev_seq:04d}.json", rec)
                self.trace.event("jev_error", step=self.step, attempt=attempt, error=str(exc)[:300])
                last = str(exc)[:300]
                if exc.retryable and attempt < j.retries:
                    self.c.jev_retries += 1
                    self.r.sleep(min(10.0, 1.0 * 2**attempt))
                    continue
                return None, last
            cost = price.cost(resp.input_tokens, resp.output_tokens) if price else 0.0
            entry = self.r.ledger.settle(
                res,
                actual_usd=cost,
                status="ok",
                note=json.dumps(
                    {
                        "provider": "typesafe",
                        "model": resp.model,
                        "input_tokens": resp.input_tokens,
                        "output_tokens": resp.output_tokens,
                    }
                ),
            )
            self.u.cost_usd += entry["charged_usd"]
            for k, v in (
                ("input_tokens", resp.input_tokens),
                ("output_tokens", resp.output_tokens),
            ):
                self.jev_usage[k] += v
            self.jev_usage["cost_usd"] += entry["charged_usd"]
            self.jev_usage["latency_s"] += latency
            rec.update(
                response=resp.raw,
                model=resp.model,
                latency_s=round(latency, 4),
                usage={"input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens},
                cost_usd=round(cost, 8),
            )
            self.trace.write_json(f"jev/{self.jev_seq:04d}.json", rec)
            return resp, None
        return None, last

    def _jev_score(
        self, update: str, batches: list[list[ReviewItem]]
    ) -> tuple[dict[str, float | None], set[str], list[str]]:
        """One Jev request per batch (with bounded retries); missing answers are None."""
        answers: dict[str, float | None] = {}
        models: set[str] = set()
        errors: list[str] = []
        for batch in batches:
            body = build_request(update, batch, self.cfg.jev.model)
            resp, err = self._jev_attempts(body)
            for i, item in enumerate(batch):
                answers[item.entry_id] = resp.probabilities.get(f"e{i}") if resp else None
            if resp:
                models.add(resp.model)
            if err:
                errors.append(err)
        return answers, models, errors

    def review_step(self, action: dict[str, Any]) -> None:
        """Advice only: the context is never changed here (the runtime appends the step's
        action and observation, as for every step)."""
        thought = str(action.get("thought", ""))[:1000]
        checked = self._validate_review(action)
        record: dict[str, Any] = {
            "step": self.step,
            "request": {
                k: action.get(k) for k in ("revision", "entry_ids", "update", "update_entry_id")
            },
        }
        if isinstance(checked, str):
            self.c.jev_reviews_rejected += 1
            record.update(status="rejected", error=checked)
            obs = f"Context review not performed: {checked}. Your context is unchanged."
        else:
            items, update, source, number = checked
            self.c.jev_reviews += 1
            j = self.cfg.jev
            batches = [items[i : i + j.batch_size] for i in range(0, len(items), j.batch_size)]
            answers, models, errors = self._jev_score(update, batches)
            answered = sum(1 for v in answers.values() if v is not None)
            obs = "\n".join(_advice_lines(number, source, answers, models, errors))
            record.update(
                status="ok" if answered else "failed",
                revision=number,
                update_source=source,
                update_chars=len(update),
                entry_ids=[i.entry_id for i in items],
                answers=answers,
                models=sorted(models),
                errors=errors,
            )
        self.reviews.append(record)
        self.trace.event("context_review", **record)
        req = {k: v for k, v in action.items() if k != "thought"}
        self.store.append(
            [
                Entry(
                    f"s{self.step}.act",
                    "assistant",
                    f"[step {self.step}] review_context {json.dumps(req)[:1500]}\nthought: {thought}",
                ),
                Entry(f"s{self.step}.obs", "observation", obs),
            ],
            "runtime",
            self.step,
        )
        self._save_revision()

    # ------------------------------------------- update-triggered context review (exp 009)
    def update_review(self, released: dict[str, str]) -> str:
        """Text appended to the release observation (identical update text in both arms). The
        candidates are every entry of the current revision, i.e. the context the model saw in
        this step's request; the advance step's own entries are appended afterwards. With
        advice, Jev scores the candidates here, before the next model call, and nothing in the
        context changes."""
        ur = self.cfg.update_review
        payload, sources, truncated = build_payload(released, ur.max_payload_chars)
        rev = self.store.current
        candidates = list(rev.entries[: ur.max_candidates])
        not_assessed = [e.id for e in rev.entries[ur.max_candidates :]]
        rec: dict[str, Any] = {
            "step": self.step,
            "stage": self.stage,
            "revision": rev.number,
            "payload_version": PAYLOAD_VERSION,
            "payload_sources": sources,
            "payload": payload,
            "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "payload_chars": len(payload),
            "payload_truncated": truncated,
            "candidate_ids": [e.id for e in candidates],
            "not_assessed_ids": not_assessed,
            "advice": self.upd == "jev",
        }
        head = (
            f"\n\nUPDATE ({PAYLOAD_VERSION}; context revision {rev.number} was current at this "
            "release). Incident-board and change-log lines released this round, verbatim:\n"
        )
        if not payload:
            rec["status"] = "no_update"
            text = (
                f"\n\nUPDATE ({PAYLOAD_VERSION}; context revision {rev.number} was current at "
                "this release). No incident-board or change-log lines were released this round."
            )
        else:
            text = head + payload + "\nConsider whether this update affects your working context."
            rec["status"] = "shown"
        if self.upd == "jev" and payload:
            if not candidates:
                rec["status"] = "no_entries"
                text += "\nContext review advice: no current entries to assess."
            else:
                j = self.cfg.jev
                before = dict(self.jev_usage)
                attempts0 = self.c.jev_attempts
                batches = _entry_batches(candidates, j.batch_size, ur.max_batch_chars)
                answers, models, errors = self._jev_score(payload, batches)
                lines = _advice_lines(
                    rev.number, "the board and change-log lines above", answers, models, errors
                )
                if not_assessed:
                    lines.append(
                        f"Not assessed (over the {ur.max_candidates}-entry limit): {not_assessed}"
                    )
                advice = "\n".join(lines)
                text += "\n" + advice
                answered = sum(1 for v in answers.values() if v is not None)
                rec.update(
                    status="advised" if answered else "advice_failed",
                    batches=[len(b) for b in batches],
                    answers=answers,
                    models=sorted(models),
                    errors=errors,
                    attempts=self.c.jev_attempts - attempts0,
                    jev_input_tokens=self.jev_usage["input_tokens"] - before["input_tokens"],
                    jev_cost_usd=round(self.jev_usage["cost_usd"] - before["cost_usd"], 8),
                    jev_latency_s=round(self.jev_usage["latency_s"] - before["latency_s"], 4),
                    advice_chars=len(advice) + 1,
                )
        rec["appended_chars"] = len(text)
        self.updates.append(rec)
        self.trace.event("update_review", **rec)
        return text

    # ------------------------------------------------ Claude-initiated context review (exp 010)
    def begin_review_step(self, action: dict[str, Any]) -> None:
        """A review Claude started. Candidates come from the current revision by a structural
        role rule; in the assisted arm Jev scores them here, before Claude's next turn. Nothing in
        the context changes except the usual runtime append of this step's entries."""
        if self.ireq:
            self._begin_review_v2(action)
            return
        ir = self.cfg.initiated_review
        thought = str(action.get("thought", ""))[:1000]
        info = action["new_information"]
        rev = self.store.current
        used = sum(1 for r in self.ireviews if r["status"] != "rejected")
        rec: dict[str, Any] = {
            "step": self.step,
            "stage": self.stage,
            "stages_total": self.task.n_stages,
            "revision": rev.number,
            "new_information": info[:4000],
            "info_chars": len(info),
            "advice": self.irev == "jev",
        }
        error = (
            f"review limit reached ({ir.max_reviews} per task)"
            if used >= ir.max_reviews
            else "the new information is empty"
            if not info.strip()
            else f"the new information is {len(info)} chars; at most {ir.max_info_chars}"
            if len(info) > ir.max_info_chars
            else None
        )
        if error:
            rec.update(status="rejected", error=error)
            obs = f"Context review not started: {error}. Your context is unchanged."
        else:
            roles = set(ir.candidate_roles)
            eligible = [e for e in rev.entries if e.role in roles]
            excluded = [e.id for e in rev.entries if e.role not in roles]
            candidates = eligible[: ir.max_candidates]
            not_assessed = [e.id for e in eligible[ir.max_candidates :]]
            rec.update(
                candidate_ids=[e.id for e in candidates],
                excluded_ids=excluded,
                not_assessed_ids=not_assessed,
            )
            listed = ", ".join(e.id for e in candidates) or "none"
            lines = [
                f"Context review (initiated-review/1; context revision {rev.number}). New "
                f"information (as you stated it): {info}",
                f"Entries to review ({len(candidates)}): {listed}"
                + (
                    f" ({len(excluded)} runtime step or receipt entries not listed)"
                    if excluded
                    else ""
                ),
            ]
            if not_assessed:
                lines.append(
                    f"Not listed (over the {ir.max_candidates}-entry limit): {not_assessed}"
                )
            lines.append(
                "Review these entries against the new information and decide in your next step "
                "whether and how to edit context.json; you may also leave it unchanged. Your "
                "context is unchanged."
            )
            rec["status"] = "started"
            if self.irev == "jev":
                if not candidates:
                    rec["status"] = "no_entries"
                    lines.append("Context review advice: no entries to assess.")
                else:
                    before = dict(self.jev_usage)
                    attempts0 = self.c.jev_attempts
                    batches = _entry_batches(
                        candidates, self.cfg.jev.batch_size, ir.max_batch_chars
                    )
                    answers, models, errors = self._jev_score(info, batches)
                    advice = _advice_lines(
                        rev.number, "the new information you stated", answers, models, errors
                    )
                    lines += advice
                    answered = sum(1 for v in answers.values() if v is not None)
                    rec.update(
                        status="advised" if answered else "advice_failed",
                        batches=[len(b) for b in batches],
                        answers=answers,
                        models=sorted(models),
                        errors=errors,
                        attempts=self.c.jev_attempts - attempts0,
                        jev_input_tokens=self.jev_usage["input_tokens"] - before["input_tokens"],
                        jev_cost_usd=round(self.jev_usage["cost_usd"] - before["cost_usd"], 8),
                        jev_latency_s=round(self.jev_usage["latency_s"] - before["latency_s"], 4),
                        advice_chars=len("\n".join(advice)) + 1,
                    )
            obs = "\n".join(lines)
        rec["observation_chars"] = len(obs)
        self.ireviews.append(rec)
        self.trace.event("initiated_review", **rec)
        req = {k: v for k, v in action.items() if k != "thought"}
        self.store.append(
            [
                Entry(
                    f"s{self.step}.act",
                    "assistant",
                    f"[step {self.step}] begin_review {json.dumps(req)[:2500]}\nthought: {thought}",
                ),
                Entry(f"s{self.step}.obs", "observation", obs),
            ],
            "runtime",
            self.step,
        )
        self._save_revision()

    # ---------------------------------------------- 010 v2: required pre-edit review
    def _begin_review_v2(self, action: dict[str, Any]) -> None:
        """initiated-review/2: a review before a context edit. It lists the current revision's
        candidate entries (and, in the Jev condition, advice for update reviews) and issues one
        edit authorisation bound to the reviewed entries. Nothing in the context changes except
        the usual runtime append of this step's entries."""
        ir = self.cfg.initiated_review
        v3 = self.isrc == "latest-round"
        thought = str(action.get("thought", ""))[:1000]
        purpose = action.get("purpose") if v3 else action["purpose"]
        info = action.get("new_information", "") or ""
        rev = self.store.current
        used = sum(1 for r in self.ireviews if r["status"] != "rejected")
        rec: dict[str, Any] = {
            "step": self.step,
            "stage": self.stage,
            "stages_total": self.task.n_stages,
            "revision": rev.number,
            "purpose": purpose,
            "new_information": info[:4000],
            "info_chars": len(info),
            "advice": self.irev == "jev",
        }
        if v3:
            # initiated-review/3: the update is the latest released round's board and
            # change-log lines (round-files/1); Claude's label and note are recorded only.
            payload, sources, truncated = build_payload(
                self.task.stages[self.stage - 1], V3_PAYLOAD_MAX_CHARS
            )
            rec.update(
                update_source="latest-round",
                update_round=self.stage,
                payload_version=PAYLOAD_VERSION,
                payload=payload,
                payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                payload_chars=len(payload),
                payload_lines=len(payload.splitlines()),
                payload_sources=sources,
                payload_truncated=truncated,
                payload_max_chars=V3_PAYLOAD_MAX_CHARS,
                round_review_index=sum(
                    1
                    for r in self.ireviews
                    if r["status"] != "rejected" and r.get("update_round") == self.stage
                ),
            )
        error = (
            f"review limit reached ({ir.max_reviews} per task)"
            if used >= ir.max_reviews
            else f"the new information is {len(info)} chars; at most {ir.max_info_chars}"
            if v3 and len(info) > ir.max_info_chars
            else None
            if v3
            else "an update review needs the new information"
            if purpose == "update" and not info.strip()
            else f"the new information is {len(info)} chars; at most {ir.max_info_chars}"
            if len(info) > ir.max_info_chars
            else "a compaction review takes no new information (use purpose 'update')"
            if purpose == "compaction" and info.strip()
            else None
        )
        if error:
            rec.update(status="rejected", error=error)
            obs = (
                f"Context review not started: {error}. No edit authorisation was issued"
                + (" (an earlier one still stands)" if self.edit_auth else "")
                + ". Your context is unchanged."
            )
        else:
            roles = set(ir.candidate_roles)
            eligible = [e for e in rev.entries if e.role in roles]
            excluded = [e.id for e in rev.entries if e.role not in roles]
            candidates = eligible[: ir.max_candidates]
            not_assessed = [e.id for e in eligible[ir.max_candidates :]]
            rec.update(
                candidate_ids=[e.id for e in candidates],
                excluded_ids=excluded,
                not_assessed_ids=not_assessed,
            )
            listed = ", ".join(e.id for e in candidates) or "none"
            if v3:
                k = self.stage
                files = ", ".join(rec["payload_sources"]) or "none"
                head = [
                    f"Context review (initiated-review/3; context revision {rev.number}; your "
                    f"label: {purpose or 'none'}).",
                    f"Update for this review (runtime-supplied, {PAYLOAD_VERSION}): the "
                    f"incident-board and change-log lines released in round {k} "
                    f"({rec['payload_lines']} lines; files {files}; sha256 "
                    f"{rec['payload_sha256'][:12]})"
                    if payload
                    else f"Update for this review: none (round {k} released no incident-board "
                    "or change-log lines).",
                ]
            lines = [
                f"Context review (initiated-review/2; context revision {rev.number}; purpose: "
                f"{purpose})."
                + (f" New information (as you stated it): {info}" if purpose == "update" else ""),
                f"Entries to review ({len(candidates)}): {listed}"
                + (
                    f" ({len(excluded)} runtime step or receipt entries not listed)"
                    if excluded
                    else ""
                ),
            ]
            if v3:
                lines[0:1] = head
            if not_assessed:
                lines.append(
                    f"Not listed (over the {ir.max_candidates}-entry limit): {not_assessed}"
                )
            lines.append(
                "This review authorises one context.json edit in a later step, until you make "
                "that edit, start another review, or the next round is released. You may also "
                "decide not to edit. Your context is unchanged."
            )
            rec["status"] = "started"
            if v3:
                applicable = bool(payload) and bool(candidates)
                rec["applicable"] = applicable
                if not applicable:
                    rec["not_applicable_reason"] = (
                        "empty_update" if not payload else "no_candidates"
                    )
            if self.irev == "jev" and v3 and not rec["applicable"]:
                rec["status"] = "not_applicable"
                lines.append(
                    "Context review advice: not applicable ("
                    + (
                        f"round {self.stage} released no incident-board or change-log lines"
                        if rec["not_applicable_reason"] == "empty_update"
                        else "no entries to assess"
                    )
                    + ")."
                )
            elif self.irev == "jev" and v3:
                before = dict(self.jev_usage)
                attempts0 = self.c.jev_attempts
                batches = _entry_batches(candidates, self.cfg.jev.batch_size, ir.max_batch_chars)
                answers, models, errors = self._jev_score(payload, batches)
                advice = _advice_lines(
                    rev.number,
                    f"round {self.stage} board and change-log lines",
                    answers,
                    models,
                    errors,
                )
                lines += advice
                answered = sum(1 for v in answers.values() if v is not None)
                rec.update(
                    status="advised" if answered else "advice_failed",
                    batches=[len(b) for b in batches],
                    answers=answers,
                    models=sorted(models),
                    errors=errors,
                    attempts=self.c.jev_attempts - attempts0,
                    jev_input_tokens=self.jev_usage["input_tokens"] - before["input_tokens"],
                    jev_cost_usd=round(self.jev_usage["cost_usd"] - before["cost_usd"], 8),
                    jev_latency_s=round(self.jev_usage["latency_s"] - before["latency_s"], 4),
                    advice_chars=len("\n".join(advice)) + 1,
                )
            elif self.irev == "jev":
                if purpose == "compaction":
                    rec["status"] = "not_applicable"
                    lines.append(
                        "Context review advice: not applicable (compaction review: no new "
                        "information stated)."
                    )
                elif not candidates:
                    rec["status"] = "no_entries"
                    lines.append("Context review advice: no entries to assess.")
                else:
                    before = dict(self.jev_usage)
                    attempts0 = self.c.jev_attempts
                    batches = _entry_batches(
                        candidates, self.cfg.jev.batch_size, ir.max_batch_chars
                    )
                    answers, models, errors = self._jev_score(info, batches)
                    advice = _advice_lines(
                        rev.number, "the new information you stated", answers, models, errors
                    )
                    lines += advice
                    answered = sum(1 for v in answers.values() if v is not None)
                    rec.update(
                        status="advised" if answered else "advice_failed",
                        batches=[len(b) for b in batches],
                        answers=answers,
                        models=sorted(models),
                        errors=errors,
                        attempts=self.c.jev_attempts - attempts0,
                        jev_input_tokens=self.jev_usage["input_tokens"] - before["input_tokens"],
                        jev_cost_usd=round(self.jev_usage["cost_usd"] - before["cost_usd"], 8),
                        jev_latency_s=round(self.jev_usage["latency_s"] - before["latency_s"], 4),
                        advice_chars=len("\n".join(advice)) + 1,
                    )
            if self.edit_auth:
                self._end_edit_auth("replaced")
            self.edit_auth = {
                "step": self.step,
                "revision": rev.number,
                "purpose": purpose,
                "bodies": {e.id: e.body for e in rev.entries},
            }
            obs = "\n".join(lines)
        rec["observation_chars"] = len(obs)
        self.ireviews.append(rec)
        self.trace.event("initiated_review", **rec)
        req = {k: v for k, v in action.items() if k != "thought"}
        self.store.append(
            [
                Entry(
                    f"s{self.step}.act",
                    "assistant",
                    f"[step {self.step}] begin_review {json.dumps(req)[:2500]}\nthought: {thought}",
                ),
                Entry(f"s{self.step}.obs", "observation", obs),
            ],
            "runtime",
            self.step,
        )
        self._save_revision()

    def _authorise_edit(self, entries: tuple[Entry, ...]) -> str | None:
        """Called for a valid, content-changing context.json candidate (010 v2). Runtime appends
        since the review add entries but leave the reviewed ones intact, so they do not
        invalidate the authorisation; any change to a reviewed entry's body does."""
        a = self.edit_auth
        if a is None and self.recovery_active:
            # Over the hard limit the runtime demands a shrinking edit in this very step, so a
            # prior review is impossible; such edits are allowed and recorded as exemptions.
            self.recovery_exempt_steps.append(self.step)
            return None
        if a is None:
            return (
                f"{REVIEW_REQUIRED}: start one with begin_review, then edit context.json in a "
                "later step"
            )
        current = {e.id: e.body for e in self.store.current.entries}
        if any(current.get(i) != body for i, body in a["bodies"].items()):
            self._end_edit_auth("invalidated")
            return (
                f"{REVIEW_REQUIRED}: the review at step {a['step']} no longer matches the current "
                "context; start a new review"
            )
        return None

    def _settle_edit_auth(self, out: EditOutcome) -> None:
        if out.status == "accepted" and self.edit_auth:
            review_step = self.edit_auth["step"]
            self._end_edit_auth("consumed", after_revision=out.after.number if out.after else None)
            self.edit_log[-1]["authorised_by_review_step"] = review_step
        elif out.status == "accepted" and self.step in self.recovery_exempt_steps:
            self.edit_log[-1]["recovery_exempt"] = True
        elif out.status == "rejected" and out.reason.startswith(REVIEW_REQUIRED):
            self.blocked_edits.append(self.step)
            self.trace.event("edit_blocked_no_review", step=self.step, reason=out.reason[:300])

    def _end_edit_auth(self, outcome: str, **extra: Any) -> None:
        a = self.edit_auth
        if a is None:
            return
        self.auth_log.append(
            {
                "review_step": a["step"],
                "revision": a["revision"],
                "purpose": a["purpose"],
                "outcome": outcome,
                "ended_step": self.step,
                **extra,
            }
        )
        self.trace.event("edit_authorisation", **self.auth_log[-1])
        self.edit_auth = None

    def _requests_carrying(self, entry_id: str, after_step: int) -> int:
        """How many later model requests still contained ``entry_id`` (advice residence)."""
        n = 0
        for t in range(after_step + 1, self.step + 1):
            revs = [r for r in self.store.revisions if r.step < t]
            if revs and any(e.id == entry_id for e in revs[-1].entries):
                n += 1
        return n

    def initiated_review_summary(self) -> dict[str, Any]:
        """Per review: what was listed and scored, and the accepted edits before the next review.
        "Followed by an edit" is temporal association only, not attribution to the advice."""
        recs = []
        started = [r for r in self.ireviews if r["status"] != "rejected"]
        for i, r in enumerate(started):
            nxt = started[i + 1]["step"] if i + 1 < len(started) else 10**9
            later = [e for e in self.edit_log if r["step"] < e["step"] < nxt]
            changed = {x for e in later for x in (*e["removed"], *e["rewritten"])}
            ans = r.get("answers") or {}
            flagged = [k for k, v in ans.items() if v is not None and v >= 0.5]
            recs.append(
                {
                    **r,
                    "reporting_threshold": 0.5,
                    "flagged": flagged,
                    "later_accepted_edits": [
                        {k: e[k] for k in ("step", "before_revision", "after_revision")}
                        for e in later
                    ],
                    "edited_next_step": any(e["step"] == r["step"] + 1 for e in later),
                    "candidates_changed": sorted(x for x in r["candidate_ids"] if x in changed),
                    "flagged_then_changed": sorted(k for k in flagged if k in changed),
                    "flagged_not_changed": sorted(k for k in flagged if k not in changed),
                    "unflagged_then_changed": sorted(
                        k for k, v in ans.items() if (v is None or v < 0.5) and k in changed
                    ),
                    "observation_requests_carried": self._requests_carrying(
                        f"s{r['step']}.obs", r["step"]
                    ),
                }
            )
        rejected = [r for r in self.ireviews if r["status"] == "rejected"]
        v2: dict[str, Any] = {}
        if self.ireq:
            auths = list(self.auth_log)
            if self.edit_auth:
                a = self.edit_auth
                auths.append(
                    {
                        "review_step": a["step"],
                        "revision": a["revision"],
                        "purpose": a["purpose"],
                        "outcome": "unused",
                        "ended_step": None,
                    }
                )
            by_step = {a["review_step"]: a for a in auths}
            for r in recs:
                a = by_step.get(r["step"], {})
                r["authorisation_outcome"] = a.get("outcome")
                r["authorised_edit_step"] = (
                    a.get("ended_step") if a.get("outcome") == "consumed" else None
                )
            outcomes = [a["outcome"] for a in auths]
            v2 = {
                "required": True,
                "reviews_update": sum(1 for r in started if r["purpose"] == "update"),
                "reviews_compaction": sum(1 for r in started if r["purpose"] == "compaction"),
                "reviews_not_applicable": sum(
                    1 for r in started if r["status"] == "not_applicable"
                ),
                "edits_blocked": len(self.blocked_edits),
                "blocked_edit_steps": list(self.blocked_edits),
                "accepted_edits_authorised": sum(
                    1 for e in self.edit_log if "authorised_by_review_step" in e
                ),
                "accepted_edits_recovery_exempt": sum(
                    1 for e in self.edit_log if e.get("recovery_exempt")
                ),
                "accepted_edits_unauthorised": sum(
                    1
                    for e in self.edit_log
                    if "authorised_by_review_step" not in e and not e.get("recovery_exempt")
                ),
                "authorisations": auths,
                "authorisation_outcomes": {o: outcomes.count(o) for o in sorted(set(outcomes))},
            }
            if self.isrc == "latest-round":
                reasons = [
                    str(r.get("not_applicable_reason")) for r in started if not r["applicable"]
                ]
                v2.update(
                    update_source="latest-round",
                    payload_version=PAYLOAD_VERSION,
                    reviews_applicable=sum(1 for r in started if r["applicable"]),
                    reviews_not_applicable_by_reason={
                        x: reasons.count(x) for x in sorted(set(reasons))
                    },
                    reviews_repeat_round=sum(1 for r in started if r["round_review_index"] > 0),
                    reviews_by_label={
                        str(lab): sum(1 for r in started if r["purpose"] == lab)
                        for lab in sorted({r["purpose"] for r in started}, key=str)
                    },
                )
        return {
            **v2,
            "mode": self.irev,
            "version": (
                "initiated-review/3"
                if self.isrc == "latest-round"
                else "initiated-review/2"
                if self.ireq
                else "initiated-review/1"
            )
            if self.irev != "off"
            else None,
            "question_version": QUESTION_VERSION if self.irev == "jev" else None,
            "model_requested": self.cfg.jev.model if self.irev == "jev" else None,
            "reviews": len(started),
            "reviews_rejected": len(rejected),
            "rejections": [{k: r[k] for k in ("step", "error")} for r in rejected],
            "reviews_before_last_stage": sum(1 for r in started if r["stage"] < r["stages_total"]),
            "reviews_advised": sum(1 for r in started if r["status"] == "advised"),
            "reviews_advice_failed": sum(1 for r in started if r["status"] == "advice_failed"),
            "entries_listed": sum(len(r["candidate_ids"]) for r in started),
            "entries_scored": sum(len(r.get("answers") or {}) for r in started),
            "observation_chars": sum(r["observation_chars"] for r in self.ireviews),
            "advice_chars": sum(r.get("advice_chars", 0) for r in started),
            "jev_attempts": sum(r.get("attempts", 0) for r in started),
            "jev_input_tokens": sum(r.get("jev_input_tokens", 0) for r in started),
            "jev_cost_usd": round(sum(r.get("jev_cost_usd", 0.0) for r in started), 8),
            "jev_latency_s": round(sum(r.get("jev_latency_s", 0.0) for r in started), 4),
            "records": recs,
        }

    def update_review_summary(self) -> dict[str, Any]:
        """Per update: what was shown and scored, and the accepted edits before the next update.
        "Followed by an edit" is temporal association only, not attribution to the advice."""
        recs = []
        for i, r in enumerate(self.updates):
            nxt = self.updates[i + 1]["step"] if i + 1 < len(self.updates) else 10**9
            later = [e for e in self.edit_log if r["step"] < e["step"] < nxt]
            changed = {x for e in later for x in (*e["removed"], *e["rewritten"])}
            ans = r.get("answers") or {}
            flagged = [k for k, v in ans.items() if v is not None and v >= 0.5]
            recs.append(
                {
                    **r,
                    "reporting_threshold": 0.5,
                    "flagged": flagged,
                    "later_accepted_edits": [
                        {k: e[k] for k in ("step", "before_revision", "after_revision")}
                        for e in later
                    ],
                    "candidates_changed": sorted(x for x in r["candidate_ids"] if x in changed),
                    "flagged_then_changed": sorted(k for k in flagged if k in changed),
                    "flagged_not_changed": sorted(k for k in flagged if k not in changed),
                    "unflagged_then_changed": sorted(
                        k for k, v in ans.items() if (v is None or v < 0.5) and k in changed
                    ),
                }
            )
        shown = [r for r in self.updates if r["status"] != "no_update"]
        advised = [r for r in self.updates if r["status"] == "advised"]
        return {
            "mode": self.upd,
            "payload_version": PAYLOAD_VERSION if self.upd != "off" else None,
            "question_version": QUESTION_VERSION if self.upd == "jev" else None,
            "model_requested": self.cfg.jev.model if self.upd == "jev" else None,
            "updates": len(self.updates),
            "updates_with_payload": len(shown),
            "updates_advised": len(advised),
            "updates_advice_failed": sum(1 for r in self.updates if r["status"] == "advice_failed"),
            "entries_scored": sum(len(r.get("answers") or {}) for r in advised),
            "appended_chars": sum(r["appended_chars"] for r in self.updates),
            "advice_chars": sum(r.get("advice_chars", 0) for r in self.updates),
            "jev_attempts": sum(r.get("attempts", 0) for r in self.updates),
            "jev_input_tokens": sum(r.get("jev_input_tokens", 0) for r in self.updates),
            "jev_cost_usd": round(sum(r.get("jev_cost_usd", 0.0) for r in self.updates), 8),
            "jev_latency_s": round(sum(r.get("jev_latency_s", 0.0) for r in self.updates), 4),
            "records": recs,
        }

    def jev_summary(self) -> dict[str, Any]:
        """Reviews, usage, and which flagged entries were later changed by accepted edits.
        "Followed by an edit" is temporal association only, not attribution to the advice."""
        reviews = []
        for i, r in enumerate(self.reviews):
            nxt = self.reviews[i + 1]["step"] if i + 1 < len(self.reviews) else 10**9
            later = [e for e in self.edit_log if r["step"] < e["step"] < nxt]
            changed = {x for e in later for x in (*e["removed"], *e["rewritten"])}
            ans = r.get("answers") or {}
            reviews.append(
                {
                    **r,
                    "reporting_threshold": 0.5,
                    "flagged": [k for k, v in ans.items() if v is not None and v >= 0.5],
                    "later_accepted_edit_steps": [e["step"] for e in later],
                    "flagged_then_changed": sorted(
                        k for k, v in ans.items() if v is not None and v >= 0.5 and k in changed
                    ),
                    "unflagged_then_changed": sorted(
                        k for k, v in ans.items() if (v is None or v < 0.5) and k in changed
                    ),
                }
            )
        return {
            "enabled": self.jev_on,
            # Run-wide Jev usage (attempts, errors, tokens, cost): from the optional
            # review_context action (008) or from update-triggered review (009).
            "source": "review_context"
            if self.jev_on
            else "update_review"
            if self.upd == "jev"
            else "initiated_review"
            if self.irev == "jev"
            else None,
            "question_version": QUESTION_VERSION if self.jev_on else None,
            "model_requested": self.cfg.jev.model if self.jev_on else None,
            "reviews": len(self.reviews) - self.c.jev_reviews_rejected,
            "reviews_rejected": self.c.jev_reviews_rejected,
            "attempts": self.c.jev_attempts,
            "errors": self.c.jev_errors,
            "retries": self.c.jev_retries,
            "usage_typesafe_tokens": {
                "input_tokens": self.jev_usage["input_tokens"],
                "output_tokens": self.jev_usage["output_tokens"],
            },
            "cost_usd": round(self.jev_usage["cost_usd"], 8),
            "latency_s": round(self.jev_usage["latency_s"], 4),
            "review_records": reviews,
        }

    # ------------------------------------------- Jev primitive in model code (exp 014)
    def _record_primitive(self, rec: dict[str, Any]) -> None:
        rec = {"step": self.step, **rec}
        n = len(self.prim_log) + 1
        self.trace.write_json(f"jev_primitive/{n:04d}.json", rec)
        self.prim_log.append(
            {k: v for k, v in rec.items() if k not in ("request", "body", "response")}
            | {
                "record": f"jev_primitive/{n:04d}.json",
                "items": len((rec.get("request") or {}).get("items", [])),
            }
        )
        self.trace.event(
            "jev_primitive", step=self.step, id=rec.get("id"), status=rec.get("status")
        )

    def jev_primitive_summary(self) -> dict[str, Any]:
        by: dict[str, int] = {}
        for r in self.prim_log:
            by[r["status"]] = by.get(r["status"], 0) + 1
        return {
            "enabled": self.jprim,
            "version": PRIMITIVE_VERSION,
            "requests": len(self.prim_log),
            "calls": self.prim_calls,
            "by_status": by,
            "items": sum(r["items"] for r in self.prim_log),
            "unanswered_at_exit": self.prim_unanswered,
            "steps_with_calls": sorted({r["step"] for r in self.prim_log}),
            "log": self.prim_log,
        }

    # ------------------------------------------- search-result screening (experiment 012)
    def _present_search(self, res: ExecResult, thought: str, code: str) -> ExecResult:
        """Both conditions: search blocks cut by the output cap keep complete passages; with
        screening on, eligible blocks are screened. The observation is built from the result."""
        out = self._screen_text(res.stdout, thought, "show")
        reads = recovery_reads(out.text, code)
        if reads["show_calls"] or reads["saved_reads"] or reads["doc_reads"]:
            omitted = {i for r in self.screen_log for i in r.get("omitted", [])}
            reads.update(step=self.step, omitted_recovered=sorted(omitted & set(reads["show_ids"])))
            self.recovery_log.append(reads)
        if out.text == res.stdout:
            return res
        whole = res.stdout_bytes <= len(res.stdout.encode())
        return replace(
            res, stdout=out.text, stdout_bytes=len(out.text.encode()) if whole else res.stdout_bytes
        )

    def _screen_text(self, text: str, thought: str, recover: str) -> Any:
        """Present (both conditions) and, in B, screen the search blocks in ``text``; record."""
        call = self._jev_attempts if self.rscr == "jev" else None
        cfg = self.cfg.result_screen
        out = screen_stdout(
            text,
            self.task.prompt,
            thought,
            self.cfg.jev.model,
            call,
            self.ws,
            cfg.threshold,
            cfg.min_keep,
            recover,
        )
        for rec in out.records:
            rec["step"] = self.step
            if "request" in rec:
                n = sum(1 for r in self.screen_log if "request" in r) + 1
                self.trace.write_json(f"result_screen/{self.step:03d}-{rec['rid']}.json", rec)
                rec = {k: v for k, v in rec.items() if k != "request"}
                rec["record"] = f"result_screen/{self.step:03d}-{rec['rid']}.json"
                rec["seq"] = n
            self.screen_log.append(rec)
            self.trace.event(
                "search_result", **{k: v for k, v in rec.items() if k != "probabilities"}
            )
        return out

    def service_step(self, action: dict[str, Any]) -> None:
        """Experiment 012 v2: the runtime's document-service actions. Both conditions get the
        same results; only search results pass through screening (in B). Fetches are never
        screened. The observation is capped at the output cap with whole passages."""
        from .docservice import MAX_K

        svc = self.service
        assert svc is not None
        thought = str(action.get("thought", ""))[:1000]
        cap = self.lim.output_cap_chars
        kind = action["action"]
        rec: dict[str, Any] = {"step": self.step, "action": kind}
        if kind == "search":
            q, k = action["query"].strip(), int(action.get("k", 5))
            hits = svc.search(q, k)
            n = sum(1 for r in self.service_log if r["action"] == "search") + 1
            rid = f"SR-{n:03d}"
            qs = q.replace('"', "'")[:200]
            sd = self.ws / "search-results"
            sd.mkdir(exist_ok=True)
            path = f"/task/workspace/search-results/{rid}.txt"
            (sd / f"{rid}.json").write_text(
                json.dumps({"id": rid, "query": qs, "k": k, "passages": hits}), encoding="utf-8"
            )
            saved = [
                f'# saved search result {rid}: query "{qs}", k={k}, {len(hits)} passages (original, unscreened)'
            ]
            for p in hits:
                saved += ["--- " + svc.header(p)[4:], p["text"]]
            (sd / f"{rid}.txt").write_text("\n".join(saved) + "\n", encoding="utf-8")
            lines = [
                f'<<search-result {rid} query="{qs}" k={k} returned={len(hits)} saved={path}>>'
            ]
            for p in hits:
                lines += [svc.header(p), p["text"]]
            lines.append(f"<<end search-result {rid}>>")
            text = "\n".join(lines) + "\n"
            if len(text) > cap:
                text = text[:cap]  # cut like any capped output; presentation keeps whole passages
            out = self._screen_text(text, thought, "fetch")
            obs = out.text
            rec.update(rid=rid, query=qs, k=k, returned=len(hits), max_k=MAX_K)
        else:
            got = svc.fetch(action["ids"])
            omitted = {i for r in self.screen_log for i in r.get("omitted", [])}
            ids = [i for i, _ in got]
            lines = ["<<passages (unscreened): " + ", ".join(ids) + ">>"]
            shown = 0
            for i, p in got:
                block = (
                    ["--- " + svc.header(p)[4:], p["text"]]
                    if p
                    else [f"--- {i}: no such passage or document"]
                )
                if len("\n".join(lines + block)) > cap - 200:
                    break
                lines += block
                shown += 1
            if shown < len(got):
                lines.append(
                    f"[output cap: {len(got) - shown} requested passages not shown; fetch fewer ids]"
                )
            lines.append("<<end passages>>")
            obs = "\n".join(lines) + "\n"
            rec.update(
                ids=ids,
                shown=shown,
                missing=[i for i, p in got if p is None],
                omitted_recovered=sorted(omitted & set(ids)),
            )
            if rec["omitted_recovered"]:
                self.recovery_log.append(
                    {
                        "step": self.step,
                        "fetch_ids": ids,
                        "omitted_recovered": rec["omitted_recovered"],
                        "show_calls": 0,
                        "show_ids": [],
                        "saved_reads": 0,
                        "doc_reads": 0,
                    }
                )
        rec["obs_chars"] = len(obs)
        self.service_log.append(rec)
        self.trace.event("service_" + kind, **rec)
        params = {k: action[k] for k in ("query", "k", "ids") if k in action}
        self.store.append(
            [
                Entry(
                    f"s{self.step}.act",
                    "assistant",
                    f"[step {self.step}] {kind} {json.dumps(params, ensure_ascii=False)}\nthought: {thought}",
                ),
                Entry(f"s{self.step}.obs", "observation", obs),
            ],
            "runtime",
            self.step,
        )
        self._save_revision()
        if self.clm:
            self.store.mirror(self.ws)

    def result_screen_summary(self) -> dict[str, Any]:
        blocks = [r for r in self.screen_log if r.get("kind") == "block"]
        screened = [r for r in blocks if r.get("screened")]
        return {
            "mode": self.rscr,
            "version": SCREEN_VERSION,
            "threshold": self.cfg.result_screen.threshold,
            "min_keep": self.cfg.result_screen.min_keep,
            "blocks": len(blocks),
            "eligible": sum(1 for r in blocks if r["eligible"]),
            "screened": len(screened),
            "errors": sum(1 for r in blocks if r.get("error")),
            "skipped_provenance": sum(1 for r in blocks if r.get("skipped")),
            "provenance_ok": sum(1 for r in blocks if r.get("provenance_ok")),
            "cap_repairs": sum(1 for r in self.screen_log if r.get("kind") == "cap_repair"),
            "passages_in": sum(r["passages"] for r in blocks),
            "passages_screened": sum(r["passages"] for r in screened),
            "passages_omitted": sum(len(r["omitted"]) for r in screened),
            "missing_answers": sum(len(r["missing_answers"]) for r in screened),
            "chars_in": sum(r["chars"] for r in blocks),
            "chars_screened": sum(r["chars"] for r in screened),
            "chars_kept": sum(r["kept_chars"] for r in screened),
            "notice_chars": sum(r.get("notice_chars", 0) for r in blocks),
            "service": {
                "searches": sum(1 for r in self.service_log if r["action"] == "search"),
                "fetches": sum(1 for r in self.service_log if r["action"] == "fetch"),
                "fetched_ids": sum(len(r.get("ids", [])) for r in self.service_log),
                "fetch_omitted_recovered": sorted(
                    {i for r in self.service_log for i in r.get("omitted_recovered", [])}
                ),
                "log": self.service_log,
            },
            "recovery": {
                "steps": len(self.recovery_log),
                "show_calls": sum(r["show_calls"] for r in self.recovery_log),
                "saved_reads": sum(r["saved_reads"] for r in self.recovery_log),
                "doc_reads": sum(r["doc_reads"] for r in self.recovery_log),
                "omitted_recovered": sorted(
                    {i for r in self.recovery_log for i in r["omitted_recovered"]}
                ),
            },
            "block_log": blocks,
            "recovery_log": self.recovery_log,
        }

    # ------------------------------------------- proposed-edit checking (011 agent pilot)
    def _note_reads(self, code: str) -> None:
        """Rereading evidence (all conditions): executions whose code opens requirement files or
        runs the visible tests."""
        reads = "REQUIREMENTS.md" in code or "/task/fixtures/stage-" in code
        tests = "current-tests" in code or "unittest" in code
        if reads or tests:
            self.code_reads.append(
                {"step": self.step, "stage": self.stage, "requirements": reads, "tests": tests}
            )

    @staticmethod
    def _ids_diff(
        before: tuple[Entry, ...], after: tuple[Entry, ...]
    ) -> tuple[list[str], list[str], list[str]]:
        b = {e.id: e.body for e in before}
        a_ids = [e.id for e in after]
        removed = [e.id for e in before if e.id not in a_ids]
        added = [i for i in a_ids if i not in b]
        rewritten = [e.id for e in after if e.id in b and b[e.id] != e.body]
        return removed, added, rewritten

    def _propose(self, out: EditOutcome, res: ExecResult) -> str:
        """Hold a valid, content-changing candidate as the pending proposal (B/C); in C, check it
        with Jev before the next model call. The live context is not changed."""
        assert out.before is not None and out.candidate is not None
        base, cand = out.before, out.candidate
        replaced = self._end_proposal("revised") if self.proposal else None
        pid = f"P{len(self.proposals) + 1}"
        removed, added, rewritten = self._ids_diff(base.entries, cand)
        rec: dict[str, Any] = {
            "id": pid,
            "step": self.step,
            "stage": self.stage,
            "base_revision": base.number,
            "base_sha256": base.sha256,
            "candidate_sha256": content_hash(cand),
            "replaces": replaced,
            "entries_before": len(base.entries),
            "entries_after": len(cand),
            "chars_before": total_chars(base.entries),
            "chars_after": total_chars(cand),
            "removed": removed,
            "added": added,
            "rewritten": rewritten,
            "status": "pending",
            "check": None,
        }
        self.trace.write_json(
            f"context/proposal-{pid}.json",
            {**rec, "candidate": [e.to_dict() for e in cand]},
        )
        if self.echk == "jev":
            rec["check"] = self._check_proposal(pid, base, cand, res.stdout)
        self.proposals.append(rec)
        self.proposal = {**rec, "base": base, "candidate": cand}
        self.trace.event("context_proposal", **{k: v for k, v in rec.items() if k != "check"})

        def ids(xs: list[str]) -> str:
            return ", ".join(xs[:12]) + (f" (+{len(xs) - 12} more)" if len(xs) > 12 else "")

        text = (
            f"context.json change at step {self.step} held as proposal {pid} (not applied). Live "
            f"context: revision {base.number}, unchanged. Proposal versus live: entries "
            f"{len(base.entries)} -> {len(cand)}; body chars {total_chars(base.entries)} -> "
            f"{total_chars(cand)}; removes [{ids(removed)}]; adds [{ids(added)}]; rewrites "
            f"[{ids(rewritten)}]. Full proposed file: /task/workspace/{PROPOSAL_FILENAME}."
        )
        if replaced:
            text += f" It replaces pending proposal {replaced}."
        text += (
            f' To apply it reply {{"action": "confirm_edit", "proposal": "{pid}", "decision": '
            '"accept"} (entries the runtime appends meanwhile are kept after it); "withdraw" '
            "discards it; writing context.json again replaces it. Other files your code changed "
            "are not affected by this decision."
        )
        if rec["check"] is not None:
            text += "\n" + self._check_text(pid, rec["check"])
        return text

    def _check_proposal(
        self, pid: str, base: Revision, cand: tuple[Entry, ...], stdout: str
    ) -> dict[str, Any]:
        """One Jev check (jev-edit-check/2 on edit-evidence/1). Errors, missing answers and the
        check limit are reported as such, never as a clear result."""
        ec = self.cfg.edit_check
        if self.ec_checks >= ec.max_checks:
            return {"verdict": "not_checked", "reason": f"check limit {ec.max_checks} reached"}
        self.ec_checks += 1
        case = {
            "task": self.task.prompt,
            "current": [e.to_dict() for e in base.entries],
            "update": self.task.stage_updates[self.stage - 1] if self.task.staged else "",
            "exec_output": stdout,
            "sources": self.task.files_upto(self.stage),
            "proposed": [e.to_dict() for e in cand],
        }
        body, meta = build_edit_request_v2(case, self.cfg.jev.model)
        t0 = time.monotonic()
        resp, err = self._jev_attempts(body)
        answers: dict[str, float | None] = {
            k: (resp.probabilities.get(k) if resp else None) for k in QUESTIONS_V2
        }
        t = ec.threshold
        harm = [answers[k] for k in HARM_KEYS]
        if resp is None:
            verdict = "error"
        elif any(v is not None and v >= t for v in harm):
            verdict = "flagged"
        elif any(v is None for v in answers.values()):
            verdict = "incomplete"
        elif (answers["insufficient"] or 0.0) >= t:
            verdict = "uncertain"
        else:
            verdict = "clear"
        check = {
            "verdict": verdict,
            "answers": answers,
            "error": err,
            "evidence": meta,
            "model": resp.model if resp else None,
            "jev_seq": self.jev_seq,
            "wall_s": round(time.monotonic() - t0, 4),
        }
        self.trace.write_json(f"edit_check/{pid}.json", {"proposal": pid, "request": body, **check})
        self.trace.event("edit_check", step=self.step, proposal=pid, verdict=verdict)
        return check

    def _check_text(self, pid: str, check: dict[str, Any]) -> str:
        """The fixed, compact presentation of a check (C only)."""
        v = check["verdict"]
        if v == "not_checked":
            return (
                f"Jev check of {pid}: not checked ({check['reason']}). This is not a clear result."
            )
        if v == "error":
            return (
                f"Jev check of {pid}: unavailable (service error: {str(check['error'])[:120]}). "
                "This is not a clear result."
            )
        a = check["answers"]
        t = self.cfg.edit_check.threshold
        parts = [
            f"{EC_LABELS[k]} {'no answer' if a[k] is None else f'{a[k]:.2f}'}"
            + (" (flag)" if a[k] is not None and a[k] >= t else "")
            for k in QUESTIONS_V2
        ]
        result = {
            "flagged": f"FLAGGED for inspection (a harm answer is at least {t})",
            "uncertain": "UNCERTAIN (the material supplied may be insufficient to check it)",
            "incomplete": "INCOMPLETE answer (some questions unanswered); not a clear result",
            "clear": f"clear (every answer below {t})",
        }[v]
        notes = check["evidence"].get("notes") or []
        text = (
            f"Jev check of {pid} (advice only; {QUESTION_VERSION_V2}): " + " | ".join(parts) + "."
            f" Result: {result}."
        )
        if notes:
            text += " Evidence notes: " + "; ".join(str(n) for n in notes)[:400] + "."
        return text + (
            " A flag means inspect, not proven wrong; missing evidence may explain it. A clear "
            "result does not guarantee the change is safe."
        )

    def _mirror_proposal(self) -> None:
        """Keep the informational proposal file in step with the host-held proposal."""
        if self.echk == "off":
            return
        path = self.ws / PROPOSAL_FILENAME
        if self.proposal:
            path.write_text(serialize(self.proposal["candidate"]), encoding="utf-8")
        elif path.is_file() and not path.is_symlink():
            path.unlink()

    def _end_proposal(self, status: str, **extra: Any) -> str | None:
        """Close the pending proposal (revised, withdrawn, expired, unresolved...)."""
        p = self.proposal
        if p is None:
            return None
        rec = next(r for r in self.proposals if r["id"] == p["id"])
        rec.update(status=status, decided_step=self.step, **extra)
        self.trace.event("context_proposal_end", proposal=p["id"], status=status, step=self.step)
        self.proposal = None
        return str(p["id"])

    def _recovery_alternative(self) -> str:
        """B/C: under OVER LIMIT, name a still-acceptable pending proposal and the size it would
        leave (added after the integration smoke, before the pilot freeze)."""
        p = self.proposal
        if self.echk == "off" or p is None:
            return ""
        live, n = self.store.entries, len(p["base"].entries)
        if live[:n] != p["base"].entries:
            return ""
        chars = total_chars(p["candidate"]) + total_chars(live[n:])
        return (
            f" In this run, accepting pending proposal {p['id']} with confirm_edit also counts: "
            f"it would leave about {chars} body chars (now {total_chars(live)}) before this "
            "step's own output. A change you write to context.json instead is applied directly."
        )

    def _recovery_applied(self) -> str:
        """An OVER LIMIT recovery edit applied directly in B/C (declared exception)."""
        superseded = self._end_proposal("superseded_by_recovery_edit")
        self.proposals.append(
            {
                "id": f"R{self.step}",
                "step": self.step,
                "stage": self.stage,
                "status": "recovery_exempt_applied",
                "supersedes": superseded,
                "check": None,
            }
        )
        self.trace.event("context_recovery_exempt", step=self.step)
        return " Applied directly (OVER LIMIT exception; not held as a proposal)" + (
            f"; pending proposal {superseded} is superseded." if superseded else "."
        )

    def confirm_step(self, action: dict[str, Any]) -> None:
        """Accept or withdraw the pending proposal; nothing else applies one."""
        pid = str(action.get("proposal", "")).strip()
        decision = action["decision"]
        thought = str(action.get("thought", ""))[:1000]
        p = self.proposal
        rec: dict[str, Any] = {"step": self.step, "proposal": pid, "decision": decision}
        receipt = ""
        if p is None or p["id"] != pid:
            rec["result"] = "no_such_pending_proposal"
            obs = (
                f"confirm_edit {decision} {pid}: no such pending proposal "
                f"({'pending: ' + p['id'] if p else 'none is pending'}). Context unchanged."
            )
        elif decision == "withdraw":
            self._end_proposal("withdrawn")
            rec["result"] = "withdrawn"
            obs = (
                f"Proposal {pid} withdrawn: the live context (revision "
                f"{self.store.current.number}) is unchanged. Other file changes made by the code "
                f"of step {p['step']} remain."
            )
        else:
            before = self.store.current
            after, why = self.store.accept_proposal(p["candidate"], p["base"], self.step)
            if after is None:
                self._end_proposal("refused_stale" if why.startswith("stale") else "refused")
                rec.update(result="refused", reason=why)
                obs = f"Proposal {pid} cannot be accepted ({why}); context unchanged."
            else:
                self._end_proposal("accepted", accepted_revision=after.number)
                rec.update(result="accepted", revision=after.number)
                receipt = (
                    self._record_edit(EditOutcome("accepted", before=before, after=after)) or ""
                )
                obs = f"Proposal {pid} accepted. {receipt}"
        self.confirms.append(rec)
        self.trace.event("confirm_edit", **rec)
        self.store.append(
            [
                Entry(
                    f"s{self.step}.act",
                    "assistant",
                    f"[step {self.step}] confirm_edit {pid} {decision}\nthought: {thought}",
                ),
                Entry(f"s{self.step}.obs", "observation", obs),
            ],
            "runtime",
            self.step,
        )
        self._save_revision()
        self.store.mirror(self.ws)
        self._mirror_proposal()

    def edit_check_summary(self) -> dict[str, Any]:
        props = [p for p in self.proposals if not p["id"].startswith("R")]
        by_status: dict[str, int] = {}
        for p in props:
            by_status[p["status"]] = by_status.get(p["status"], 0) + 1
        checks = [p["check"] for p in props if p["check"]]
        verdicts: dict[str, int] = {}
        for c in checks:
            verdicts[c["verdict"]] = verdicts.get(c["verdict"], 0) + 1
        reactions: dict[str, dict[str, int]] = {}
        for p in props:
            if p["check"]:
                r = reactions.setdefault(p["check"]["verdict"], {})
                r[p["status"]] = r.get(p["status"], 0) + 1
        return {
            "mode": self.echk,
            "version": "edit-check-pilot/1",
            "question_version": QUESTION_VERSION_V2 if self.echk == "jev" else None,
            "questions_sha256": questions_v2_sha256() if self.echk == "jev" else None,
            "evidence_version": EVIDENCE_VERSION if self.echk == "jev" else None,
            "threshold": self.cfg.edit_check.threshold,
            "max_checks": self.cfg.edit_check.max_checks,
            "proposals": len(props),
            "proposals_by_status": by_status,
            "recovery_exempt_edits": sum(1 for p in self.proposals if p["id"].startswith("R")),
            "confirm_actions": len(self.confirms),
            "confirm_invalid": sum(
                1 for c in self.confirms if c["result"] == "no_such_pending_proposal"
            ),
            "checks": len(checks),
            "verdicts": verdicts,
            "reactions_by_verdict": reactions,
            "flagged_then_accepted": reactions.get("flagged", {}).get("accepted", 0),
            "uncertain_then_accepted": reactions.get("uncertain", {}).get("accepted", 0),
            "requirement_reads": sum(1 for r in self.code_reads if r["requirements"]),
            "test_runs": sum(1 for r in self.code_reads if r["tests"]),
            "proposal_log": self.proposals,
            "confirm_log": self.confirms,
        }

    def premature_final_step(self, action: dict[str, Any]) -> None:
        self.c.premature_finals += 1
        self.trace.event(
            "premature_final", step=self.step, stage=self.stage, answer=action.get("answer")
        )
        obs = (
            f"Final answer not accepted: {self.stage} of {self.task.n_stages} evidence stages "
            "released. Use advance to release the remaining stages."
        )
        self.store.append(
            [
                Entry(
                    f"s{self.step}.act",
                    "assistant",
                    f"[step {self.step}] final (not accepted)\n{json.dumps(action.get('answer'))[:2000]}",
                ),
                Entry(f"s{self.step}.obs", "observation", obs),
            ],
            "runtime",
            self.step,
        )
        self._save_revision()

    def execute(self) -> RunResult:
        self.task.write_fixtures(self.fx, 1 if self.task.staged else None)
        (self.ws / "helpers").mkdir()
        for rel, text in self.task.workspace_seed.items():
            path = self.ws / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        run_meta = {
            "run_id": self.run_id,
            "label": self.label,
            "mode": self.mode,
            "comparison_arm": self.mode in ("summary", "clm"),
            "task": self.task.spec.name,
            "task_kind": self.task.kind,
            "split": self.task.spec.split,
            "scenario_hidden_from_model": True,
            "fixture_seed": self.task.spec.seed,
            "fixture_sha256": self.task.fixture_sha256,
            "generator_version": self.task.generator_version,
            "stages": self.task.n_stages,
            "prompt_version": PROMPT_VERSION,
            "summary_policy": self.policy.policy if self.mode == "summary" else None,
            "system_prompt_sha256": sha256_text(self.prompt_system),
            "task_prompt_sha256": sha256_text(self.task.prompt),
            "extra_instruction": self.extra_instruction,
            "request": self.request_settings(),
            "provider": self.r.provider.describe(),
            "price": self.r.price.__dict__ if self.r.price else None,
            "executor": self.r.executor.describe(),
            "live": self.r.live,
            "config": self.cfg.to_dict(),
            "started": self.started,
        }
        self.trace.write_json("run.json", run_meta)
        self.trace.event(
            "run_start",
            **{
                k: run_meta[k]
                for k in (
                    "mode",
                    "task",
                    "fixture_seed",
                    "fixture_sha256",
                    "prompt_version",
                    "live",
                )
            },
        )
        if self.clm:
            self.store.mirror(self.ws)
        human = "none"
        try:
            status, reason = self.loop()
        except _Stop as stop:
            status, reason = stop.status, stop.reason
        except KeyboardInterrupt:
            status, reason, human = (
                "interrupted",
                "KeyboardInterrupt",
                "interrupted by user (Ctrl-C)",
            )
        result = self.finish(status, reason, human)
        if status == "interrupted":
            raise KeyboardInterrupt
        return result

    def management_summary(self) -> dict[str, Any]:
        """When context management first affected a request, and how much work followed."""
        tl = self.timeline
        pressure = [t for t in tl if t["pressure_before_management"]]
        managed = sorted(s for k in ("edit", "summary") for s in self.mgmt[k])
        first = managed[0] if managed else None
        after = [t for t in tl if first is not None and t["step"] >= first]
        stage_of = {t["step"]: t["stage"] for t in tl}
        return {
            "stages_total": self.task.n_stages,
            "stages_released": self.stage,
            "first_pressure_step": pressure[0]["step"] if pressure else None,
            "first_pressure_stage": pressure[0]["stage"] if pressure else None,
            "edit_effective_steps": self.mgmt["edit"],
            "summary_steps": self.mgmt["summary"],
            "spill_steps": self.mgmt["spill"],
            "recovery_steps": self.mgmt["recovery"],
            "first_management_step": first,
            "first_management_stage": stage_of.get(first) if first is not None else None,
            "action_steps_after_first_management": len(after),
            "executions_after_first_management": sum(
                1 for t in after if t.get("action") == "execute"
            ),
            "advances_after_first_management": sum(
                1 for t in after if t.get("action") == "advance"
            ),
            "premature_finals": self.c.premature_finals,
            "timeline": tl,
        }

    @staticmethod
    def _submission_ignore() -> Any:
        return shutil.ignore_patterns(
            CONTEXT_FILENAME,
            f".{CONTEXT_FILENAME}.host-tmp",
            PROPOSAL_FILENAME,
            CLIENT_FILENAME,
            MAILBOX,
            "spill",
            "__pycache__",
        )

    def _sandbox_eval(
        self, runner_code: str, source: Path, checks: str, name: str
    ) -> tuple[str | None, str | None]:
        """Run evaluator code against a disposable copy of ``source`` in a fresh sandbox."""
        ev = self.dir / "evaluator"
        work, fx = ev / f"_eval_work_{name}", ev / f"_eval_fixtures_{name}"
        shutil.copytree(source, work)  # disposable copy the evaluation may alter
        fx.mkdir()
        (fx / "checks.json").write_text(checks, encoding="utf-8")
        error: str | None = None
        output: str | None = None
        if not isinstance(self.r.executor, DockerExecutor):
            error = "coding evaluation requires the Docker executor"
        else:
            evaluator = replace(self.r.executor, timeout_s=180, output_cap_bytes=2_000_000)
            try:
                res = evaluator.run(runner_code, work, fx)
                output = res.stdout
                self.trace.write(f"evaluator/{name}_stdout.txt", res.stdout)
                self.trace.write(f"evaluator/{name}_stderr.txt", res.stderr)
                if res.timed_out or res.exit_code not in (0, None):
                    error = f"evaluator exit {res.exit_code} timed_out={res.timed_out}"
            except ExecutorUnavailable as exc:
                error = f"evaluator unavailable: {exc}"
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(fx, ignore_errors=True)
        return output, error

    def evaluate_coding(self, completed: bool) -> Any:
        """Score the submitted workspace in a fresh sandbox; never run it on the host."""
        from . import coding, coding2, w2, w3

        ev = self.dir / "evaluator"
        shutil.copytree(self.ws, ev / "submission", ignore=self._submission_ignore())
        truth = self.task.truth
        score: Any
        staged_mod: Any = {
            coding2.GENERATOR_VERSION: coding2,
            w2.GENERATOR_VERSION: w2,
            w3.GENERATOR_VERSION: w3,
        }.get(self.task.generator_version)
        if staged_mod is None:
            output, error = self._sandbox_eval(
                coding.EVAL_RUNNER, ev / "submission", coding.checks_json(truth), "eval"
            )
            score = coding.score_coding(truth, output, completed, error)
        else:
            output, error = self._sandbox_eval(
                staged_mod.EVAL_RUNNER, ev / "submission", staged_mod.checks_json(truth), "eval"
            )
            snaps: dict[int, str | None] = {}
            for stage in sorted(truth.snapshot_checks):
                src = ev / "snapshots" / f"stage-{stage}"
                if src.is_dir():
                    snaps[stage], _ = self._sandbox_eval(
                        staged_mod.EVAL_RUNNER,
                        src,
                        staged_mod.snapshot_checks_json(truth, stage),
                        f"snapshot_{stage}",
                    )
            scorer = coding2.score_coding if staged_mod is coding2 else w2.score_export
            score = scorer(truth, output, completed, error, snaps)
        self.trace.event(
            "coding_evaluation",
            outcome=score.outcome,
            passed=score.passed,
            checks=score.checks,
            stale=score.stale,
            error=error,
        )
        return score

    def finish(self, status: str, reason: str, human: str) -> RunResult:
        self._end_proposal("unresolved_at_end")  # never applied (011 pilot)
        # Ground truth is written only now, outside the sandbox mounts.
        truth = self.task.truth
        score: Any
        if self.task.kind == "coding":
            score = self.evaluate_coding(completed=self.answer is not None)
        elif self.task.kind == "rounds":
            score = score_rounds(self.answer, truth, self.task.files)
        elif self.task.kind in ("handbook", "docservice"):
            score = score_handbook(self.answer, truth)
        else:
            score = (
                score_answer(self.answer, truth, self.task.files)
                if self.answer
                else no_answer_score(truth)
            )
        self.trace.write_json("evaluator/truth.json", truth.to_dict())
        if self.register_task:
            from .register_score import score_run

            revs = {r.number: [e.to_dict() for e in r.entries] for r in self.store.revisions}
            self.register_score = score_run(self.register_points, revs, truth)
            self.trace.write_json("evaluator/register_score.json", self.register_score)
        self.trace.write_json(
            "evaluator/score.json",
            {
                **score.to_dict(),
                "components": score.components,
                "scorer_version": scorer_version_for(self.task),
            },
        )
        elapsed = time.monotonic() - self.t_start
        u = self.u
        metrics = {
            "run_id": self.run_id,
            "label": self.label,
            "mode": self.mode,
            "task": self.task.spec.name,
            "status": status,
            "stop_reason": reason,
            "valid_for_comparison": status not in INVALID_FOR_COMPARISON
            and getattr(score, "outcome", "") != "evaluation_error",
            "human_intervention": human,
            "started": self.started,
            "ended": now_iso(),
            "elapsed_s": round(elapsed, 1),
            "model": self.r.provider.model,
            "prompt_version": PROMPT_VERSION,
            "summary_policy": self.policy.policy if self.mode == "summary" else None,
            "fixture_sha256": self.task.fixture_sha256,
            "steps": self.step,
            "counts": self.c.__dict__,
            "usage_provider_reported": {
                "input_tokens_uncached": u.input_tokens,
                "cache_read_input_tokens": u.cache_read_input_tokens,
                "cache_creation_input_tokens": u.cache_creation_input_tokens,
                "output_tokens": u.output_tokens,
                "thinking_tokens_within_output": u.thinking_tokens if u.thinking_reported else None,
                "source": "scripted-estimate" if self.r.provider.is_scripted else "provider",
            },
            "usage_local_estimate": {
                "input_tokens": u.est_input_tokens,
                "basis": "chars / calibrated chars-per-token",
            },
            "cost": {
                "incurred_usd": round(u.cost_usd, 6),
                "by_provider": {
                    "anthropic": round(u.cost_usd - self.jev_usage["cost_usd"], 6),
                    "typesafe": round(self.jev_usage["cost_usd"], 8),
                },
                "of_which_assumed_from_reservation_usd": round(u.assumed_cost_usd, 6),
                "reserved_total_usd": round(u.reserved_usd_total, 6),
                "basis": "provider usage x dated price"
                if self.r.price
                else "unavailable (no price)",
                "price_source": self.r.price.source if self.r.price else None,
                "price_retrieved": self.r.price.retrieved if self.r.price else None,
            },
            "peak_request_tokens_est": self.peak_req_est,
            "peak_request_input_tokens_reported": self.peak_req_reported,
            "jev": self.jev_summary(),
            "update_review": self.update_review_summary(),
            "initiated_review": self.initiated_review_summary(),
            "edit_check": self.edit_check_summary(),
            "result_screen": self.result_screen_summary(),
            "jev_primitive": self.jev_primitive_summary(),
            "register": self.register_score,
            "request": self.request_summary(),
            "peak_working_context_chars": self.peak_ctx_chars,
            "context_revisions": len(self.store.revisions) - 1,
            "helpers": self.helper_summary(),
            "management": self.management_summary(),
            "score": {**score.to_dict(), "components": score.components},
        }
        self.trace.write_json("summary.json", metrics)
        self.trace.event(
            "run_end",
            status=status,
            reason=reason,
            outcome=score.outcome,
            cost_usd=round(u.cost_usd, 6),
        )
        self.trace.close()
        return RunResult(
            self.run_id, self.dir, self.mode, self.task.spec.name, status, reason, score, metrics
        )
