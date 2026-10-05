"""Experiment 007 task family: a multi-service incident whose evidence arrives in 10-12 rounds.

Generator ``incident-rounds-gen/1``, scorer ``rounds-score/1``. Each round is one hour of
evidence under ``round-NN/``: structured service logs, the incident board's updates for that
hour, and sometimes a change log. Earlier rounds never change and stay readable.

Storyline per instance (services, causes, timings and patterns vary by seed):
- Thread A: a confirmed cause in one service with impact on a second; mitigated by an APPLIED
  change, later resolved (its earlier "mitigated" status is superseded).
- Thread B: errors in one service, first attributed to a suspected cause that a later board
  update revises (the suspected cause is superseded); a partial update adds an affected service
  while keeping cause and status; later mitigated.
- Thread C: a late symptom whose logs show a rejected configuration key/value; the key/value was
  shipped by a change recorded in round 1 or 2 (an early fact that must be connected later).
  It stays ongoing.
- Optional thread D: alerts that the board later closes as a false alarm (not an incident).
- Follow-up items opened and some closed on the board; those still open at the end are the
  unresolved issues.
- From one round onward the service logs switch from key=value text to JSON lines.

The answer contract and code lists are in the task prompt. Ground truth is generated with the
fixtures and stays in host memory until the run ends.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

from .tasks import MAX_RANGE, REF_RE, InstanceSpec, TaskInstance

GENERATOR_VERSION = "incident-rounds-gen/1"
SCORER_VERSION = "rounds-score/1"
STATUSES = ("ongoing", "mitigated", "resolved")

CAUSES: dict[str, str] = {
    "DB_POOL_EXHAUSTED": "database connection pool exhausted",
    "CACHE_STAMPEDE": "cache miss storm after mass expiry",
    "CERT_EXPIRED": "an expired TLS certificate on a dependency",
    "DNS_RESOLUTION": "DNS lookups failing for a dependency",
    "QUEUE_BACKLOG": "message consumer lag growing",
    "MEMORY_LEAK": "process memory growing until restarts",
    "UPSTREAM_RATE_LIMIT": "an upstream provider rejecting requests with 429",
    "DISK_PRESSURE": "a data volume running out of space",
    "BAD_CONFIG_ROLLOUT": "a configuration value shipped by a rollout is invalid",
}
FOLLOW_UPS: dict[str, str] = {
    "CUSTOMER_COMMS": "customer communication about the impact",
    "DATA_BACKFILL": "backfill of records missed during the incident",
    "CAPACITY_REVIEW": "capacity review of the affected tier",
    "ALERT_TUNING": "tuning of noisy or late alerts",
    "VENDOR_TICKET": "a ticket open with an external vendor",
    "POSTMORTEM_DRAFT": "drafting the postmortem",
    "RUNBOOK_UPDATE": "updating the runbook",
}
SERVICES = [
    "checkout-api",
    "payments-svc",
    "inventory-svc",
    "notify-worker",
    "search-api",
    "auth-svc",
    "ledger-svc",
    "pricing-svc",
    "shipping-svc",
]
# Causes that can be a thread's real or suspected cause (BAD_CONFIG_ROLLOUT is thread C's).
GENERAL = [c for c in CAUSES if c != "BAD_CONFIG_ROLLOUT"]
CONFIG_KEYS = [
    ("http.max_inflight", ["0", "-1"]),
    ("retry.backoff_ms", ["-50", "0"]),
    ("cache.ttl_s", ["-1", "0"]),
    ("db.pool_max", ["0", "-4"]),
    ("feature.batch_size", ["0", "-10"]),
]


# ------------------------------------------------------------- log lines
def signature(cause: str, rng: random.Random, extra: dict[str, str]) -> tuple[str, str, dict]:
    """(level, message, fields) of one log line showing ``cause``."""
    if cause == "DB_POOL_EXHAUSTED":
        n = rng.choice([10, 12, 16, 20])
        return (
            "WARN",
            "db pool exhausted",
            {"wait_ms": rng.randrange(800, 3000), "in_use": f"{n}/{n}"},
        )
    if cause == "CACHE_STAMPEDE":
        return (
            "WARN",
            "cache miss storm",
            {
                "key_prefix": rng.choice(["sku:", "price:", "user:"]),
                "misses_per_s": rng.randrange(800, 4000),
            },
        )
    if cause == "CERT_EXPIRED":
        return (
            "ERROR",
            "tls handshake failed",
            {"peer": extra.get("peer", "partner-gw"), "reason": "certificate_expired"},
        )
    if cause == "DNS_RESOLUTION":
        return (
            "ERROR",
            "dns lookup failed",
            {"host": extra.get("host", "geo.internal"), "err": "SERVFAIL"},
        )
    if cause == "QUEUE_BACKLOG":
        return (
            "WARN",
            "consumer lag",
            {"topic": extra.get("topic", "events"), "lag": rng.randrange(20_000, 90_000)},
        )
    if cause == "MEMORY_LEAK":
        return (
            "WARN",
            "heap usage high",
            {"rss_mb": rng.randrange(1800, 3900), "gc_pause_ms": rng.randrange(300, 1400)},
        )
    if cause == "UPSTREAM_RATE_LIMIT":
        return (
            "ERROR",
            "upstream returned 429",
            {
                "upstream": extra.get("upstream", "tax-provider"),
                "retry_after_s": rng.randrange(5, 60),
            },
        )
    if cause == "DISK_PRESSURE":
        return "WARN", "disk usage high", {"path": "/var/data", "used_pct": rng.randrange(91, 99)}
    if cause == "BAD_CONFIG_ROLLOUT":
        return (
            "ERROR",
            "config validation failed",
            {"key": extra["key"], "value": extra["value"], "build": extra["build"]},
        )
    raise KeyError(cause)


ROUTINE = [
    (
        "INFO",
        "request ok",
        lambda r: {
            "route": r.choice(["/v1/items", "/v1/orders", "/v1/health", "/v1/quote"]),
            "status": 200,
            "ms": r.randrange(4, 90),
        },
    ),
    ("INFO", "health ok", lambda r: {"probe": "readiness"}),
    ("INFO", "heartbeat", lambda r: {"uptime_s": r.randrange(1000, 90000)}),
    (
        "WARN",
        "slow query",
        lambda r: {"ms": r.randrange(200, 450), "table": r.choice(["orders", "items", "events"])},
    ),
    ("INFO", "gc cycle", lambda r: {"pause_ms": r.randrange(2, 40)}),
]


def fmt_line(ts: str, level: str, svc: str, msg: str, fields: dict, as_json: bool) -> str:
    if as_json:
        return json.dumps(
            {"ts": ts, "level": level, "svc": svc, "msg": msg, **fields}, separators=(",", ":")
        )
    kv = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"{ts} {level:<5} {svc} {msg} {kv}"


def clock(round_no: int, minute: int, second: int) -> str:
    return f"2026-09-14T{7 + round_no:02d}:{minute:02d}:{second:02d}Z"


# ------------------------------------------------------------- truth
@dataclass
class RoundsTruth:
    incidents: list[dict[str, Any]]  # cause, services, status, evidence_groups
    unresolved: list[str]
    superseded_causes: list[str]
    false_alarm_causes: list[str]
    stale_statuses: dict[str, list[str]]  # cause -> earlier statuses
    closed_follow_ups: list[str]
    notes: str = ""
    stale_causes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "rounds",
            "generator_version": GENERATOR_VERSION,
            "incidents": self.incidents,
            "unresolved": self.unresolved,
            "superseded_causes": self.superseded_causes,
            "false_alarm_causes": self.false_alarm_causes,
            "stale_statuses": self.stale_statuses,
            "closed_follow_ups": self.closed_follow_ups,
            "notes": self.notes,
        }


class _Board:
    """Per-round incident board files; records line numbers of every update."""

    def __init__(self, n_rounds: int) -> None:
        self.lines: dict[int, list[str]] = {
            k: [f"# Incident board: updates in round {k:02d}", ""] for k in range(1, n_rounds + 1)
        }

    def add(self, k: int, text: str) -> tuple[str, int]:
        self.lines[k].append(f"- {text}")
        return f"round-{k:02d}/board.md", len(self.lines[k])


def _plan(rng: random.Random, n_rounds: int) -> dict[str, Any]:
    """Event timing (rounds) for this instance, satisfying the storyline's order."""
    last = n_rounds
    a1 = rng.choice([2, 3])
    a2 = a1 + rng.choice([2, 3])
    a3 = min(last - 2, a2 + rng.choice([2, 3]))
    b1 = rng.choice([3, 4])
    b_rev = min(last - 2, b1 + rng.choice([3, 4, 5]))
    b_part = rng.choice([r for r in range(b1 + 1, last) if r != b_rev])
    b_mit = min(last - 1, max(b_rev, b_part) + rng.choice([1, 2]))
    c_origin = rng.choice([1, 2])
    c1 = rng.choice([last - 3, last - 2])
    c2 = c1 + 1
    fmt = rng.choice(range(5, 9))
    return dict(
        a1=a1,
        a2=a2,
        a3=a3,
        b1=b1,
        b_rev=b_rev,
        b_part=b_part,
        b_mit=b_mit,
        c_origin=c_origin,
        c1=c1,
        c2=c2,
        fmt=fmt,
    )


