"""Reference implementation of the ``invoice-gen/2`` rules (experiment 005).

Standalone (standard library only) so that tests can copy this source verbatim into the sandbox
workspace as a known-correct or deliberately faulty submission. The evaluator's expected results
are computed host-side from the same functions.

``cfg`` maps a rule module to the version in force (absent: not in force). ``faults`` names
deliberate mistakes used only to validate the evaluator; a correct implementation uses none:

- ``partial:TIER``    tier change applied as a new table that drops the unchanged 'silver' rate
- ``partial:TAX``     regional tax change that drops the unchanged rate of other regions (0%)
- ``partial:VALIDATE`` qty-0 change that also drops the negative-qty check
- ``partial:COUPON``  coupon order change that drops the cap (coupon applied uncapped)
- ``order:SHIP_PRE_DISCOUNT``  shipping threshold compared with the subtotal before discount
- ``order:TAX_PRE_DISCOUNT``   tax computed on the subtotal, ignoring the discount
- ``order:ROUND_LATE``         per-step rounding applied only once to the subtotal, not per line
- ``interact:REFUND_SHIP``     refunds keep shipping in both invoices
- ``interact:REFUND_RETURNED`` refund = invoice total of the returned units alone
- ``interact:REFUND_NEG``      negative refunds are not floored at 0.00

Forgotten rules (a module left out of ``cfg``) and outdated rules (an older version in ``cfg``)
are expressed through ``cfg`` itself.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any

Q2 = Decimal("0.01")
KEYS = ("subtotal", "discount", "shipping", "tax", "total")
BULK_THRESHOLD = {1: 100, 2: 50}
BULK_FACTOR = Decimal("0.90")
TIER_RATES = {
    1: {"gold": "0.05", "silver": "0.02"},
    2: {"platinum": "0.10", "gold": "0.08", "silver": "0.02"},
}
TAX_RATES = {1: {}, 2: {"NZ": "0.15", "US": "0"}}
TAX_DEFAULT = "0.10"
SHIP_FEE = Decimal("7.50")
SHIP_FREE_FROM = Decimal("100.00")

Faults = frozenset[str]
NO_FAULTS: Faults = frozenset()


def _validated_lines(lines: list[dict[str, Any]], cfg: dict[str, int], faults: Faults) -> list:
    v = cfg.get("VALIDATE", 0)
    if v and not lines:
        raise ValueError("no lines")
    kept = []
    for ln in lines:
        qty, price = ln["qty"], Decimal(ln["unit_price"])
        if v == 1 and (type(qty) is not int or qty <= 0):
            raise ValueError("qty")
        if v == 2 and (type(qty) is not int or (qty < 0 and "partial:VALIDATE" not in faults)):
            raise ValueError("qty")
        if v and price < 0:
            raise ValueError("unit_price")
        if v == 2 and qty == 0:
            continue
        kept.append((qty, price))
    if v == 2 and not kept:
        raise ValueError("no lines left")
    return kept


def ref_invoice(
    lines: list[dict[str, Any]],
    customer: dict[str, Any],
    cfg: dict[str, int],
    faults: Faults = NO_FAULTS,
    shipping_on: bool = True,
) -> dict[str, str]:
    end_rounding = cfg.get("ROUND", 1) == 2
    late = "order:ROUND_LATE" in faults

    def step(x: Decimal) -> Decimal:  # per-step rounding (ROUND v1)
        return x if end_rounding else x.quantize(Q2, ROUND_HALF_UP)

    bulk_v = cfg.get("BULK", 0)
    subtotal = eligible = Decimal(0)
    for qty, price in _validated_lines(lines, cfg, faults):
        amount = qty * price
        is_bulk = bool(bulk_v) and qty >= BULK_THRESHOLD[bulk_v]
        if is_bulk:
            amount *= BULK_FACTOR
        if not late:
            amount = step(amount)
        subtotal += amount
        if not (is_bulk and cfg.get("STACK")):
            eligible += amount
    if late:
        subtotal, eligible = step(subtotal), step(eligible)

    tier_v = cfg.get("TIER", 0)
    rates = dict(TIER_RATES[tier_v]) if tier_v else {}
    if tier_v == 2 and "partial:TIER" in faults:
        rates.pop("silver")
    rate = Decimal(rates.get(customer.get("tier", ""), "0"))
    coupon_v = cfg.get("COUPON", 0)
    coupon = Decimal(customer.get("coupon", "0")) if coupon_v else Decimal(0)
    if coupon_v == 2:
        applied = coupon if "partial:COUPON" in faults else min(coupon, subtotal)
        tier_disc = step(rate * max(eligible - applied, Decimal(0)))
        discount = applied + tier_disc
    else:
        tier_disc = step(rate * eligible)
        applied = min(coupon, subtotal - tier_disc) if coupon_v else Decimal(0)
        discount = tier_disc + applied

    ship_v = cfg.get("SHIP", 0)
    ship_basis = subtotal if "order:SHIP_PRE_DISCOUNT" in faults else subtotal - discount
    shipping = SHIP_FEE if ship_v and shipping_on and ship_basis < SHIP_FREE_FROM else Decimal(0)

    tax_v = cfg.get("TAX", 0)
    if tax_v:
        default = "0" if (tax_v == 2 and "partial:TAX" in faults) else TAX_DEFAULT
        t_rate = Decimal(TAX_RATES[tax_v].get(customer.get("region", ""), default))
    else:
        t_rate = Decimal(0)
    base = subtotal if "order:TAX_PRE_DISCOUNT" in faults else subtotal - discount
    if ship_v == 2:
        base += shipping
    tax = step(base * t_rate)

    if end_rounding:
        subtotal = subtotal.quantize(Q2, ROUND_HALF_EVEN)
        discount = discount.quantize(Q2, ROUND_HALF_EVEN)
        tax = tax.quantize(Q2, ROUND_HALF_EVEN)
    total = subtotal - discount + shipping + tax
    values = (subtotal, discount, shipping, tax, total)
    return {k: str(v.quantize(Q2)) for k, v in zip(KEYS, values, strict=True)}


def ref_refund(
    lines: list[dict[str, Any]],
    customer: dict[str, Any],
    returns: dict[str, int],
    cfg: dict[str, int],
    faults: Faults = NO_FAULTS,
) -> str:
    by_sku = {ln["sku"]: ln for ln in lines}
    for sku, n in returns.items():
        if sku not in by_sku or type(n) is not int or not 1 <= n <= by_sku[sku]["qty"]:
            raise ValueError("returns")
    ship = "interact:REFUND_SHIP" in faults
    if "interact:REFUND_RETURNED" in faults:
        back = [dict(ln, qty=returns[ln["sku"]]) for ln in lines if ln["sku"] in returns]
        return ref_invoice(back, customer, cfg, faults, shipping_on=ship)["total"]
    original = Decimal(ref_invoice(lines, customer, cfg, faults, shipping_on=ship)["total"])
    kept = [dict(ln, qty=ln["qty"] - returns.get(ln["sku"], 0)) for ln in lines]
    kept = [ln for ln in kept if ln["qty"] > 0]
    kept_total = (
        Decimal(ref_invoice(kept, customer, cfg, faults, shipping_on=ship)["total"])
        if kept
        else Decimal(0)
    )
    refund = original - kept_total
    if refund < 0 and "interact:REFUND_NEG" not in faults:
        refund = Decimal(0)
    return str(refund.quantize(Q2))


def ref_call(
    fn: str, args: list[Any], cfg: dict[str, int], faults: Faults = NO_FAULTS
) -> dict[str, Any]:
    try:
        if fn == "compute_invoice":
            return {"value": ref_invoice(args[0], args[1], cfg, faults)}
        if fn == "compute_refund":
            if not cfg.get("REFUND"):
                return {"raises": "AttributeError"}
            return {"value": ref_refund(args[0], args[1], args[2], cfg, faults)}
    except ValueError:
        return {"raises": "ValueError"}
    raise KeyError(fn)
