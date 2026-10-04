"""Coding task family 2 (experiment 005): an invoice calculator with interacting rules that change.

Generator ``invoice-gen/2``. ``invoice-gen/1`` (``coding.py``, experiment 004) is unchanged.

Compared with ``invoice-gen/1``:
- 6 stages (base workload) or 8 stages (the predefined harder variant, which appends two stages
  adding coupons, refunds and further changes to the same schedule);
- rules that feed each other (bulk lines are excluded from the tier discount, the shipping
  threshold depends on the discount, shipping may be taxed, coupons change the calculation order,
  refunds recompute both invoices with every rule);
- partial changes: a CHANGED rule replaces one part of an earlier rule and refers back to the
  stage that introduced it; the rest stays in force without being restated;
- longer gaps between introducing a rule and changing it.

Visible material (fixture mount, read-only): ``stage-N/REQUIREMENTS.md`` (immutable once released)
and ``current-tests/test_invoice.py`` (rewritten at each release for every rule then in force, so
it never demands superseded behaviour).

Evaluator-only material (host memory until scoring): checks computed by the reference
implementation (``invoice_ref2``) on inputs different from the visible tests, covering only
disclosed requirements, in the categories:
- ``retained``: a rule introduced before the final stage and never changed;
- ``replaced``: behaviour that a CHANGED rule altered (the superseded result is recorded, so output
  matching the old rule is reported as a stale rule rather than a general failure);
- ``kept_part``: the part of a partially changed rule that stayed in force;
- ``interaction``: inputs where several rules affect the result together;
- ``final_stage``: a rule introduced in the final stage.
Checks may also be flagged ``boundary`` (thresholds, rounding ties, caps). Return types are checked
exactly (a dict with exactly the documented keys and ``str`` values, or a ``str``).

Snapshot checks (also evaluator-only): when the agent releases stage k+1, the runtime copies the
workspace host-side; after the run each copy is tested against checks for the rules in force at
stage k. A final retained-rule failure is called a regression only if that rule's snapshot checks
had passed earlier.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

from . import invoice_ref2 as ref
from .tasks import InstanceSpec, TaskInstance

GENERATOR_VERSION = "invoice-gen/2"
SCORER_VERSION = "invoice-checks/2"
MODULES = ("ROUND", "VALIDATE", "BULK", "TIER", "STACK", "COUPON", "SHIP", "TAX", "REFUND")
CATEGORIES = ("retained", "replaced", "kept_part", "interaction", "final_stage")
# Deliberate partial-change mistakes (see invoice_ref2) that the kept_part checks must detect.
PARTIAL_FAULT = {
    "TIER": "partial:TIER",
    "TAX": "partial:TAX",
    "VALIDATE": "partial:VALIDATE",
    "COUPON": "partial:COUPON",
}
KEPT_PART_MODULES = ("TIER", "TAX", "VALIDATE", "COUPON", "BULK", "SHIP")

# Rule text per module and version; {s} is the stage that introduced the module.
RULES: dict[str, dict[int, str]] = {
    "ROUND": {
        1: (
            "Rounding: round each line amount to 2 decimals (half up) after any line "
            "adjustment; the subtotal is the sum of the rounded line amounts. Round every discount "
            "component and the tax to 2 decimals (half up) as soon as each is computed, and "
            "compute later values from the rounded ones."
        ),
        2: (
            "Rounding (replaces the whole rounding rule from stage {s}): do not round line "
            "amounts or any intermediate value; all calculations and comparisons use exact values. "
            "Only at the end, round subtotal, discount and tax each to 2 decimals using half-even "
            "(banker's) rounding; total = rounded subtotal - rounded discount + shipping + rounded "
            "tax."
        ),
    },
    "VALIDATE": {
        1: (
            "Validation: compute_invoice raises ValueError if lines is empty, if any qty is not an "
            "integer greater than 0, or if any unit_price is negative."
        ),
        2: (
            "Validation (partly replaces the stage-{s} rule): a line with qty 0 is now "
            "allowed and ignored entirely. If every line is ignored, raise ValueError as for an "
            "empty list. All other validation from stage {s} is unchanged."
        ),
    },
    "BULK": {
        1: (
            "Bulk lines: a line with qty >= 100 is a bulk line; its line amount is multiplied by "
            "0.90. This is a line adjustment, applied before any line rounding."
        ),
        2: (
            "Bulk lines (partly replaces the stage-{s} rule): a line is now a bulk line "
            "when qty >= 50. The 0.90 multiplier and everything else about bulk lines are "
            "unchanged."
        ),
    },
    "TIER": {
        1: (
            "Tier discount: a percentage discount by customer['tier']: 'gold' 5%, 'silver' 2%, "
            "any other tier 0%. The tier discount is the rate times the tier-eligible amount, "
            "which is the subtotal unless a rule says otherwise."
        ),
        2: (
            "Tier discount (partly replaces the stage-{s} rates): the 'gold' rate is now "
            "8%, and a new tier 'platinum' gets 10%. The 'silver' rate, the 0% for other tiers and "
            "the rest of the tier discount rule are unchanged."
        ),
    },
    "STACK": {
        1: (
            "Bulk and tier discounts do not stack: bulk lines are not tier-eligible. The "
            "tier-eligible amount is the sum of the line amounts of the lines that are not bulk "
            "lines."
        ),
    },
    "COUPON": {
        1: (
            "Coupons: customer may contain 'coupon', a decimal string such as '15.00' (a missing "
            "coupon means 0). The coupon is applied after the tier discount, and only up to the "
            "amount left: coupon applied = min(coupon, subtotal - tier discount). "
            "discount = tier discount + coupon applied."
        ),
        2: (
            "Coupons (partly replaces the stage-{s} rule): the coupon is now applied "
            "before the tier discount: coupon applied = min(coupon, subtotal); tier discount = "
            "tier rate x max(tier-eligible amount - coupon applied, 0); discount = coupon applied "
            "+ tier discount. The coupon field and the missing-coupon rule are unchanged."
        ),
    },
    "SHIP": {
        1: (
            "Shipping: shipping is 7.50 when subtotal - discount is below 100.00, otherwise 0.00. "
            "Shipping is not taxed and not discounted."
        ),
        2: (
            "Shipping (partly replaces the stage-{s} rule): shipping is now taxed, so the "
            "tax is computed on (subtotal - discount + shipping). The 7.50 fee and the 100.00 "
            "threshold are unchanged."
        ),
    },
    "TAX": {
        1: "Tax: the tax rate is 10% for every customer.",
        2: (
            "Tax (partly replaces the stage-{s} rule): customers with region 'NZ' are now "
            "taxed at 15% and region 'US' at 0%. Every other region keeps the stage-{s} rate."
        ),
    },
    "REFUND": {
        1: (
            "Refunds: add invoice.compute_refund(lines, customer, returns) -> str. returns maps a "
            "sku to the number of units returned. Compute the original invoice and an invoice for "
            "the kept quantities (qty minus returned; lines with nothing kept are left out) with "
            "every rule in force for compute_invoice, except that shipping is 0.00 in both. The "
            "refund is the original total minus the kept total, where the kept total is 0.00 if "
            "no line is kept; a negative difference gives a refund of 0.00. Return it as a string "
            "with exactly 2 decimals. Raise ValueError if a returned sku is not in lines or a "
            "returned quantity is not an integer between 1 and that line's qty."
        ),
    },
}

Schedule = list[list[tuple[str, int]]]
Config = dict[str, int]

# Base workload: 6 stages. The harder variant appends HARDER_EXTENSION[name] (stages 7 and 8).
# Each instance has its own order of introduction, set of changed rules and change gaps.
BASE_SCHEDULES: dict[str, Schedule] = {
    "dev-1": [
        [("ROUND", 1), ("VALIDATE", 1), ("TAX", 1)],
        [("TIER", 1), ("BULK", 1)],
        [("SHIP", 1)],
        [("STACK", 1), ("TAX", 2)],
        [("TIER", 2)],
        [("ROUND", 2), ("SHIP", 2)],
    ],
    "dev-2": [
        [("ROUND", 1), ("TIER", 1)],
        [("VALIDATE", 1), ("SHIP", 1)],
        [("BULK", 1), ("TAX", 1)],
        [("STACK", 1)],
        [("BULK", 2), ("VALIDATE", 2)],
        [("TAX", 2), ("TIER", 2)],
    ],
    "eval-1": [
        [("ROUND", 1), ("VALIDATE", 1), ("BULK", 1)],
        [("TAX", 1), ("SHIP", 1)],
        [("TIER", 1)],
        [("BULK", 2)],
        [("STACK", 1), ("SHIP", 2)],
        [("TAX", 2)],
    ],
    "eval-2": [
        [("ROUND", 1), ("TAX", 1), ("TIER", 1)],
        [("VALIDATE", 1)],
        [("BULK", 1), ("STACK", 1)],
        [("SHIP", 1), ("TIER", 2)],
        [("VALIDATE", 2)],
        [("ROUND", 2)],
    ],
    "eval-3": [
        [("ROUND", 1), ("VALIDATE", 1), ("TIER", 1), ("BULK", 1)],
        [("TAX", 1)],
        [("STACK", 1), ("TAX", 2)],
        [("SHIP", 1)],
        [("BULK", 2)],
        [("SHIP", 2), ("TIER", 2)],
    ],
    "eval-4": [
        [("ROUND", 1), ("TAX", 1)],
        [("BULK", 1), ("SHIP", 1)],
        [("VALIDATE", 1), ("TIER", 1)],
        [("TAX", 2), ("ROUND", 2)],
        [("STACK", 1)],
        [("BULK", 2), ("VALIDATE", 2)],
    ],
    "eval-5": [
        [("ROUND", 1), ("SHIP", 1), ("VALIDATE", 1)],
        [("TIER", 1), ("TAX", 1)],
        [("BULK", 1)],
        [("SHIP", 2)],
        [("TIER", 2), ("STACK", 1)],
        [("TAX", 2)],
    ],
    "eval-6": [
        [("ROUND", 1), ("TIER", 1), ("BULK", 1), ("TAX", 1)],
        [("STACK", 1)],
        [("VALIDATE", 1), ("SHIP", 1)],
        [("ROUND", 2)],
        [("TIER", 2)],
        [("VALIDATE", 2), ("BULK", 2)],
    ],
}
HARDER_EXTENSION: dict[str, Schedule] = {
    "dev-1": [[("COUPON", 1), ("BULK", 2)], [("REFUND", 1), ("COUPON", 2)]],
    "dev-2": [[("COUPON", 1), ("SHIP", 2)], [("REFUND", 1), ("ROUND", 2)]],
    "eval-1": [[("COUPON", 1), ("TIER", 2)], [("REFUND", 1), ("VALIDATE", 2)]],
    "eval-2": [[("COUPON", 1), ("TAX", 2)], [("REFUND", 1), ("COUPON", 2)]],
    "eval-3": [[("COUPON", 1), ("ROUND", 2)], [("REFUND", 1), ("VALIDATE", 2)]],
    "eval-4": [[("COUPON", 1), ("SHIP", 2)], [("REFUND", 1), ("TIER", 2)]],
    "eval-5": [[("COUPON", 1), ("BULK", 2)], [("REFUND", 1), ("COUPON", 2)]],
    "eval-6": [[("COUPON", 1), ("TAX", 2)], [("REFUND", 1), ("SHIP", 2)]],
}


def schedule_for(name: str) -> Schedule:
    """``coding2-<key>`` is the base workload; ``coding2h-<key>`` the harder variant."""
    prefix, key = name.split("-", 1)
    base = BASE_SCHEDULES[key]
    return base + HARDER_EXTENSION[key] if prefix == "coding2h" else base


def config_at(schedule: Schedule, stage: int) -> Config:
    cfg: Config = {}
    for changes in schedule[:stage]:
        for module, version in changes:
            cfg[module] = version
    return cfg


def introduced_at(schedule: Schedule) -> dict[str, int]:
    out: dict[str, int] = {}
    for stage, changes in enumerate(schedule, start=1):
        for module, _ in changes:
            out.setdefault(module, stage)
    return out


def categories(schedule: Schedule) -> dict[str, str]:
    final = config_at(schedule, len(schedule))
    intro = introduced_at(schedule)
    cats = {}
    for module, version in final.items():
        if version > 1:
            cats[module] = "replaced"
        elif intro[module] == len(schedule):
            cats[module] = "final_stage"
        else:
            cats[module] = "retained"
    return cats


# ------------------------------------------------------------- case generation
def _price(rng: random.Random, lo: int = 1, hi: int = 60) -> str:
    return f"{rng.randrange(lo, hi)}.{rng.randrange(0, 100):02d}"


def _lines(rng: random.Random, n: int, qtys: list[int] | None = None) -> list[dict[str, Any]]:
    skus = rng.sample(range(100, 1000), n)
    out = []
    for i in range(n):
        qty = qtys[i] if qtys and i < len(qtys) else rng.randrange(1, 9)
        price = _price(rng, 1, 4) if qty >= 40 else _price(rng)
        out.append({"sku": f"SKU-{skus[i]}", "qty": qty, "unit_price": price})
    return out


def _customer(rng: random.Random, cfg: Config, tier: str | None = None) -> dict[str, Any]:
    c: dict[str, Any] = {
        "tier": tier or rng.choice(["gold", "silver", "platinum", "standard"]),
        "region": rng.choice(["NZ", "AU", "US", "JP"]),
    }
    if cfg.get("COUPON") and rng.random() < 0.6:
        c["coupon"] = rng.choice(["5.00", "12.50", "20.00", "40.00", "300.00"])
    return c


Case = tuple[str, list[Any], bool]  # (fn, args, boundary)


# Input kinds per module. pick_cases cycles through them in order, so every module's checks
# include its boundary kinds (marked True) as well as typical inputs.
KINDS: dict[str, list[tuple[str, bool]]] = {
    "ROUND": [("tie", True), ("plain", False)],
    "VALIDATE": [
        ("zero_mixed", True),
        ("negqty", False),
        ("zero_only", True),
        ("empty", False),
        ("negprice", False),
    ],
    "BULK": [("at_threshold", True), ("above", False), ("below_threshold", True), ("far", False)],
    "TIER": [("plain", False)],
    "STACK": [("mixed", False)],
    "COUPON": [("cap", True), ("small", False)],
    "SHIP": [("edge", True), ("discount_cross", True), ("plain", False), ("discount_cross", True)],
    "TAX": [("plain", False)],
    "REFUND": [
        ("negative", True),
        ("ship_cross", True),
        ("cross", True),
        ("bad_sku", False),
        ("partial", False),
        ("full", True),
        ("too_many", False),
    ],
}


def module_case(module: str, rng: random.Random, cfg: Config, hint: int | None = None) -> Case:
    """One candidate input of kind ``hint`` (random if None) that exercises ``module``."""
    kinds = KINDS[module]
    kind, boundary = kinds[hint % len(kinds)] if hint is not None else rng.choice(kinds)
    thr = ref.BULK_THRESHOLD.get(cfg.get("BULK", 0), 100)
    if module == "ROUND":
        lines = _lines(rng, rng.randrange(2, 4))
        for ln in lines:
            last = "5" if kind == "tie" else str(rng.choice([3, 7]))
            ln["unit_price"] = f"{rng.randrange(1, 30)}.{rng.randrange(0, 100):02d}{last}"
            if kind == "tie":
                ln["qty"] = 1
        return "compute_invoice", [lines, _customer(rng, cfg)], boundary
    if module == "VALIDATE":
        lines = _lines(rng, 2)
        if kind == "empty":
            lines = []
        elif kind == "zero_only":
            lines = [dict(ln, qty=0) for ln in lines]
        elif kind == "zero_mixed":
            lines[0]["qty"] = 0
        elif kind == "negqty":
            lines[0]["qty"] = -rng.randrange(1, 5)
        else:
            lines[1]["unit_price"] = f"-{rng.randrange(1, 20)}.00"
        return "compute_invoice", [lines, _customer(rng, cfg)], boundary
    if module == "BULK":
        q = {
            "at_threshold": thr,
            "below_threshold": thr - 1,
            "above": thr + rng.randrange(1, 40),
            "far": rng.choice([49, 50, 99, 100, 120, 160]),
        }[kind]
        lines = _lines(rng, 2, [q])
        return "compute_invoice", [lines, _customer(rng, cfg)], q in (thr - 1, thr)
    if module == "TIER":
        lines = _lines(rng, rng.randrange(1, 4))
        return "compute_invoice", [lines, _customer(rng, cfg)], False
    if module == "STACK":
        lines = _lines(rng, 3, [rng.choice([thr, thr + 30]), rng.randrange(1, 9)])
        tier = rng.choice(["gold", "silver", "platinum"])
        return "compute_invoice", [lines, _customer(rng, cfg, tier)], False
    if module == "COUPON":
        lines = _lines(rng, rng.randrange(1, 3))
        c = _customer(rng, cfg, rng.choice(["gold", "silver", "platinum", "standard"]))
        c["coupon"] = rng.choice(["500.00", "900.00"] if kind == "cap" else ["5.00", "15.00"])
        return "compute_invoice", [lines, c], boundary
    if module == "SHIP":
        if kind == "edge":  # exactly at or one cent below the threshold, no discount
            price = rng.choice(["99.99", "100.00"])
            lines = [{"sku": f"SKU-{rng.randrange(100, 999)}", "qty": 1, "unit_price": price}]
            c = _customer(rng, cfg, "standard")
            c.pop("coupon", None)
            return "compute_invoice", [lines, c], True
        if kind == "discount_cross":  # subtotal above the threshold, below it after discount
            price = f"{rng.randrange(100, 112)}.{rng.randrange(0, 100):02d}"
            lines = [{"sku": f"SKU-{rng.randrange(100, 999)}", "qty": 1, "unit_price": price}]
            c = _customer(rng, cfg, rng.choice(["gold", "platinum", "silver"]))
            return "compute_invoice", [lines, c], True
        lines = _lines(rng, rng.randrange(1, 4))
        return "compute_invoice", [lines, _customer(rng, cfg)], False
    if module == "TAX":
        lines = _lines(rng, rng.randrange(1, 4))
        return "compute_invoice", [lines, _customer(rng, cfg)], False
    if module == "REFUND":
        lines = _lines(rng, rng.randrange(2, 4), [rng.choice([thr, thr + 5, 3]), 2])
        if kind == "bad_sku":
            returns = {"SKU-000": 1}
        elif kind == "too_many":
            returns = {lines[1]["sku"]: lines[1]["qty"] + 1}
        elif kind == "full":
            returns = {ln["sku"]: ln["qty"] for ln in lines}
        elif kind == "ship_cross":  # the original ships free, the kept order alone would not
            lines = _lines(rng, 2, [3, 1])
            lines[0]["unit_price"] = f"{rng.randrange(35, 39)}.{rng.randrange(0, 100):02d}"
            lines[1]["unit_price"] = f"{rng.randrange(1, 6)}.{rng.randrange(0, 100):02d}"
            returns = {lines[0]["sku"]: 1}
        elif kind == "negative":  # one unit back from a line exactly at the bulk threshold
            lines[0]["qty"] = thr
            returns = {lines[0]["sku"]: 1}
        elif kind == "cross":
            lines[0]["qty"] = thr + rng.randrange(0, 6)
            returns = {lines[0]["sku"]: lines[0]["qty"] - thr + 1 + rng.randrange(0, 3)}
        else:
            ln = rng.choice(lines)
            returns = {ln["sku"]: rng.randrange(1, ln["qty"] + 1)}
        customer = _customer(rng, cfg)
        if kind == "ship_cross":  # no discount, so the original stays above the threshold
            customer["tier"] = "standard"
            customer.pop("coupon", None)
        if kind == "negative":  # without a large tier rate, the kept non-bulk line costs more
            customer["tier"] = rng.choice(["standard", "silver"])
            customer.pop("coupon", None)
        return "compute_refund", [lines, customer, returns], boundary
    raise KeyError(module)


def interaction_case(rng: random.Random, cfg: Config) -> Case:
    thr = ref.BULK_THRESHOLD.get(cfg.get("BULK", 0), 100)
    lines = _lines(
        rng, rng.randrange(2, 5), [rng.choice([thr, thr + 20, 60, 120]), rng.randrange(1, 9)]
    )
    c = _customer(rng, cfg)
    if cfg.get("REFUND") and rng.random() < 0.5:
        ln = rng.choice(lines)
        return "compute_refund", [lines, c, {ln["sku"]: rng.randrange(1, ln["qty"] + 1)}], False
    return "compute_invoice", [lines, c], False


def _without(cfg: Config, module: str) -> Config:
    return {k: v for k, v in cfg.items() if k != module}


def _with_old(cfg: Config, module: str) -> Config:
    return {**cfg, module: cfg[module] - 1}


def _call(fn: str, args: list[Any], cfg: Config, faults: frozenset[str] = frozenset()) -> Any:
    return ref.ref_call(fn, args, cfg, faults)


def _purpose_ok(purpose: str, module: str, fn: str, args: list[Any], cfg: Config) -> bool:
    exp = _call(fn, args, cfg)
    if module == "REFUND" or fn == "compute_refund":
        if purpose == "interaction":
            n = sum(_call(fn, args, _without(cfg, m)) != exp for m in cfg if m != "REFUND")
            return n >= 2
        return True  # without REFUND the function does not exist
    if purpose == "meaningful":
        if module == "ROUND" and cfg["ROUND"] == 1:  # no rounding rule still means 2 decimals
            return exp != _call(fn, args, cfg, frozenset({"order:ROUND_LATE"}))
        return exp != _call(fn, args, _without(cfg, module))
    if purpose == "changed":
        return exp != _call(fn, args, _with_old(cfg, module))
    if purpose == "kept":
        if exp != _call(fn, args, _with_old(cfg, module)):
            return False
        if module in PARTIAL_FAULT:
            return exp != _call(fn, args, cfg, frozenset({PARTIAL_FAULT[module]}))
        return exp != _call(fn, args, _without(cfg, module))
    if purpose == "interaction":
        return sum(_call(fn, args, _without(cfg, m)) != exp for m in cfg) >= 3
    raise KeyError(purpose)


def pick_cases(
    module: str,
    purpose: str,
    rng: random.Random,
    cfg: Config,
    n: int,
    avoid: set[str],
    cycle_kinds: bool = True,
) -> list[Case]:
    """Select ``n`` distinct qualifying inputs, cycling through the module's input kinds.

    A kind that cannot qualify for ``purpose`` under ``cfg`` is given up after 150 attempts and
    that position is filled with a random kind.
    """
    picked: list[Case] = []
    tries = 0
    for _ in range(4000):
        if len(picked) >= n:
            break
        tries += 1
        hint = len(picked) if cycle_kinds and tries <= 150 else None
        fn, args, boundary = (
            interaction_case(rng, cfg)
            if purpose == "interaction"
            else module_case(module, rng, cfg, hint)
        )
        key = repr(args)
        if key in avoid or not _purpose_ok(purpose, module, fn, args, cfg):
            continue
        avoid.add(key)
        picked.append((fn, args, boundary))
        tries = 0
    if len(picked) < n:
        raise RuntimeError(f"could not generate {n} {purpose} cases for {module} under {cfg}")
    return picked


def stale_variants(fn: str, args: list[Any], cfg: Config, expect: Any) -> dict[str, Any]:
    """Old-version results of every changed module whose old rule gives a different result."""
    out = {}
    for module, version in sorted(cfg.items()):
        if version > 1:
            old = _call(fn, args, _with_old(cfg, module))
            if old != expect:
                out[module] = old
    return out


# ------------------------------------------------------------- documents and tests
API_DOC = """## Package and API

