"""Solutions for the invoice-gen/2 task, used only by tests.

``INDEPENDENT_SOURCE`` is a second implementation written from the requirement text, structured
differently from ``clm_lib.invoice_ref2``; it cross-checks the reference host-side and serves as
the known-correct sandbox submission. Faulty submissions are the reference module with a fault
set or a modified configuration (see ``faulty_core``).
"""

from __future__ import annotations

import inspect

from clm_lib import invoice_ref2

INDEPENDENT_SOURCE = """
from decimal import Decimal, ROUND_HALF_UP, ROUND_HALF_EVEN

CENT = Decimal("0.01")


def _q_up(x):
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def _q_even(x):
    return x.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _tier_rate(cfg, tier):
    v = cfg.get("TIER")
    if v is None:
        return Decimal("0")
    if tier == "silver":
        return Decimal("0.02")
    if tier == "gold":
        return Decimal("0.05") if v == 1 else Decimal("0.08")
    if tier == "platinum" and v == 2:
        return Decimal("0.10")
    return Decimal("0")


def _tax_rate(cfg, region):
    v = cfg.get("TAX")
    if v is None:
        return Decimal("0")
    if v == 2 and region == "NZ":
        return Decimal("0.15")
    if v == 2 and region == "US":
        return Decimal("0")
    return Decimal("0.10")


def _check_lines(cfg, lines):
    v = cfg.get("VALIDATE")
    if v is None:
        return list(lines)
    if len(lines) == 0:
        raise ValueError("empty")
    out = []
    for line in lines:
        qty = line["qty"]
        if not isinstance(qty, int) or isinstance(qty, bool):
            raise ValueError("qty type")
        if qty < 0 or (qty == 0 and v == 1):
            raise ValueError("qty")
        if Decimal(line["unit_price"]) < 0:
            raise ValueError("price")
        if qty == 0:
            continue
        out.append(line)
    if not out:
        raise ValueError("all lines ignored")
    return out


def invoice_for(cfg, lines, customer, with_shipping=True):
    per_step = cfg.get("ROUND", 1) == 1
    lines = _check_lines(cfg, lines)
    bulk_from = {1: 100, 2: 50}.get(cfg.get("BULK"))
    amounts, bulk_flags = [], []
    for line in lines:
        a = Decimal(line["qty"]) * Decimal(line["unit_price"])
        is_bulk = bulk_from is not None and line["qty"] >= bulk_from
        if is_bulk:
            a = a * Decimal("0.90")
        amounts.append(_q_up(a) if per_step else a)
        bulk_flags.append(is_bulk)
    subtotal = sum(amounts, Decimal("0"))
    if cfg.get("STACK"):
        eligible = sum((a for a, b in zip(amounts, bulk_flags) if not b), Decimal("0"))
    else:
        eligible = subtotal
    rate = _tier_rate(cfg, customer.get("tier"))
    coupon = Decimal(customer.get("coupon", "0")) if cfg.get("COUPON") else Decimal("0")
    if cfg.get("COUPON") == 2:
        used = coupon if coupon < subtotal else subtotal
        rest = eligible - used
        if rest < 0:
            rest = Decimal("0")
        tier_part = rate * rest
        if per_step:
            tier_part = _q_up(tier_part)
        discount = used + tier_part
    else:
        tier_part = rate * eligible
        if per_step:
            tier_part = _q_up(tier_part)
        left = subtotal - tier_part
        used = (coupon if coupon < left else left) if cfg.get("COUPON") else Decimal("0")
        discount = tier_part + used
    shipping = Decimal("0")
    if cfg.get("SHIP") and with_shipping and subtotal - discount < Decimal("100"):
        shipping = Decimal("7.50")
    taxable = subtotal - discount
    if cfg.get("SHIP") == 2:
        taxable = taxable + shipping
    tax = taxable * _tax_rate(cfg, customer.get("region"))
    if per_step:
        tax = _q_up(tax)
        subtotal, discount = _q_up(subtotal), _q_up(discount)
    else:
        subtotal, discount, tax = _q_even(subtotal), _q_even(discount), _q_even(tax)
    total = subtotal - discount + shipping + tax
    return {
        "subtotal": str(_q_up(subtotal)),
        "discount": str(_q_up(discount)),
        "shipping": str(_q_up(shipping)),
        "tax": str(_q_up(tax)),
        "total": str(_q_up(total)),
    }


def refund_for(cfg, lines, customer, returns):
    qty_of = {line["sku"]: line["qty"] for line in lines}
    for sku, n in returns.items():
        if sku not in qty_of:
            raise ValueError("unknown sku")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > qty_of[sku]:
            raise ValueError("bad quantity")
    before = Decimal(invoice_for(cfg, lines, customer, with_shipping=False)["total"])
    kept = []
    for line in lines:
        left = line["qty"] - returns.get(line["sku"], 0)
        if left > 0:
            kept.append({**line, "qty": left})
    after = Decimal("0")
    if kept:
        after = Decimal(invoice_for(cfg, kept, customer, with_shipping=False)["total"])
    diff = before - after
    if diff < 0:
        diff = Decimal("0")
    return str(_q_up(diff))
"""


def independent_core(cfg: dict[str, int]) -> str:
    """invoice/core.py for the independent implementation under ``cfg``."""
    body = INDEPENDENT_SOURCE + f"\nCFG = {cfg!r}\n\n"
    body += "def compute_invoice(lines, customer):\n    return invoice_for(CFG, lines, customer)\n"
    if cfg.get("REFUND"):
        body += (
            "\n\ndef compute_refund(lines, customer, returns):\n"
            "    return refund_for(CFG, lines, customer, returns)\n"
        )
    return body


def faulty_core(cfg: dict[str, int], faults: frozenset[str] = frozenset()) -> str:
    """invoice/core.py: the reference rules for ``cfg`` with deliberate ``faults``."""
    body = (
        inspect.getsource(invoice_ref2)
        + f"\nCFG = {cfg!r}\nFAULTS = frozenset({sorted(faults)!r})\n\n"
    )
    body += "def compute_invoice(lines, customer):\n    return ref_invoice(lines, customer, CFG, FAULTS)\n"
    if cfg.get("REFUND"):
        body += (
            "\n\ndef compute_refund(lines, customer, returns):\n"
            "    return ref_refund(lines, customer, returns, CFG, FAULTS)\n"
        )
    return body


INIT = "from .core import *  # noqa: F403\n"


def write_core_code(core: str) -> str:
    """Agent-side Python that installs ``core`` as the package implementation."""
    return (
        f"open('invoice/core.py', 'w').write({core!r})\n"
        f"open('invoice/__init__.py', 'w').write({INIT!r})\nprint('written')\n"
    )
