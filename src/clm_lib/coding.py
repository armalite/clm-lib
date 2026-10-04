"""Coding task family: an invoice calculator whose requirements change over stages.

The agent implements a small, dependency-free package (``invoice``) in its workspace. Requirements
arrive in four stages, released by the ``advance`` action. Each stage adds rule modules and/or
replaces (supersedes) the version of an earlier module; modules that are never changed after they
are introduced remain in force. Later stage documents state only the changes, so the agent has
to keep track of what is still in force (earlier documents stay readable).

Visible material (fixture mount, read-only):
- ``stage-N/REQUIREMENTS.md``: released with stage N and never changed afterwards.
- ``current-tests/test_invoice.py``: a visible ``unittest`` suite for every rule currently in force.
  It is rewritten at each release, so tests of superseded rules are updated rather than left
  contradicting the new requirements.

Evaluator-only material (host memory until scoring; never mounted during the run):
- data-driven checks with expected results computed by a host-side reference implementation,
  covering only disclosed requirements, on inputs different from the visible tests. For
  replaced rules a "stale" expectation (the old rule's result) is also recorded.

Scoring runs the submitted workspace copy in a fresh sandbox container (see ``EVAL_RUNNER``).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any

from .tasks import InstanceSpec, TaskInstance

CODING_GENERATOR_VERSION = "invoice-gen/1"
CODING_SCORER_VERSION = "invoice-checks/1"
N_STAGES = 4
MODULES = ("ROUND", "TAX", "TIER", "BULK", "EXEMPT", "VALIDATE", "FORMAT")

# Rule text, per module and version.
RULES: dict[str, dict[int, str]] = {
    "ROUND": {
        1: (
            "Rounding: round each line amount to 2 decimals (half up) before summing; compute "
            "discount from the rounded subtotal and round it to 2 decimals (half up); compute tax "
            "from (subtotal - discount) and round it to 2 decimals (half up); "
            "total = subtotal - discount + tax."
        ),
        2: (
            "Rounding (replaces the earlier rounding rule): do NOT round line amounts. Compute the "
            "subtotal, discount and tax exactly (discount from the exact subtotal; tax from the exact "
            "subtotal minus the exact discount), then round each of subtotal, discount and tax to 2 "
            "decimals using banker's rounding (half even); total = rounded subtotal - rounded "
            "discount + rounded tax."
        ),
    },
    "TAX": {
        1: "Tax: the tax rate is 10% for every customer.",
        2: (
            "Tax (replaces the flat 10% rate): the tax rate depends on customer['region']: "
            "'NZ' 15%, 'AU' 10%, 'US' 0%, any other region 12%."
        ),
    },
    "TIER": {
        1: (
            "Tier discount: discount rate by customer['tier']: 'gold' 5%, 'silver' 2%, any other "
            "tier 0%."
        ),
        2: (
            "Tier discount (replaces the earlier tier rates): 'platinum' 10%, 'gold' 7%, "
            "'silver' 3%, any other tier 0%."
        ),
    },
    "BULK": {
        1: (
            "Bulk lines: a line with qty >= 100 has its line amount multiplied by 0.90 (before any "
            "line rounding)."
        ),
        2: (
            "Bulk lines (replaces the earlier bulk rule): qty >= 200 multiplies the line amount by "
            "0.88; otherwise qty >= 50 multiplies it by 0.95; smaller lines are unchanged "
            "(before any line rounding)."
        ),
    },
    "EXEMPT": {
        1: "Tax exemption: if customer.get('tax_exempt') is True, tax is 0.00 whatever the rate.",
    },
    "VALIDATE": {
        1: (
            "Validation: compute_invoice raises ValueError if lines is empty, if any qty is not a "
            "positive integer, or if any unit_price is negative."
        ),
        2: (
            "Validation (replaces the earlier qty rule): qty 0 is now allowed and such a line is "
            "skipped entirely; a negative qty still raises ValueError; an empty lines list or a "
            "negative unit_price still raises ValueError."
        ),
    },
    "FORMAT": {
        1: (
            "Money formatting: add format_money(amount: str, currency: str) -> str. Prefix 'NZD' "
            "with 'NZ$', 'AUD' with 'A$', 'USD' with 'US$'; always 2 decimals; a negative amount "
            "puts '-' before the prefix (for example '-NZ$5.00'); any other currency raises "
            "ValueError."
        ),
        2: (
            "Money formatting (replaces the earlier format): format_money also uses comma "
            "thousands separators, for example format_money('1234.5', 'NZD') == 'NZ$1,234.50'. "
            "Prefixes, the minus sign position and unknown-currency errors are unchanged."
        ),
    },
}

# Stage schedules: stage -> list of (module, version). Version 2 replaces version 1.
SCHEDULES: dict[str, list[list[tuple[str, int]]]] = {
    "coding-dev-1": [
        [("ROUND", 1), ("TAX", 1), ("VALIDATE", 1)],
        [("TIER", 1), ("TAX", 2)],
        [("BULK", 1), ("ROUND", 2)],
        [("FORMAT", 1), ("TIER", 2)],
    ],
    "coding-dev-2": [
        [("ROUND", 1), ("TIER", 1), ("FORMAT", 1)],
        [("TAX", 1), ("FORMAT", 2)],
        [("VALIDATE", 1), ("EXEMPT", 1), ("TIER", 2)],
        [("BULK", 1), ("TAX", 2)],
    ],
    "coding-eval-1": [
        [("ROUND", 1), ("TAX", 1), ("BULK", 1)],
        [("VALIDATE", 1), ("BULK", 2)],
        [("TIER", 1), ("ROUND", 2)],
        [("EXEMPT", 1), ("FORMAT", 1), ("TAX", 2)],
    ],
    "coding-eval-2": [
        [("ROUND", 1), ("TIER", 1), ("VALIDATE", 1)],
        [("TAX", 1), ("FORMAT", 1)],
        [("BULK", 1), ("TIER", 2), ("VALIDATE", 2)],
        [("EXEMPT", 1), ("FORMAT", 2)],
    ],
    "coding-eval-3": [
        [("ROUND", 1), ("TAX", 1), ("FORMAT", 1)],
        [("TIER", 1), ("EXEMPT", 1), ("ROUND", 2)],
        [("BULK", 1), ("FORMAT", 2)],
        [("VALIDATE", 1), ("TAX", 2), ("BULK", 2)],
    ],
    "coding-eval-4": [
        [("ROUND", 1), ("BULK", 1), ("TIER", 1)],
        [("TAX", 1), ("VALIDATE", 1), ("TIER", 2)],
        [("FORMAT", 1), ("BULK", 2)],
        [("EXEMPT", 1), ("ROUND", 2), ("VALIDATE", 2)],
    ],
}

Config = dict[str, int]


def config_at(schedule: list[list[tuple[str, int]]], stage: int) -> Config:
    cfg: Config = {}
    for changes in schedule[:stage]:
        for module, version in changes:
            cfg[module] = version
    return cfg


# ------------------------------------------------------------- reference model
Q2 = Decimal("0.01")
TIER_RATES = {
    1: {"gold": "0.05", "silver": "0.02"},
    2: {"platinum": "0.10", "gold": "0.07", "silver": "0.03"},
}
REGION_RATES = {"NZ": "0.15", "AU": "0.10", "US": "0"}
PREFIX = {"NZD": "NZ$", "AUD": "A$", "USD": "US$"}


def ref_compute(
    lines: list[dict[str, Any]], customer: dict[str, Any], cfg: Config
) -> dict[str, str]:
    v = cfg.get("VALIDATE", 0)
    if v and not lines:
        raise ValueError("empty")
    rnd = cfg.get("ROUND", 1)
    sub = Decimal(0)
    for ln in lines:
        qty, price = ln["qty"], Decimal(ln["unit_price"])
        if v == 1 and (not isinstance(qty, int) or qty <= 0):
            raise ValueError("qty")
        if v == 2 and qty < 0:
            raise ValueError("qty")
        if v and price < 0:
            raise ValueError("price")
        if v == 2 and qty == 0:
            continue
        amount = qty * price
        bulk = cfg.get("BULK", 0)
        if bulk == 1 and qty >= 100:
            amount *= Decimal("0.90")
        elif bulk == 2:
            if qty >= 200:
                amount *= Decimal("0.88")
            elif qty >= 50:
                amount *= Decimal("0.95")
        if rnd == 1:
            amount = amount.quantize(Q2, ROUND_HALF_UP)
        sub += amount
    tier = cfg.get("TIER", 0)
    d_rate = Decimal(TIER_RATES[tier].get(customer.get("tier", ""), "0")) if tier else Decimal(0)
    tax_v = cfg.get("TAX", 0)
    if tax_v == 1:
        t_rate = Decimal("0.10")
    elif tax_v == 2:
        t_rate = Decimal(REGION_RATES.get(customer.get("region", ""), "0.12"))
    else:
        t_rate = Decimal(0)
    if cfg.get("EXEMPT") and customer.get("tax_exempt") is True:
        t_rate = Decimal(0)
    if rnd == 1:
        discount = (sub * d_rate).quantize(Q2, ROUND_HALF_UP)
        tax = ((sub - discount) * t_rate).quantize(Q2, ROUND_HALF_UP)
        subtotal = sub.quantize(Q2, ROUND_HALF_UP)
    else:
        d_exact = sub * d_rate
        t_exact = (sub - d_exact) * t_rate
        subtotal = sub.quantize(Q2, ROUND_HALF_EVEN)
        discount = d_exact.quantize(Q2, ROUND_HALF_EVEN)
        tax = t_exact.quantize(Q2, ROUND_HALF_EVEN)
    total = subtotal - discount + tax
    return {
        "subtotal": str(subtotal),
        "discount": str(discount),
        "tax": str(tax),
        "total": str(total),
    }


def ref_format(amount: str, currency: str, cfg: Config) -> str:
    if currency not in PREFIX:
        raise ValueError("currency")
    value = Decimal(amount).quantize(Q2, ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    body = f"{abs(value):,.2f}" if cfg.get("FORMAT") == 2 else f"{abs(value):.2f}"
    return f"{sign}{PREFIX[currency]}{body}"


def ref_call(fn: str, args: list[Any], cfg: Config) -> dict[str, Any]:
    try:
        if fn == "compute_invoice":
            return {"value": ref_compute(args[0], args[1], cfg)}
        return {"value": ref_format(args[0], args[1], cfg)}
    except ValueError:
        return {"raises": "ValueError"}


# ------------------------------------------------------------- case generation
def _line(rng: random.Random, qty: int | None = None, price: str | None = None) -> dict[str, Any]:
    return {
        "sku": f"SKU-{rng.randrange(100, 999)}",
        "qty": qty if qty is not None else rng.randrange(1, 9),
        "unit_price": price
        if price is not None
        else f"{rng.randrange(1, 90)}.{rng.randrange(0, 99):02d}",
    }


def _customer(tier: str = "standard", region: str = "US", exempt: bool = False) -> dict[str, Any]:
    c: dict[str, Any] = {"tier": tier, "region": region}
    if exempt:
        c["tax_exempt"] = True
    return c


def module_cases(module: str, rng: random.Random, n: int) -> list[tuple[str, list[Any]]]:
    """Inputs that exercise ``module`` (other rules are kept as neutral as possible)."""
    out: list[tuple[str, list[Any]]] = []
    for _ in range(n):
        if module == "ROUND":
            lines = [
                _line(
                    rng,
                    rng.randrange(1, 9),
                    f"{rng.randrange(1, 30)}.{rng.randrange(0, 99):02d}{rng.choice([5, 3, 7])}",
                )
                for _ in range(3)
            ]
            out.append(
                ("compute_invoice", [lines, _customer("standard", rng.choice(["NZ", "AU"]))])
            )
        elif module == "TAX":
            lines = [_line(rng) for _ in range(2)]
            out.append(
                (
                    "compute_invoice",
                    [lines, _customer("standard", rng.choice(["NZ", "AU", "US", "JP"]))],
                )
            )
        elif module == "TIER":
            lines = [_line(rng) for _ in range(2)]
            out.append(
                (
                    "compute_invoice",
                    [lines, _customer(rng.choice(["gold", "silver", "platinum"]), "US")],
                )
            )
        elif module == "BULK":
            lines = [
                _line(
                    rng,
                    rng.choice([60, 120, 250]),
                    f"{rng.randrange(1, 9)}.{rng.randrange(0, 99):02d}",
                ),
                _line(rng),
            ]
            out.append(("compute_invoice", [lines, _customer("standard", "US")]))
        elif module == "EXEMPT":
            lines = [_line(rng) for _ in range(2)]
            out.append(
                (
                    "compute_invoice",
                    [lines, _customer("standard", rng.choice(["NZ", "AU"]), exempt=True)],
                )
            )
        elif module == "VALIDATE":
            kind = rng.choice(["empty", "zero", "negqty", "negprice"])
            if kind == "empty":
                bad: list[dict[str, Any]] = []
            elif kind == "zero":
                bad = [_line(rng, 0), _line(rng)]
            elif kind == "negqty":
                bad = [_line(rng, -rng.randrange(1, 5)), _line(rng)]
            else:
                bad = [_line(rng, price=f"-{rng.randrange(1, 20)}.00"), _line(rng)]
            out.append(("compute_invoice", [bad, _customer("standard", "US")]))
        elif module == "FORMAT":
            amount = rng.choice(
                [
                    f"{rng.randrange(1000, 99999)}.{rng.randrange(0, 99):02d}",
                    f"-{rng.randrange(1000, 9999)}.5",
                    f"{rng.randrange(1, 999)}.{rng.randrange(0, 9)}",
                ]
            )
            out.append(("format_money", [amount, rng.choice(["NZD", "AUD", "USD"])]))
    return out


def _distinct(
    module: str, rng: random.Random, cfg: Config, old: Config | None, n: int
) -> list[tuple[str, list[Any]]]:
    """Cases for a module; for replaced modules, only cases where old and new rules differ."""
    picked: list[tuple[str, list[Any]]] = []
    for _ in range(400):
        if len(picked) >= n:
            break
        (fn, args) = module_cases(module, rng, 1)[0]
        if old is not None and ref_call(fn, args, cfg) == ref_call(fn, args, old):
            continue
        picked.append((fn, args))
    return picked


@dataclass
class CodingTruth:
    schedule: list[list[tuple[str, int]]]
    final_config: Config
    checks: list[dict[str, Any]]
    categories: dict[str, str]  # module -> retained | replaced | final_stage
    notes: str = ""
    stale_causes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "coding",
            "schedule": [[list(x) for x in st] for st in self.schedule],
            "final_config": self.final_config,
            "categories": self.categories,
            "checks": self.checks,
            "notes": self.notes,
        }


def _categories(schedule: list[list[tuple[str, int]]]) -> dict[str, str]:
    introduced: dict[str, int] = {}
    replaced: set[str] = set()
    for stage, changes in enumerate(schedule, start=1):
        for module, version in changes:
            introduced.setdefault(module, stage)
            if version > 1:
                replaced.add(module)
    cats = {}
    for module, stage in introduced.items():
        if module in replaced:
            cats[module] = "replaced"
        elif stage == len(schedule):
            cats[module] = "final_stage"
        else:
            cats[module] = "retained"
    return cats


def _visible_tests(cfg: Config, rng: random.Random, stage: int) -> str:
    lines = [
        f'"""Visible tests for the requirements in force after stage {stage}.',
        "",
        "Run from /task/workspace:  python -m unittest discover -s /task/fixtures/current-tests",
        '"""',
        "",
        "import unittest",
        "",
        "import invoice",
        "",
        "",
        "class TestInvoice(unittest.TestCase):",
        "    maxDiff = None",
        "",
    ]
    n = 0
    for module in [m for m in MODULES if m in cfg]:
        for fn, args in _distinct(module, rng, cfg, None, 2):
            n += 1
            exp = ref_call(fn, args, cfg)
            call = f"invoice.{fn}(*{args!r})"
            lines.append(f"    def test_{module.lower()}_{n}(self):")
            if "raises" in exp:
                lines.append(f"        with self.assertRaises(ValueError):\n            {call}")
            else:
                lines.append(f"        self.assertEqual({call}, {exp['value']!r})")
            lines.append("")
    lines += ["", 'if __name__ == "__main__":', "    unittest.main()", ""]
    return "\n".join(lines)


