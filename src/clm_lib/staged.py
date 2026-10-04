"""Experiment 002 task family: a staged incident whose evidence arrives in three stages.

Storyline (entities, values, times and arrangement vary by seed):

- Stage 1 (08:00-09:30): a build is deployed; upstream timeouts start. Its release notes,
  among many routine items, lower both a client timeout and the DB pool default. Sparse pool
  waits are an early hint. The obvious hypothesis is CLIENT_TIMEOUT_TOO_LOW.
- Stage 2 (09:30-10:30): an APPLIED change raises the timeout (supersedes stage 1); timeouts
  stop and DB pool exhaustion takes over. A note repeats a stale pool value from the repo config.
- Stage 3 (10:30-11:30): an APPLIED change raises the pool part-way (supersedes the build
  default); exhaustion persists at the new size. A PROPOSED larger value is not applied.

Final truth: DB_POOL_EXHAUSTED, current effective pool size from stage 3, RAISE_DB_POOL_LIMIT,
with evidence for the symptom (stage 2/3 logs), the current value (stage 3 change record) and
the origin (the stage 1 release-note line, an exact early fact).

Stage files are written under ``stage-N/`` and never change after release, so earlier evidence
can always be re-read and line references stay valid.
"""

from __future__ import annotations

import random
from dataclasses import replace

from .tasks import (
    CAUSE_CODES,
    PEOPLE,
    REMEDY_CODES,
    SERVICES,
    InstanceSpec,
    TaskInstance,
    Truth,
    _base_ctx,
    _Ctx,
    _edge_503s,
    _Line,
    _metrics,
    _noise_edge_log,
    _noise_service_log,
    _notes,
    _other_changes,
    _render,
    _renumber_changes,
)

STAGED_GENERATOR_VERSION = "staged-incident-gen/1"
N_STAGES = 3

ROUTINE_NOTES = [
    "logging: structured request ids added to access logs",
    "metrics: new histogram for cache hit latency",
    "deps: http client library upgraded to a patch release",
    "cache: default `cache.ttl_s` unchanged (120)",
    "build: container base image refreshed",
    "api: `/v1/status` returns build id",
    "tracing: sampling rate default 1% (unchanged)",
    "config: `log.level` default remains info",
    "security: TLS session tickets rotated hourly",
    "deps: json serializer upgraded",
    "feature flags: `ff-legacy-export` removed (was 0%)",
    "retry: idempotent GETs retried once on connect errors",
    "metrics: db pool gauges renamed to `db_pool_*`",
    "docs: runbook links updated",
    "health: readiness probe timeout 2s (unchanged)",
    "batching: max batch size default 200 (unchanged)",
    "scheduler: cron jitter added to cleanup job",
    "locale: currency formatting fixes",
]


def _window(c: _Ctx, start_h: float, end_h: float, incident_h: float) -> _Ctx:
    return replace(c, t0=start_h * 3600, t1=end_h * 3600, incident=incident_h * 3600)


def _stage_listing(files: dict[str, str]) -> str:
    return "\n".join(f"  {p} ({t.count(chr(10))} lines)" for p, t in sorted(files.items()))


