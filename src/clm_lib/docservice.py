"""Experiment 012 v2: a deterministic document *service* (search and fetch) over a handbook.

Generator ``docsvc-gen/1``; answers are scored by ``handbook-checks/1`` (the v1 scorer,
unchanged). The v1 workload (``handbook-gen/1``) mounted the handbook in the sandbox, and agents
read it directly. Here the corpus exists only in host memory: the agent reaches it through the
runtime actions ``search`` and ``fetch`` (the declared service boundary, identical in every
condition). Nothing of the corpus is written to the fixture mount or the workspace except the
search results and fetched passages the agent receives, and saved copies of search results.

Corpus: 16 policy subjects with overlapping vocabulary (travel and expenses; people and
facilities), each with a current policy, a superseded version, an exception memo with a
carve-out and a related-budgets distractor; two FAQ documents restating current standard
values; and general documents. Sections carry subject-specific procedural sentences, not
generic filler. Questions and support sets follow ``handbook-gen/1`` (5 types, 6 questions).
Hidden truth (answers, support sets) stays in host memory until scoring.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any

from .handbook import DOMAINS as V1_DOMAINS
from .handbook import HandbookTruth, tools_namespace
from .tasks import InstanceSpec, TaskInstance

GENERATOR_VERSION = "docsvc-gen/1"
SCORER_VERSION = "handbook-checks/1"
MAX_K = 8
MAX_FETCH_IDS = 10
TODAY = "2026-10-13"

_EXTRA: dict[str, dict[str, Any]] = {
    "rail": dict(
        title="Travel policy: rail travel",
        quantity="rail fare limit",
        unit="EUR per journey",
        base=(80, 141, 5),
        exc=(160, 241, 10),
        add=(15, 36, 5),
        rule="The rail fare limit for a single business journey is {v} EUR, including seat reservations.",
        base_q="a standard business journey by train",
        group="journeys longer than four hours",
        group_q="a business train journey of five hours",
        sub="journeys booked less than 24 hours ahead",
        sub_q="a five-hour business train journey booked the evening before",
        cond="journeys that include a night train sleeper",
        cond_q="a standard business journey by train with a night sleeper",
        distract="Rail passes for commuting are handled by HR benefits; the commuting subsidy covers up to {v} EUR per quarter.",
        owner="travel office",
    ),
    "airfare": dict(
        title="Travel policy: flights",
        quantity="flight fare limit",
        unit="EUR per return flight",
        base=(300, 551, 25),
        exc=(700, 1101, 50),
        add=(50, 151, 25),
        rule="The flight fare limit for an economy return flight is {v} EUR.",
        base_q="a standard return flight within Europe",
        group="intercontinental flights",
        group_q="a return flight to Singapore",
        sub="flights booked by contractors",
        sub_q="a contractor's return flight to Singapore",
        cond="flights that require checked equipment",
        cond_q="a standard return flight within Europe with checked equipment",
        distract="Group flights for company offsites are bought centrally with a budget of {v} EUR per traveller.",
        owner="travel office",
    ),
    "taxi": dict(
        title="Travel policy: taxis and ride-hailing",
        quantity="taxi allowance",
        unit="EUR per ride",
        base=(25, 46, 1),
        exc=(50, 81, 5),
        add=(10, 26, 1),
        rule="A single taxi or ride-hailing trip on business is reimbursed up to {v} EUR.",
        base_q="a daytime business taxi ride",
        group="rides between 22:00 and 06:00",
        group_q="a business taxi ride at 23:30",
        sub="rides that start at a company office",
        sub_q="a business taxi ride at 23:30 starting at a company office",
        cond="rides with luggage for a trade fair",
        cond_q="a daytime business taxi ride with trade-fair luggage",
        distract="Shuttle buses between office sites are operated by facilities at a cost of {v} EUR per day.",
        owner="travel office",
    ),
    "parking": dict(
        title="Facilities policy: parking",
        quantity="monthly parking allowance",
        unit="EUR per month",
        base=(40, 91, 5),
        exc=(100, 161, 10),
        add=(15, 41, 5),
        rule="Employees who drive to the office receive a monthly parking allowance of {v} EUR.",
        base_q="a regular employee driving to the office",
        group="employees at the city-centre office",
        group_q="an employee who drives to the city-centre office",
        sub="employees with an assigned company parking space",
        sub_q="a city-centre employee with an assigned company parking space",
        cond="employees with an electric car using a charging bay",
        cond_q="a regular employee driving to the office in an electric car using a charging bay",
        distract="Visitor parking is paid by the hosting team at {v} EUR per visitor day.",
        owner="facilities",
    ),
    "sickleave": dict(
        title="HR policy: sick leave",
        quantity="paid sick days",
        unit="days per year",
        base=(10, 21, 1),
        exc=(25, 36, 1),
        add=(3, 8, 1),
        rule="Employees have {v} paid sick days per calendar year.",
        base_q="a regular full-time employee",
        group="employees with more than ten years of service",
        group_q="an employee with twelve years of service",
        sub="employees on a fixed-term contract",
        sub_q="an employee with twelve years of service on a fixed-term contract",
        cond="employees caring for a sick child",
        cond_q="a regular full-time employee caring for a sick child",
        distract="The health budget for team wellbeing events is {v} EUR per team each year.",
        owner="HR",
    ),
    "parental": dict(
        title="HR policy: parental leave",
        quantity="paid parental leave",
        unit="weeks",
        base=(8, 17, 1),
        exc=(18, 27, 1),
        add=(2, 5, 1),
        rule="Each parent may take {v} weeks of paid parental leave.",
        base_q="a regular employee becoming a parent",
        group="employees who are the primary carer",
        group_q="an employee who is the primary carer of a newborn",
        sub="employees in their first year of employment",
        sub_q="a primary carer in their first year of employment",
        cond="parents of multiple births",
        cond_q="a regular employee who becomes a parent of twins",
        distract="Parent networking groups receive a budget of {v} EUR per year for meetings.",
        owner="HR",
    ),
    "childcare": dict(
        title="HR policy: childcare subsidy",
        quantity="childcare subsidy",
        unit="EUR per month",
        base=(100, 251, 10),
        exc=(260, 401, 10),
        add=(30, 81, 10),
        rule="Employees with children under six receive a childcare subsidy of {v} EUR per month.",
        base_q="a regular employee with one child under six",
        group="single parents",
        group_q="a single parent with one child under six",
        sub="single parents working fewer than 20 hours a week",
        sub_q="a single parent with one child under six working 16 hours a week",
        cond="children with special care needs",
        cond_q="a regular employee with one child under six who has special care needs",
        distract="The holiday camp programme for employees' children is budgeted at {v} EUR per child.",
        owner="HR",
    ),
    "bike": dict(
        title="Benefits policy: bike leasing",
        quantity="bike-leasing allowance",
        unit="EUR per month",
        base=(30, 71, 5),
        exc=(80, 121, 5),
        add=(10, 26, 5),
        rule="Employees who lease a bike for commuting receive a bike-leasing allowance of {v} EUR per month.",
        base_q="a regular employee leasing a commuter bike",
        group="cargo bikes",
        group_q="an employee leasing a cargo bike for commuting",
        sub="cargo bikes used for private deliveries",
        sub_q="an employee leasing a cargo bike that is also used for private deliveries",
        cond="e-bikes with an insurance package",
        cond_q="a regular employee leasing an e-bike with the insurance package",
        distract="Bike parking racks at the offices are maintained by facilities for {v} EUR per year.",
        owner="facilities",
    ),
}
OWNER_V1 = {
    "lodging": "travel office",
    "meals": "travel office",
    "mileage": "travel office",
    "training": "learning team",
    "purchase": "procurement",
    "equipment": "facilities",
    "phone": "IT",
    "relocation": "HR",
}
DOMAINS: dict[str, dict[str, Any]] = {
    **{k: {**v, "owner": OWNER_V1[k]} for k, v in V1_DOMAINS.items()},
    **_EXTRA,
}
TRAVEL = ("lodging", "meals", "mileage", "rail", "airfare", "taxi", "purchase", "phone")

DETAIL = [
    "Claims for the {q} are filed in the expense tool under '{t}'.",
    "The {o} reviews the {q} once a year and announces changes in advance.",
    "Questions about the {q} go to the {o}, who keeps the record of decisions.",
    "Each claim for the {q} needs a receipt showing the date, the amount and the supplier.",
    "Managers confirm claims for the {q} before payment in the next payroll run.",
    "The {o} publishes worked examples of the {q} on the intranet.",
]
GENERIC = [
    (
        "Code of conduct",
        [
            "Respect at work: everyone is treated with courtesy and fairness.",
            "Conflicts of interest are declared to the line manager in writing.",
            "Gifts above 50 EUR are declined or registered with compliance.",
        ],
    ),
    (
        "Information security",
        [
            "Passwords are at least 14 characters and never shared.",
            "Laptops are locked when unattended, also in hotels and trains.",
            "Security incidents are reported to the service desk within one hour.",
        ],
    ),
    (
        "Onboarding",
        [
            "New employees receive their accounts on the first day.",
            "The mandatory trainings are completed within the first month.",
            "A buddy is assigned to every new employee for the first quarter.",
        ],
    ),
    (
        "Expense submission process",
        [
            "Claims are submitted in the expense tool with one line per cost.",
            "Approval routing follows the cost centre of the employee.",
            "Payments are made with the next payroll run after approval.",
        ],
    ),
    (
        "Public holidays and closures",
        [
            "Public holidays follow the calendar of the office location.",
            "The offices close between 24 and 26 December.",
            "Bridge days are announced by HR at the start of the year.",
        ],
    ),
    (
        "Office access",
        [
            "Badges are personal and must be worn visibly.",
            "Visitors are registered at reception and accompanied.",
            "Overnight stays in the office are not permitted.",
        ],
    ),
    (
        "Data retention",
        [
            "Records are kept for the periods in the retention schedule.",
            "Personal data is deleted when its purpose has ended.",
            "Legal holds suspend deletion until released by legal.",
        ],
    ),
    (
        "Glossary",
        [
            "A budget owner approves costs for a cost centre.",
            "Business travel means travel required by the employee's role.",
            "A claim is a request for reimbursement of a cost.",
        ],
    ),
]


def _val(rng: random.Random, spec: tuple[int, int, int]) -> int:
    return rng.randrange(*spec)


def _sec(rng: random.Random, s: dict[str, Any], core: str) -> str:
    details = rng.sample(DETAIL, 2)
    return " ".join(
        [core, *(d.format(q=s["quantity"], t=s["title"], o=s["owner"]) for d in details)]
    )


def _doc(doc: str, title: str, meta: str, sections: list[tuple[str, str]]) -> str:
    lines = [f"# {doc} · {title}", meta, ""]
    for i, (h, t) in enumerate(sections, start=1):
        lines += [f"§{i} {h}", t, ""]
    return "\n".join(lines)


def build_corpus(seed: int, n_questions: int = 6) -> tuple[dict[str, str], dict[str, Any]]:
    """(corpus documents by id, truth). At most 99 documents (ids HB-01..HB-99)."""
    rng = random.Random(seed)
    subjects = list(DOMAINS)
    n_docs = 4 * len(subjects) + 2 + len(GENERIC)
    ids = [f"HB-{i:02d}" for i in range(1, n_docs + 1)]
    rng.shuffle(ids)
    take = iter(ids)
    faq = {"travel": next(take), "people": next(take)}
    faq_secs: dict[str, list[tuple[str, str]]] = {"travel": [], "people": []}
    docs: dict[str, str] = {}
    facts: dict[str, dict[str, Any]] = {}
    for d in subjects:
        s = DOMAINS[d]
        v2, v1 = _val(rng, s["base"]), _val(rng, s["base"])
        while v1 == v2:
            v1 = _val(rng, s["base"])
        vx, add = _val(rng, s["exc"]), _val(rng, s["add"])
        e2, e1 = f"2026-0{rng.randrange(2, 7)}-01", f"2025-0{rng.randrange(1, 10)}-01"
        p, old, memo, dis = next(take), next(take), next(take), next(take)
        docs[p] = _doc(
            p,
            s["title"],
            f"version: 3 · status: current · effective: {e2} · supersedes: {old}",
            [
                ("Scope", _sec(rng, s, f"This policy sets the {s['quantity']} for all employees.")),
                ("Standard " + s["quantity"], _sec(rng, s, s["rule"].format(v=v2))),
                (
                    "Additions",
                    _sec(
                        rng,
                        s,
                        f"For {s['cond']}, {add} {s['unit']} is added to the standard {s['quantity']}.",
                    ),
                ),
                (
                    "Exceptions",
                    _sec(
                        rng, s, f"Group exceptions to the {s['quantity']} are published in {memo}."
                    ),
                ),
            ],
        )
        docs[old] = _doc(
            old,
            s["title"],
            f"version: 2 · status: superseded · effective: {e1} to the day before {e2} · superseded by: {p}",
            [
                ("Scope", _sec(rng, s, f"This version set the {s['quantity']} for all employees.")),
                (
                    "Standard " + s["quantity"],
                    _sec(rng, s, s["rule"].format(v=v1).replace(" is ", " was ", 1)),
                ),
                (
                    "Version status",
                    f"This version applied from {e1} until the day before {e2}. From {e2} it is superseded by {p}; claims for earlier dates keep this version's amounts.",
                ),
            ],
        )
        docs[memo] = _doc(
            memo,
            f"Memo: {s['quantity']} exceptions",
            f"version: 1 · status: current · effective: {e1}",
            [
                (
                    "Purpose",
                    _sec(
                        rng, s, f"This memo lists group exceptions to the {s['quantity']} in {p}."
                    ),
                ),
                (
                    "Exception for " + s["group"],
                    _sec(
                        rng,
                        s,
                        f"For {s['group']}, the {s['quantity']} is {vx} {s['unit']} instead of the standard amount.",
                    ),
                ),
                (
                    "Carve-out",
                    _sec(
                        rng,
                        s,
                        f"The exception above does not apply to {s['sub']}; they follow the standard {s['quantity']} in {p} §2.",
                    ),
                ),
            ],
        )
        docs[dis] = _doc(
            dis,
            f"Related budgets: {s['title'].split(': ')[-1]}",
            f"version: 1 · status: current · effective: {e1}",
            [
                (
                    "Scope",
                    _sec(
                        rng,
                        s,
                        f"This document covers budgets related to, but separate from, the {s['quantity']}.",
                    ),
                ),
                (
                    "Central budget",
                    _sec(
                        rng, s, s["distract"].format(v=_val(rng, s["base"]) + _val(rng, s["add"]))
                    ),
                ),
            ],
        )
        area = "travel" if d in TRAVEL else "people"
        faq_secs[area].append(
            (
                f"Current {s['quantity']}",
                f"Q: What is the current standard {s['quantity']}? A: {v2} {s['unit']}, as set in {p} §2 (effective {e2}). Group exceptions are listed in {memo}.",
            )
        )
        facts[d] = dict(
            v1=v1,
            v2=v2,
            vx=vx,
            add=add,
            e1=e1,
            e2=e2,
            p=p,
            old=old,
            memo=memo,
            dis=dis,
            faq=faq[area],
            faq_sec=len(faq_secs[area]),
        )
    for area, doc in faq.items():
        docs[doc] = _doc(
            doc,
            f"Policy FAQ: {'travel and expenses' if area == 'travel' else 'people and facilities'}",
            "version: 7 · status: current · effective: 2026-01-15",
            faq_secs[area],
        )
    for title, lines in GENERIC:
        doc = next(take)
        docs[doc] = _doc(
            doc,
            title,
            "version: 1 · status: current · effective: 2025-01-01",
            [(f"Part {i}", t) for i, t in enumerate(lines, start=1)],
        )
    asked = rng.sample(subjects, n_questions)
    types = [
        "current",
        "past",
        "exception",
        "carveout",
        "additive",
        rng.choice(["carveout", "additive"]),
    ]
    rng.shuffle(types)
    questions = []
    for i, (d, qt) in enumerate(zip(asked, types[:n_questions], strict=True), start=1):
        s, f = DOMAINS[d], facts[d]
        faq_ref, rule, rule_old = f"{f['faq']} §{f['faq_sec']}", f"{f['p']} §2", f"{f['old']} §2"
        if qt == "current":
            text, value, sets = (
                f"What is the {s['quantity']} for {s['base_q']} today ({TODAY})?",
                f["v2"],
                [[rule], [faq_ref]],
            )
        elif qt == "past":
            day = f"2026-0{int(f['e2'][6]) - 1}-15"
            text, value, sets = (
                f"What {s['quantity']} applied to {s['base_q']} on {day}?",
                f["v1"],
                [[rule_old]],
            )
        elif qt == "exception":
            text, value, sets = (
                f"What is the {s['quantity']} for {s['group_q']} today?",
                f["vx"],
                [[f"{f['memo']} §2"]],
            )
        elif qt == "carveout":
            text, value, sets = (
                f"What is the {s['quantity']} for {s['sub_q']} today?",
                f["v2"],
                [[f"{f['memo']} §3", rule], [f"{f['memo']} §3", faq_ref]],
            )
        else:
            text, value, sets = (
                f"What is the {s['quantity']} for {s['cond_q']} today?",
                f["v2"] + f["add"],
                [[rule, f"{f['p']} §3"], [faq_ref, f"{f['p']} §3"]],
            )
        questions.append(
            dict(
                qid=f"Q{i}",
                domain=d,
                type=qt,
                text=text,
                value=value,
                unit=s["unit"],
                support_sets=sets,
                old_value=f["v1"],
                old_passage=rule_old,
                facts=f,
            )
        )
    return docs, {"questions": questions, "faq_doc": faq["travel"]}


# ----------------------------------------------------------------------- the service
class DocService:
    """Host-side search and fetch over an in-memory corpus. Uses the v1 tool's parser and
    BM25 ranking (one algorithm), but never touches the sandbox filesystem."""

    def __init__(self, docs: dict[str, str]) -> None:
        import tempfile
        from pathlib import Path

        self.ns = tools_namespace()
        with tempfile.TemporaryDirectory() as tmp:
            for doc, text in docs.items():
                (Path(tmp) / f"{doc}.md").write_text(text, encoding="utf-8")
            self.passages: list[dict[str, Any]] = list(self.ns["load_passages"](tmp))
        self.by_id = {p["id"]: p for p in self.passages}
        self.docs = sorted(docs)

    def header(self, p: dict[str, Any]) -> str:
        return str(self.ns["header"](p))

    def search(self, query: str, k: int) -> list[dict[str, Any]]:
        return list(self.ns["rank"](self.passages, query, max(1, min(int(k), MAX_K))))

    def fetch(self, ids: list[str]) -> list[tuple[str, dict[str, Any] | None]]:
        """(requested id, passage or None). A document id ("HB-07") returns all its sections."""
        out: list[tuple[str, dict[str, Any] | None]] = []
        for raw in ids[:MAX_FETCH_IDS]:
            i = " ".join(str(raw).replace("#", " §").replace("§ ", "§").split())
            if i in self.by_id:
                out.append((i, self.by_id[i]))
            elif i in self.docs:
                out += [(p["id"], p) for p in self.passages if p["doc"] == i]
            else:
                out.append((i, None))
        return out


def task_prompt(questions: list[dict[str, Any]], smoke: bool = False) -> str:
    qs = "\n".join(f"- {q['qid']}: {q['text']} (answer in {q['unit']})" for q in questions)
    text = f"""Question-answering task using the company document service.