API_DOC = """## Package and API

Implement the package `invoice` in /task/workspace/invoice/ (a starting skeleton is provided).
Standard library only. Use `decimal.Decimal` arithmetic.

- `invoice.compute_invoice(lines, customer) -> dict`
  - `lines`: list of `{"sku": str, "qty": int, "unit_price": str}`. unit_price is a decimal
    string such as "12.50"; it may have more than 2 decimal places (for example "3.125").
  - `customer`: `{"tier": str, "region": str}` and optionally `"tax_exempt": bool`.
  - Returns `{"subtotal": str, "discount": str, "tax": str, "total": str}`, each a string with
    exactly 2 decimals, for example "12.30".
- Base calculation (always in force): line amount = qty x unit_price; subtotal = sum of line
  amounts; discount = subtotal x discount rate; tax = (subtotal - discount) x tax rate;
  total = subtotal - discount + tax. With no discount rule in force the discount rate is 0; with
  no tax rule in force the tax rate is 0.
"""


def _stage_doc(stage: int, changes: list[tuple[str, int]]) -> str:
    head = [f"# Stage {stage} of {N_STAGES} requirements", ""]
    if stage == 1:
        head += [API_DOC, "## Rules introduced in stage 1", ""]
    else:
        head += [
            "Only the changes are listed. Every earlier requirement that is not replaced below "
            "remains in force.",
            "",
            "## Changes",
            "",
        ]
    for module, version in changes:
        tag = "NEW" if version == 1 else "CHANGED"
        head.append(f"- [{tag}] {RULES[module][version]}")
    head += [
        "",
        "Visible tests for all rules now in force: /task/fixtures/current-tests/test_invoice.py "
        "(this file is updated at each stage).",
        "",
    ]
    return "\n".join(head)