def generate_rounds(spec: InstanceSpec) -> TaskInstance:
    rng = random.Random(spec.seed)
    n_rounds = {0: 10, 1: 11, 2: 12}[spec.seed % 3]
    p = _plan(rng, n_rounds)
    svcs = rng.sample(SERVICES, 5)
    sA1, sA2, sB, sB2, sC = svcs
    cA, cB, cB_wrong = rng.sample(GENERAL, 3)
    has_d = rng.random() < 0.6
    cD = rng.choice([c for c in GENERAL if c not in (cA, cB, cB_wrong)]) if has_d else None
    sD = rng.choice([sA1, sB, sC]) if has_d else None
    key, bad_values = rng.choice(CONFIG_KEYS)
    bad_value = rng.choice(bad_values)
    build = f"{rng.randrange(3, 9)}.{rng.randrange(10, 60)}.{rng.randrange(0, 9)}"
    extra = {
        "peer": rng.choice(["partner-gw", "bank-api", "sso.example.net"]),
        "host": rng.choice(["geo.internal", "rates.internal", "kms.internal"]),
        "topic": rng.choice(["order-events", "stock-updates", "mail-out"]),
        "upstream": rng.choice(["tax-provider", "fx-rates", "sms-gateway"]),
        "key": key,
        "value": bad_value,
        "build": build,
    }
    board = _Board(n_rounds)
    groups: dict[str, dict[str, list[list[Any]]]] = {"A": {}, "B": {}, "C": {}}
    changes: dict[int, list[str]] = {}
    change_refs: dict[str, tuple[str, int]] = {}

    def change(k: int, text: str, tag: str | None = None) -> None:
        changes.setdefault(k, [f"# Change log: round {k:02d}", ""])
        changes[k].append(f"- {text}")
        if tag:
            change_refs[tag] = (f"round-{k:02d}/changes.md", len(changes[k]))

    # Routine change records, so the relevant ones are not the only entries.
    routine_changes = [
        "APPLIED: log sampling for /v1/health reduced to 1%",
        "APPLIED: dashboard panels for p99 latency added",
        "PROPOSED: raise HPA max replicas for search tier (not applied)",
        "APPLIED: TLS session ticket rotation set to hourly",
        "APPLIED: retry budget for idempotent GETs set to 1",
        "PROPOSED: move batch jobs to off-peak window (not applied)",
    ]
    for k in range(1, n_rounds + 1):
        if rng.random() < 0.6:
            change(k, f"CHG-{100 + k * 7 + rng.randrange(0, 6)} {rng.choice(routine_changes)}")
    # Thread C origin: an early change record shipping the bad value (the early fact).
    co = p["c_origin"]
    change(
        co,
        f"CHG-{100 + co * 7 + 6} APPLIED: build {build} rolled out to {sC}; sets `{key}={bad_value}`",
        tag="C_origin",
    )
    change(
        co, f"CHG-{100 + co * 7 + 7} APPLIED: build {build} rolled out to {sB}; no config changes"
    )

    # Thread A
    t = board.add(
        p["a1"],
        f"Thread A opened: {CAUSES[cA]} confirmed as the cause (code {cA}) in {sA1}; impact also on {sA2}. Status: ongoing.",
    )
    groups["A"]["cause"] = [list(t)]
    change(
        p["a2"],
        f"CHG-{100 + p['a2'] * 7 + 8} APPLIED: mitigation for thread A deployed to {sA1}",
        tag="A_fix",
    )
    board.add(
        p["a2"], "Thread A status: mitigated (errors reduced after the applied change; monitoring)."
    )
    t = board.add(p["a3"], "Thread A status: resolved (no recurrence for two hours).")
    groups["A"]["status"] = [list(t)]
    # Thread B
    board.add(
        p["b1"],
        f"Thread B opened: errors in {sB}. Suspected cause: {CAUSES[cB_wrong]} ({cB_wrong}). Status: ongoing.",
    )
    t = board.add(
        p["b_rev"],
        f"Thread B root cause revised: {CAUSES[cB]} ({cB}); this supersedes the earlier suspicion of {cB_wrong}.",
    )
    groups["B"]["cause"] = [list(t)]
    board.add(
        p["b_part"],
        f"Thread B update: affected services now also include {sB2}; cause and status unchanged.",
    )
    t = board.add(
        p["b_mit"], "Thread B status: mitigated (workaround in place; permanent fix pending)."
    )
    groups["B"]["status"] = [list(t)]
    # Thread C
    t = board.add(
        p["c1"],
        f"Thread C opened: request failures on some {sC} pods; cause not yet identified. Status: ongoing.",
    )
    groups["C"]["status"] = [list(t)]
    board.add(p["c2"], "Thread C note: failing pods still restarting; investigation continues.")
    groups["C"]["origin"] = [list(change_refs["C_origin"])]
    # Thread D (false alarm)
    d1 = d2 = 0
    if cD is not None:
        d1 = rng.choice(range(2, n_rounds - 3))
        d2 = d1 + rng.choice([2, 3])
        board.add(d1, f"Thread D opened: {CAUSES[cD]} alerts ({cD}) on {sD}. Status: ongoing.")
        board.add(
            d2, "Thread D closed: false alarm (synthetic monitor misconfigured); not an incident."
        )
    # Follow-ups
    fu = rng.sample(list(FOLLOW_UPS), rng.choice([4, 5]))
    n_closed = rng.choice([1, 2])
    closed, still_open = fu[:n_closed], fu[n_closed:]
    for code in fu:
        k = rng.randrange(2, n_rounds - 1)
        board.add(k, f"Follow-up OPEN {code}: {FOLLOW_UPS[code]}.")
        if code in closed:
            board.add(rng.randrange(k + 1, n_rounds + 1), f"Follow-up CLOSED {code}.")

    # Service logs per round.
    files: dict[str, str] = {}
    stages: list[dict[str, str]] = []
    cause_lines_c: list[list[Any]] = []
    sig_lines: dict[str, list[list[Any]]] = {"A": [], "B": []}
    for k in range(1, n_rounds + 1):
        as_json = k >= p["fmt"]
        rf: dict[str, str] = {}
        for svc in svcs:
            events: list[tuple[int, int, str, str, dict]] = []
            for _ in range(rng.randrange(55, 80)):
                lvl, msg, f = rng.choice(ROUTINE)
                events.append((rng.randrange(60), rng.randrange(60), lvl, msg, f(rng)))
            active: list[str] = []
            if svc == sA1 and p["a1"] - 1 <= k < p["a2"]:
                active.append(cA)
            if svc == sA2 and p["a1"] <= k < p["a2"]:
                active.append(cA)
            if svc == sB and k >= p["b1"]:
                active.append(cB)
            if svc == sB2 and k >= p["b_part"] - 1:
                active.append(cB)
            if svc == sC and k >= p["c1"]:
                active.append("BAD_CONFIG_ROLLOUT")
            if cD is not None and svc == sD and d1 <= k < d2:
                active.append(cD)  # alerting noise the board later dismisses
            for cause in active:
                n_sig = rng.randrange(6, 14) if not (cause == cB and k >= p["b_mit"]) else 2
                for _ in range(n_sig):
                    s_lvl, s_msg, s_fields = signature(cause, rng, extra)
                    events.append((rng.randrange(60), rng.randrange(60), s_lvl, s_msg, s_fields))
            events.sort(key=lambda e: (e[0], e[1]))
            lines = [
                fmt_line(clock(k, m, s), lvl, svc, msg, f, as_json) for m, s, lvl, msg, f in events
            ]
            path = f"round-{k:02d}/logs/{svc}.log"
            rf[path] = "\n".join(lines) + "\n"
            # Log lines showing a thread's current cause in one of its services also support
            # that cause (thread C's are the config validation failures).
            for thread, cause, owners in (
                ("A", cA, (sA1, sA2)),
                ("B", cB, (sB, sB2)),
                ("C", "BAD_CONFIG_ROLLOUT", (sC,)),
            ):
                if svc in owners and cause in active:
                    msg = signature(cause, random.Random(0), extra)[1]
                    for i, line in enumerate(lines, start=1):
                        if msg in line:
                            (cause_lines_c if thread == "C" else sig_lines[thread]).append(
                                [path, i]
                            )
        rf[f"round-{k:02d}/board.md"] = "\n".join(board.lines[k]) + "\n"
        if k in changes:
            rf[f"round-{k:02d}/changes.md"] = "\n".join(changes[k]) + "\n"
        stages.append(rf)
        files.update(rf)
    groups["C"]["cause"] = cause_lines_c
    groups["A"]["cause"] += sig_lines["A"]
    groups["B"]["cause"] += sig_lines["B"]

    incidents = [
        {
            "thread": "A",
            "cause": cA,
            "services": sorted([sA1, sA2]),
            "status": "resolved",
            "evidence_groups": groups["A"],
        },
        {
            "thread": "B",
            "cause": cB,
            "services": sorted([sB, sB2]),
            "status": "mitigated",
            "evidence_groups": groups["B"],
        },
        {
            "thread": "C",
            "cause": "BAD_CONFIG_ROLLOUT",
            "services": [sC],
            "status": "ongoing",
            "evidence_groups": groups["C"],
        },
    ]
    truth = RoundsTruth(
        incidents=incidents,
        unresolved=sorted(still_open),
        superseded_causes=[cB_wrong],
        false_alarm_causes=[cD] if cD else [],
        stale_statuses={cA: ["ongoing", "mitigated"], cB: ["ongoing"]},
        closed_follow_ups=sorted(closed),
        notes=f"plan {p}; services {svcs}; key {key}={bad_value} build {build}",
    )
    updates = [
        f"Round {k} of {n_rounds} released. New files are under /task/fixtures/round-{k:02d}/."
        for k in range(1, n_rounds + 1)
    ]
    return TaskInstance(
        spec=spec,
        prompt=task_prompt(n_rounds),
        files=files,
        truth=truth,
        stages=stages,
        stage_updates=updates,
        generator_version=GENERATOR_VERSION,
        kind="rounds",
        scorer_version=SCORER_VERSION,
    )