Implement the package `invoice` in /task/workspace/invoice/ (a starting skeleton is provided).
Standard library only. Use `decimal.Decimal` arithmetic, never float.

- `invoice.compute_invoice(lines, customer) -> dict`
  - `lines`: list of `{"sku": str, "qty": int, "unit_price": str}`. unit_price is a decimal
    string such as "12.50"; it may have more than 2 decimal places (for example "3.125").
    skus are unique within a list.
  - `customer`: a dict with `"tier": str` and `"region": str`; later stages may add optional
    fields.
  - Returns a dict with exactly the keys "subtotal", "discount", "shipping", "tax" and "total".
    Each value is a str with exactly 2 decimals, for example "12.30" or "0.00".
- Base calculation (in force unless a rule changes it): line amount = qty x unit_price;
  subtotal = sum of line amounts; discount = sum of the discounts in force (0 if none);
  shipping = 0 unless a shipping rule is in force; tax = (subtotal - discount) x tax rate (rate 0
  if no tax rule is in force); total = subtotal - discount + shipping + tax.
"""


def stage_doc(schedule: Schedule, stage: int) -> str:
    n = len(schedule)
    intro = introduced_at(schedule)
    head = [f"# Stage {stage} of {n} requirements", ""]
    if stage == 1:
        head += [API_DOC, "## Rules introduced in stage 1", ""]
    else:
        head += [
            "Only the changes are listed. A CHANGED rule says which part of an earlier rule it "
            "replaces; everything else from earlier stages stays in force.",
            "",
            "## Changes",
            "",
        ]
    for module, version in schedule[stage - 1]:
        tag = "NEW" if version == 1 else "CHANGED"
        head.append(f"- [{tag}] {RULES[module][version].format(s=intro[module])}")
    head += [
        "",
        "Visible tests for all rules now in force: /task/fixtures/current-tests/test_invoice.py "
        "(this file is updated at each stage).",
        "",
    ]
    return "\n".join(head)


def visible_tests(cfg: Config, rng: random.Random, stage: int, avoid: set[str]) -> str:
    out = [
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
        cases = pick_cases(module, "meaningful", rng, cfg, 1, avoid, cycle_kinds=False)
        if cfg[module] > 1:
            cases += pick_cases(module, "changed", rng, cfg, 1, avoid, cycle_kinds=False)
        else:
            cases += pick_cases(module, "meaningful", rng, cfg, 1, avoid, cycle_kinds=False)
        for fn, args, _ in cases:
            n += 1
            exp = _call(fn, args, cfg)
            call = f"invoice.{fn}(*{args!r})"
            out.append(f"    def test_{module.lower()}_{n}(self):")
            if "raises" in exp:
                out.append(f"        with self.assertRaises(ValueError):\n            {call}")
            else:
                out.append(f"        self.assertEqual({call}, {exp['value']!r})")
            out.append("")
    out += ["", 'if __name__ == "__main__":', "    unittest.main()", ""]
    return "\n".join(out)


SEED_INIT = '''"""Invoice calculator package (implement the requirements in /task/fixtures/stage-*/)."""

from .core import compute_invoice

__all__ = ["compute_invoice"]
'''
SEED_CORE = '''"""Implementation goes here."""

from decimal import Decimal  # noqa: F401


def compute_invoice(lines, customer):
    raise NotImplementedError
'''


def task_prompt(n_stages: int) -> str:
    return f"""Software task with requirements released in {n_stages} stages over time.