The company handbook is held by a document service; it is not available as files. Documents
have a header line (version, status, effective dates, what they supersede) and numbered
sections; each section is a passage, cited as "HB-07 §2". Documents can have superseded
versions, and memos can add group exceptions and carve-outs. Use the rule that applies to the
situation and date in the question.

Service actions (reply with one of these instead of "execute"):
  {{"action": "search", "query": "lodging limit Geneva", "k": 5, "thought": "..."}}
      up to k (at most {MAX_K}; default 5) ranked passages with ids and document headers. Each
      result is also saved under /task/workspace/search-results/ (path in the result header),
      where your code can read it later.
  {{"action": "fetch", "ids": ["HB-07 §2", "HB-12"], "thought": "..."}}
      the named passages in full; a document id ("HB-12") returns all its sections; at most
      {MAX_FETCH_IDS} ids per fetch.
You can still use "execute" for code (for example to read saved results or manage your
context), and "final" to answer.

Questions:
{qs}

Finish with {{"action": "final", "answer": {{"answers": [{{"question": "Q1", "value": "...",
"citations": ["HB-07 §2", ...]}}, ...]}}}}. Give one entry per question. The value must be the
exact amount; citations must include every passage needed to support it (for example the rule
and any exception or carve-out that applies), not just one passage.
"""
    if smoke:
        text += (
            "\nIntegration smoke run (this instance only): start with one search with k=6 for "
            "the first question. If the result lists omitted passages, fetch one of them. Then "
            "answer normally.\n"
        )
    return text


@dataclass
class ServiceTruth(HandbookTruth):
    corpus: dict[str, str] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        sha = hashlib.sha256(
            "".join(f"{k}\0{v}\0" for k, v in sorted(self.corpus.items())).encode()
        ).hexdigest()
        return {
            **super().to_dict(),
            "generator_version": GENERATOR_VERSION,
            "kind": "docservice",
            "corpus_documents": len(self.corpus),
            "corpus_sha256": sha,
        }


def generate_docservice(spec: InstanceSpec) -> TaskInstance:
    smoke = spec.name.startswith("docsvc-smoke")
    corpus, truth = build_corpus(spec.seed, 2 if smoke else 6)
    return TaskInstance(
        spec=spec,
        prompt=task_prompt(truth["questions"], smoke),
        files={
            "SERVICE.md": "The handbook is available only through the search and fetch actions.\n"
        },
        truth=ServiceTruth(truth["questions"], truth["faq_doc"], corpus=corpus),
        generator_version=GENERATOR_VERSION,
        kind="docservice",
        scorer_version=SCORER_VERSION,
    )