def task_prompt(n_rounds: int) -> str:
    causes = "\n".join(f"  {c}: {d}" for c, d in CAUSES.items())
    fus = "\n".join(f"  {c}: {d}" for c, d in FOLLOW_UPS.items())
    return f"""Production incident investigation across several services. Evidence arrives in
{n_rounds} hourly rounds. Only round 1 is available now, under /task/fixtures/round-01/.

Each round directory holds:
- logs/<service>.log: that hour's structured service logs (the log format may change).
- board.md: the incident board's updates during that hour. An update lists only what
  changed; anything it does not mention is unchanged. The latest update for a thread wins.
- changes.md (some rounds): change records. APPLIED changes took effect; PROPOSED did not.

Reply {{"action": "advance"}} to release the next round. Released rounds never change and stay
readable. A final answer is accepted only after all {n_rounds} rounds are released.

Final answer (all fields required):
  {{"action": "final", "answer": {{
     "incidents": [{{"cause": "<CAUSE_CODE>", "services": ["<service>", ...],
                    "status": "ongoing|mitigated|resolved",
                    "evidence_refs": ["round-NN/<file>:<line>", ...]}}, ...],
     "unresolved": ["<FOLLOW_UP_CODE>", ...],
     "summary": "<short free text>"}}}}
- incidents: every real incident at the end of the final round (not false alarms), one entry
  per incident, with its current cause, all affected services and its current status.
- evidence_refs: path:line references (relative to /task/fixtures, ranges up to 20 lines as
  path:a-b) supporting the incident's current cause and current status; when the cause traces
  back to an earlier change record, cite that record too.
- unresolved: follow-up items still open at the end of the final round.

Cause codes:
{causes}
Follow-up codes:
{fus}
"""