SEED_INIT = '''"""Invoice calculator package (implement the requirements in /task/fixtures/stage-*/)."""

from .core import compute_invoice, format_money

__all__ = ["compute_invoice", "format_money"]
'''
SEED_CORE = '''"""Implementation goes here."""

from decimal import Decimal  # noqa: F401


def compute_invoice(lines, customer):
    raise NotImplementedError


def format_money(amount, currency):
    raise NotImplementedError
'''


def coding_task_prompt() -> str:
    return f"""Software task with requirements released in {N_STAGES} stages over time.

Implement the Python package `invoice` in /task/workspace/invoice/ (a skeleton is provided; you
may add modules). Requirements are under /task/fixtures/stage-N/REQUIREMENTS.md; only stage 1 is
available now. Visible unittest tests for every rule currently in force are in
/task/fixtures/current-tests/ (updated at each stage). To run them from your code:
  import subprocess, sys
  subprocess.run([sys.executable, "-m", "unittest", "discover", "-s",
                  "/task/fixtures/current-tests"], cwd="/task/workspace")

Staged requirements:
- Reply {{"action": "advance"}} to release the next stage when you are ready. Its requirements
  appear under /task/fixtures/stage-N/ and are added to your working context; the visible tests
  are updated to match all rules then in force.
- Later stages list only changes. A rule marked CHANGED replaces the earlier version of that rule;
  every other earlier rule stays in force. Earlier requirement files stay readable.
- A final answer is accepted only after all {N_STAGES} stages are released.

Finish with {{"action": "final", "answer": {{"summary": "..."}}}} once the package implements every
rule in force at the end of the final stage. Your code in /task/workspace is evaluated afterwards
by separate tests of the final requirements, including rules that have stayed unchanged since
earlier stages.
"""