Implement the Python package `invoice` in /task/workspace/invoice/ (a skeleton is provided; you
may add modules and functions). Requirements are under /task/fixtures/stage-N/REQUIREMENTS.md;
only stage 1 is available now. Visible unittest tests for every rule currently in force are in
/task/fixtures/current-tests/ (updated at each stage). To run them from your code:
  import subprocess, sys
  subprocess.run([sys.executable, "-m", "unittest", "discover", "-s",
                  "/task/fixtures/current-tests"], cwd="/task/workspace")

Staged requirements:
- Reply {{"action": "advance"}} to release the next stage when you are ready. Its requirements
  appear under /task/fixtures/stage-N/ and are added to your working context; the visible tests
  are updated to match all rules then in force.
- Later stages list only changes. A rule marked CHANGED says which part of an earlier rule it
  replaces; every other earlier rule, and every part of a rule that is not replaced, stays in
  force. Earlier requirement files stay readable.
- A final answer is accepted only after all {n_stages} stages are released.

Finish with {{"action": "final", "answer": {{"summary": "..."}}}} once the package implements every
rule in force at the end of the final stage. Your code in /task/workspace is evaluated afterwards
by separate tests of the final requirements, including rules that have stayed unchanged since
earlier stages and combinations of rules.
"""


# ------------------------------------------------------------- truth and generation
@dataclass
class CodingTruth2:
    schedule: Schedule
    final_config: Config
    checks: list[dict[str, Any]]
    categories: dict[str, str]  # module -> retained | replaced | final_stage
    snapshot_checks: dict[int, list[dict[str, Any]]]  # stage -> checks for config_at(stage)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "coding",
            "generator_version": GENERATOR_VERSION,
            "schedule": [[list(x) for x in st] for st in self.schedule],
            "final_config": self.final_config,
            "categories": self.categories,
            "checks": self.checks,
            "snapshot_checks": {str(k): v for k, v in self.snapshot_checks.items()},
            "notes": self.notes,
        }


def _check(cid: str, module: str, category: str, case: Case, cfg: Config) -> dict[str, Any]:
    fn, args, boundary = case
    expect = _call(fn, args, cfg)
    return {
        "id": cid,
        "module": module,
        "category": category,
        "boundary": boundary,
        "fn": fn,
        "args": args,
        "expect": expect,
        "stale": stale_variants(fn, args, cfg, expect),
    }


def build_checks(schedule: Schedule, seed: int, avoid: set[str]) -> list[dict[str, Any]]:
    final = config_at(schedule, len(schedule))
    cats = categories(schedule)
    rng = random.Random(seed * 7919 + 13)
    checks: list[dict[str, Any]] = []
    for module in [m for m in MODULES if m in final]:
        cat = cats[module]
        purpose = "changed" if cat == "replaced" else "meaningful"
        for i, case in enumerate(pick_cases(module, purpose, rng, final, 4, avoid), start=1):
            checks.append(_check(f"{module}-{cat}-{i}", module, cat, case, final))
        if cat == "replaced" and module in KEPT_PART_MODULES:
            for i, case in enumerate(pick_cases(module, "kept", rng, final, 2, avoid), start=1):
                checks.append(_check(f"{module}-kept_part-{i}", module, "kept_part", case, final))
    for i, case in enumerate(
        pick_cases("*", "interaction", rng, final, 6, avoid, cycle_kinds=False), start=1
    ):
        checks.append(_check(f"interaction-{i}", "*", "interaction", case, final))
    return checks


def build_snapshot_checks(
    schedule: Schedule, seed: int, avoid: set[str]
) -> dict[int, list[dict[str, Any]]]:
    rng = random.Random(seed * 104729 + 7)
    out: dict[int, list[dict[str, Any]]] = {}
    for stage in range(1, len(schedule)):
        cfg = config_at(schedule, stage)
        checks = []
        for module in [m for m in MODULES if m in cfg]:
            cases = pick_cases(module, "meaningful", rng, cfg, 2, avoid, cycle_kinds=False)
            for i, case in enumerate(cases, start=1):
                c = _check(f"s{stage}-{module}-{i}", module, "snapshot", case, cfg)
                c["version"] = cfg[module]
                checks.append(c)
        out[stage] = checks
    return out


def generate_coding2(spec: InstanceSpec) -> TaskInstance:
    schedule = schedule_for(spec.name)
    n = len(schedule)
    rng = random.Random(spec.seed)
    avoid: set[str] = set()
    stages: list[dict[str, str]] = []
    for k in range(1, n + 1):
        stages.append(
            {
                f"stage-{k}/REQUIREMENTS.md": stage_doc(schedule, k),
                "current-tests/test_invoice.py": visible_tests(
                    config_at(schedule, k), rng, k, avoid
                ),
            }
        )
    final = config_at(schedule, n)
    truth = CodingTruth2(
        schedule=schedule,
        final_config=final,
        checks=build_checks(schedule, spec.seed, avoid),
        categories=categories(schedule),
        snapshot_checks=build_snapshot_checks(schedule, spec.seed, avoid),
        notes=f"final config {final}",
    )
    files: dict[str, str] = {}
    for st in stages:
        files.update(st)
    return TaskInstance(
        spec=spec,
        prompt=task_prompt(n),
        files=files,
        truth=truth,
        stages=stages,
        stage_updates=[st[f"stage-{k}/REQUIREMENTS.md"] for k, st in enumerate(stages, start=1)],
        generator_version=GENERATOR_VERSION,
        kind="coding",
        workspace_seed={"invoice/__init__.py": SEED_INIT, "invoice/core.py": SEED_CORE},
        scorer_version=SCORER_VERSION,
    )


# ------------------------------------------------------------- evaluation
EVAL_MARKER = "@@CLM-EVAL@@"

# Runs inside a fresh sandbox container with a submission copy at /task/workspace and the checks
# at /task/fixtures/checks.json (read-only). Each check has a 5 s alarm. Return types are checked
# exactly; values are compared as returned, without conversion.
EVAL_RUNNER = r"""
import json, signal, sys
sys.path.insert(0, '/task/workspace')
checks = json.load(open('/task/fixtures/checks.json'))
KEYS = {'subtotal', 'discount', 'shipping', 'tax', 'total'}
def _alarm(*_):
    raise TimeoutError('check timed out')