def generate_staged(spec: InstanceSpec) -> TaskInstance:
    base = _base_ctx(spec)
    rng: random.Random = base.rng
    svc, edge, up = base.svc, base.edge, base.upstream
    maj, mn = rng.randrange(5, 9), rng.randrange(10, 40)
    b0 = f"{maj}.{mn}.{rng.randrange(0, 9)}"
    b1 = f"{maj}.{mn + 1}.{rng.randrange(0, 5)}-{rng.getrandbits(16):04x}"
    cfg_default = rng.choice([40, 48, 60])
    v1 = rng.choice([10, 12, 14])
    v2 = v1 + rng.choice([6, 8, 10])
    v3 = v2 + 16
    t1 = rng.choice([600, 750, 800])
    t2 = rng.choice([3000, 3500, 4000])
    t_deploy = 8 * 3600 + rng.randrange(12, 25) * 60 + rng.randrange(0, 59)
    t_mitig = 9 * 3600 + rng.randrange(35, 46) * 60 + rng.randrange(0, 59)
    t_pool = 10 * 3600 + rng.randrange(44, 56) * 60 + rng.randrange(0, 59)
    clock = base.clock
    rel_notes_path = f"stage-1/deploy/release-notes-{b1}.md"

    # ---------------- stage 1
    c1 = _window(base, 8.0, 9.5, t_deploy / 3600 + 1 / 60)
    distract1 = [
        f"tls certificate for {svc}.internal expires in {rng.randrange(18, 40)} days",
        f"disk usage /var/data at {rng.randrange(60, 72)}%",
        "dns cache refresh retry succeeded",
    ]
    lines = _noise_service_log(c1, 760, distract1)
    for _ in range(95):
        t = rng.uniform(c1.incident, c1.t1)
        lines.append(
            _Line(
                t,
                f"{clock.ts(t)} ERROR {svc} [{rng.choice(base.pods)}] upstream call {up} timed out after {t1}ms (attempt 1/1) req={rng.getrandbits(40):010x}",
            )
        )
    for _ in range(7):
        t = rng.uniform(c1.incident, c1.t1)
        lines.append(
            _Line(
                t,
                f"{clock.ts(t)} WARN  {svc} [{rng.choice(base.pods)}] db pool wait {rng.randrange(900, 2400)}ms (in_use {v1}/{v1})",
            )
        )
    s1_log, _ = _render(lines)
    s1_edge, _ = _render(_noise_edge_log(c1, 320) + _edge_503s(c1, 60, "upstream timeout"))
    items = list(ROUTINE_NOTES)
    rng.shuffle(items)
    items = items[: rng.randrange(13, 18)]
    pool_item = f"db: default connection pool `db.pool.max_size` lowered from {cfg_default} to {v1} to fit the shared-db connection quota"
    timeout_item = (
        f"clients: default `clients.{up}.timeout_ms` lowered from 2500 to {t1} (fail fast)"
    )
    items.insert(rng.randrange(len(items) // 2, len(items)), pool_item)
    items.insert(rng.randrange(0, len(items) // 2), timeout_item)
    notes_head = [
        f"# Release notes: {svc} build {b1}",
        "",
        f"Previous build: {b0}. Defaults below override repository config files.",
        "",
    ]
    rel_lines = notes_head + [f"- {x}" for x in items]
    rel_notes = "\n".join(rel_lines) + "\n"
    origin_line = rel_lines.index(f"- {pool_item}") + 1
    deploys = []
    for _ in range(6):
        t = rng.uniform(7 * 3600, 9.5 * 3600)
        o = rng.choice([x for x in SERVICES if x != svc])
        deploys.append(
            _Line(
                t,
                f"{clock.ts(t, ms=False)} DEPLOY {o} build {rng.randrange(2, 9)}.{rng.randrange(0, 30)}.{rng.randrange(0, 9)} rollout 100% by deploy-bot",
            )
        )
    deploys.append(
        _Line(
            t_deploy,
            f"{clock.ts(t_deploy, ms=False)} DEPLOY {svc} build {b1} (previous {b0}) rollout 100% by deploy-bot; release notes: {rel_notes_path}",
        )
    )
    s1_deploys, _ = _render(deploys)
    config = (
        f"# Repository default configuration for {svc}.\n"
        f"# Build release notes and APPLIED change records override these values.\n"
        f"service: {svc}\nclients:\n  {up}:\n    base_url: http://{up}.internal\n    timeout_ms: 2500\n"
        f"database:\n  host: {svc}-db.internal\n  pool:\n    max_size: {cfg_default}\n    acquire_timeout_ms: 5000\n"
        f"cache:\n  ttl_s: 120\n"
    )
    notes1 = _notes(
        c1,
        [
            (
                t_deploy + 900,
                f"{edge} 5xx for {svc} since build {b1} rolled out. Many {up} timeouts.",
            ),
            (
                t_deploy + 1500,
                f"Suspect the {up} timeout default change in {b1}; asking for a mitigation.",
            ),
        ],
    )
    stage1 = {
        "stage-1/UPDATE.md": (
            f"# Stage 1 of {N_STAGES} (08:00-09:30 UTC)\n\n"
            f"{edge} began returning 5xx for {svc} shortly after a deploy. Investigate the files below.\n"
            f"More evidence will be released in later stages.\n"
        ),
        f"stage-1/logs/{svc}.log": s1_log,
        f"stage-1/logs/{edge}.log": s1_edge,
        "stage-1/deploy/deploys.log": s1_deploys,
        rel_notes_path: rel_notes,
        f"stage-1/config/{svc}.yaml": config,
        "stage-1/ops/oncall-notes.md": notes1,
    }

    # ---------------- stage 2
    c2 = _window(base, 9.5, 10.5, t_mitig / 3600 + 2 / 60)
    lines = _noise_service_log(c2, 760, distract1)
    for _ in range(40):
        t = rng.uniform(c2.t0, t_mitig)
        lines.append(
            _Line(
                t,
                f"{clock.ts(t)} ERROR {svc} [{rng.choice(base.pods)}] upstream call {up} timed out after {t1}ms (attempt 1/1) req={rng.getrandbits(40):010x}",
            )
        )
    for _ in range(110):
        t = rng.uniform(c2.incident, c2.t1)
        lines.append(
            _Line(
                t,
                f"{clock.ts(t)} ERROR {svc} [{rng.choice(base.pods)}] db pool exhausted: {v1}/{v1} connections in use, waited 5000ms (acquire_timeout) req={rng.getrandbits(40):010x}",
                "cause",
            )
        )
    s2_log, s2_tags = _render(lines)
    s2_edge, _ = _render(_noise_edge_log(c2, 300) + _edge_503s(c2, 70, "upstream returned 503"))
    chg = [x for x in _other_changes(c2, 10, 0) if x.t >= c2.t0 - 3600]
    chg.append(
        _Line(
            t_mitig,
            f"{clock.ts(t_mitig, ms=False)} CHG-X APPLIED  {svc} override clients.{up}.timeout_ms={t2} (was {t1} from build {b1}) by {rng.choice(PEOPLE)}; mitigation",
        )
    )
    s2_changes, _ = _render(_renumber_changes(chg, rng.randrange(5100, 5500)))

    def row2(t: float) -> str:
        on = t >= c2.incident
        return f"{rng.randrange(180, 260)},{rng.randrange(2600, 5200) if on else rng.randrange(900, 1600)},{v1 if on else rng.randrange(v1 - 4, v1 + 1)}"

    notes2 = _notes(
        c2,
        [
            (
                t_mitig + 600,
                f"{up} timeouts stopped after the mitigation, but {edge} still shows 503s for {svc}.",
            ),
            (
                t_mitig + 1200,
                f"New errors look like db pool exhaustion, but config/{svc}.yaml says max_size {cfg_default}, so the pool should have plenty of room?",
            ),
        ],
    )
    stage2 = {
        "stage-2/UPDATE.md": (
            f"# Stage 2 of {N_STAGES} (09:30-10:30 UTC)\n\n"
            f"On-call applied a mitigation (see stage-2/deploy/changes.log). The {up} timeouts stopped,\n"
            f"but {edge} still returns 503s for {svc} with a different error. New files below.\n"
        ),
        f"stage-2/logs/{svc}.log": s2_log,
        f"stage-2/logs/{edge}.log": s2_edge,
        "stage-2/deploy/changes.log": s2_changes,
        f"stage-2/metrics/{svc}.csv": _metrics(c2, "minute,rps,p99_ms,db_pool_in_use", row2),
        "stage-2/ops/oncall-notes.md": notes2,
    }

    # ---------------- stage 3
    c3 = _window(base, 10.5, 11.5, t_pool / 3600)
    lines = _noise_service_log(c3, 700, distract1)
    for _ in range(60):
        t = rng.uniform(c3.t0, t_pool)
        lines.append(
            _Line(
                t,
                f"{clock.ts(t)} ERROR {svc} [{rng.choice(base.pods)}] db pool exhausted: {v1}/{v1} connections in use, waited 5000ms (acquire_timeout) req={rng.getrandbits(40):010x}",
                "cause",
            )
        )
    for _ in range(70):
        t = rng.uniform(t_pool + 60, c3.t1)
        lines.append(
            _Line(
                t,
                f"{clock.ts(t)} ERROR {svc} [{rng.choice(base.pods)}] db pool exhausted: {v2}/{v2} connections in use, waited 5000ms (acquire_timeout) req={rng.getrandbits(40):010x}",
                "cause",
            )
        )
    s3_log, s3_tags = _render(lines)
    s3_edge, _ = _render(_noise_edge_log(c3, 280) + _edge_503s(c3, 40, "upstream returned 503"))
    chg = [x for x in _other_changes(c3, 9, 0) if x.t >= c3.t0 - 3600]
    chg.append(
        _Line(
            t_pool,
            f"{clock.ts(t_pool, ms=False)} CHG-X APPLIED  {svc} override db.pool.max_size={v2} (was {v1}, the build {b1} default) by {rng.choice(PEOPLE)}; partial mitigation",
            "value",
        )
    )
    tp = t_pool + rng.randrange(600, 1500)
    chg.append(
        _Line(
            tp,
            f"{clock.ts(tp, ms=False)} CHG-X PROPOSED {svc} override db.pool.max_size={v3} (pending review; not applied)",
        )
    )
    s3_changes, s3_chg_tags = _render(_renumber_changes(chg, rng.randrange(5600, 5900)))

    def row3(t: float) -> str:
        cur = v1 if t < t_pool else v2
        return f"{rng.randrange(180, 260)},{rng.randrange(1800, 4200)},{cur}"

    stage3 = {
        "stage-3/UPDATE.md": (
            f"# Stage 3 of {N_STAGES} (10:30-11:30 UTC)\n\n"
            f"Another change was applied (see stage-3/deploy/changes.log) and errors persist at a lower rate.\n"
            f"This is the final evidence stage. Give your final diagnosis once you have reviewed it.\n"
        ),
        f"stage-3/logs/{svc}.log": s3_log,
        f"stage-3/logs/{edge}.log": s3_edge,
        "stage-3/deploy/changes.log": s3_changes,
        f"stage-3/metrics/{svc}.csv": _metrics(c3, "minute,rps,p99_ms,db_pool_in_use", row3),
    }

    stages = [stage1, stage2, stage3]
    files: dict[str, str] = {}
    for st in stages:
        files.update(st)
    truth = Truth(
        cause="DB_POOL_EXHAUSTED",
        remedies=["RAISE_DB_POOL_LIMIT"],
        value_variants=[str(v2)],
        stale_variants=[str(v1), str(cfg_default)],
        evidence_groups={
            "cause": [(f"stage-2/logs/{svc}.log", n) for n in s2_tags["cause"]]
            + [(f"stage-3/logs/{svc}.log", n) for n in s3_tags["cause"]],
            "value": [("stage-3/deploy/changes.log", n) for n in s3_chg_tags["value"]],
            "origin": [(rel_notes_path, origin_line)],
        },
        notes=(f"build {b1} lowered pool default {cfg_default}->{v1}; CHG raised to {v2}; "
               f"proposed {v3} not applied; timeout ({t1}->{t2}) was the superseded hypothesis"),
        stale_causes=["CLIENT_TIMEOUT_TOO_LOW"],
    )  # fmt: skip
    updates = [
        stage1["stage-1/UPDATE.md"],
        stage2["stage-2/UPDATE.md"],
        stage3["stage-3/UPDATE.md"],
    ]
    return TaskInstance(
        spec=spec,
        prompt=staged_task_prompt(stage1),
        files=files,
        truth=truth,
        stages=stages,
        stage_updates=updates,
        generator_version=STAGED_GENERATOR_VERSION,
    )


def staged_task_prompt(stage1: dict[str, str]) -> str:
    causes = "\n".join(f"  {k}: {v}" for k, v in CAUSE_CODES.items())
    remedies = "\n".join(f"  {k}: {v}" for k, v in REMEDY_CODES.items())
    return f"""Production incident with evidence released in {N_STAGES} stages over time.
Read-only files are under /task/fixtures. Stage 1 is available now:
{_stage_listing(stage1)}

Staged evidence:
- Reply {{"action": "advance"}} to release the next stage when you are ready. Its files appear
  under /task/fixtures/stage-N/ and an update message is added to your working context.
  Released files never change and stay readable for the rest of the task.
- A final answer is accepted only after all {N_STAGES} stages are released.

Rules of evidence:
- Build release notes describe defaults shipped in a build; they override repository config files.
- Records marked APPLIED in change logs are authoritative and supersede release-note defaults,
  config files and earlier notes. If two authoritative records conflict, the later wins.
- PROPOSED records were not applied. On-call notes are informal and may be out of date.
- Earlier hypotheses may be superseded by later evidence; diagnose the incident as it stands at
  the end of the final stage.

Answer fields (all required):
- root_cause: start with exactly one cause code, then a colon and a one-sentence explanation.
- required_value: the exact current effective value of the setting at the centre of the root
  cause at the end of the final stage.
- remedy: start with exactly one remedy code, then a colon and a concrete action.
- evidence_refs: list of "path:line" or "path:start-end" references (paths relative to
  /task/fixtures, 1-based, ranges of at most 20 lines) supporting (1) the current symptom,
  (2) the record that sets the current effective value, and (3) the record of the change that
  originally introduced the problem.

Cause codes:
{causes}
Remedy codes:
{remedies}
"""
