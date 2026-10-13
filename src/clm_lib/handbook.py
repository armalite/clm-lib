"""Experiment 012: document-search question answering over a synthetic handbook.

Generator ``handbook-gen/1``, scorer ``handbook-checks/1``. Each instance is a handbook of 40
documents (``/task/fixtures/handbook/HB-NN.md``); every numbered section is a passage cited as
``HB-07 §2``. Six questions per instance need an exact value and the passages that support it:

- ``current``: the value in force today (a superseded older version is a natural distractor);
- ``past``: the value that applied on a past date (only the superseded version says so);
- ``exception``: a group exception in a memo;
- ``carveout``: a subgroup excluded from that exception, so the standard value applies (the
  memo's carve-out **and** the standard value are both needed);
- ``additive``: the standard value plus a condition-specific addition (both passages needed).

Every question has alternative valid evidence sets where the handbook restates a fact (a policy
FAQ restates each current standard value). Values are compared numerically, citations by
passage id in several spellings, so exact wording is not the only route to a correct answer.

Tools (fixture file ``handbook_tools.py``, identical in every condition): ``search(query, k)``
prints a ranked, structured result (stdlib BM25 over passages, deterministic) and saves the
original under ``/task/workspace/search-results/``; ``show(ids)`` prints named passages in full,
in a different, never-screened format. Documents can also be read directly. Hidden truth
(answers, support sets) stays in host memory until scoring and is never a tool or Jev input.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

from .tasks import InstanceSpec, TaskInstance

GENERATOR_VERSION = "handbook-gen/1"
SCORER_VERSION = "handbook-checks/1"
QTYPES = ("current", "past", "exception", "carveout", "additive")
TODAY = "2026-10-13"

DOMAINS: dict[str, dict[str, Any]] = {
    "lodging": dict(
        title="Travel policy: lodging",
        quantity="nightly lodging limit",
        unit="EUR per night",
        base=(150, 215, 5),
        exc=(240, 300, 10),
        add=(20, 45, 5),
        rule="The nightly lodging limit for business travel is {v} EUR per night, including taxes and city fees.",
        base_q="a standard business trip",
        group="business trips to Zurich, Geneva or Oslo",
        group_q="a business trip to Geneva",
        sub="contractors",
        sub_q="a contractor on a business trip to Geneva",
        cond="a stay that includes a Saturday night",
        cond_q="a standard business trip whose stay includes a Saturday night",
        distract="Lodging for speakers at company events is booked centrally by the events team; the event budget caps each speaker room at {v} EUR per night.",
    ),
    "meals": dict(
        title="Travel policy: meals",
        quantity="daily meal allowance",
        unit="EUR per day",
        base=(40, 61, 1),
        exc=(70, 91, 1),
        add=(25, 41, 1),
        rule="The daily meal allowance during business travel is {v} EUR per day.",
        base_q="a standard travel day",
        group="client-site days in London or Paris",
        group_q="a client-site day in Paris",
        sub="interns",
        sub_q="an intern on a client-site day in Paris",
        cond="a day with an approved client dinner",
        cond_q="a standard travel day with an approved client dinner",
        distract="Team lunches during internal workshops are paid from the workshop budget, which allows {v} EUR per participant per workshop.",
    ),
    "training": dict(
        title="Learning policy: training budget",
        quantity="annual training budget",
        unit="EUR per year",
        base=(900, 1550, 50),
        exc=(2000, 3050, 50),
        add=(300, 650, 50),
        rule="Each employee has an annual training budget of {v} EUR per calendar year.",
        base_q="a regular employee",
        group="employees in the security team",
        group_q="an employee in the security team",
        sub="employees still in their probation period",
        sub_q="a security-team employee who is still in their probation period",
        cond="employees who hold a team-lead role",
        cond_q="a regular employee who holds a team-lead role",
        distract="Conference attendance is funded by the department conference pool, which reserves {v} EUR per department each year.",
    ),
    "mileage": dict(
        title="Travel policy: private car use",
        quantity="mileage rate",
        unit="cents per km",
        base=(28, 37, 1),
        exc=(40, 49, 1),
        add=(4, 9, 1),
        rule="Business kilometres driven with a private car are reimbursed at {v} cents per km.",
        base_q="a business trip in a private petrol car",
        group="electric vehicles",
        group_q="a business trip in a private electric car",
        sub="electric rental cars",
        sub_q="a business trip in an electric rental car",
        cond="trips towing a trailer",
        cond_q="a business trip in a private petrol car towing a trailer",
        distract="Company pool cars are charged to projects at an internal rate of {v} cents per km to cover fuel and maintenance.",
    ),
    "purchase": dict(
        title="Procurement policy: approvals",
        quantity="purchase approval threshold",
        unit="EUR",
        base=(2000, 4250, 250),
        exc=(8000, 12500, 500),
        add=(1000, 2250, 250),
        rule="Purchases up to {v} EUR may be approved by the budget owner alone; larger purchases need a second approval.",
        base_q="a purchase by a regular department",
        group="the facilities department",
        group_q="a purchase by the facilities department",
        sub="IT hardware bought by the facilities department",
        sub_q="IT hardware bought by the facilities department",
        cond="purchases from suppliers with a framework contract",
        cond_q="a regular department's purchase from a supplier with a framework contract",
        distract="Petty-cash purchases are limited to {v} EUR per receipt and need no approval.",
    ),
    "equipment": dict(
        title="Remote work: home-office equipment",
        quantity="home-office equipment allowance",
        unit="EUR",
        base=(300, 525, 25),
        exc=(600, 825, 25),
        add=(100, 225, 25),
        rule="Employees may claim a one-off home-office equipment allowance of {v} EUR.",
        base_q="a regular employee",
        group="employees with a medical accessibility plan",
        group_q="an employee with a medical accessibility plan",
        sub="part-time employees working fewer than 20 hours a week",
        sub_q="an employee with a medical accessibility plan who works 16 hours a week",
        cond="employees who work remotely four or more days a week",
        cond_q="a regular employee who works remotely four days a week",
        distract="Office desks are refurbished on a five-year cycle; each refurbishment is budgeted at {v} EUR per desk.",
    ),
    "phone": dict(
        title="Communication policy: mobile phones",
        quantity="monthly mobile allowance",
        unit="EUR per month",
        base=(25, 41, 1),
        exc=(50, 71, 1),
        add=(15, 31, 1),
        rule="Employees who use a private phone for work receive a mobile allowance of {v} EUR per month.",
        base_q="a regular employee using a private phone",
        group="field engineers",
        group_q="a field engineer using a private phone",
        sub="field engineers on a company-paid phone contract",
        sub_q="a field engineer on a company-paid phone contract",
        cond="employees on international rotation",
        cond_q="a regular employee using a private phone while on international rotation",
        distract="Conference-call services are billed centrally; each team's licence costs {v} EUR per month.",
    ),
    "relocation": dict(
        title="HR policy: relocation support",
        quantity="relocation lump sum",
        unit="EUR",
        base=(2500, 4250, 250),
        exc=(5000, 7250, 250),
        add=(500, 1250, 250),
        rule="Employees who relocate at the company's request receive a relocation lump sum of {v} EUR.",
        base_q="a regular employee relocating at the company's request",
        group="relocations to another country",
        group_q="an employee relocating to another country at the company's request",
        sub="relocations within the first six months of employment",
        sub_q="an employee relocating to another country in their third month of employment",
        cond="employees relocating with dependent children",
        cond_q="a regular employee relocating at the company's request with dependent children",
        distract="Temporary housing near a new office is arranged by facilities for up to {v} EUR per month during the first month.",
    ),
}
FILLER = [
    "Claims are submitted in the expense tool within 30 days of the date the cost was incurred.",
    "Receipts must be attached for every item claimed; photographs of paper receipts are accepted.",
    "Questions about this section go to the policy owner named in the document header.",
    "Line managers check each claim for plausibility before it is released for payment.",
    "This section is reviewed once a year by the finance team together with the policy owner.",
    "Amounts are stated in euros; conversions use the published rate on the day of payment.",
    "Any deviation from this section must be explained in the comment field of the claim.",
    "The policy owner may publish clarifications in the policy FAQ without changing this section.",
    "Repeated late or incomplete claims may be escalated to the head of the department.",
    "Supporting records are kept for seven years in line with the retention schedule.",
    "Where a local agreement sets stricter rules, the stricter rule applies for that location.",
    "Employees should keep costs reasonable and book through the approved channels where possible.",
    "Claims that combine several costs must list each cost on a separate line of the claim.",
    "Audit samples are drawn every quarter, and selected employees are asked for documentation.",
    "Costs paid with a company card are reconciled in the same tool as personal claims.",
    "Managers may reject claims that are not supported by the documentation described here.",
]
GENERIC = [
    ("Glossary of handbook terms", ["Budget owner", "Business travel", "Claim", "Policy owner"]),
    ("Code of conduct", ["Respect at work", "Conflicts of interest", "Gifts and hospitality"]),
    ("Information security basics", ["Passwords", "Laptops on the road", "Reporting incidents"]),
    ("Onboarding checklist", ["First week", "Accounts and access", "Mandatory trainings"]),
    ("Expense submission process", ["Submitting a claim", "Approval routing", "Payment dates"]),
    ("Public holidays and office closures", ["Public holidays", "Year-end closure", "Bridge days"]),
    ("Office access and visitors", ["Badges", "Visitor registration", "Overnight guests"]),
]


def _val(rng: random.Random, spec: tuple[int, int, int]) -> int:
    return rng.randrange(*spec)


def _text(rng: random.Random, core: str, n: int = 1) -> str:
    """A section: its core statement plus n + 2 general sentences (sections run to 3-6 lines)."""
    return " ".join([core, *rng.sample(FILLER, n + 2)])


@dataclass
class Passage:
    pid: str  # "HB-07 §2"
    doc: str
    title: str
    heading: str
    meta: str
    text: str


def _fmt_doc(doc: str, title: str, meta: str, sections: list[tuple[str, str]]) -> str:
    lines = [f"# {doc} · {title}", meta, ""]
    for i, (heading, text) in enumerate(sections, start=1):
        lines += [f"§{i} {heading}", text, ""]
    return "\n".join(lines)


def build_handbook(seed: int, n_questions: int = 6) -> tuple[dict[str, str], dict[str, Any]]:
    """(files, truth): 40 documents plus the question set with values and support sets."""
    rng = random.Random(seed)
    ids = [f"HB-{i:02d}" for i in range(1, 41)]
    rng.shuffle(ids)
    take = iter(ids)
    domains = list(DOMAINS)
    rng.shuffle(domains)
    asked = domains[:n_questions]
    types = [*QTYPES, rng.choice(["carveout", "additive"])]
    rng.shuffle(types)
    docs: dict[str, str] = {}
    facts: dict[str, dict[str, Any]] = {}
    faq_doc = next(take)
    faq_sections: list[tuple[str, str]] = []
    for d in domains:
        s = DOMAINS[d]
        v2, v1 = _val(rng, s["base"]), _val(rng, s["base"])
        while v1 == v2:
            v1 = _val(rng, s["base"])
        vx, add = _val(rng, s["exc"]), _val(rng, s["add"])
        e2 = f"2026-0{rng.randrange(2, 7)}-01"
        e1 = f"2025-0{rng.randrange(1, 10)}-01"
        p, old, memo, dis = next(take), next(take), next(take), next(take)
        title = s["title"]
        docs[p] = _fmt_doc(
            p,
            title,
            f"version: 3 · status: current · effective: {e2} · supersedes: {old}",
            [
                (
                    "Scope",
                    _text(rng, f"This policy sets the {s['quantity']} for all employees.", 1),
                ),
                ("Standard " + s["quantity"], _text(rng, s["rule"].format(v=v2))),
                (
                    "Additions",
                    _text(
                        rng,
                        f"For {s['cond']}, {add} {s['unit']} is added to the standard {s['quantity']}.",
                        1,
                    ),
                ),
                (
                    "Approval and receipts",
                    _text(rng, "Claims follow the expense submission process.", 2),
                ),
                (
                    "Exceptions",
                    _text(
                        rng, f"Group exceptions to the {s['quantity']} are published in {memo}.", 1
                    ),
                ),
            ],
        )
        docs[old] = _fmt_doc(
            old,
            title,
            f"version: 2 · status: superseded · effective: {e1} to the day before {e2} · superseded by: {p}",
            [
                (
                    "Scope",
                    _text(rng, f"This version set the {s['quantity']} for all employees.", 1),
                ),
                (
                    "Standard " + s["quantity"],
                    _text(rng, s["rule"].format(v=v1).replace(" is ", " was ", 1)),
                ),
                (
                    "Additions",
                    _text(
                        rng,
                        f"For {s['cond']}, {add} {s['unit']} was added to the standard {s['quantity']}.",
                        1,
                    ),
                ),
                (
                    "Version status",
                    f"This version applied from {e1} until the day before {e2}. From {e2} it is superseded by {p}; claims for earlier dates keep this version's amounts.",
                ),
            ],
        )
        docs[memo] = _fmt_doc(
            memo,
            f"Memo: {s['quantity']} exceptions",
            f"version: 1 · status: current · effective: {e1}",
            [
                (
                    "Purpose",
                    _text(
                        rng, f"This memo lists group exceptions to the {s['quantity']} in {p}.", 1
                    ),
                ),
                (
                    "Exception for " + s["group"],
                    _text(
                        rng,
                        f"For {s['group']}, the {s['quantity']} is {vx} {s['unit']} instead of the standard amount.",
                        1,
                    ),
                ),
                (
                    "Carve-out",
                    _text(
                        rng,
                        f"The exception above does not apply to {s['sub']}; they follow the standard {s['quantity']} in {p} §2.",
                        1,
                    ),
                ),
                ("Review", _text(rng, "This memo is reviewed together with the main policy.", 2)),
            ],
        )
        other = _val(rng, s["base"]) + _val(rng, s["add"])
        docs[dis] = _fmt_doc(
            dis,
            f"Related budgets: {s['title'].split(': ')[-1]}",
            f"version: 1 · status: current · effective: {e1}",
            [
                (
                    "Scope",
                    _text(
                        rng,
                        f"This document covers budgets related to, but separate from, the {s['quantity']}.",
                        1,
                    ),
                ),
                ("Central budget", _text(rng, s["distract"].format(v=other), 1)),
                (
                    "Contacts",
                    _text(rng, "The finance team answers questions about these budgets.", 2),
                ),
            ],
        )
        faq_sections.append(
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
            faq_sec=len(faq_sections),
        )
    docs[faq_doc] = _fmt_doc(
        faq_doc, "Policy FAQ", "version: 7 · status: current · effective: 2026-01-15", faq_sections
    )
    for title, heads in GENERIC:
        doc = next(take)
        docs[doc] = _fmt_doc(
            doc,
            title,
            "version: 1 · status: current · effective: 2025-01-01",
            [(h, _text(rng, f"{h}: general guidance for all employees.", 2)) for h in heads],
        )
    questions = []
    for i, (d, qt) in enumerate(zip(asked, types[:n_questions], strict=True), start=1):
        s, f = DOMAINS[d], facts[d]
        faq = f"{faq_doc} §{f['faq_sec']}"
        rule, rule_old = f"{f['p']} §2", f"{f['old']} §2"
        if qt == "current":
            text, value, sets = (
                f"What is the {s['quantity']} for {s['base_q']} today ({TODAY})?",
                f["v2"],
                [[rule], [faq]],
            )
        elif qt == "past":
            day = f"2026-0{int(f['e2'][6]) - 1}-15"  # e2 is 2026-02..06-01, e1 is in 2025
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
                [[f"{f['memo']} §3", rule], [f"{f['memo']} §3", faq]],
            )
        else:
            text, value, sets = (
                f"What is the {s['quantity']} for {s['cond_q']} today?",
                f["v2"] + f["add"],
                [[rule, f"{f['p']} §3"], [faq, f"{f['p']} §3"]],
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
    files = {f"handbook/{doc}.md": body for doc, body in sorted(docs.items())}
    return files, {"questions": questions, "faq_doc": faq_doc}


# ----------------------------------------------------------------------- the search tool
# The exact source written to /task/fixtures/handbook_tools.py. The host imports the same text
# for the offline opportunity check and the component diagnostic, so both use one algorithm.
TOOLS_SOURCE = r'''
"""Handbook tools (identical in every condition).