signal.signal(signal.SIGALRM, _alarm)
try:
    import invoice
    import_error = None
except BaseException as e:
    invoice, import_error = None, repr(e)[:300]
def observe(fn, v):
    if fn == 'compute_invoice':
        if type(v) is not dict:
            return {'type_error': type(v).__name__}
        if set(v) != KEYS or not all(type(x) is str for x in v.values()):
            return {'type_error': 'dict keys/types ' + repr({str(k): type(x).__name__ for k, x in v.items()})[:200]}
        return {'value': dict(v)}
    if type(v) is not str:
        return {'type_error': type(v).__name__}
    return {'value': v}
results = []
for c in checks:
    r = {'id': c['id']}
    if invoice is None:
        r.update(passed=False, stale=[], observed={'import_error': import_error})
        results.append(r)
        continue
    signal.alarm(5)
    try:
        f = getattr(invoice, c['fn'], None)
        if f is None:
            obs = {'raises': 'AttributeError', 'detail': 'missing function ' + c['fn']}
        else:
            obs = observe(c['fn'], f(*json.loads(json.dumps(c['args']))))
    except ValueError:
        obs = {'raises': 'ValueError'}
    except BaseException as e:
        obs = {'raises': type(e).__name__, 'detail': str(e)[:200]}
    finally:
        signal.alarm(0)
    r['observed'] = obs
    r['passed'] = obs == c['expect']
    r['stale'] = sorted(m for m, old in (c.get('stale') or {}).items() if not r['passed'] and obs == old)
    results.append(r)
