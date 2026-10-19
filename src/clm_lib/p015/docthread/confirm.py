"""Confirmation-instance checks (`p015-instance-check/1`): the predeclared mechanical rule, and its manifest.

The rule is clm-lib-test-results 015 `docthread-workload.md`, "Confirmation instances": instance k (1 to 6) is
`docthread-confirm-k`, with seed 7400 + k and the mechanism lease, memory, pages, lease, memory, pages. It is generated
by the frozen generator, and kept only if every mechanical check passes. Otherwise the seed is skipped and recorded,
and the next seed is 7400 + k + 6j (j = 1, 2, ...), with the same mechanism. Nothing here reads an agent's behaviour
or an outcome, and no instance is chosen for difficulty or pressure.

The checks, none of which reports an answer:
- `generates`: the generator returns without error;
- `unique_support`: `uniqueness_problems` is empty (every scored fact held exactly by its declared outputs; every
  output at most 7,800 characters);
- `output_size`: every tool output is at most 7,800 characters (also part of `unique_support`; reported separately);
- `journal_layout`: the failing component's incident-hour journal has 15 pages, the first rejection is on page 8 to
  11, and the explicit evidence line is 2 to 4 pages after it;
- `evidence_size`: the characters of the outputs a natural investigation reads lie within the bounds of the chosen
  size reading (see `SIZE_READINGS`: the rule's wording admits more than one, so the reading must be chosen and is
  recorded);
- `no_dev_identifiers`: no component name, change id or job id equals one in a development instance.

The manifest records, per instance: the seeds tried, each check's pass or fail, the generator version, the measured
evidence size, a sha256 fingerprint of all rendered outputs, and the chosen seed. Failure reasons name the check
only: no fact value, component role, page number or excerpt is ever written or printed.

Command line (only at the approved freeze; it generates the confirmation instances):

    python -m clm_lib.p015.docthread.confirm --size-reading NAME --confirm-freeze OUT.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import _parse_key, plan
from .fixture import (
    DEV_INSTANCES,
    GENERATOR_VERSION,
    INCIDENT_HOUR,
    OUTPUT_BUDGET_CHARS,
    ROOT_PAGES,
    Fixture,
    generate,
    instance,
    uniqueness_problems,
)

CHECK_VERSION = "p015-instance-check/1"
RULE = "docthread-confirm/1 (docthread-workload.md, Confirmation instances, 2026-10-16)"
MECHANISM_ORDER = ("lease", "memory", "pages", "lease", "memory", "pages")
SEED_BASE, SEED_STRIDE = 7400, 6
FIRST_REJECTION_PAGES = (8, 11)
EVIDENCE_OFFSET_PAGES = (2, 4)
WIDENING = 0.10
MAX_TRIES = (
    50  # not in the rule: a safety stop, never reached by any seed so far; reaching it is reported
)
CHECKS = (
    "generates",
    "unique_support",
    "output_size",
    "journal_layout",
    "evidence_size",
    "no_dev_identifiers",
)
MEMORY_FACTS = ("adjacent_change", "first_rejected_job", "peak_queue_wait_s")

_CHANGE_ID = re.compile(r"\bCHG-\d{5}\b")
_JOB_ID = re.compile(r"\bJ-[A-Z0-9]{8}\b")


# ---------------------------------------------------------------------- the evidence-size readings


def plan_chars(fx: Fixture) -> int:
    """The literal reading: the outputs `agent.plan` reads (questions 1, 2, 3, 4 and 6), as characters."""
    return sum(len(fx.output(t, a)) for calls in plan(fx).values() for t, a in calls)


def plan_with_recall_chars(fx: Fixture) -> int:
    """`agent.plan` plus one read of each recall fact's source outputs (questions 5, 7 and 8 read again)."""
    recall = sum(len(fx.output(*_parse_key(k))) for f in MEMORY_FACTS for k in fx.sources[f])
    return plan_chars(fx) + recall


@dataclass(frozen=True)
class SizeReading:
    """One reading of "the evidence a natural investigation reads ... within the development instances' range,
    widened by 10%". `measure` is applied to the confirmation instance; `bounds` come from the development instances
    (or are the numbers the rule states)."""

    name: str
    description: str
    measure: Callable[[Fixture], int]
    stated_bounds: tuple[int, int] | None = None  # when the bounds are the rule's stated numbers

    def bounds(self) -> tuple[int, int]:
        if self.stated_bounds is not None:
            return self.stated_bounds
        sizes = [self.measure(instance(n)) for n in DEV_INSTANCES]
        return round(min(sizes) * (1 - WIDENING)), round(max(sizes) * (1 + WIDENING))


SIZE_READINGS = {
    "plan": SizeReading(
        "plan",
        "agent.plan outputs only (the rule's literal measure), bounds from the development instances widened by 10%",
        plan_chars,
    ),
    "plan_with_recall": SizeReading(
        "plan_with_recall",
        "agent.plan outputs plus one read of each recall fact's sources, bounds from the development instances "
        "widened by 10%",
        plan_with_recall_chars,
    ),
    "stated": SizeReading(
        "stated",
        "agent.plan outputs only, against the rule's stated approximate bounds (100,000 to 139,000 characters)",
        plan_chars,
        (100_000, 139_000),
    ),
}


# ---------------------------------------------------------------------- the checks


def identifiers(fx: Fixture) -> set[str]:
    """Component names, change ids and job ids appearing anywhere in the instance's outputs."""
    out = set(fx.components)
    for text in fx.render_all().values():
        out |= set(_CHANGE_ID.findall(text)) | set(_JOB_ID.findall(text))
    return out


