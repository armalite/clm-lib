"""The docthread fixture (`docthread-gen/1`): one fictional incident on a document-processing pipeline.

The platform accepts uploaded documents and pushes each one, as a job, through a chain of components (intake,
scan, split, ocr, extract, index, notify), each consuming its own queue. On the incident day, a configuration
change on one processing component makes it fail jobs it used to handle; after repeated failures it rejects them,
and customers see their uploads come back rejected.

Everything the investigation needs is in one read-only collection, available from the start:

- **job journals:** each component's event journal, by hour and page (the incident hour of the failing component is
  much longer than any other);
- **the configuration change log** of each component;
- **queue statistics** for each queue, by hour, in ten-minute rows;
- **the operations playbook**, with a symptom catalogue and procedures.

Three things look relevant but are not the cause: a burst of legitimate rejections by the scan component two hours
earlier, an unrelated change on another component shortly before the incident, and a backlog on the index queue during
the incident (a scheduled reindex). Every scored fact appears in a declared set of tool outputs and in no other
(`uniqueness_problems`), so which tool call exposed it is known exactly.

All names, ids and numbers are invented and generated from the seed. Nothing in this module is model-visible except
the text the tool functions return.
"""

from __future__ import annotations

import dataclasses
import random
import re
import string
from collections.abc import Iterator
from typing import Any

GENERATOR_VERSION = "docthread-gen/1"

ROLES = ("intake", "scan", "split", "ocr", "extract", "index", "notify")
QUEUED_ROLES = ROLES[1:]  # intake has no inbound queue
HOURS = ("08:00", "09:00", "10:00", "11:00", "12:00")
INCIDENT_HOUR, BURST_HOUR = "11:00", "09:00"
EVENTS_PER_PAGE = 58
OUTPUT_BUDGET_CHARS = 7_800  # every tool output stays under the shared 8,000-code-point cap, so the cap never cuts a fact
ROOT_PAGES = 15  # the failing component's journal in the incident hour
INTAKE_INCIDENT_PAGES = 6
BURST_PAGES = 6
OTHER_PAGES = 3

WORDS = (
    "amber", "basalt", "cinder", "dune", "ember", "fjord", "garnet", "heath", "iris", "juniper",
    "kelp", "larch", "moss", "nimbus", "opal", "pike", "quarry", "reed", "slate", "tundra",
)  # fmt: skip
TEAMS = ("Sable", "Wren", "Otter", "Lynx", "Heron", "Marten", "Puffin", "Stoat", "Vole", "Kestrel")
DOC_KINDS = (
    "contract",
    "invoice scan",
    "claim form",
    "lease agreement",
    "lab report",
    "permit",
    "statement",
)


@dataclasses.dataclass(frozen=True)
class Mechanism:
    """A failure template: the setting a change sets too low, and how the failing component reports it."""

    key: str
    roles: tuple[str, ...]  # components it can affect
    setting: str
    old: int
    new: int
    unit: str
    precursor: str  # most failure lines (never states the setting's value)
    rejection: str  # the rejection line's detail
    evidence: str  # the one line that states the setting at its new value
    symptom_code: str
    symptom_section: str
    symptom_summary: str


