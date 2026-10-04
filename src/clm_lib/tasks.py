"""Synthetic service-incident task family: deterministic fixtures and scorer.

All instances come from one generator. Held-out instances differ from the
development instance in scenario, entities, values and evidence arrangement.
Ground truth is returned separately from the fixture files and must never be
written into the sandbox.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GENERATOR_VERSION = "incident-gen/1"
# score/1: exact normalised match of required_value.
# score/2 (post-hoc correction, 2026-10-04): also accepts "<setting>=<value>", the form in
# which values appear in the authoritative change records. Applied uniformly to all runs;
# score/1 results are kept in each run's evaluator/score.json.
SCORER_VERSION = "score/2"

CAUSE_CODES: dict[str, str] = {
    "DB_POOL_EXHAUSTED": "database connection pool too small for load",
    "TLS_CERT_EXPIRED": "an expired TLS certificate is being served or presented",
    "CLIENT_TIMEOUT_TOO_LOW": "a client timeout is lower than the upstream's normal latency",
    "FEATURE_FLAG_REGRESSION": "a feature flag rollout enabled faulty code",
    "DISK_FULL": "a volume ran out of space",
    "DNS_RESOLUTION_FAILURE": "service discovery / DNS records are wrong or failing",
    "MEMORY_LIMIT_OOM": "processes are killed for exceeding memory limits",
    "UPSTREAM_RATE_LIMITED": "an upstream is rejecting calls due to rate limits",
}

REMEDY_CODES: dict[str, str] = {
    "RAISE_DB_POOL_LIMIT": "raise the connection pool limit to cover demand",
    "ROTATE_CERTIFICATE": "deploy a valid, unexpired certificate",
    "RAISE_CLIENT_TIMEOUT": "raise the client timeout above upstream latency",
    "DISABLE_FEATURE_FLAG": "turn the faulty feature flag off",
    "EXPAND_VOLUME": "free or expand the full volume",
    "FIX_DNS_RECORD": "correct the DNS / discovery record",
    "RAISE_MEMORY_LIMIT": "raise the memory limit or fix the leak",
    "REQUEST_RATE_LIMIT_INCREASE": "reduce call rate or obtain a higher quota",
}


@dataclass(frozen=True)
class InstanceSpec:
    name: str
    seed: int
    scenario: str
    split: str  # "dev" | "heldout"


INSTANCES: dict[str, InstanceSpec] = {
    "dev": InstanceSpec("dev", 101, "db_pool", "dev"),
    "heldout-1": InstanceSpec("heldout-1", 201, "tls_cert", "heldout"),
    "heldout-2": InstanceSpec("heldout-2", 202, "client_timeout", "heldout"),
    "heldout-3": InstanceSpec("heldout-3", 203, "feature_flag", "heldout"),
    # Experiment 002: staged evidence (see staged.py). Dev instances are for calibration only;
    # eval instances are not run until the comparison is frozen.
    "staged-dev-1": InstanceSpec("staged-dev-1", 3101, "staged_pool", "staged-dev"),
    "staged-dev-2": InstanceSpec("staged-dev-2", 3102, "staged_pool", "staged-dev"),
    "staged-dev-3": InstanceSpec("staged-dev-3", 3103, "staged_pool", "staged-dev"),
    "staged-eval-1": InstanceSpec("staged-eval-1", 3201, "staged_pool", "staged-eval"),
    "staged-eval-2": InstanceSpec("staged-eval-2", 3202, "staged_pool", "staged-eval"),
    "staged-eval-3": InstanceSpec("staged-eval-3", 3203, "staged_pool", "staged-eval"),
}
HELDOUT = ("heldout-1", "heldout-2", "heldout-3")
STAGED_EVAL = ("staged-eval-1", "staged-eval-2", "staged-eval-3")


@dataclass
class Truth:
    cause: str
    remedies: list[str]
    value_variants: list[str]
    stale_variants: list[str]
    # group name -> list of [file, line]
    evidence_groups: dict[str, list[tuple[str, int]]]
    notes: str = ""
    # Cause codes of superseded hypotheses (e.g. an earlier, mitigated cause).
    stale_causes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause": self.cause,
            "remedies": self.remedies,
            "value_variants": self.value_variants,
            "stale_variants": self.stale_variants,
            "stale_causes": self.stale_causes,
            "evidence_groups": {k: [list(x) for x in v] for k, v in self.evidence_groups.items()},
            "notes": self.notes,
        }


@dataclass
class TaskInstance:
    spec: InstanceSpec
    prompt: str
    files: dict[str, str]
    truth: Truth = field(repr=False)
    # Staged tasks: stages[k] holds the files released at stage k+1 (union == files) and
    # stage_updates[k] the update message shown when that stage is released.
    stages: list[dict[str, str]] = field(default_factory=list)
    stage_updates: list[str] = field(default_factory=list)
    generator_version: str = GENERATOR_VERSION

    @property
    def staged(self) -> bool:
        return bool(self.stages)

    @property
    def n_stages(self) -> int:
        return len(self.stages) or 1

    def files_upto(self, stage: int | None = None) -> dict[str, str]:
        if not self.staged or stage is None:
            return self.files
        out: dict[str, str] = {}
        for files in self.stages[:stage]:
            out.update(files)
        return out

    @staticmethod
    def _sha(files: dict[str, str]) -> str:
        h = hashlib.sha256()
        for path in sorted(files):
            h.update(path.encode())
            h.update(b"\0")
            h.update(files[path].encode())
            h.update(b"\0")
        return h.hexdigest()

    @property
    def fixture_sha256(self) -> str:
        return self._sha(self.files)

    def sha_upto(self, stage: int | None) -> str:
        return self._sha(self.files_upto(stage))

    def write_fixtures(self, root: Path, stage: int | None = None) -> None:
        """Write all files, or for staged tasks only stage ``stage`` (1-based) itself."""
        files = self.files if not self.staged or stage is None else self.stages[stage - 1]
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- generation

SERVICES = [
    "orders-api",
    "billing-svc",
    "catalog-api",
    "fulfil-svc",
    "quotes-api",
    "profile-svc",
    "payments-api",
    "inventory-svc",
    "shipping-api",
    "returns-svc",
]
EDGES = ["edge-gw", "front-proxy", "ingress-a", "api-gateway"]
UPSTREAMS = ["ledger-api", "pricing-core", "tax-engine", "rates-svc", "risk-score"]
ROUTES = [
    "/v1/items",
    "/v1/orders",
    "/v1/quote",
    "/v1/cart",
    "/v1/status",
    "/v1/search",
    "/v1/account",
    "/v1/history",
]
PEOPLE = ["r.okafor", "m.lindqvist", "a.tanaka", "j.moreau", "s.patel", "k.novak"]


@dataclass
class _Line:
    t: float  # seconds since 00:00 of incident day
    text: str
    tag: str | None = None


class _Clock:
    def __init__(self, date: str) -> None:
        self.date = date

    def ts(self, t: float, ms: bool = True) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        if ms:
            return f"{self.date}T{h:02d}:{m:02d}:{s:06.3f}Z"
        return f"{self.date}T{h:02d}:{m:02d}:{int(s):02d}Z"

    @staticmethod
    def hm(t: float) -> str:
        return f"{int(t // 3600):02d}:{int((t % 3600) // 60):02d}"


def _render(lines: list[_Line]) -> tuple[str, dict[str, list[int]]]:
    lines = sorted(lines, key=lambda x: x.t)
    tags: dict[str, list[int]] = {}
    out = []
    for i, ln in enumerate(lines, start=1):
        out.append(ln.text)
        if ln.tag:
            tags.setdefault(ln.tag, []).append(i)
    return "\n".join(out) + "\n", tags


def _hexpairs(rng: random.Random, n: int = 8) -> str:
    return ":".join(f"{rng.randrange(256):02X}" for _ in range(n))


@dataclass
class _Ctx:
    rng: random.Random
    clock: _Clock
    svc: str
    edge: str
    upstream: str
    pods: list[str]
    build: str
    t0: float  # window start
    t1: float  # window end
    incident: float  # incident start


def _base_ctx(spec: InstanceSpec) -> _Ctx:
    rng = random.Random(spec.seed)
    day = rng.randrange(3, 27)
    month = rng.choice(["03", "04", "05", "06"])
    clock = _Clock(f"2031-{month}-{day:02d}")
    svc = rng.choice(SERVICES)
    edge = rng.choice(EDGES)
    upstream = rng.choice(UPSTREAMS)
    pods = [f"{svc}-{rng.choice('abcdef')}{rng.randrange(10, 99)}" for _ in range(3)]
    build = f"{rng.randrange(2, 9)}.{rng.randrange(0, 40)}.{rng.randrange(0, 9)}"
    t0 = 7 * 3600 + 30 * 60
    incident = 8 * 3600 + rng.randrange(10, 70) * 60 + rng.randrange(0, 59)
    t1 = incident + 75 * 60
    return _Ctx(rng, clock, svc, edge, upstream, pods, build, t0, t1, incident)


def _noise_service_log(c: _Ctx, n: int, distractors: list[str]) -> list[_Line]:
    rng, out = c.rng, []
    for _ in range(n):
        t = rng.uniform(c.t0, c.t1)
        pod = rng.choice(c.pods)
        r = rng.random()
        if r < 0.55:
            text = (
                f"{c.clock.ts(t)} INFO  {c.svc} [{pod}] req={rng.getrandbits(40):010x} "
                f"route={rng.choice(ROUTES)} status=200 dur_ms={rng.randrange(8, 140)}"
            )
        elif r < 0.80:
            text = f"{c.clock.ts(t)} INFO  {c.svc} [{pod}] heartbeat ok build={c.build}"
        elif r < 0.90:
            text = f"{c.clock.ts(t)} WARN  {c.svc} [{pod}] runtime gc pause {rng.randrange(120, 260)}ms (young)"
        else:
            text = f"{c.clock.ts(t)} WARN  {c.svc} [{pod}] {rng.choice(distractors)}"
        out.append(_Line(t, text))
    return out


def _noise_edge_log(c: _Ctx, n: int) -> list[_Line]:
    rng, out = c.rng, []
    for _ in range(n):
        t = rng.uniform(c.t0, c.t1)
        status = 200
        text = (
            f"{c.clock.ts(t)} {c.edge} access upstream={c.svc} method=GET "
            f"path={rng.choice(ROUTES)} status={status} rt={rng.uniform(0.01, 0.2):.3f}s"
        )
        out.append(_Line(t, text))
    for _ in range(n // 6):
        t = rng.uniform(c.t0, c.t1)
        out.append(_Line(t, f"{c.clock.ts(t)} {c.edge} health-check upstream={c.svc} ok"))
    return out


def _edge_503s(c: _Ctx, n: int, reason: str) -> list[_Line]:
    rng, out = c.rng, []
    for _ in range(n):
        t = rng.uniform(c.incident + 20, c.t1)
        out.append(
            _Line(
                t,
                (
                    f"{c.clock.ts(t)} {c.edge} access upstream={c.svc} method=GET "
                    f"path={rng.choice(ROUTES)} status=503 rt={rng.uniform(0.8, 5.5):.3f}s "
                    f'upstream_error="{reason}"'
                ),
            )
        )
    return out


def _metrics(c: _Ctx, header: str, row: Any) -> str:
    rows = [header]
    t = c.t0 - c.t0 % 60
    while t < c.t1:
        rows.append(f"{c.clock.hm(t)},{row(t)}")
        t += 60
    return "\n".join(rows) + "\n"


def _other_changes(c: _Ctx, n: int, start_id: int) -> list[_Line]:
    rng, out = c.rng, []
    others = [s for s in SERVICES if s != c.svc]
    templates = [
        "APPLIED  {o} scale replicas {a} -> {b} by autoscaler",
        "APPLIED  {o} image {o}:{v} rollout complete by deploy-bot",
        "APPLIED  {o} override log.level=info (was debug) by {p}",
        "REVERTED {o} override cache.ttl_s=30 (back to 120) by {p}",
        "PROPOSED {o} override http.max_body_kb=512 (pending review; not applied)",
    ]
    for i in range(n):
        t = rng.uniform(c.t0 - 3600 * 6, c.t1)
        tpl = rng.choice(templates)
        text = tpl.format(
            o=rng.choice(others),
            a=rng.randrange(2, 5),
            b=rng.randrange(5, 9),
            v=f"{rng.randrange(1, 9)}.{rng.randrange(0, 30)}",
            p=rng.choice(PEOPLE),
        )
        out.append(_Line(t, f"{c.clock.ts(t, ms=False)} CHG-{start_id + i} {text}"))
    return out


def _renumber_changes(lines: list[_Line], start_id: int) -> list[_Line]:
    """Give change records increasing IDs in time order."""
    lines = sorted(lines, key=lambda x: x.t)
    out = []
    for i, ln in enumerate(lines):
        out.append(
            _Line(ln.t, re.sub(r"CHG-\d+|CHG-X", f"CHG-{start_id + i}", ln.text, count=1), ln.tag)
        )
    return out


def _notes(c: _Ctx, items: list[tuple[float, str]]) -> str:
    out = [
        f"# On-call notes - {c.svc} incident {c.clock.date}",
        "",
        "Informal running notes. Not reviewed; may be out of date.",
        "",
    ]
    for t, text in sorted(items):
        out.append(f"- {c.clock.hm(t)} {text}")
    return "\n".join(out) + "\n"


def _yaml_config(c: _Ctx, extra: str) -> str:
    return (
        f"# Repository default configuration for {c.svc} (build {c.build}).\n"
        f"# Deploy-time overrides are recorded in deploy/changes.log.\n"
        f"service: {c.svc}\n"
        f"listen_port: {c.rng.choice([8080, 8443, 9000])}\n"
        f"log:\n  level: info\n"
        f"{extra}"
        f"cache:\n  ttl_s: 120\n  max_entries: {c.rng.choice([5000, 10000, 20000])}\n"
    )


def _gen_db_pool(spec: InstanceSpec) -> tuple[dict[str, str], Truth]:
    c = _base_ctx(spec)
    rng = c.rng
    stale = rng.choice([40, 48, 50, 60])
    value = rng.choice([8, 10, 12, 14])
    proposed = stale + 16
    t_override = c.incident - rng.randrange(90, 400)
    t_note = t_override - rng.randrange(600, 1800)
    distractors = [
        f"tls certificate for {c.svc}.internal expires in {rng.randrange(18, 40)} days",
        f"disk usage /var/data at {rng.randrange(62, 74)}%",
        f"upstream {c.upstream} rate-limit headroom {rng.randrange(35, 60)}%",
        "dns cache refresh retry succeeded",
    ]
    svc_lines = _noise_service_log(c, 1350, distractors)
    for _ in range(140):
        t = rng.uniform(c.incident, c.t1)
        svc_lines.append(
            _Line(
                t,
                (
                    f"{c.clock.ts(t)} ERROR {c.svc} [{rng.choice(c.pods)}] db pool "
                    f"exhausted: {value}/{value} connections in use, waited 5000ms "
                    f"(acquire_timeout) req={rng.getrandbits(40):010x}"
                ),
                "cause",
            )
        )
    for _ in range(25):
        t = rng.uniform(c.t0, c.incident)
        svc_lines.append(
            _Line(
                t,
                (
                    f"{c.clock.ts(t)} INFO  {c.svc} [{rng.choice(c.pods)}] db pool stats "
                    f"in_use={rng.randrange(3, 9)} idle={rng.randrange(1, 4)}"
                ),
            )
        )
    svc_log, svc_tags = _render(svc_lines)
    edge_log, _ = _render(_noise_edge_log(c, 520) + _edge_503s(c, 90, "upstream timeout"))
    chg = _other_changes(c, 14, 0)
    chg.append(
        _Line(
            t_override,
            (
                f"{c.clock.ts(t_override, ms=False)} CHG-X APPLIED  {c.svc} override "
                f"db.pool.max_size={value} (was {stale}) by deploy-bot; reason: align "
                f"with shared-db connection quota"
            ),
            "value",
        )
    )
    tp = c.incident + rng.randrange(900, 1800)
    chg.append(
        _Line(
            tp,
            (
                f"{c.clock.ts(tp, ms=False)} CHG-X PROPOSED {c.svc} override "
                f"db.pool.max_size={proposed} (pending review; not applied)"
            ),
        )
    )
    changes, chg_tags = _render(_renumber_changes(chg, rng.randrange(4100, 4900)))
    notes = _notes(
        c,
        [
            (
                t_note,
                f"Checked config/{c.svc}.yaml: db pool max_size is {stale}; pool should not be a concern.",
            ),
            (
                t_note + 240,
                f"Cert warning for {c.svc}.internal in logs - still weeks away, ignoring.",
            ),
            (
                c.incident + 300,
                f"{c.edge} showing 503s for {c.svc}. Suspect {c.upstream} slowness?",
            ),
            (c.incident + 900, "Upstream dashboards look normal. Still investigating."),
        ],
    )
    config = _yaml_config(
        c,
        (
            f"database:\n  host: {c.svc}-db.internal\n  pool:\n    max_size: {stale}\n"
            f"    min_idle: 4\n    acquire_timeout_ms: 5000\n"
        ),
    )

    def row(t: float) -> str:
        on = t >= c.incident
        in_use = value if on else rng.randrange(3, 9)
        p99 = rng.randrange(2600, 5200) if on else rng.randrange(60, 160)
        return f"{rng.randrange(180, 260)},{p99},{in_use},{rng.randrange(40, 60)}"

    metrics = _metrics(c, "minute,rps,p99_ms,db_pool_in_use,cpu_pct", row)
    files = {
        f"logs/{c.svc}.log": svc_log,
        f"logs/{c.edge}.log": edge_log,
        f"config/{c.svc}.yaml": config,
        "deploy/changes.log": changes,
        "ops/oncall-notes.md": notes,
        f"metrics/{c.svc}.csv": metrics,
    }
    truth = Truth(
        cause="DB_POOL_EXHAUSTED",
        remedies=["RAISE_DB_POOL_LIMIT"],
        value_variants=[str(value)],
        stale_variants=[str(stale)],
        evidence_groups={
            "cause": [(f"logs/{c.svc}.log", n) for n in svc_tags["cause"]],
            "value": [("deploy/changes.log", n) for n in chg_tags["value"]],
        },
        notes=f"effective db.pool.max_size={value} via override; config default {stale} is stale",
    )
    return files, truth


def _gen_tls_cert(spec: InstanceSpec) -> tuple[dict[str, str], Truth]:
    c = _base_ctx(spec)
    rng = c.rng
    old, new = _hexpairs(rng), _hexpairs(rng)
    t_note = c.incident - rng.randrange(2400, 4000)
    t_rollback = c.incident - rng.randrange(600, 1500)
    expiry = c.incident - rng.randrange(5, 40)
    distractors = [
        f"db pool stats in_use={rng.randrange(4, 9)} max={rng.choice([32, 40, 48])}",
        f"disk usage /var/data at {rng.randrange(60, 72)}%",
        f"upstream {c.upstream} latency p99 {rng.randrange(150, 300)}ms",
        "dns cache refresh retry succeeded",
    ]
    svc_log, _ = _render(_noise_service_log(c, 1300, distractors))
    edge = _noise_edge_log(c, 600)
    for _ in range(120):
        t = rng.uniform(c.incident, c.t1)
        edge.append(
            _Line(
                t,
                (
                    f"{c.clock.ts(t)} {c.edge} ERROR upstream={c.svc} tls handshake failed: "
                    f"x509: certificate has expired (serial={old}, "
                    f"notAfter={c.clock.ts(expiry, ms=False)})"
                ),
                "cause",
            )
        )
    edge += _edge_503s(c, 80, "tls handshake failed")
    edge_log, edge_tags = _render(edge)
    chg = _other_changes(c, 13, 0)
    t_up = t_note - rng.randrange(1200, 3000)
    chg.append(
        _Line(
            t_up,
            (
                f"{c.clock.ts(t_up, ms=False)} CHG-X APPLIED  {c.edge} cert bundle for "
                f"{c.svc} upgraded to serial {new} by {rng.choice(PEOPLE)}"
            ),
        )
    )
    chg.append(
        _Line(
            t_rollback,
            (
                f"{c.clock.ts(t_rollback, ms=False)} CHG-X APPLIED  {c.edge} ROLLBACK "
                f"cert bundle for {c.svc}: reverted to previous bundle (serial {old}) "
                f"after config error; serial {new} no longer served"
            ),
            "value",
        )
    )
    changes, chg_tags = _render(_renumber_changes(chg, rng.randrange(4100, 4900)))
    inv_rows = ["name,serial,not_after,status_at_snapshot"]
    inv_rows.append(f"{c.svc}-mtls,{new},2032-{rng.randrange(1, 12):02d}-15T00:00:00Z,active")
    inv_rows.append(f"{c.svc}-mtls,{old},{c.clock.ts(expiry, ms=False)},superseded")
    for s in rng.sample([x for x in SERVICES if x != c.svc], 6):
        inv_rows.append(
            f"{s}-mtls,{_hexpairs(rng)},2031-{rng.randrange(7, 12):02d}-0{rng.randrange(1, 9)}T00:00:00Z,active"
        )
    inv = [
        "# certificate inventory snapshot taken " + c.clock.ts(t_note - 600, ms=False),
        *inv_rows,
    ]
    notes = _notes(
        c,
        [
            (
                t_note,
                f"Renewal ticket says new cert {new} rolled out for {c.svc}; cert expiry is not a concern.",
            ),
            (
                c.incident + 240,
                f"{c.edge} 503s for {c.svc}. Could be db pool? stats look normal though.",
            ),
            (c.incident + 800, f"{c.upstream} latency fine. Still investigating."),
        ],
    )
    config = _yaml_config(
        c,
        (
            f"tls:\n  client_bundle: /etc/certs/{c.svc}-mtls.pem\n"
            f"database:\n  pool:\n    max_size: {rng.choice([32, 40, 48])}\n"
        ),
    )

    def row(t: float) -> str:
        on = t >= c.incident
        err = rng.randrange(35, 80) if on else rng.randrange(0, 2)
        return f"{rng.randrange(180, 260)},{err},{rng.randrange(60, 160)}"

    files = {
        f"logs/{c.svc}.log": svc_log,
        f"logs/{c.edge}.log": edge_log,
        f"config/{c.svc}.yaml": config,
        "deploy/changes.log": changes,
        "certs/inventory.csv": "\n".join(inv) + "\n",
        "ops/oncall-notes.md": notes,
        f"metrics/{c.edge}-{c.svc}.csv": _metrics(c, "minute,rps,error_pct,p99_ms", row),
    }
    variants = [old, old.replace(":", ""), old.lower(), old.replace(":", "").lower()]
    stale = [new, new.replace(":", ""), new.lower(), new.replace(":", "").lower()]
    truth = Truth(
        cause="TLS_CERT_EXPIRED",
        remedies=["ROTATE_CERTIFICATE"],
        value_variants=variants,
        stale_variants=stale,
        evidence_groups={
            "cause": [(f"logs/{c.edge}.log", n) for n in edge_tags["cause"]],
            "value": [("deploy/changes.log", n) for n in chg_tags["value"]],
        },
        notes=f"rollback re-served expired serial {old}; inventory/notes claim {new} is stale",
    )
    return files, truth


def _gen_client_timeout(spec: InstanceSpec) -> tuple[dict[str, str], Truth]:
    c = _base_ctx(spec)
    rng = c.rng
    stale = rng.choice([2000, 2500, 3000])
    value = rng.choice([600, 750, 800, 900])
    t_fix = c.incident - rng.randrange(60, 400)
    t_note = t_fix - rng.randrange(900, 2400)
    distractors = [
        f"db pool stats in_use={rng.randrange(4, 9)} max={rng.choice([32, 40, 48])}",
        f"tls certificate for {c.svc}.internal expires in {rng.randrange(20, 45)} days",
        f"upstream {c.upstream} rate-limit headroom {rng.randrange(35, 60)}%",
        f"disk usage /var/data at {rng.randrange(55, 70)}%",
    ]
    lines = _noise_service_log(c, 1400, distractors)
    for _ in range(130):
        t = rng.uniform(c.incident, c.t1)
        lines.append(
            _Line(
                t,
                (
                    f"{c.clock.ts(t)} ERROR {c.svc} [{rng.choice(c.pods)}] upstream call "
                    f"{c.upstream} timed out after {value}ms (attempt 1/1) "
                    f"req={rng.getrandbits(40):010x}"
                ),
                "cause",
            )
        )
    svc_log, svc_tags = _render(lines)
    edge_log, _ = _render(_noise_edge_log(c, 520) + _edge_503s(c, 70, "upstream returned 503"))
    chg = _other_changes(c, 15, 0)
    chg.append(
        _Line(
            t_fix,
            (
                f"{c.clock.ts(t_fix, ms=False)} CHG-X APPLIED  {c.svc} hotfix override "
                f"clients.{c.upstream}.timeout_ms={value} (was {stale}) by {rng.choice(PEOPLE)}; "
                f"reason: fail fast during retry storm"
            ),
            "value",
        )
    )
    changes, chg_tags = _render(_renumber_changes(chg, rng.randrange(4100, 4900)))
    notes = _notes(
        c,
        [
            (
                t_note,
                f"{c.upstream} timeout is {stale}ms per config/{c.svc}.yaml; upstream p99 is ~1.1s so timeouts are not a risk.",
            ),
            (c.incident + 200, f"{c.edge} reporting 503s from {c.svc}. Checking db pool."),
            (c.incident + 700, f"db pool fine. Rate-limit headroom on {c.upstream} also fine."),
        ],
    )
    config = _yaml_config(
        c,
        (
            f"clients:\n  {c.upstream}:\n    base_url: http://{c.upstream}.internal\n"
            f"    timeout_ms: {stale}\n    retries: 0\n"
        ),
    )

    def row(t: float) -> str:
        return f"{rng.randrange(300, 420)},{rng.randrange(950, 1350)},{rng.randrange(1500, 2100)}"

    files = {
        f"logs/{c.svc}.log": svc_log,
        f"logs/{c.edge}.log": edge_log,
        f"config/{c.svc}.yaml": config,
        "deploy/changes.log": changes,
        "ops/oncall-notes.md": notes,
        f"metrics/{c.upstream}-latency.csv": _metrics(c, "minute,rps,p99_ms,max_ms", row),
    }
    truth = Truth(
        cause="CLIENT_TIMEOUT_TOO_LOW",
        remedies=["RAISE_CLIENT_TIMEOUT"],
        value_variants=[str(value), f"{value}ms", f"{value} ms"],
        stale_variants=[str(stale), f"{stale}ms", f"{stale} ms"],
        evidence_groups={
            "cause": [(f"logs/{c.svc}.log", n) for n in svc_tags["cause"]],
            "value": [("deploy/changes.log", n) for n in chg_tags["value"]],
        },
        notes=f"hotfix set timeout {value}ms below upstream p99; config {stale}ms is stale",
    )
    return files, truth


def _gen_feature_flag(spec: InstanceSpec) -> tuple[dict[str, str], Truth]:
    c = _base_ctx(spec)
    rng = c.rng
    words = ["batch", "encoder", "ledger", "fastpath", "async", "pricing", "bulk", "stream"]
    flag = f"ff-{rng.choice(words)}-{rng.choice(words)}-v{rng.randrange(2, 6)}"
    other = f"ff-{rng.choice(words)}-{rng.choice(words)}-v{rng.randrange(6, 9)}"
    t_snap = c.incident - rng.randrange(2400, 4200)
    t_other_off = c.incident - rng.randrange(1200, 2000)
    t_on = c.incident - rng.randrange(30, 200)
    distractors = [
        f"db pool stats in_use={rng.randrange(4, 9)} max={rng.choice([32, 40, 48])}",
        f"tls certificate for {c.svc}.internal expires in {rng.randrange(20, 45)} days",
        f"upstream {c.upstream} latency p99 {rng.randrange(150, 300)}ms",
        f"disk usage /var/data at {rng.randrange(55, 70)}%",
    ]
    lines = _noise_service_log(c, 1400, distractors)
    for _ in range(130):
        t = rng.uniform(c.incident, c.t1)
        lines.append(
            _Line(
                t,
                (
                    f"{c.clock.ts(t)} ERROR {c.svc} [{rng.choice(c.pods)}] serializer failure "
                    f"in BatchEncoder (flag={flag}): field 'currency' missing "
                    f"req={rng.getrandbits(40):010x}"
                ),
                "cause",
            )
        )
    svc_log, svc_tags = _render(lines)
    edge_log, _ = _render(_noise_edge_log(c, 520) + _edge_503s(c, 80, "upstream returned 500"))
    third = f"ff-{rng.choice(words)}-ui-v{rng.randrange(1, 4)}"
    registry = {
        "snapshot_taken": c.clock.ts(t_snap, ms=False),
        "service": c.svc,
        "flags": {
            flag: {"rollout_pct": 0, "owner": rng.choice(PEOPLE)},
            other: {"rollout_pct": 100, "owner": rng.choice(PEOPLE)},
            third: {"rollout_pct": 50, "owner": rng.choice(PEOPLE)},
        },
    }
    audit = [
        _Line(
            t_snap - 5000,
            f"{c.clock.ts(t_snap - 5000, ms=False)} APPLIED flag {other} rollout 0% -> 100% for {c.svc} by {rng.choice(PEOPLE)}",
        ),
        _Line(
            t_other_off,
            f"{c.clock.ts(t_other_off, ms=False)} APPLIED flag {other} rollout 100% -> 0% for {c.svc} by {rng.choice(PEOPLE)} (cleanup)",
        ),
        _Line(
            t_on,
            f"{c.clock.ts(t_on, ms=False)} APPLIED flag {flag} rollout 0% -> 100% for {c.svc} by {rng.choice(PEOPLE)}",
            "value",
        ),
        _Line(
            c.incident + 1500,
            f"{c.clock.ts(c.incident + 1500, ms=False)} PROPOSED flag {third} rollout 50% -> 100% for {c.svc} (pending review; not applied)",
        ),
    ]
    for _ in range(10):
        t = rng.uniform(c.t0 - 7200, c.t1)
        s = rng.choice([x for x in SERVICES if x != c.svc])
        audit.append(
            _Line(
                t,
                f"{c.clock.ts(t, ms=False)} APPLIED flag ff-{rng.choice(words)}-{rng.choice(words)}-v1 rollout {rng.choice([0, 10, 50])}% -> {rng.choice([25, 100])}% for {s} by {rng.choice(PEOPLE)}",
            )
        )
    audit_log, audit_tags = _render(audit)
    chg = _other_changes(c, 12, 0)
    changes, _ = _render(_renumber_changes(chg, rng.randrange(4100, 4900)))
    notes = _notes(
        c,
        [
            (
                t_snap + 300,
                f"flags/registry.json shows {other} at 100% for {c.svc} and {flag} at 0% - if anything, suspect {other}.",
            ),
            (c.incident + 250, f"{c.edge} 500/503s for {c.svc}. db pool stats look normal."),
            (
                c.incident + 900,
                "No deploys of our service today per changes.log. Still investigating.",
            ),
        ],
    )
    config = _yaml_config(
        c, "flags:\n  source: flags/registry.json (snapshot) + flags/audit.log (live changes)\n"
    )
    files = {
        f"logs/{c.svc}.log": svc_log,
        f"logs/{c.edge}.log": edge_log,
        f"config/{c.svc}.yaml": config,
        "deploy/changes.log": changes,
        "flags/registry.json": json.dumps(registry, indent=2) + "\n",
        "flags/audit.log": audit_log,
        "ops/oncall-notes.md": notes,
    }
    truth = Truth(
        cause="FEATURE_FLAG_REGRESSION",
        remedies=["DISABLE_FEATURE_FLAG"],
        value_variants=[flag],
        stale_variants=[other],
        evidence_groups={
            "cause": [(f"logs/{c.svc}.log", n) for n in svc_tags["cause"]],
            "value": [("flags/audit.log", n) for n in audit_tags["value"]],
        },
        notes=f"{flag} enabled at 100% just before incident; registry snapshot suggesting {other} is stale",
    )
    return files, truth


_GENERATORS = {
    "db_pool": _gen_db_pool,
    "tls_cert": _gen_tls_cert,
    "client_timeout": _gen_client_timeout,
    "feature_flag": _gen_feature_flag,
}


def task_prompt(files: dict[str, str]) -> str:
    causes = "\n".join(f"  {k}: {v}" for k, v in CAUSE_CODES.items())
    remedies = "\n".join(f"  {k}: {v}" for k, v in REMEDY_CODES.items())
    listing = "\n".join(f"  {p} ({t.count(chr(10))} lines)" for p, t in sorted(files.items()))
    return f"""Production incident: the edge gateway started returning errors for one service.