# ------------------------------------------------------------- scoring
def _refs(answer_refs: list[Any], files: dict[str, str]) -> tuple[set[tuple[str, int]], list[str]]:
    covered: set[tuple[str, int]] = set()
    invalid: list[str] = []
    for raw in answer_refs:
        m = REF_RE.match(str(raw))
        if not m:
            invalid.append(str(raw))
            continue
        path, a = m.group(1), int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        n_lines = files[path].count("\n") if path in files else 0
        if path not in files or a < 1 or b < a or b > n_lines or b - a >= MAX_RANGE:
            invalid.append(str(raw))
            continue
        covered.update((path, i) for i in range(a, b + 1))
    return covered, invalid


@dataclass
class RoundsScore:
    completed: bool
    checks: int
    passed: int
    by_category: dict[str, dict[str, int]]
    stale: int
    reported_causes: list[str]
    missing_causes: list[str]
    extra_causes: list[str]
    unresolved_reported: list[str]
    invalid_refs: list[str]
    details: list[dict[str, Any]]
    strict_success: bool
    outcome: str
    import_error: None = None
    evaluation_error: None = None

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "components": self.components}

    @property
    def components(self) -> dict[str, float]:
        out = {"all_checks": self.passed / self.checks if self.checks else 0.0}
        for cat, v in self.by_category.items():
            out[cat] = v["passed"] / v["total"] if v["total"] else 0.0
        return out