MECHANISMS = {
    "lease": Mechanism(
        "lease",
        ("ocr", "extract"),
        "worker.lease_seconds",
        600,
        45,
        "s",
        "lease lost before completion; returned to queue (attempt {a} of 5)",
        "rejected after 5 delivery attempts; customer notified",
        "processing took {n} s but worker.lease_seconds=45, so the lease expired mid-job and the job was redelivered",
        "SYM-LEASE-SHORT",
        "lease-starvation",
        "jobs keep returning to the queue before they finish and are eventually rejected",
    ),
    "memory": Mechanism(
        "memory",
        ("ocr", "extract"),
        "worker.memory_limit_mb",
        4096,
        384,
        "MB",
        "worker process ended unexpectedly; job requeued (attempt {a} of 3)",
        "rejected after 3 worker crashes; customer notified",
        "worker killed while holding {n} MB, above worker.memory_limit_mb=384; large pages cannot be processed",
        "SYM-WORKER-MEM",
        "worker-memory-exhaustion",
        "workers die partway through larger documents and the jobs are eventually rejected",
    ),
    "pages": Mechanism(
        "pages",
        ("split",),
        "split.max_pages_per_job",
        1000,
        40,
        "pages",
        "document refused at split; held for retry (attempt {a} of 2)",
        "rejected: document cannot be split; customer notified",
        "document has {n} pages, more than split.max_pages_per_job=40, so it cannot be split into tasks",
        "SYM-PAGE-CAP",
        "page-cap-rejections",
        "documents above a certain length are refused at the split stage and rejected",
    ),
}
DECOY_SYMPTOMS = (
    (
        "SYM-SCAN-HOLD",
        "scan-quarantine",
        "uploads are quarantined by the malware scanner and rejected",
    ),
    (
        "SYM-INDEX-LAG",
        "index-backlog",
        "search results lag behind because the index queue is backed up",
    ),
    ("SYM-HOOK-DROP", "webhook-delivery-failures", "customers stop receiving completion webhooks"),
    (
        "SYM-TENANT-QUOTA",
        "tenant-quota",
        "one tenant's jobs are refused because it has reached its quota",
    ),
)