print('@@CLM-EVAL@@' + json.dumps({'import_error': import_error, 'results': results}))
"""


def _parse(output: str | None) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """(results, import_error, error)."""
    if output is None or EVAL_MARKER not in output:
        return [], None, "evaluator produced no result"
    payload = json.loads(output.rsplit(EVAL_MARKER, 1)[1].strip().splitlines()[0])
    return payload["results"], payload["import_error"], None


@dataclass
class CodingScore2:
    completed: bool
    checks: int
    passed: int
    by_category: dict[str, dict[str, int]]
    stale: int  # failed checks whose output matches a superseded rule version
    stale_modules: dict[str, int]
    boundary: dict[str, int]
    type_errors: int
    retained_rule_failures: int  # failed retained + kept_part checks
    retained_failed_modules: list[str]
    regressions: list[str]  # retained modules that failed now but passed snapshot checks earlier
    snapshots: dict[str, dict[str, Any]]
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


def score_snapshots(
    truth: CodingTruth2, outputs: dict[int, str | None]
) -> dict[str, dict[str, Any]]:
    """Per snapshot stage: per module, passed/total of the snapshot checks."""
    out: dict[str, dict[str, Any]] = {}
    for stage, output in sorted(outputs.items()):
        checks = {c["id"]: c for c in truth.snapshot_checks.get(stage, [])}
        results, import_error, error = _parse(output)
        mods: dict[str, dict[str, int]] = {}
        for c in checks.values():
            mods.setdefault(c["module"], {"total": 0, "passed": 0, "version": c["version"]})
            mods[c["module"]]["total"] += 1
        for r in results:
            if r["passed"] and r["id"] in checks:
                mods[checks[r["id"]]["module"]]["passed"] += 1
        out[str(stage)] = {"modules": mods, "import_error": import_error, "error": error}
    return out


def score_coding(
    truth: CodingTruth2,
    eval_output: str | None,
    completed: bool,
    error: str | None = None,
    snapshot_outputs: dict[int, str | None] | None = None,
) -> CodingScore2:
    by: dict[str, dict[str, int]] = {}
    for c in truth.checks:
        by.setdefault(c["category"], {"total": 0, "passed": 0, "stale": 0})["total"] += 1
    results, import_error, parse_error = _parse(eval_output)
    if error is None:
        error = parse_error
    by_id = {c["id"]: c for c in truth.checks}
    passed = stale = type_errors = 0
    stale_mods: dict[str, int] = {}
    boundary = {"total": sum(1 for c in truth.checks if c["boundary"]), "passed": 0}
    failed_retained: set[str] = set()
    retained_failures = 0
    for r in results:
        c = by_id[r["id"]]
        cat = c["category"]
        if "type_error" in r.get("observed", {}):
            type_errors += 1
        if r["passed"]:
            passed += 1
            by[cat]["passed"] += 1
            boundary["passed"] += c["boundary"]
            continue
        if r["stale"]:
            stale += 1
            by[cat]["stale"] += 1
            for m in r["stale"]:
                stale_mods[m] = stale_mods.get(m, 0) + 1
        if cat in ("retained", "kept_part"):
            retained_failures += 1
            failed_retained.add(c["module"])
    snaps = score_snapshots(truth, snapshot_outputs or {})
    regressions = []
    for module in sorted(failed_retained):
        if truth.categories.get(module) != "retained":
            continue  # kept_part failures: the changed rule makes earlier evidence inapplicable
        version = truth.final_config[module]
        for snap in snaps.values():
            m = snap["modules"].get(module)
            if m and m["version"] == version and m["total"] and m["passed"] == m["total"]:
                regressions.append(module)
                break
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
    return CodingScore2(
        completed=completed,
        checks=total,
        passed=passed,
        by_category=by,
        stale=stale,
        stale_modules=stale_mods,
        boundary=boundary,
        type_errors=type_errors,
        retained_rule_failures=retained_failures,
        retained_failed_modules=sorted(failed_retained),
        regressions=regressions,
        snapshots=snaps,
        import_error=import_error,
        strict_success=strict,
        outcome=outcome,
        evaluation_error=error,
        results=results,
    )


def checks_json(truth: CodingTruth2) -> str:
    return json.dumps(truth.checks)


def snapshot_checks_json(truth: CodingTruth2, stage: int) -> str:
    return json.dumps(truth.snapshot_checks.get(stage, []))