def generate_coding(spec: InstanceSpec) -> TaskInstance:
    rng = random.Random(spec.seed)
    schedule = SCHEDULES[spec.name]
    stages: list[dict[str, str]] = []
    updates: list[str] = []
    for k in range(1, N_STAGES + 1):
        cfg = config_at(schedule, k)
        doc = _stage_doc(k, schedule[k - 1])
        stages.append(
            {
                f"stage-{k}/REQUIREMENTS.md": doc,
                "current-tests/test_invoice.py": _visible_tests(cfg, rng, k),
            }
        )
        updates.append(doc if k > 1 else "")
    final = config_at(schedule, N_STAGES)
    cats = _categories(schedule)
    checks: list[dict[str, Any]] = []
    erng = random.Random(spec.seed * 7919 + 13)
    for module in [m for m in MODULES if m in final]:
        category = cats[module]
        old = {**final, module: 1} if category == "replaced" else None
        for i, (fn, args) in enumerate(_distinct(module, erng, final, old, 4), start=1):
            checks.append(
                {
                    "id": f"{module}-{category}-{i}",
                    "module": module,
                    "category": category,
                    "fn": fn,
                    "args": args,
                    "expect": ref_call(fn, args, final),
                    "stale": ref_call(fn, args, old) if old is not None else None,
                }
            )
    files: dict[str, str] = {}
    for st in stages:
        files.update(st)
    truth = CodingTruth(
        schedule=schedule,
        final_config=final,
        checks=checks,
        categories=cats,
        notes=f"final config {final}",
    )
    return TaskInstance(
        spec=spec,
        prompt=coding_task_prompt(),
        files=files,
        truth=truth,
        stages=stages,
        stage_updates=[u or stages[0]["stage-1/REQUIREMENTS.md"] for u in updates],
        generator_version=CODING_GENERATOR_VERSION,
        kind="coding",
        workspace_seed={"invoice/__init__.py": SEED_INIT, "invoice/core.py": SEED_CORE},
    )


