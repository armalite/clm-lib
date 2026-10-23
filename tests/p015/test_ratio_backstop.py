"""guarded-clm 0.2.0.dev1 on the p015 path: `strict_ratio_learning` is gone and never passed; the library's
always-on implausible-ratio backstop gives identical transcripts directly and through `GuardedSession`, with
provider-like reported usage, on the edit-and-read regression, append-only learning and an edit with a plausible
observation; obsolete or superseded configurations are refused; and the library's version, revision and source
fingerprint reach contracts, run records and exports. Synthetic data only; network blocked (conftest)."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import subprocess
from pathlib import Path
from typing import Any

import guarded_clm
import pytest
from guarded_clm import ClmContext, clm_context
from guarded_clm.estimate import MIN_PLAUSIBLE_NEW_TEXT_CHARS_PER_TOKEN, Calibration
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import ModelResponse, ToolCallPart

from clm_lib.budget import Ledger
from clm_lib.p015 import launcher as L
from clm_lib.p015.export import export_session, verify_export
from clm_lib.p015.provenance import library_provenance
from clm_lib.p015.session import GuardedSettings

from . import harness as h

REPO = Path(__file__).resolve().parents[2]
# The backstop's implementation was introduced at revision 3f18473 (0.2.0.dev1, source 51e6dbe8..., which
# confirm1 ran at cce58c1). Later revisions keep it (2026-10-23: d1d6d1d, 0.2.0.dev2, the request layouts).
ACCEPTED_REVISION = "3f184734362c4c9e24d7f5e2a2d6de5fa18153dd"
ACCEPTED = h.loaded_guarded_pin()  # the loaded checkout, stated as a pin


def read_page(n: int, size: int) -> str:
    """Read a page of the log."""
    return f"page {n}: " + " ".join(f"line{i:05d}" for i in range(size // 10))[:size]


def collapse_steps() -> list[h.Step]:
    """guarded-clm's regression shape: a read, then one response that deletes it and reads about the same amount (the
    request grows by a few characters while the reported input rises), then a large read."""
    return [
        h.call("read_page", {"n": 1, "size": 3_000}, "p1"),
        ModelResponse(
            parts=[
                ToolCallPart("edit_context", {"ops": [{"op": "delete", "ids": ["s1.obs"]}]}, "e1"),
                ToolCallPart("read_page", {"n": 2, "size": 2_640}, "p2"),
            ]
        ),
        h.call("read_page", {"n": 3, "size": 6_000}, "p3"),
        h.text("done"),
    ]


def appending_steps() -> list[h.Step]:
    return [
        h.call("read_page", {"n": i, "size": s}, f"p{i}")
        for i, s in ((1, 3_000), (2, 4_000), (3, 2_000))
    ] + [h.text("done")]


def plausible_edit_steps() -> list[h.Step]:
    """An edit that deletes a read while the same response reads much more: the request grows by about 3,000
    characters at provider-like usage, a plausible observation that the library learns from."""
    return [
        h.call("read_page", {"n": 1, "size": 3_000}, "p1"),
        ModelResponse(
            parts=[
                ToolCallPart("edit_context", {"ops": [{"op": "delete", "ids": ["s1.obs"]}]}, "e1"),
                ToolCallPart("read_page", {"n": 2, "size": 6_000}, "p2"),
            ]
        ),
        h.call("read_page", {"n": 3, "size": 2_000}, "p3"),
        h.text("done"),
    ]


def case(name: str, steps: Any, rises: frozenset[int] = frozenset()) -> h.Case:
    return h.Case(name, steps, ["Investigate."], rises=rises, tools=[read_page])


def both(c: h.Case, tmp: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    direct = h.run_direct(c)
    via, _ = h.run_session(c, tmp / c.name, Ledger.open(tmp / f"{c.name}.json", 100.0))
    return direct, via


@h.frozen_clock()
def new_text_ratio(c: h.Case) -> float:
    """The library's own shared estimator after a direct run (read from the library object, not recomputed)."""
    script = h.Script(c.steps(), usage=c.usage, rises=c.rises)
    combined = GuardedSettings().capability(lambda r: None)
    [context] = [x for x in combined.capabilities if isinstance(x, ClmContext)]
    agent = Agent(
        script.model(), system_prompt=h.SYSTEM, tools=list(c.tools or []), capabilities=[combined]
    )
    asyncio.run(
        agent.run(c.prompts[0], conversation_id="conv", usage_limits=UsageLimits(request_limit=144))
    )
    return float(context.estimators.get("conv").calibration.new_text_chars_per_token)