def score_rounds(
    answer: dict[str, Any] | None, truth: RoundsTruth, files: dict[str, str]
) -> RoundsScore:
    """Deterministic checks. Per real incident: cause found, services, status, evidence; plus
    no superseded or false-alarm cause reported, and the exact set of open follow-ups."""
    n = len(truth.incidents)
    by = {
        c: {"total": t, "passed": 0, "stale": 0}
        for c, t in (
            ("causes", n + 1),
            ("services", n),
            ("status", n),
            ("evidence", n),
            ("unresolved", 1),
        )
    }
    completed = answer is not None
    reported: list[dict[str, Any]] = []
    if answer and isinstance(answer.get("incidents"), list):
        reported = [x for x in answer["incidents"] if isinstance(x, dict)]
    rep_causes = [str(x.get("cause", "")).strip().upper() for x in reported]
    want = {i["cause"]: i for i in truth.incidents}
    details: list[dict[str, Any]] = []
    all_invalid: list[str] = []
    for cause, inc in want.items():
        d: dict[str, Any] = {"thread": inc["thread"], "cause": cause}
        match = [x for x, c in zip(reported, rep_causes, strict=True) if c == cause]
        d["found"] = bool(match)
        if match:
            by["causes"]["passed"] += 1
            x = match[0]
            services = (
                sorted({str(s).strip() for s in x.get("services", []) if isinstance(s, str)})
                if isinstance(x.get("services"), list)
                else []
            )
            d["services_ok"] = services == inc["services"]
            status = str(x.get("status", "")).strip().lower()
            d["status"] = status
            d["status_ok"] = status == inc["status"]
            d["status_stale"] = status in truth.stale_statuses.get(cause, [])
            raw_refs = x.get("evidence_refs")
            refs: list[Any] = raw_refs if isinstance(raw_refs, list) else []
            covered, invalid = _refs(refs, files)
            all_invalid += invalid
            groups = {
                g: any(tuple(r) in covered for r in rows)
                for g, rows in inc["evidence_groups"].items()
            }
            d["evidence_groups"] = groups
            d["evidence_ok"] = all(groups.values())
            by["services"]["passed"] += d["services_ok"]
            by["status"]["passed"] += d["status_ok"]
            by["status"]["stale"] += d["status_stale"]
            by["evidence"]["passed"] += d["evidence_ok"]
        details.append(d)
    extra = sorted({c for c in rep_causes if c not in want})
    superseded = [c for c in extra if c in truth.superseded_causes]
    false_alarm = [c for c in extra if c in truth.false_alarm_causes]
    no_extra = completed and not extra and len(rep_causes) == len(set(rep_causes))
    by["causes"]["passed"] += no_extra  # the extra "no wrong incident" check
    by["causes"]["stale"] += len(superseded)
    unresolved = []
    if answer and isinstance(answer.get("unresolved"), list):
        unresolved = sorted({str(u).strip().upper() for u in answer["unresolved"]})
    unresolved_ok = completed and unresolved == truth.unresolved
    by["unresolved"]["passed"] += unresolved_ok
    by["unresolved"]["stale"] += sum(1 for u in unresolved if u in truth.closed_follow_ups)
    total = sum(v["total"] for v in by.values())
    passed = sum(v["passed"] for v in by.values())
    stale = sum(v["stale"] for v in by.values())
    strict = completed and passed == total
    if not completed:
        outcome = "no_answer"
    elif strict:
        outcome = "correct"
    elif stale:
        outcome = "stale"
    else:
        outcome = "incorrect"
    details.append(
        {
            "extra_causes": extra,
            "superseded_reported": superseded,
            "false_alarm_reported": false_alarm,
        }
    )
    return RoundsScore(
        completed=completed,
        checks=total,
        passed=passed,
        by_category=by,
        stale=stale,
        reported_causes=rep_causes,
        missing_causes=[c for c in want if c not in rep_causes],
        extra_causes=extra,
        unresolved_reported=unresolved,
        invalid_refs=all_invalid,
        details=details,
        strict_success=strict,
        outcome=outcome,
    )


ROUNDS_FIELDS = ("incidents", "unresolved", "summary")
ROUNDS_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "incidents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cause": {"type": "string"},
                    "services": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["cause", "services", "status", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["incidents", "unresolved", "summary"],
    "additionalProperties": False,
}