# ------------------------------------------------------------- evaluation
EVAL_MARKER = "@@CLM-EVAL@@"

# Runs inside a fresh sandbox container with the submission copy at /task/workspace (rw copy)
# and the evaluator checks at /task/fixtures/checks.json (read-only). Each check has a 5 s alarm.
EVAL_RUNNER = r"""
import json, signal, sys
sys.path.insert(0, '/task/workspace')
checks = json.load(open('/task/fixtures/checks.json'))
def _alarm(*_):
    raise TimeoutError('check timed out')
signal.signal(signal.SIGALRM, _alarm)
results = []
try:
    import invoice
    import_error = None
except BaseException as e:
    invoice, import_error = None, repr(e)[:300]
def norm(v):
    if isinstance(v, dict):
        return {str(k): str(x) for k, x in v.items()}
    return str(v)
for c in checks:
    r = {'id': c['id']}
    if invoice is None:
        r.update(passed=False, stale=False, observed={'import_error': import_error})
        results.append(r)
        continue
    signal.alarm(5)
    try:
        obs = {'value': norm(getattr(invoice, c['fn'])(*c['args']))}
    except ValueError:
        obs = {'raises': 'ValueError'}
    except BaseException as e:
        obs = {'raises': type(e).__name__, 'detail': str(e)[:200]}
    finally:
        signal.alarm(0)
    r['observed'] = obs
    r['passed'] = obs == c['expect']
    r['stale'] = c.get('stale') is not None and obs == c['stale']
    results.append(r)
print('@@CLM-EVAL@@' + json.dumps({'import_error': import_error, 'results': results}))
"""


