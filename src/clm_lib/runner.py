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

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .baseline import SummaryPolicy
from .budget import BudgetExhausted, Ledger, ModelPrice, reservation_input_tokens
from .config import Config
from .context import (
    CONTEXT_FILENAME,
    ContextLimits,
    ContextStore,
    EditOutcome,
    Entry,
    total_chars,
    unified_diff,
)
from .executor import ExecResult, Executor, ExecutorUnavailable
from .prompts import PROMPT_VERSION, render_user, system_prompt
from .provider import ACTION_SCHEMA, ModelRequest, ModelResponse, Provider, ProviderError
from .tasks import GENERATOR_VERSION, Score, TaskInstance, no_answer_score, score_answer
from .tracing import RunTrace, now_iso, sha256_text, snapshot_files

MODES = ("summary", "clm", "guided")
INVALID_FOR_COMPARISON = {"infrastructure_error", "executor_unavailable"}


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
    score: Score
    metrics: dict[str, Any]


@dataclass
class _Counters:
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


def parse_action(text: str, max_code_chars: int) -> tuple[dict[str, Any] | None, str]:
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
    if kind == "execute":
        code = obj.get("code")
        if not isinstance(code, str) or not code.strip():
            return None, "execute action needs a non-empty string 'code'"
        if len(code) > max_code_chars:
            return None, f"code is {len(code)} chars; limit {max_code_chars}"
        return obj, ""
    if kind == "final":
        ans = obj.get("answer")
        need = ("root_cause", "required_value", "remedy", "evidence_refs")
        if not isinstance(ans, dict) or any(k not in ans for k in need):
            return None, f"final action needs answer with fields {list(need)}"
        if not isinstance(ans["evidence_refs"], list):
            return None, "evidence_refs must be a list of strings"
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

    def __post_init__(self) -> None:
        if self.live and self.provider.is_scripted:
            raise ValueError("a scripted provider cannot be used for a live run")
        if not self.live and not self.provider.is_scripted:
            raise ValueError("a real provider requires live=True (explicit opt-in to paid calls)")
        if self.live and self.price is None:
            raise ValueError(
                f"no dated price for model {self.provider.model}; cost cannot be bounded"
            )

    # ------------------------------------------------------------------ run
    def run(self, task: TaskInstance, mode: str, label: str = "") -> RunResult:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{mode}-{task.spec.name}-{secrets.token_hex(2)}"
        return _Run(self, task, mode, run_id, label).execute()


