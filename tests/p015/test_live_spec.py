"""A live launch plan must state every experimental choice explicitly (`launcher.live_spec_problems`): an incomplete
one is refused before anything is written, dispatched or reserved; a complete one is recorded exactly; and the
separate live-authorisation gate still applies. Offline: network blocked (conftest), temporary ledgers, no
credentials, and no provider request is made."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from clm_lib.p015 import launcher as L
from clm_lib.p015.provenance import library_provenance
from clm_lib.p015.records import read_jsonl

REPO = Path(__file__).resolve().parents[2]
PROPOSED = json.loads((REPO / "configs/p015/proposed-comparison-0.2.0.dev1.json").read_text())
ACCEPTED_SOURCE = (
    "51e6dbe8df7669ff8265d725976260f61891b6624d9c2201c7edc83dfcf2d9e4"  # guarded-clm 0.2.0.dev1
)


def loaded_pin() -> dict[str, Any]:
    """The loaded guarded-clm, stated as a pin: these tests check completeness, not which checkout is current."""
    p = library_provenance()
    return {
        "version": p["version"],
        "revision": p["git"]["revision"],
        "source_sha256": p["source_sha256"],
    }


def full_spec(tmp: Path, launch_id: str = "live1", **over: Any) -> dict[str, Any]:
    spec = copy.deepcopy(PROPOSED) | {
        "launch_id": launch_id, "instances": ["docthread-dev-1"], "questions": 1,
        "ledger": str(tmp / "ledger.json"), "ledger_ceiling_usd": 100.0, "authorisation": str(tmp / "auth.json"),
        "library": {"guarded_clm": loaded_pin()}, "task_version": "docthread-task/2",
        "turn_endings": "p015-turn-endings/1",
    }  # fmt: skip
    spec["arm_settings"]["paper_clm_shell"]["version"] = "paper-clm-shell/2"
    return spec | over


def test_the_proposed_settings_pin_the_accepted_library_and_await_the_freeze_updates() -> None:
    """The proposal predates the task and turn-ending versions and the README-only guarded-clm commit: it pins the
    accepted source exactly, and plan() refuses it until it is updated at the freeze (the pin is never loosened)."""
    pin = PROPOSED["library"]["guarded_clm"]
    assert pin["version"] == "0.2.0.dev1" and pin["source_sha256"] == ACCEPTED_SOURCE
    assert (
        pin["source_sha256"] == library_provenance()["source_sha256"]
    )  # the library source is unchanged
    assert L.live_spec_problems(PROPOSED) == [
        "task_version is not set (one of docthread-task/1, docthread-task/2)",
        "turn_endings is not set (one of p015-turn-endings/1, p015-turn-endings/2)",
        'arm_settings.paper_clm_shell.version is not set ("paper-clm-shell/1" or "paper-clm-shell/2")',
    ]
    problems = L.library_pin_problems(PROPOSED)
    assert all(p.startswith("library.guarded_clm.revision is ") for p in problems)
    assert PROPOSED["arm_settings"]["guarded_clm"] == {}  # no removed option
    assert "model.thinking adaptive" in PROPOSED["settings_status"]["proposed"]


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (("model", "thinking"), "model.thinking is not set"),
        (("model", "effort"), "model.effort is not set"),
        (("model", "max_tokens"), "model.max_tokens is not set"),
        (("model", "cache_ttl"), "model.cache_ttl is not set"),
        (("model", "cache_breakpoints"), "model.cache_breakpoints is not set"),
        (("model", "max_retries"), "model.max_retries is not set"),
        (("retry_policy",), "retry_policy must be stated"),
        (("task_version",), "task_version is not set"),
        (("turn_endings",), "turn_endings is not set"),
        (("library", "guarded_clm", "revision"), "library.guarded_clm.revision is not set"),
        (
            ("library", "guarded_clm", "source_sha256"),
            "library.guarded_clm.source_sha256 is not set",
        ),
        (("arm_settings", "paper_clm_shell", "profile"), "paper_clm_shell.profile is not set"),
        (
            ("arm_settings", "paper_clm_shell", "reminder_before_tokens"),
            "reminder_before_tokens is not set",
        ),
        (("paper_rules", "reminder_repeat"), "paper_rules.reminder_repeat is not set"),
        (("arm_settings", "token_tail_p015", "summary_call_cap"), "summary_call_cap is not set"),
        (("arm_settings", "paper_clm_shell", "version"), "paper_clm_shell.version is not set"),
    ],
)
def test_an_incomplete_live_plan_is_refused_before_anything_is_written(
    tmp_path: Path, path: tuple[str, ...], message: str
) -> None:
    spec = full_spec(tmp_path)
    node = spec
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    with pytest.raises(ValueError, match="state every experimental choice explicitly") as exc:
        L.plan(spec, tmp_path / "L")
    assert message in str(exc.value)
    assert not (tmp_path / "L").exists() and not (tmp_path / "ledger.json").exists()


def test_arm_specific_choices_are_required_only_for_selected_arms(tmp_path: Path) -> None:
    spec = full_spec(tmp_path, arms=["guarded_clm"])
    del (
        spec["arm_settings"]["paper_clm_shell"],
        spec["arm_settings"]["token_tail_p015"],
        spec["paper_rules"],
    )
    assert L.live_spec_problems(spec) == []
    del spec["library"]
    assert L.live_spec_problems(spec) == [
        f"library.guarded_clm.{k} is not set (the accepted guarded-clm {k})"
        for k in L.LIBRARY_PIN_FIELDS
    ]
    spec = full_spec(tmp_path, arms=["paper_clm_shell", "token_tail_p015"])
    del spec["library"]  # the guarded-clm pin is required only when that arm runs
    assert L.live_spec_problems(spec) == []


def test_an_unimplemented_paper_rule_is_refused(tmp_path: Path) -> None:
    spec = full_spec(tmp_path)
    spec["paper_rules"]["rejected_edit_free_step"] = "counted"
    problems = L.live_spec_problems(spec)
    assert problems == ['paper_rules.rejected_edit_free_step = "counted" is not implemented (only "free"); '
                        "choosing it needs a reviewed change to the arm"]  # fmt: skip


def test_scripted_plans_keep_their_test_defaults(tmp_path: Path) -> None:
    spec = {"launch_id": "s1", "instances": ["docthread-dev-1"], "ledger": str(tmp_path / "l.json"),
            "ledger_ceiling_usd": 100.0, "authorisation": str(tmp_path / "a.json")}  # fmt: skip
    assert L.live_spec_problems(spec) == []
    contract = L.plan(spec, tmp_path / "L")
    assert contract["model"]["thinking"] == "adaptive"  # a scripted test default, not an approval


def test_a_complete_live_plan_records_the_supplied_settings_exactly(tmp_path: Path) -> None:
    spec = full_spec(tmp_path)
    spec["model"]["effort"] = None  # an explicit "not sent"
    spec["model"]["cache_breakpoints"] = ["automatic"]
    L.plan(spec, tmp_path / "L")
    contract = json.loads((tmp_path / "L/contract.json").read_text())  # what is recorded
    for key in L.LIVE_MODEL_FIELDS:
        assert contract["model"][key] == spec["model"][key], key
    assert contract["arm_settings"] == spec["arm_settings"]
    assert (
        contract["paper_rules"] == spec["paper_rules"]
        and contract["retry_policy"] == "p015-retry/1"
    )
    assert contract["settings_status"] == spec["settings_status"] and contract["mode"] == "live"
    from clm_lib.p015.live import ModelConfig

    settings = ModelConfig(**contract["model"]).model_settings()
    assert "anthropic_effort" not in settings and settings["anthropic_cache"] == "5m"
    assert (
        "anthropic_cache_tool_definitions" not in settings
        and "anthropic_cache_instructions" not in settings
    )


def test_the_live_authorisation_gate_still_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    # No authorisation file: the launcher stops before any session, attempt or reservation.
    L.plan(full_spec(tmp_path, arms=["guarded_clm"]), tmp_path / "L")
    assert L.run(tmp_path / "L") == L.EXIT_SPEND
    assert not list((tmp_path / "L/runs").iterdir())
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["entries"] == [] and ledger["pending"] == []
    # An authorisation without "live": true (and no credential source): the first attempt is refused before dispatch.
    (tmp_path / "b").mkdir()
    (tmp_path / "b/auth.json").write_text(json.dumps({"experiment": "015", "launch_id": "live2", "live": False,
                                                      "programme_ceiling_usd": 5.0, "block_stop_usd": 1.0}))  # fmt: skip
    L.plan(full_spec(tmp_path / "b", launch_id="live2", arms=["guarded_clm"]), tmp_path / "b/L")
    assert L.run(tmp_path / "b/L") == L.EXIT_SPEND
    run_dir = tmp_path / "b/L/runs/s01-docthread-dev-1-guarded_clm/r1"
    attempts = read_jsonl(run_dir / "attempts.jsonl")
    assert not [a for a in attempts if a["event"] == "started"]  # nothing was dispatched
    [refused] = attempts
    assert refused["outcome"] == "refused" and refused["refusal"] == "not_authorised"
    assert (
        json.loads((tmp_path / "b/ledger.json").read_text())["entries"] == []
    )  # nothing reserved or charged
    status = L.read_status(tmp_path / "b/L", "s01-docthread-dev-1-guarded_clm")
    assert status["runs"][0]["outcome"] == "spend_stopped"