search(query, k=5): print up to k (at most 8) ranked passages as a structured result, and save
  the original under /task/workspace/search-results/ (path printed in the result header).
show(ids): print the named passages in full (never screened).
"""
import json, math, os, re

FIXTURES = os.environ.get("HANDBOOK_FIXTURES", "/task/fixtures/handbook")
SAVE_DIR = os.environ.get("HANDBOOK_SAVE_DIR", "/task/workspace/search-results")
MAX_K = 8
_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("a an and are as at be by for from has have in is it its of on or the this to was were with what which who how does do".split())


def _tok(text):
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def load_passages(root=None):
    out = []
    root = root or FIXTURES
    for name in sorted(os.listdir(root)):
        if not name.endswith(".md"):
            continue
        lines = open(os.path.join(root, name), encoding="utf-8").read().splitlines()
        doc = name[:-3]
        title = lines[0].split(" · ", 1)[1] if " · " in lines[0] else lines[0]
        meta = lines[1]
        sec = None
        for line in lines[2:]:
            m = re.match(r"^§(\d+) (.*)$", line)
            if m:
                sec = {"id": f"{doc} §{m.group(1)}", "doc": doc, "title": title, "heading": m.group(2), "meta": meta, "text": ""}
                out.append(sec)
            elif sec is not None and line.strip():
                sec["text"] = (sec["text"] + " " + line.strip()).strip()
    return out


def header(p):
    return f"=== {p['id']} | {p['title']} — {p['heading']} | {p['meta']}"


def rank(passages, query, k):
    docs = [_tok(p["title"] + " " + p["heading"] + " " + p["text"]) for p in passages]
    n = len(docs)
    avg = sum(len(d) for d in docs) / max(1, n)
    df = {}
    for d in docs:
        for w in set(d):
            df[w] = df.get(w, 0) + 1
    q = _tok(query)
    scored = []
    for i, d in enumerate(docs):
        s = 0.0
        for w in q:
            tf = d.count(w)
            if tf:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d) / avg))
        if s > 0:
            scored.append((-s, passages[i]["id"], i))
    scored.sort()
    return [passages[i] for _, _, i in scored[: max(1, min(int(k), MAX_K))]]


def _next_id():
    os.makedirs(SAVE_DIR, exist_ok=True)
    n = 1 + sum(1 for f in os.listdir(SAVE_DIR) if f.endswith(".json"))
    return f"SR-{n:03d}"


def search(query, k=5):
    hits = rank(load_passages(), str(query), k)
    rid = _next_id()
    path = os.path.join(SAVE_DIR, rid + ".txt")
    q = str(query).replace('"', "'")[:200]
    json.dump({"id": rid, "query": q, "k": int(k), "passages": hits}, open(os.path.join(SAVE_DIR, rid + ".json"), "w"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# saved search result {rid}: query \"{q}\", k={int(k)}, {len(hits)} passages (original, unscreened)\n")
        for p in hits:
            fh.write("--- " + header(p)[4:] + "\n" + p["text"] + "\n")
    print(f'<<search-result {rid} query="{q}" k={int(k)} returned={len(hits)} saved={path}>>')
    for p in hits:
        print(header(p))
        print(p["text"])
    print(f"<<end search-result {rid}>>")


def show(ids):
    want = [ids] if isinstance(ids, str) else list(ids)
    by_id = {p["id"]: p for p in load_passages()}
    norm = {re.sub(r"\s+", " ", i.replace("#", " §").replace("§ ", "§")).strip(): i for i in want}
    print("<<passages (unscreened): " + ", ".join(norm) + ">>")
    for i in norm:
        p = by_id.get(i)
        print("--- " + header(p)[4:] if p else f"--- {i}: no such passage")
        if p:
            print(p["text"])
    print("<<end passages>>")
'''


def tools_namespace() -> dict[str, Any]:
    ns: dict[str, Any] = {"__name__": "handbook_tools"}
    exec(compile(TOOLS_SOURCE, "handbook_tools", "exec"), ns)  # our own fixed source
    return ns


def passages_from_files(files: dict[str, str]) -> list[dict[str, Any]]:
    """The tool's passage parser applied to in-memory files (host side, same code)."""
    import tempfile
    from pathlib import Path

    ns = tools_namespace()
    with tempfile.TemporaryDirectory() as tmp:
        for rel, text in files.items():
            if rel.startswith("handbook/"):
                (Path(tmp) / rel.split("/", 1)[1]).write_text(text, encoding="utf-8")
        return list(ns["load_passages"](tmp))