@dataclass
class CodingScore:
    completed: bool
    checks: int
    passed: int
    by_category: dict[str, dict[str, int]]
    stale: int
    import_error: str | None
    strict_success: bool
    outcome: str
    evaluation_error: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "components": self.components}

    @property
    def components(self) -> dict[str, float]:
        out = {"all_checks": self.passed / self.checks if self.checks else 0.0}
        for cat, v in self.by_category.items():
            out[cat] = v["passed"] / v["total"] if v["total"] else 0.0
        return out


def score_coding(
    truth: CodingTruth, eval_output: str | None, completed: bool, error: str | None = None
) -> CodingScore:
    by: dict[str, dict[str, int]] = {}
    for c in truth.checks:
        by.setdefault(c["category"], {"total": 0, "passed": 0, "stale": 0})["total"] += 1
    results: list[dict[str, Any]] = []
    import_error = None
    if eval_output is not None and EVAL_MARKER in eval_output:
        payload = json.loads(eval_output.rsplit(EVAL_MARKER, 1)[1].strip().splitlines()[0])
        results, import_error = payload["results"], payload["import_error"]
    elif error is None:
        error = "evaluator produced no result"
    cat_of = {c["id"]: c["category"] for c in truth.checks}
    passed = stale = 0
    for r in results:
        cat = cat_of[r["id"]]
        if r["passed"]:
            passed += 1
            by[cat]["passed"] += 1
        if r["stale"]:
            stale += 1
            by[cat]["stale"] += 1
    total = len(truth.checks)
    strict = completed and error is None and passed == total
    if error is not None:
        outcome = "evaluation_error"
    elif strict:
        outcome = "correct"
    elif not completed:
        outcome = "correct_unsubmitted" if passed == total else "no_submission"
    else:
        outcome = "failed_checks"
    return CodingScore(
        completed, total, passed, by, stale, import_error, strict, outcome, error, results
    )


def checks_json(truth: CodingTruth) -> str:
    return json.dumps(truth.checks)