@dataclasses.dataclass
class Fixture:
    name: str
    seed: int
    date: str
    mechanism: Mechanism
    names: dict[str, str]  # role -> component name
    roles: dict[str, str]  # "root", "adjacent", "burst", "backlog" -> role
    journals: dict[tuple[str, str], list[str]]  # (component, hour) -> lines (without page headers)
    changes: dict[str, list[str]]  # component -> rendered change entries, newest first
    stats: dict[tuple[str, str], str]  # (queue, hour) -> rendered table
    playbook: dict[str, str]  # section -> text
    facts: dict[str, str]
    sources: dict[str, list[str]]  # fact -> call keys of the outputs that hold it
    distractors: dict[str, list[str]]
    layout: dict[str, Any]
    evidence_lines: list[str]  # the failing component's incident-hour journal lines (scorer only)
    signature: re.Pattern[str]

    @property
    def components(self) -> list[str]:
        return [self.names[r] for r in ROLES]

    @property
    def queues(self) -> list[str]:
        return [f"q.{self.names[r]}" for r in QUEUED_ROLES]

    def root(self) -> str:
        return self.names[self.roles["root"]]

    def pages(self, component: str, hour: str) -> int:
        lines = self.journals.get((component, hour), [])
        return max(1, -(-len(lines) // EVENTS_PER_PAGE))

    # ------------------------------------------------------------------ the tool outputs
    def journal_page(self, component: str, hour: str, page: int) -> str:
        lines = self.journals[(component, hour)]
        total = self.pages(component, hour)
        chunk = lines[(page - 1) * EVENTS_PER_PAGE : page * EVENTS_PER_PAGE]
        end = f"{int(hour[:2]) + 1:02d}:00"
        head = f"journal {component} {self.date} {hour}-{end} UTC, page {page} of {total}"
        cols = "seq    | time         | job        | tenant  | event          | detail"
        return "\n".join([head, cols, *chunk]) if chunk else f"{head}\n(no events on this page)"

    def change_log(self, component: str) -> str:
        entries = self.changes[component]
        head = f"configuration changes for {component}, {self.date}, newest first"
        return "\n\n".join([head, *entries]) if entries else f"{head}\n(no changes)"

    def queue_table(self, queue: str, hour: str) -> str:
        return self.stats[(queue, hour)]

    def playbook_section(self, section: str) -> str | None:
        return self.playbook.get(section)

    # ------------------------------------------------------------------ every possible call
    def every_call(self) -> Iterator[tuple[str, dict[str, Any]]]:
        for c in self.components:
            for h in HOURS:
                for p in range(1, self.pages(c, h) + 1):
                    yield "read_journal", {"component": c, "hour": h, "page": p}
            yield "list_changes", {"component": c}
        for q in self.queues:
            for h in HOURS:
                yield "queue_stats", {"queue": q, "hour": h}
        for s in self.playbook:
            yield "read_playbook", {"section": s}

    def output(self, tool: str, arguments: dict[str, Any]) -> str:
        args = arguments
        if tool == "read_journal":
            return self.journal_page(args["component"], args["hour"], int(args["page"]))
        if tool == "list_changes":
            return self.change_log(args["component"])
        if tool == "queue_stats":
            return self.queue_table(args["queue"], args["hour"])
        if tool == "read_playbook":
            return self.playbook[args["section"]]
        raise KeyError(tool)

    def render_all(self) -> dict[str, str]:
        return {canonical_call(t, a): self.output(t, a) for t, a in self.every_call()}


def canonical_call(tool: str, args: dict[str, Any]) -> str:
    """A canonical identity for a call (used for refetch counting and fact sources)."""
    parts = ", ".join(f"{k}={args[k]}" for k in sorted(args))
    return f"{tool}({parts})"


_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:")


def mentions(text: str, value: str) -> bool:
    """Whether `value` occurs in `text` as a whole token: the characters on either side may not continue an
    identifier, number or time (letters, digits, `_ - . :`), except that a sentence's full stop or comma may follow
    as long as no digit comes after it. A number also matches when written with thousands separators."""
    spellings = {value, f"{int(value):,}"} if value.isdigit() else {value}
    for form in spellings:
        start = text.find(form)
        while start != -1:
            before = text[start - 1] if start else " "
            after = text[start + len(form) : start + len(form) + 2]
            ends_token = (
                not after
                or after[0] not in _TOKEN_CHARS
                or (after[0] in ".," and (len(after) < 2 or not after[1].isdigit()))
            )
            if before not in _TOKEN_CHARS and ends_token:
                return True
            start = text.find(form, start + 1)
    return False


def uniqueness_problems(fx: Fixture) -> list[str]:
    """Every fact must appear in exactly its declared source outputs and nowhere else."""
    outs = fx.render_all()
    problems = []
    for fact, value in fx.facts.items():
        if fact in ("evidence_event", "rejecting_component"):
            continue  # a quote is checked against journal lines; the component name appears everywhere by design
        holders = sorted(k for k, text in outs.items() if mentions(text, value))
        if holders != sorted(fx.sources[fact]):
            problems.append(
                f"{fact}={value!r}: held by {holders}, declared {sorted(fx.sources[fact])}"
            )
    for key, text in outs.items():
        if len(text) > OUTPUT_BUDGET_CHARS:
            problems.append(f"{key}: {len(text)} characters, above {OUTPUT_BUDGET_CHARS}")
    return problems


# ---------------------------------------------------------------------- generation


class _Ids:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.used: set[str] = set()

    def fresh(self, make: Any) -> str:
        """`make()` until it yields an id not handed out before (ids are sparse, so this ends quickly)."""
        candidates = (make() for _ in range(10_000))
        value = next(c for c in candidates if c not in self.used)
        self.used.add(value)
        return value

    def job(self) -> str:
        alphabet = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
        return self.fresh(lambda: "J-" + "".join(self.rng.choice(alphabet) for _ in range(8)))

    def tenant(self) -> str:
        return f"tn-{self.rng.randint(1000, 9999)}"

    def change(self) -> str:
        return self.fresh(lambda: f"CHG-{self.rng.randint(10000, 99999)}")

    def upload(self) -> str:
        return self.fresh(lambda: "UP-" + "".join(self.rng.choice(string.digits) for _ in range(7)))


def _clock(hour: str, seconds: float) -> str:
    millis = round(
        (int(hour[:2]) * 3600 + seconds) * 1000
    )  # whole milliseconds, so rounding carries into seconds
    t, ms = divmod(millis, 1000)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}.{ms:03d}"


def _line(seq: int, time: str, job: str, tenant: str, event: str, detail: str) -> str:
    return f"{seq:06d} | {time} | {job:<10} | {tenant:<7} | {event:<14} | {detail}"


_NORMAL = (
    ("received", "from {up}, {kind}, {pages} pages"),
    ("leased", "worker w{w} took the job (queue wait {wait} s)"),
    ("progress", "{done} of {pages} pages handled in {secs} s"),
    ("completed", "{pages} pages in {secs} s; handed to the next stage"),
)


def _normal_detail(rng: random.Random, ids: _Ids, event_ix: int) -> tuple[str, str]:
    event, template = _NORMAL[event_ix % len(_NORMAL)]
    pages = rng.randint(2, 30)
    return event, template.format(
        up=ids.upload(),
        kind=rng.choice(DOC_KINDS),
        pages=pages,
        w=rng.randint(10, 49),
        wait=rng.randint(1, 40),
        done=rng.randint(1, pages),
        secs=rng.randint(3, 35),
    )


def _times(rng: random.Random, n: int, start: float, end: float) -> list[float]:
    return sorted(round(rng.uniform(start, end), 3) for _ in range(n))


def generate(name: str, seed: int, mechanism: str) -> Fixture:
    """One instance. The mechanism is part of the instance definition (not drawn), so instances cover the
    mechanisms deliberately; everything else is drawn from the seed."""
    for attempt in range(
        50
    ):  # re-draw (deterministically) if a generated number collides with another output
        fx = _generate(name, seed, attempt, MECHANISMS[mechanism])
        if not uniqueness_problems(fx):
            return fx
    raise RuntimeError(f"{name}: could not generate a fixture with unique facts")


def _generate(name: str, seed: int, attempt: int, mech: Mechanism) -> Fixture:
    rng = random.Random(f"{GENERATOR_VERSION}:{seed}:{attempt}")
    ids = _Ids(rng)
    words = rng.sample(WORDS, len(ROLES))
    names = {role: f"{w}-{role}" for role, w in zip(ROLES, words, strict=True)}
    root = rng.choice(mech.roles)
    adjacent = rng.choice([r for r in ("index", "notify", "intake") if r != root])
    roles = {"root": root, "adjacent": adjacent, "burst": "scan", "backlog": "index"}
    month, day = rng.randint(1, 12), rng.randint(1, 28)
    date = f"2026-{month:02d}-{day:02d}"
    root_name = names[root]

    # --- the failing component's incident hour: the first rejection deep in it, the evidence later still
    n_root = ROOT_PAGES * EVENTS_PER_PAGE
    first_page = rng.randint(8, 11)
    first_ix = (first_page - 1) * EVENTS_PER_PAGE + rng.randint(4, EVENTS_PER_PAGE - 5)
    ev_page = min(first_page + rng.randint(2, 4), ROOT_PAGES - 1)
    ev_ix = (ev_page - 1) * EVENTS_PER_PAGE + rng.randint(3, EVENTS_PER_PAGE - 4)
    precursor_from = first_ix - rng.randint(60, 90)
    times = _times(rng, n_root, 0, 3599)
    first_job = ids.job()
    first_tenant = ids.tenant()
    lines: list[str] = []
    seq0 = rng.randint(100_000, 400_000)
    rejected = 0
    for i, t in enumerate(times):
        clock = _clock(INCIDENT_HOUR, t)
        if i == first_ix:
            lines.append(
                _line(seq0 + i, clock, first_job, first_tenant, "job-rejected", mech.rejection)
            )
            rejected += 1
        elif i == ev_ix:
            lines.append(
                _line(
                    seq0 + i,
                    clock,
                    ids.job(),
                    ids.tenant(),
                    "diagnostic",
                    mech.evidence.format(
                        n=rng.randint(60, 900) if mech.key != "pages" else rng.randint(41, 400)
                    ),
                )
            )
        elif i in (
            first_ix - 9,
            first_ix - 4,
        ):  # the first rejected job's earlier attempts (same pages, turn 1)
            a = 3 if i == first_ix - 9 else 4
            lines.append(
                _line(seq0 + i, clock, first_job, first_tenant, "retry", mech.precursor.format(a=a))
            )
        elif i > precursor_from and (i > first_ix and rng.random() < 0.3):
            lines.append(
                _line(seq0 + i, clock, ids.job(), ids.tenant(), "job-rejected", mech.rejection)
            )
            rejected += 1
        elif i > precursor_from and rng.random() < 0.35:
            lines.append(
                _line(
                    seq0 + i,
                    clock,
                    ids.job(),
                    ids.tenant(),
                    "retry",
                    mech.precursor.format(a=rng.randint(1, 2)),
                )
            )
        else:
            event, detail = _normal_detail(rng, ids, i)
            lines.append(_line(seq0 + i, clock, ids.job(), ids.tenant(), event, detail))
    first_time = _clock(INCIDENT_HOUR, times[first_ix])
    first_seconds = 11 * 3600 + times[first_ix]

    journals: dict[tuple[str, str], list[str]] = {(root_name, INCIDENT_HOUR): lines}

    # --- every other journal: ordinary traffic, plus the intake view and the scan burst
    intake = names["intake"]
    burst_name = names["scan"]
    burst_times = _times(rng, 12, 1200, 1700)
    for role in ROLES:
        comp = names[role]
        for hour in HOURS:
            if (comp, hour) in journals:
                continue
            pages = OTHER_PAGES
            if role == "intake" and hour == INCIDENT_HOUR:
                pages = INTAKE_INCIDENT_PAGES
            if role == "scan" and hour == BURST_HOUR:
                pages = BURST_PAGES
            count = pages * EVENTS_PER_PAGE - rng.randint(0, 6)
            ts = _times(rng, count, 0, 3599)
            out = []
            seq = rng.randint(100_000, 900_000)
            for i, t in enumerate(ts):
                clock = _clock(hour, t)
                if role == "intake":
                    up = ids.upload()
                    if hour == INCIDENT_HOUR and t > times[first_ix] + 4 and rng.random() < 0.3:
                        detail = (
                            f"upload {up} status REJECTED (stage {root_name}); customer notified"
                        )
                        out.append(
                            _line(seq + i, clock, "-", ids.tenant(), "status-change", detail)
                        )
                        continue
                    if (
                        hour == BURST_HOUR
                        and burst_times[0] + 3 < t < burst_times[-1] + 3
                        and rng.random() < 0.15
                    ):
                        detail = (
                            f"upload {up} status REJECTED (stage {burst_name}); customer notified"
                        )
                        out.append(
                            _line(seq + i, clock, "-", ids.tenant(), "status-change", detail)
                        )
                        continue
                    detail = (
                        f"upload {up} accepted, {rng.choice(DOC_KINDS)}, {rng.randint(1, 40)} pages"
                    )
                    out.append(_line(seq + i, clock, "-", ids.tenant(), "upload", detail))
                    continue
                event, detail = _normal_detail(rng, ids, i)
                out.append(_line(seq + i, clock, ids.job(), ids.tenant(), event, detail))
            if role == "scan" and hour == BURST_HOUR:
                for k, bt in enumerate(burst_times):
                    out.append(
                        _line(
                            seq + 900 + k,
                            _clock(hour, bt),
                            ids.job(),
                            ids.tenant(),
                            "job-rejected",
                            "quarantined: file matched a known malware signature; customer notified",
                        )
                    )
                out.sort(key=lambda line: line.split(" | ")[1])
            journals[(comp, hour)] = out
    burst_first = _clock(BURST_HOUR, burst_times[0])
    intake_first = next(
        line.split(" | ")[1].strip()
        for line in journals[(intake, INCIDENT_HOUR)]
        if "REJECTED" in line
    )

    # --- configuration change logs
    changes: dict[str, list[str]] = {}
    faulty = ids.change()
    adjacent_id = ids.change()
    change_at = first_seconds - rng.randint(35, 80) * 60
    adjacent_at = first_seconds - rng.randint(8, 25) * 60
    adjacent_setting = {
        "index": ("index.commit_interval_ms", 1000, 1500),
        "notify": ("notify.retry_backoff_ms", 500, 800),
        "intake": ("intake.max_upload_mb", 50, 75),
    }[adjacent]

    def entry(cid: str, at: float, setting: str, old: Any, new: Any, note: str) -> str:
        hh, mm = int(at // 3600), int(at % 3600 // 60)
        team = rng.choice(TEAMS)
        return f"{cid}  {date} {hh:02d}:{mm:02d} UTC  by team-{team}  status=applied\n  set {setting}: {old} -> {new}\n  note: {note}"

    benign_notes = (
        "tidy log verbosity",
        "rotate service credentials",
        "raise dashboard sampling",
        "dependency refresh",
    )
    for role in ROLES:
        comp = names[role]
        items: list[tuple[float, str]] = []
        for _ in range(rng.randint(1, 2)):
            at = rng.uniform(6 * 3600, 9.5 * 3600)
            old, new = rng.choice(
                [("info", "warn"), ("warn", "info"), ("info", "debug"), ("debug", "info")]
            )
            items.append(
                (
                    at,
                    entry(
                        ids.change(), at, f"{role}.log_level", old, new, rng.choice(benign_notes)
                    ),
                )
            )
        if role == root:
            items.append(
                (
                    change_at,
                    entry(
                        faulty,
                        change_at,
                        mech.setting,
                        mech.old,
                        mech.new,
                        "align worker settings with the new capacity plan",
                    ),
                )
            )
        if role == adjacent:
            s, o, n = adjacent_setting
            items.append(
                (adjacent_at, entry(adjacent_id, adjacent_at, s, o, n, "smooth out load spikes"))
            )
        changes[comp] = [text for _, text in sorted(items, reverse=True)]

    # --- queue statistics
    stats: dict[tuple[str, str], str] = {}
    peak_age = rng.randint(1200, 2900)
    peak_slot = rng.randint(3, 5)
    backlog_peak = rng.randint(3000, 5400)
    for role in QUEUED_ROLES:
        comp = names[role]
        q = f"q.{comp}"
        for hour in HOURS:
            rows = []
            totals = {"enqueued": 0, "done": 0, "rejected": 0, "requeued": 0}
            peak = (0, "")
            incident_root = role == root and hour == INCIDENT_HOUR
            # The rows count the journal's own rejection events, so the table and the journal agree.
            reject_split = [0] * 6
            if incident_root:
                for i, line in enumerate(lines):
                    if "| job-rejected" in line:
                        reject_split[int(times[i] // 600)] += 1
            for slot in range(6):
                start = f"{hour[:2]}:{slot * 10:02d}"
                enq = rng.randint(180, 320)
                rej = 0
                req = rng.randint(0, 4)
                age = rng.randint(4, 40)
                if incident_root:
                    rej = reject_split[slot]
                    req = (
                        rng.randint(30, 90)
                        if slot >= reject_split.index(next(x for x in reject_split if x)) - 1
                        else req
                    )
                    age = (
                        peak_age
                        if slot == peak_slot
                        else rng.randint(60, peak_age - 100)
                        if slot > 1
                        else age
                    )
                if role == "index" and hour == INCIDENT_HOUR:
                    age = backlog_peak if slot == 4 else rng.randint(400, backlog_peak - 200)
                if role == "scan" and hour == BURST_HOUR and slot in (2, 3):
                    rej = 6
                done = max(enq - rej - rng.randint(0, 12), 0)
                depth = (
                    rng.randint(5, 60)
                    if not (incident_root or (role == "index" and hour == INCIDENT_HOUR))
                    else rng.randint(300, 900)
                )
                p95 = rng.randint(8, 40)
                rows.append(
                    f"{start}  {enq:>8}  {done:>6}  {rej:>8}  {req:>8}  {depth:>6}  {age:>13}  {p95:>12}"
                )
                totals["enqueued"] += enq
                totals["done"] += done
                totals["rejected"] += rej
                totals["requeued"] += req
                if age > peak[0]:
                    peak = (age, start)
            end = f"{int(hour[:2]) + 1:02d}:00"
            head = f"queue {q}, {date} {hour}-{end} UTC, ten-minute rows"
            cols = (
                "from   enqueued    done  rejected  requeued   depth  oldest_wait_s  p95_service_s"
            )
            foot = (
                f"hour totals: enqueued {totals['enqueued']}, done {totals['done']}, rejected {totals['rejected']}, "
                f"requeued {totals['requeued']}; longest wait of a queued job {peak[0]} s (in the row from {peak[1]})"
            )
            stats[(q, hour)] = "\n".join([head, cols, *rows, foot])
    root_q = f"q.{root_name}"

    # --- the playbook
    symptoms = [(mech.symptom_code, mech.symptom_section, mech.symptom_summary)]
    for other in MECHANISMS.values():
        if other.key != mech.key:
            symptoms.append((other.symptom_code, other.symptom_section, other.symptom_summary))
    symptoms += list(DECOY_SYMPTOMS)
    rng.shuffle(symptoms)
    playbook: dict[str, str] = {}
    step_ids: set[str] = set()
    restore_step = ""
    for n, (code, section, summary) in enumerate(symptoms, start=1):
        steps = []
        for k, (verb, text) in enumerate(
            [
                (
                    "confirm",
                    "check the queue statistics and the component's journal for the pattern above",
                ),
                (
                    "contain",
                    "pause new uploads for the affected tenants if the backlog keeps growing",
                ),
                ("restore", _restore_text(code, mech, section)),
                (
                    "verify",
                    "watch the next two ten-minute rows of the queue statistics return to normal",
                ),
            ],
            start=1,
        ):
            sid = f"P-{n}.{k}"
            step_ids.add(sid)
            steps.append(f"  {sid} ({verb}): {text}")
            if code == mech.symptom_code and verb == "restore":
                restore_step = sid
        playbook[section] = (
            f"playbook: {section} ({code})\nwhat you see: {summary}\nprocedure:\n"
            + "\n".join(steps)
        )
    playbook["symptoms"] = "playbook: symptom catalogue\n" + "\n".join(
        f"  {code}  {summary}  (section: {section})" for code, section, summary in symptoms
    )
    playbook["contents"] = "playbook contents (read a section by name):\n" + "\n".join(
        f"  {s}" for s in ["symptoms", *(sec for _, sec, _ in symptoms), "escalation"]
    )
    playbook["escalation"] = (
        "playbook: escalation\n  page the owning team through the on-call rota after 30 minutes of customer impact;\n"
        "  incidents with rejected customer documents also need a status-page note."
    )

    # --- facts, their sources and distractors
    root_pages = [
        canonical_call("read_journal", {"component": root_name, "hour": INCIDENT_HOUR, "page": p})
        for p in range(1, ROOT_PAGES + 1)
    ]
    first_job_pages = sorted(
        {
            (first_ix - 9) // EVENTS_PER_PAGE,
            (first_ix - 4) // EVENTS_PER_PAGE,
            first_ix // EVENTS_PER_PAGE,
        }
    )
    facts = {
        "rejecting_component": root_name,
        "first_rejection_time": first_time,
        "faulty_change": faulty,
        "rejected_jobs": str(rejected),
        "evidence_event": lines[ev_ix],
        "adjacent_change": adjacent_id,
        "symptom_code": mech.symptom_code,
        "procedure_step": restore_step,
        "first_rejected_job": first_job,
        "peak_queue_wait_s": str(peak_age),
    }
    sources = {
        "rejecting_component": [],  # a name: appears widely by design; scored as an answer, not as a memory fact
        "first_rejection_time": [root_pages[first_ix // EVENTS_PER_PAGE]],
        "faulty_change": [canonical_call("list_changes", {"component": root_name})],
        "rejected_jobs": [canonical_call("queue_stats", {"queue": root_q, "hour": INCIDENT_HOUR})],
        "adjacent_change": [canonical_call("list_changes", {"component": names[adjacent]})],
        "symptom_code": [
            canonical_call("read_playbook", {"section": "symptoms"}),
            canonical_call("read_playbook", {"section": mech.symptom_section}),
        ],
        "procedure_step": [canonical_call("read_playbook", {"section": mech.symptom_section})],
        "first_rejected_job": [root_pages[p] for p in first_job_pages],
        "peak_queue_wait_s": [
            canonical_call("queue_stats", {"queue": root_q, "hour": INCIDENT_HOUR})
        ],
    }
    other_steps = sorted(step_ids - {restore_step})
    second_reject = next(
        line.split(" | ")[2].strip() for line in lines[first_ix + 1 :] if "job-rejected" in line
    )
    distractors = {
        "rejecting_component": [burst_name, names["index"], intake],
        "first_rejection_time": [burst_first, intake_first],
        "faulty_change": [adjacent_id],
        "adjacent_change": [faulty],
        "symptom_code": [c for c, _, _ in symptoms if c != mech.symptom_code],
        "procedure_step": other_steps,
        "first_rejected_job": [second_reject],
        "peak_queue_wait_s": [str(backlog_peak)],
        "rejected_jobs": [],
    }
    layout = {
        "first_rejection_page": first_page,
        "evidence_page": ev_page,
        "first_rejection_line": first_ix,
        "evidence_line": ev_ix,
        "intake_first_rejected_page": next(
            i // EVENTS_PER_PAGE + 1
            for i, line in enumerate(journals[(intake, INCIDENT_HOUR)])
            if "REJECTED" in line
        ),
        "attempt": attempt,
    }
    sig = re.compile(re.escape(f"{mech.setting}={mech.new}") + r"(?!\d)")
    fx = Fixture(
        name,
        seed,
        date,
        mech,
        names,
        roles,
        journals,
        changes,
        stats,
        playbook,
        facts,
        sources,
        distractors,
        layout,
        lines,
        sig,
    )
    # The component name is everywhere by design; don't check it for uniqueness.
    return fx


def _restore_text(code: str, mech: Mechanism, section: str) -> str:
    if code == mech.symptom_code:
        return f"revert the change that set {mech.setting.split('.')[-1]} through the change log, then drain the retry queue"
    return {
        "lease-starvation": "revert the most recent lease setting change and drain the retry queue",
        "worker-memory-exhaustion": "revert the most recent worker memory change and restart the worker pool",
        "page-cap-rejections": "revert the most recent page limit change and resubmit refused documents",
        "scan-quarantine": "release false positives from quarantine after a second scan",
        "index-backlog": "let the scheduled reindex finish, or pause it if it overlaps peak hours",
        "webhook-delivery-failures": "re-enable the customer's webhook endpoint and replay the dead letters",
        "tenant-quota": "raise the tenant's quota after billing approval",
    }[section]


# Development instances (seed, mechanism), fixed before any agent ran. Confirmation instances are not defined yet:
# they will be drawn when the protocol is frozen and are never used in development.
DEV_INSTANCES = {"docthread-dev-1": (7301, "lease"), "docthread-dev-2": (7302, "memory")}


def instance(name: str) -> Fixture:
    seed, mechanism = DEV_INSTANCES[name]
    return generate(name, seed, mechanism)