# ----------------------------------------------------------------------- documents
def task_prompt(questions: list[dict[str, Any]], smoke: bool = False) -> str:
    qs = "\n".join(f"- {q['qid']}: {q['text']} (answer in {q['unit']})" for q in questions)
    text = f"""Question-answering task over a company handbook.

The handbook is in /task/fixtures/handbook/ (40 documents, HB-01.md to HB-40.md). Each document
has a header line (version, status, effective dates, what it supersedes) and numbered sections;
each section is a passage, cited as "HB-07 §2". Documents can have superseded versions, and
memos can add group exceptions and carve-outs. Use the rule that applies to the situation and
date in the question.

Tools (in your code):
  import sys; sys.path.insert(0, "/task/fixtures")
  from handbook_tools import search, show
  search("lodging limit Geneva", k=5)   # up to k (at most 8) ranked passages, with ids
  show(["HB-07 §2", "HB-12 §3"])        # the named passages in full
Every search result is also saved under /task/workspace/search-results/ (the path is printed in
the result header). You may also read the documents directly.

Questions:
{qs}

Finish with {{"action": "final", "answer": {{"answers": [{{"question": "Q1", "value": "...",
"citations": ["HB-07 §2", ...]}}, ...]}}}}. Give one entry per question. The value must be the
exact amount; citations must include every passage needed to support it (for example the
rule and any exception or carve-out that applies), not just one passage.
"""
    if smoke:
        text += (
            "\nIntegration smoke run (this instance only): start with one search with k=6 for "
            "the first question. If the result lists omitted passages, print one of them with "
            "show(). Then answer normally.\n"
        )
    return text