def test_the_accepted_revision_is_loaded_and_the_option_is_gone() -> None:
    """The loaded library descends from the backstop's revision, is clean, and has no strict_ratio_learning."""
    p = library_provenance()
    assert guarded_clm.__version__ == ACCEPTED["version"] == p["version"]
    assert p["source_sha256"] == ACCEPTED["source_sha256"] and not p["git"]["package_dirty"]
    checkout = Path(guarded_clm.__file__).resolve().parents[2]
    ancestry = subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", ACCEPTED_REVISION,
                               p["git"]["revision"]], check=False)  # fmt: skip
    assert ancestry.returncode == 0  # the loaded checkout descends from the accepting revision
    assert "strict_ratio_learning" not in inspect.signature(clm_context).parameters
    assert "strict_ratio_learning" not in inspect.signature(ClmContext.__init__).parameters
    assert "strict_ratio_learning" not in GuardedSettings.__dataclass_fields__
    with pytest.raises(TypeError):
        GuardedSettings(strict_ratio_learning=False)  # type: ignore[call-arg]
    GuardedSettings().capability(lambda r: None)  # builds: no removed argument is passed


def test_the_library_backstop_boundary() -> None:
    """The library's Calibration: an observation at 0.5 characters per token lowers the new-text ratio; one below it
    does not; the overall ratio is updated either way."""
    assert MIN_PLAUSIBLE_NEW_TEXT_CHARS_PER_TOKEN == 0.5
    for added_tokens, expected in ((1_000, 0.5), (1_001, 2.5)):  # 500 characters: 0.5, then 0.4995
        cal = Calibration()
        cal.begin_request(10_000, appended=True)
        cal.record_usage(4_000, real_usage=True)
        overall = cal.chars_per_token
        cal.begin_request(10_500, appended=True)
        cal.record_usage(4_000 + added_tokens, real_usage=True)
        assert cal.new_text_chars_per_token == expected
        assert cal.chars_per_token != overall


def test_the_edit_and_read_regression_no_longer_collapses(tmp_path: Path) -> None:
    c = case("collapse", collapse_steps, frozenset({2}))
    direct, via = both(c, tmp_path)
    assert direct == h.without_endings(via)
    assert via["runs"][0]["ending"] == "completed"
    assert not [r for r in via["records"] if r["type"] in ("context_limit", "lite_over_limit")]
    assert any(r["type"] == "lite_edit" and r["status"] == "accepted" for r in via["records"])
    assert max(call["reported_input_tokens"] for call in via["calls"]) < 10_000
    # The implausible observation was ignored, so the ratio did not collapse; the later, plausible append is
    # learnt from as the library's rules say (the library's own check: above 2.0).
    assert new_text_ratio(c) > 2.0


def test_append_only_learning_follows_the_library(tmp_path: Path) -> None:
    c = case("append", appending_steps)
    direct, via = both(c, tmp_path)
    assert direct == h.without_endings(via) and via["runs"][0]["ending"] == "completed"
    assert 0.5 <= new_text_ratio(c) < 2.5  # learnt from provider-like usage


def test_an_edit_with_a_plausible_observation_still_learns(tmp_path: Path) -> None:
    c = case("plausible_edit", plausible_edit_steps)
    direct, via = both(c, tmp_path)
    assert direct == h.without_endings(via) and via["runs"][0]["ending"] == "completed"
    assert any(r["type"] == "lite_edit" and r["status"] == "accepted" for r in via["records"])
    assert 0.5 <= new_text_ratio(c) < 2.5


# ---------------------------------------------------------------------- configurations, contracts and records


def scripted_spec(tmp: Path, launch_id: str, **over: Any) -> dict[str, Any]:
    (tmp / "auth.json").write_text(json.dumps({"experiment": "015", "launch_id": launch_id,
                                               "programme_ceiling_usd": 50.0, "block_stop_usd": 20.0}))  # fmt: skip
    base = {
        "launch_id": launch_id,
        "instances": ["docthread-dev-1"],
        "arms": ["guarded_clm"],
        "questions": 1,
        "ledger": str(tmp / "ledger.json"),
        "ledger_ceiling_usd": 100.0,
        "authorisation": str(tmp / "auth.json"),
    }
    return base | over


