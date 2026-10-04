"""Known solutions for the coding task, used only by tests (written into the sandbox workspace)."""

from __future__ import annotations

import inspect

from clm_lib import coding


def solution_source(cfg: dict[str, int]) -> str:
    """A standalone invoice/core.py implementing the reference rules for ``cfg``."""
    parts = [
        "from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal",
        "from typing import Any",
        "Q2 = Decimal('0.01')",
        f"TIER_RATES = {coding.TIER_RATES!r}",
        f"REGION_RATES = {coding.REGION_RATES!r}",
        f"PREFIX = {coding.PREFIX!r}",
        f"CFG = {cfg!r}",
        "Config = dict",
        inspect.getsource(coding.ref_compute),
        inspect.getsource(coding.ref_format),
        "def compute_invoice(lines, customer):\n    return ref_compute(lines, customer, CFG)\n",
        "def format_money(amount, currency):\n    return ref_format(amount, currency, CFG)\n",
    ]
    return "\n".join(parts)


def write_solution_code(cfg: dict[str, int]) -> str:
    """Agent-side Python that writes the solution into /task/workspace/invoice/core.py."""
    return f"open('invoice/core.py', 'w').write({solution_source(cfg)!r})\nprint('written')\n"


RUN_VISIBLE_TESTS = (
    "import subprocess, sys\n"
    "r = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', "
    "'/task/fixtures/current-tests'], cwd='/task/workspace', capture_output=True, text=True)\n"
    "print('rc', r.returncode)\nprint(r.stderr[-1500:])\n"
)