Find the root cause using the read-only files under /task/fixtures:
{listing}

Rules of evidence:
- Records marked APPLIED in change or audit logs are authoritative. They supersede repository
  config files, snapshots and earlier notes. If two authoritative records conflict, the later wins.
- PROPOSED records were not applied. On-call notes are informal and may be out of date.

Answer fields (all required):
- root_cause: start with exactly one cause code, then a colon and a one-sentence explanation.
- required_value: the exact effective configuration value or identifier at the centre of the
  root cause (for example a setting's effective value, a certificate serial, or a flag id),
  copied exactly as it appears in the authoritative evidence.
- remedy: start with exactly one remedy code, then a colon and a concrete action.
- evidence_refs: list of "path:line" or "path:start-end" references (paths relative to
  /task/fixtures, 1-based line numbers, ranges of at most 20 lines) supporting both the
  cause and the required value.

Cause codes:
{causes}
Remedy codes:
{remedies}
"""


def generate(name: str) -> TaskInstance:
    spec = INSTANCES[name]
    if spec.scenario.startswith("staged"):
        from .staged import generate_staged

        return generate_staged(spec)
    files, truth = _GENERATORS[spec.scenario](spec)
    return TaskInstance(spec=spec, prompt=task_prompt(files), files=files, truth=truth)


# ------------------------------------------------------------------- scoring

REF_RE = re.compile(
    r"^\s*(?:/task/fixtures/)?([\w./-]+?)\s*(?::|#L|\s+line\s+)L?(\d+)(?:\s*-\s*L?(\d+))?\s*$"
)
MAX_RANGE = 20


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().strip("`'\"").strip()).lower()


def _first_code(text: str, codes: dict[str, str]) -> str | None:
    best: tuple[int, str] | None = None
    up = text.upper()
    for code in codes:
        i = up.find(code)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, code)
    return best[1] if best else None


@dataclass
class Score:
    cause_code: str | None
    cause_ok: bool
    remedy_code: str | None
    remedy_ok: bool
    value_status: str  # "exact" | "stale" | "wrong" | "missing"
    groups_covered: dict[str, bool]
    valid_refs: int
    invalid_refs: list[str]
    strict_success: bool
    outcome: str  # "correct" | "stale_value" | "unsupported" | "incorrect" | "no_answer"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @property
    def components(self) -> dict[str, float]:
        g = self.groups_covered
        return {
            "cause": float(self.cause_ok),
            "remedy": float(self.remedy_ok),
            "value": float(self.value_status == "exact"),
            "evidence": sum(g.values()) / len(g) if g else 0.0,
        }


def no_answer_score(truth: Truth) -> Score:
    return Score(
        None,
        False,
        None,
        False,
        "missing",
        dict.fromkeys(truth.evidence_groups, False),
        0,
        [],
        False,
        "no_answer",
    )


SETTING_RE = re.compile(r"^[a-z_][\w.\-]*\s*=\s*(\S.*)$")


def score_answer(
    answer: dict[str, Any] | None,
    truth: Truth,
    files: dict[str, str],
    version: str = SCORER_VERSION,
) -> Score:
    if not answer:
        return no_answer_score(truth)
    root = str(answer.get("root_cause", ""))
    remedy = str(answer.get("remedy", ""))
    value = _norm(str(answer.get("required_value", "")))
    cause_code = _first_code(root, CAUSE_CODES)
    remedy_code = _first_code(remedy, REMEDY_CODES)
    candidates = {value}
    if version != "score/1":
        m = SETTING_RE.match(value)
        if m:
            candidates.add(_norm(m.group(1)))
    if not value:
        value_status = "missing"
    elif candidates & {_norm(v) for v in truth.value_variants}:
        value_status = "exact"
    elif candidates & {_norm(v) for v in truth.stale_variants}:
        value_status = "stale"
    else:
        value_status = "wrong"
    line_counts = {p: t.count("\n") for p, t in files.items()}
    groups = {g: set(map(tuple, lines)) for g, lines in truth.evidence_groups.items()}
    covered = dict.fromkeys(groups, False)
    valid, invalid = 0, []
    refs = answer.get("evidence_refs") or []
    if not isinstance(refs, list):
        refs = [refs]
    for ref in refs:
        m = REF_RE.match(str(ref))
        if not m:
            invalid.append(str(ref)[:120])
            continue
        path, a = m.group(1), int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        if (
            path not in line_counts
            or a < 1
            or b < a
            or b > line_counts[path]
            or b - a + 1 > MAX_RANGE
        ):
            invalid.append(str(ref)[:120])
            continue
        valid += 1
        for g, lines in groups.items():
            if any((path, n) in lines for n in range(a, b + 1)):
                covered[g] = True
    cause_ok = cause_code == truth.cause
    remedy_ok = remedy_code in truth.remedies
    strict = cause_ok and remedy_ok and value_status == "exact" and all(covered.values())
    if strict:
        outcome = "correct"
    elif value_status == "stale":
        outcome = "stale_value"
    elif cause_code in truth.stale_causes:
        outcome = "stale_hypothesis"
    elif cause_ok and remedy_ok and value_status == "exact":
        outcome = "unsupported"
    else:
        outcome = "incorrect"
    return Score(
        cause_code,
        cause_ok,
        remedy_code,
        remedy_ok,
        value_status,
        covered,
        valid,
        invalid,
        strict,
        outcome,
    )