@pytest.mark.parametrize("value", [True, False])
def test_an_obsolete_setting_is_refused_whatever_its_value(tmp_path: Path, value: bool) -> None:
    spec = scripted_spec(
        tmp_path, "ob1", arm_settings={"guarded_clm": {"strict_ratio_learning": value}}
    )
    with pytest.raises(
        ValueError, match=r"strict_ratio_learning was removed in guarded-clm 0\.2\.0\.dev1"
    ):
        L.plan(spec, tmp_path / "L")
    assert not (tmp_path / "L").exists()


def test_the_superseded_configuration_is_refused(tmp_path: Path) -> None:
    old = json.loads(
        (REPO / "configs/p015/superseded/proposed-comparison-strict-db619a8.json").read_text()
    )
    spec = old | scripted_spec(tmp_path, "ob2") | {"mode": "live", "arms": old["arms"]}
    with pytest.raises(ValueError, match="Migrate it") as exc:
        L.plan(spec, tmp_path / "L")
    assert "proposed-comparison-0.2.0.dev1.json" in str(exc.value) and not (tmp_path / "L").exists()


def test_a_pin_that_differs_from_the_loaded_library_is_refused(tmp_path: Path) -> None:
    pin = dict(ACCEPTED)
    for key, wrong in (
        ("revision", "db619a8a61eb2aebd4f127c05c9ab12f98ba8b6d"),
        ("version", "0.2.0.dev0"),
    ):
        spec = scripted_spec(tmp_path, "pin1", library={"guarded_clm": pin | {key: wrong}})
        with pytest.raises(ValueError, match=f"library.guarded_clm.{key}"):
            L.plan(spec, tmp_path / "L")
    assert not (tmp_path / "L").exists()


def test_version_revision_and_fingerprint_reach_contract_records_and_export(tmp_path: Path) -> None:
    prov = library_provenance()
    pin = dict(ACCEPTED)
    contract = L.plan(scripted_spec(tmp_path, "rec1", library={"guarded_clm": pin}), tmp_path / "L")
    assert contract["library"] == {"guarded_clm": pin}
    assert contract["fingerprints"]["guarded_clm_source_sha256"] == prov["source_sha256"]
    assert (
        contract["fingerprints"]["dependencies"]["guarded-clm"] == pin["version"]
    )  # installed metadata agrees
    assert L.run(tmp_path / "L") == L.EXIT_OK
    run_dir = tmp_path / "L/runs/s01-docthread-dev-1-guarded_clm/r1"
    session = json.loads((run_dir / "session.json").read_text())
    recorded = session["provenance"]["guarded_clm"]
    assert (
        recorded["version"] == pin["version"]
        and recorded["git"]["revision"] == ACCEPTED["revision"]
    )
    assert recorded["source_sha256"] == prov["source_sha256"]
    assert "strict_ratio_learning" not in session["settings"]
    dest = export_session(run_dir, tmp_path / "export")
    assert verify_export(dest) == []
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["guarded_clm"]["version"] == pin["version"]
    assert manifest["guarded_clm"]["git"]["revision"] == ACCEPTED["revision"]


def test_an_old_contract_is_not_resumed_against_the_new_revision(tmp_path: Path) -> None:
    L.plan(scripted_spec(tmp_path, "old1"), tmp_path / "L")
    contract = json.loads((tmp_path / "L/contract.json").read_text())
    # As a contract made against db619a8 would record it, with the removed setting.
    contract["fingerprints"]["guarded_clm_source_sha256"] = (
        "ed22df2936cfd3cd170a967a42a71518677b6ee66157e2ac84e81f63b5fbfcc1"
    )
    contract["arm_settings"] = {"guarded_clm": {"strict_ratio_learning": True}}
    text = json.dumps(contract, indent=1, sort_keys=True) + "\n"
    (tmp_path / "L/contract.json").write_text(text)
    (tmp_path / "L/contract.sha256").write_text(hashlib.sha256(text.encode()).hexdigest() + "\n")
    problems = L.contract_problems(tmp_path / "L")
    assert "fingerprint guarded_clm_source_sha256 differs from the launch contract" in problems
    assert any("strict_ratio_learning was removed" in p for p in problems)
    assert L.run(tmp_path / "L") == L.EXIT_CONTRACT
    assert not list((tmp_path / "L/runs").iterdir())