@dataclass
class HandbookTruth:
    questions: list[dict[str, Any]]
    faq_doc: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "handbook",
            "generator_version": GENERATOR_VERSION,
            "questions": self.questions,
            "faq_doc": self.faq_doc,
        }


def generate_handbook(spec: InstanceSpec) -> TaskInstance:
    smoke = spec.name.startswith("handbook-smoke")
    files, truth = build_handbook(spec.seed, 2 if smoke else 6)
    files["handbook_tools.py"] = TOOLS_SOURCE
    return TaskInstance(
        spec=spec,
        prompt=task_prompt(truth["questions"], smoke),
        files=files,
        truth=HandbookTruth(truth["questions"], truth["faq_doc"]),
        generator_version=GENERATOR_VERSION,
        kind="handbook",
        scorer_version=SCORER_VERSION,
    )


# ----------------------------------------------------------------------- scoring
HANDBOOK_FIELDS = ("answers",)
HANDBOOK_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "value": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question", "value", "citations"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}
CITE = re.compile(r"HB-?\s*(\d{1,2})\s*(?:§|#|s(?:ec(?:tion)?)?\.?)\s*(\d+)", re.I)
NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def norm_citations(cites: list[Any]) -> set[str]:
    out = set()
    for c in cites:
        for m in CITE.finditer(str(c)):
            out.add(f"HB-{int(m.group(1)):02d} §{int(m.group(2))}")
    return out


