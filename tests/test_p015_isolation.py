"""The programme 015 path is opt-in and isolated: historical code paths never load it, historical configs never
select it, and the historical environment does not contain its dependencies."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_historical_entry_points_do_not_load_the_p015_path() -> None:
    code = (
        "import sys, clm_lib.cli, clm_lib.runner, clm_lib.export, clm_lib.provider, clm_lib.budget; "
        "bad = [m for m in sys.modules if m.startswith(('clm_lib.p015', 'pydantic_ai', 'guarded_clm'))]; print(bad)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_no_historical_config_selects_the_p015_path() -> None:
    for path in sorted((REPO / "configs").glob("*.toml")):
        text = path.read_text(encoding="utf-8").lower()
        assert "p015" not in text and "guarded" not in text, path.name


def test_the_historical_environment_is_unchanged() -> None:
    if importlib.util.find_spec("pydantic_ai") is not None:
        pytest.skip("running in the p015 environment")
    import anthropic
    import pydantic

    assert importlib.util.find_spec("guarded_clm") is None
    assert (pydantic.VERSION, anthropic.__version__) == ("2.13.5", "1.11.0")