def outputs_sha256(fx: Fixture) -> str:
    rendered = fx.render_all()
    return hashlib.sha256(
        json.dumps(rendered, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def check(
    name: str,
    seed: int,
    mechanism: str,
    reading: SizeReading,
    dev_identifiers: set[str],
    *,
    make: Callable[[str, int, str], Fixture] = generate,
) -> dict[str, Any]:
    """One seed's checks. The result names checks and counts only; it holds no answer or location."""
    result: dict[str, Any] = {"seed": seed, "checks": {}, "valid": False}
    try:
        fx = make(name, seed, mechanism)
    except Exception as exc:  # the rule's first check: the generator returns without error
        result["checks"] = {c: (False if c == "generates" else None) for c in CHECKS}
        result["failed"] = ["generates"]
        result["generator_error"] = type(exc).__name__
        return result
    c = result["checks"]
    c["generates"] = True
    c["unique_support"] = not uniqueness_problems(fx)
    c["output_size"] = all(len(t) <= OUTPUT_BUDGET_CHARS for t in fx.render_all().values())
    first, ev = fx.layout["first_rejection_page"], fx.layout["evidence_page"]
    c["journal_layout"] = (
        fx.pages(fx.root(), INCIDENT_HOUR) == ROOT_PAGES
        and FIRST_REJECTION_PAGES[0] <= first <= FIRST_REJECTION_PAGES[1]
        and EVIDENCE_OFFSET_PAGES[0] <= ev - first <= EVIDENCE_OFFSET_PAGES[1]
    )
    size = reading.measure(fx)
    low, high = reading.bounds()
    c["evidence_size"] = low <= size <= high
    c["no_dev_identifiers"] = not (identifiers(fx) & dev_identifiers)
    result["failed"] = [k for k in CHECKS if c[k] is False]
    result["valid"] = not result["failed"]
    result["evidence_chars"] = size
    result["generator_attempt"] = fx.layout["attempt"]  # the generator's own deterministic re-draws
    result["outputs_sha256"] = outputs_sha256(fx)
    return result


def seeds_for(k: int) -> list[int]:
    return [SEED_BASE + k + SEED_STRIDE * j for j in range(MAX_TRIES)]


def check_all(
    reading_name: str,
    *,
    count: int = 6,
    prefix: str = "docthread-confirm",
    seed_list: Callable[[int], Sequence[int]] = seeds_for,
    make: Callable[[str, int, str], Fixture] = generate,
    dev: Sequence[str] = tuple(DEV_INSTANCES),
) -> dict[str, Any]:
    """Apply the rule to instances 1..count and return the manifest. Tests pass other seeds and generators."""
    reading = SIZE_READINGS[reading_name]
    dev_ids = set().union(*(identifiers(instance(n)) for n in dev)) if dev else set()
    excluded = {seed for seed, _ in DEV_INSTANCES.values()}
    used: set[int] = set()
    rows = []
    for k in range(1, count + 1):
        name, mechanism = f"{prefix}-{k}", MECHANISM_ORDER[(k - 1) % len(MECHANISM_ORDER)]
        tried = []
        chosen = None
        for seed in seed_list(k):
            if seed in excluded or seed in used:
                tried.append({"seed": seed, "valid": False, "failed": ["seed_excluded"]})
                continue
            used.add(seed)
            row = check(name, seed, mechanism, reading, dev_ids, make=make)
            tried.append(row)
            if row["valid"]:
                chosen = row
                break
        rows.append(
            {
                "k": k,
                "name": name,
                "mechanism": mechanism,
                "seed": chosen["seed"] if chosen else None,
                "valid": chosen is not None,
                "outputs_sha256": chosen["outputs_sha256"] if chosen else None,
                "tried": tried,
                "skipped": [t["seed"] for t in tried if not t["valid"]],
            }
        )
    low, high = reading.bounds()
    return {
        "format": CHECK_VERSION,
        "rule": RULE,
        "generator": GENERATOR_VERSION,
        "size_reading": {
            "name": reading.name,
            "description": reading.description,
            "bounds_chars": [low, high],
        },
        "checks": list(CHECKS),
        "instances": rows,
        "all_valid": all(r["valid"] for r in rows),
        "config_instances": [
            {"name": r["name"], "seed": r["seed"], "mechanism": r["mechanism"]}
            for r in rows
            if r["valid"]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("out", type=Path, help="where to write the manifest (JSON)")
    parser.add_argument("--size-reading", required=True, choices=sorted(SIZE_READINGS))
    parser.add_argument("--confirm-freeze", action="store_true",
                        help="required: this generates the confirmation instances (only at the approved freeze)")  # fmt: skip
    args = parser.parse_args(argv)
    if not args.confirm_freeze:
        print("refused: generating confirmation instances needs --confirm-freeze (the approved freeze only)",
              file=sys.stderr)  # fmt: skip
        return 2
    if args.out.exists():
        print(f"refused: {args.out} exists; manifests are written once", file=sys.stderr)
        return 2
    manifest = check_all(args.size_reading)
    args.out.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    for r in manifest["instances"]:  # names, seeds and pass/fail only
        print(
            f"{r['name']}: seed {r['seed']} {'valid' if r['valid'] else 'NO VALID SEED'}; skipped {r['skipped']}"
        )
    return 0 if manifest["all_valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