def value_number(value: Any) -> float | None:
    m = NUM.search(
        str(value).replace(",", "") if "," in str(value) and "." in str(value) else str(value)
    )
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


@dataclass
class HandbookScore:
    completed: bool
    checks: int
    passed: int
    by_category: dict[str, dict[str, int]]
    stale: int
    details: list[dict[str, Any]]
    strict_success: bool
    outcome: str
    unsupported_citations: int = 0
    import_error: None = None
    evaluation_error: None = None
    results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "components": self.components}

    @property
    def components(self) -> dict[str, float]:
        out = {"all_checks": self.passed / self.checks if self.checks else 0.0}
        for cat, v in self.by_category.items():
            out[cat] = v["passed"] / v["total"] if v["total"] else 0.0
        return out


def score_handbook(answer: dict[str, Any] | None, truth: HandbookTruth) -> HandbookScore:
    """Per question: value correct (numeric) and a complete valid support set cited. Stale: the
    superseded value given while the question is not about the past. Extra citations outside
    every support set are counted (unsupported_citations), not failed."""
    given: dict[str, dict[str, Any]] = {}
    if answer and isinstance(answer.get("answers"), list):
        for a in answer["answers"]:
            if isinstance(a, dict) and isinstance(a.get("question"), str):
                given.setdefault(a["question"].strip().upper(), a)
    by: dict[str, dict[str, int]] = {}
    details = []
    passed = stale = unsupported = 0
    for q in truth.questions:
        a = given.get(q["qid"])
        cat = q["type"]
        by.setdefault(cat, {"total": 0, "passed": 0, "stale": 0})["total"] += 1
        num = value_number(a.get("value")) if a else None
        cites = (
            norm_citations(a.get("citations", []))
            if a and isinstance(a.get("citations"), list)
            else set()
        )
        value_ok = num is not None and abs(num - q["value"]) < 1e-9
        sets = [set(s) for s in q["support_sets"]]
        evidence_ok = any(s <= cites for s in sets)
        allowed = set().union(*sets)
        extra = sorted(cites - allowed)
        unsupported += len(extra)
        is_stale = q["type"] != "past" and num is not None and abs(num - q["old_value"]) < 1e-9
        ok = value_ok and evidence_ok
        passed += ok
        by[cat]["passed"] += ok
        if is_stale:
            stale += 1
            by[cat]["stale"] += 1
        details.append(
            dict(
                qid=q["qid"],
                type=cat,
                value_ok=value_ok,
                evidence_ok=evidence_ok,
                stale=is_stale,
                given_value=a.get("value") if a else None,
                cited=sorted(cites),
                unsupported=extra,
                answered=a is not None,
            )
        )
    completed = answer is not None
    total = len(truth.questions)
    strict = completed and passed == total
    outcome = "correct" if strict else "no_submission" if not completed else "failed_checks"
    return HandbookScore(completed, total, passed, by, stale, details, strict, outcome, unsupported)