class _Run:
    def __init__(
        self, runner: Runner, task: TaskInstance, mode: str, run_id: str, label: str
    ) -> None:
        self.r = runner
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
        self.climits = ContextLimits()
        self.store = ContextStore(self.climits)
        self.policy = SummaryPolicy(
            self.cfg.baseline.tail_entries,
            self.cfg.baseline.summary_max_chars,
            self.cfg.baseline.summary_retries,
        )
        self.c = _Counters()
        self.u = _Usage()
        self.cpt = self.lim.initial_chars_per_token
        self.step = 0
        self.summary_index = 0
        self.recovery_active = False
        self.peak_req_est = 0
        self.peak_req_reported = 0
        self.peak_ctx_chars = 0
        self.helpers: dict[str, Any] = {"created": {}, "revised": [], "invocations": []}
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
        )

    # --------------------------------------------------------------- helpers
    @property
    def clm(self) -> bool:
        return self.mode in ("clm", "guided")

    def est_tokens(self, system: str, user: str) -> int:
        return int((len(system) + len(user)) / self.cpt) + 1

    def status_text(self, est: int, pressure: bool, recovery: bool) -> str:
        b = self.lim.context_budget_tokens
        entries = self.store.entries
        lines = [
            f"step {self.step}; model calls used {self.c.provider_calls}/{self.lim.max_calls}",
            f"request size ~{est} tokens of {b} budget ({100 * est // b}%); "
            f"pressure threshold {int(self.lim.pressure_ratio * 100)}%; "
            f"hard limit {b - self.lim.recovery_reserve_tokens} tokens",
            f"working context: {len(entries)} entries, {total_chars(entries)} chars "
            f"(~{self.cpt:.2f} chars/token)",
        ]
        if pressure and self.clm:
            lines.append(
                "PRESSURE: the request is above the pressure threshold. Manage your "
                "working context now by editing context.json; keep what you still need."
            )
        if recovery:
            lines.append(
                "OVER LIMIT: this request uses the recovery reserve. Your next step must "
                "shrink context.json below the hard limit, or the run ends."
            )
        return "\n".join(lines)

    def assemble(
        self,
        pressure: bool = False,
        recovery: bool = False,
        entries: tuple[Entry, ...] | None = None,
    ) -> tuple[str, int]:
        ents = self.store.entries if entries is None else entries
        user = render_user(self.task.prompt, self.status_text(0, pressure, recovery), ents)
        est = self.est_tokens(self.system, user)
        user = render_user(self.task.prompt, self.status_text(est, pressure, recovery), ents)
        return user, self.est_tokens(self.system, user)

    # ------------------------------------------------------------ provider
    def call(self, req: ModelRequest, kind: str, meta: dict[str, Any]) -> ModelResponse:
        retries = self.cfg.provider.api_retries
        for attempt in range(retries + 1):
            if self.c.provider_calls >= self.lim.max_calls:
                raise _Stop(
                    "max_calls", f"call cap {self.lim.max_calls} reached before {kind} call"
                )
            payload = self.r.provider.payload(req)
            chars = len(json.dumps(payload, ensure_ascii=False))
            reserve = (
                self.r.price.max_cost(reservation_input_tokens(chars), req.max_tokens)
                if self.r.price
                else 0.0
            )
            try:
                res = self.r.ledger.reserve(reserve, self.run_id, kind)
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
                    "reserved_usd": reserve,
                    "context_revision": self.store.current.number,
                    "context_sha256": self.store.current.sha256,
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
            try:
                resp = self.r.provider.complete(req)
            except ProviderError as exc:
                self.c.provider_errors += 1
                billed_resp: ModelResponse | None = getattr(exc, "response", None)
                if billed_resp is not None and self.r.price:
                    actual: float | None = self.r.price.cost(billed_resp.usage)
                elif exc.billing == "none":
                    actual = 0.0
                else:
                    actual = None
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
                    charged_usd=entry["charged_usd"],
                    cost_basis=entry["cost_basis"],
                )
                if exc.kind == "refusal":
                    raise _Stop("refused", "model refused the request") from exc
                if exc.retryable and attempt < retries:
                    self.c.api_retries += 1
                    self.r.sleep(min(30.0, 2.0 * 4**attempt))
                    continue
                raise _Stop("infrastructure_error", str(exc)[:300]) from exc
            cost = self.r.price.cost(resp.usage) if self.r.price else 0.0
            entry = self.r.ledger.settle(res, actual_usd=cost, status="ok", usage=resp.usage)
            self._add_usage(resp, cost, req)
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
            )
            return resp
        raise AssertionError("unreachable")

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
        self.peak_req_reported = max(self.peak_req_reported, reported)
        if resp.usage_source == "provider" and reported > 0 and req.purpose == "action":
            observed = (len(req.system) + len(req.user)) / reported
            self.cpt = round(0.5 * self.cpt + 0.5 * observed, 4)

    # ----------------------------------------------------- context pressure
    def maybe_summarise(self, forced: bool) -> bool:
        if not self.policy.eligible(self.store.entries):
            return False
        older, tail = self.policy.split(self.store.entries)
        hard = self.lim.context_budget_tokens - self.lim.recovery_reserve_tokens
        for attempt in range(self.policy.retries + 1):
            req = self.policy.request(self.task.prompt, older, attempt, self.lim.max_output_tokens)
            resp = self.call(
                req,
                "summary",
                {
                    "older_entries": [e.id for e in older],
                    "summary_attempt": attempt,
                    "forced": forced,
                },
            )
            text = resp.text.strip()
            if not text:
                continue
            candidate = tuple(SummaryPolicy.apply(text, tail, self.summary_index + 1))
            _, est = self.assemble(entries=candidate)
            if est <= hard:
                self.summary_index += 1
                self.store.commit(candidate, "summary", self.step)
                self.c.summaries_applied += 1
                self.trace.event(
                    "summary_applied",
                    step=self.step,
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
                attempt=attempt,
                summary_chars=len(text),
                est_request_tokens=est,
            )
        self.c.summary_overflows += 1
        raise _Stop("context_overflow", "summary still exceeds the hard limit after retry")

    def spill_latest(self) -> bool:
        entries = list(self.store.entries)
        if not entries or entries[-1].role != "observation":
            return False
        last = entries[-1]
        if len(last.body) < self.lim.spill_min_chars:
            return False
        spill_dir = self.ws / "spill"
        spill_dir.mkdir(exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", last.id) + ".txt"
        (spill_dir / name).write_text(last.body, encoding="utf-8")
        entries[-1] = Entry(
            last.id,
            "observation",
            (
                f"[runtime: observation of {len(last.body)} chars moved to "
                f"/task/workspace/spill/{name} because the request exceeded the hard limit]"
            ),
        )
        self.store.commit(entries, "spill", self.step)
        self.c.spills += 1
        self.trace.event(
            "spill", step=self.step, entry=last.id, chars=len(last.body), file=f"spill/{name}"
        )
        self._save_revision()
        return True

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
        self.recovery_active = recovery
        user, est = self.assemble(pressure=pressure, recovery=recovery)
        if est > (b if recovery else hard):
            raise _Stop("context_overflow", f"request ~{est} tokens exceeds limit")
        return user, est, pressure, recovery

    # ---------------------------------------------------------- execution
    def _track_helpers(self, before: dict[str, str], after: dict[str, str], code: str) -> list[str]:
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
        py_before = [p for p in before if p.endswith(".py") and p not in skip]
        invoked = []
        for p in py_before:
            mod = Path(p).with_suffix("").as_posix().replace("/", ".")
            stem = Path(p).stem
            if (
                p in code
                or re.search(rf"\b(import|from)\s+{re.escape(mod)}\b", code)
                or re.search(rf"\bimport\s+{re.escape(stem)}\b", code)
                or re.search(rf"\bfrom\s+\S*{re.escape(stem)}\s+import\b", code)
            ):
                invoked.append(p)
        if invoked:
            self.helpers["invocations"].append({"step": self.step, "files": invoked})
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
                helper_invocations=invoked,
            )
        return changed

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
        try:
            res = self.r.executor.run(code, self.ws, self.fx)
        except ExecutorUnavailable as exc:
            raise _Stop("executor_unavailable", str(exc)) from exc
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
        )
        self._track_helpers(before, after, code)
        receipt: str | None = None
        if self.clm:
            outcome = self.store.read_back(self.ws, mirrored, self.step)
            receipt = self._record_edit(outcome)
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
        while True:
            self.step += 1
            user, _est, pressure, recovery = self.prepare_request()
            req = ModelRequest(
                self.system, user, self.lim.max_output_tokens, "action", ACTION_SCHEMA
            )
            resp = self.call(req, "action", {"pressure": pressure, "recovery": recovery})
            action, err = parse_action(resp.text, self.lim.max_code_chars)
            if action is None:
                self.trace.event(
                    "invalid_action", step=self.step, error=err, stop_reason=resp.stop_reason
                )
                note = (
                    f"\n\n<runtime_status>\nYour previous reply was rejected: {err}"
                    f"{' (output token limit reached)' if resp.stop_reason == 'max_tokens' else ''}. "
                    "Reply again with exactly one JSON object; keep code short.\n</runtime_status>\n"
                )
                repair = ModelRequest(
                    self.system, user + note, self.lim.max_output_tokens, "repair", ACTION_SCHEMA
                )
                resp = self.call(repair, "repair", {"error": err})
                action, err = parse_action(resp.text, self.lim.max_code_chars)
                if action is None:
                    self.trace.event("invalid_action", step=self.step, error=err, after_repair=True)
                    return "failed_protocol", f"invalid response after repair: {err}"
            if action["action"] == "final":
                self.answer = action["answer"]
                self.trace.event("final_answer", step=self.step, answer=self.answer)
                return "completed", "final answer"
            self.execute_step(action)

    def execute(self) -> RunResult:
        self.task.write_fixtures(self.fx)
        (self.ws / "helpers").mkdir()
        run_meta = {
            "run_id": self.run_id,
            "label": self.label,
            "mode": self.mode,
            "comparison_arm": self.mode in ("summary", "clm"),
            "task": self.task.spec.name,
            "split": self.task.spec.split,
            "scenario_hidden_from_model": True,
            "fixture_seed": self.task.spec.seed,
            "fixture_sha256": self.task.fixture_sha256,
            "generator_version": GENERATOR_VERSION,
            "prompt_version": PROMPT_VERSION,
            "system_prompt_sha256": sha256_text(self.system),
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

    def finish(self, status: str, reason: str, human: str) -> RunResult:
        # Ground truth is written only now, outside the sandbox mounts.
        truth = self.task.truth
        score = (
            score_answer(self.answer, truth, self.task.files)
            if self.answer
            else no_answer_score(truth)
        )
        self.trace.write_json("evaluator/truth.json", truth.to_dict())
        self.trace.write_json(
            "evaluator/score.json", {**score.to_dict(), "components": score.components}
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
            "valid_for_comparison": status not in INVALID_FOR_COMPARISON,
            "human_intervention": human,
            "started": self.started,
            "ended": now_iso(),
            "elapsed_s": round(elapsed, 1),
            "model": self.r.provider.model,
            "prompt_version": PROMPT_VERSION,
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
            "peak_working_context_chars": self.peak_ctx_chars,
            "context_revisions": len(self.store.revisions) - 1,
            "helpers": self.helpers,
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
