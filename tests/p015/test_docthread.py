"""The docthread workload: generation, evidence support, information boundaries, scoring, tools and offline
sessions through the real p015 path (network blocked by conftest)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clm_lib.p015.docthread.agent import plan
from clm_lib.p015.docthread.fixture import (
    DEV_INSTANCES,
    INCIDENT_HOUR,
    MECHANISMS,
    OUTPUT_BUDGET_CHARS,
    canonical_call,
    generate,
    instance,
    mentions,
    uniqueness_problems,
)
from clm_lib.p015.docthread.offline import Plan, run
from clm_lib.p015.docthread.scoring import answer_block, extract_answer, score_session, true_answers
from clm_lib.p015.docthread.task import questions, user_message
from clm_lib.p015.docthread.tools import EvidenceService
from clm_lib.p015.export import export_session, verify_export
from clm_lib.p015.records import read_jsonl

DEV = sorted(DEV_INSTANCES)


@pytest.fixture(scope="module", params=DEV)
def fx(request: pytest.FixtureRequest) -> Any:
    return instance(request.param)


# ---------------------------------------------------------------------- the fixture


def test_generation_is_deterministic_and_mechanisms_are_declared() -> None:
    a, b = instance("docthread-dev-1"), instance("docthread-dev-1")
    assert a.render_all() == b.render_all() and a.facts == b.facts
    assert {instance(n).mechanism.key for n in DEV} == {"lease", "memory"}
    for mech in MECHANISMS:  # every mechanism generates cleanly (throwaway structure-check seeds)
        assert uniqueness_problems(generate("structure-check", 901, mech)) == []


def test_every_fact_is_supported_once_and_outputs_fit_the_cap(fx: Any) -> None:
    assert uniqueness_problems(fx) == []
    outs = fx.render_all()
    assert max(len(t) for t in outs.values()) <= OUTPUT_BUDGET_CHARS < 8_000
    for name, sources in fx.sources.items():
        for key in sources:
            assert mentions(outs[key], fx.facts[name]), (name, key)
    quote = true_answers(fx, questions(fx)[3])["evidence_event"]
    assert fx.signature.search(quote) and sum(quote in line for line in fx.evidence_lines) == 1
    # The cause's signature appears in exactly one journal line, the evidence line.
    assert [
        line for lines in fx.journals.values() for line in lines if fx.signature.search(line)
    ] == [fx.facts["evidence_event"]]


def test_memory_facts_are_exposed_only_in_their_source_turn(fx: Any) -> None:
    """Each memory fact is held only by outputs that its source turn's natural investigation reads, and by none
    that a later non-memory turn's investigation reads."""
    outs = fx.render_all()
    steps = {n: {canonical_call(t, a) for t, a in calls} for n, calls in plan(fx).items()}
    for f in (f for t in questions(fx) for f in t.asks if f.recall_from is not None):
        holders = {k for k, text in outs.items() if mentions(text, fx.facts[f.key])}
        assert holders and holders <= steps[f.recall_from], f.key
        for later, keys in steps.items():
            if later > f.recall_from:
                assert not holders & keys, (f.key, later)


def test_hidden_answers_stay_out_of_prompts_schemas_and_errors(fx: Any) -> None:
    desk = EvidenceService(fx)
    visible = json.dumps([t.spec.to_api() for t in desk.tools()])
    visible += "".join(user_message(t) for t in questions(fx))
    visible += desk.fetch(
        "read_journal", {"component": fx.root(), "hour": INCIDENT_HOUR, "page": 99}
    ).content
    visible += desk.fetch("read_playbook", {"section": "nope"}).content
    for name, value in fx.facts.items():
        if name not in ("rejecting_component", "evidence_event"):
            assert not mentions(visible, value), name
    assert fx.facts["evidence_event"] not in visible
    assert fx.mechanism.setting not in visible and "Held back" not in visible


# ---------------------------------------------------------------------- scoring


def replies_for(fx: Any, overrides: dict[int, dict[str, Any]] | None = None) -> list[str | None]:
    out: list[str | None] = []
    for t in questions(fx):
        values = true_answers(fx, t) | (overrides or {}).get(t.number, {})
        out.append(f"Findings.\n\n{answer_block(values)}")
    return out


def statuses(score: dict[str, Any]) -> dict[str, str]:
    return {k: v["status"] for t in score["questions"] for k, v in t["values"].items()}


def test_correct_answers_and_permitted_alternative_forms(fx: Any) -> None:
    s = score_session(fx, replies_for(fx))
    assert (s["points"], s["out_of"], s["recall_points"], s["recall_out_of"]) == (10, 10, 3, 3)
    alt = {
        1: {
            "rejecting_component": fx.facts["rejecting_component"].upper(),
            "first_rejection_time": f"{fx.date}T{fx.facts['first_rejection_time']}Z",
        },
        2: {"faulty_change": fx.facts["faulty_change"].lower()},
        3: {"rejected_jobs": f"{fx.facts['rejected_jobs']} jobs"},
        4: {"evidence_event": '"' + true_answers(fx, questions(fx)[3])["evidence_event"] + '"'},
        8: {"peak_queue_wait_s": f"{int(fx.facts['peak_queue_wait_s']):,} s"},
    }
    assert score_session(fx, replies_for(fx, alt))["points"] == 10
    untagged = [r.replace("```json", "```") if r else r for r in replies_for(fx)]
    assert score_session(fx, untagged)["points"] == 10
    earlier_block = ['```json\n{"draft": true}\n```\n' + r if r else r for r in replies_for(fx)]
    assert score_session(fx, earlier_block)["points"] == 10  # the last block counts


def test_faulty_answers_are_scored_wrong_with_neutral_statuses(fx: Any) -> None:
    root_q_line = next(line for line in fx.evidence_lines if "| job-rejected" in line)
    wrong = {
        1: {
            "rejecting_component": fx.names["scan"],
            "first_rejection_time": fx.distractors["first_rejection_time"][1],
        },
        2: {"faulty_change": fx.facts["adjacent_change"]},
        3: {"rejected_jobs": int(fx.facts["rejected_jobs"]) + 1},
        4: {"evidence_event": root_q_line.split(" | ", 5)[5]},
        5: {"adjacent_change": "CHG-00000"},
        6: {"symptom_code": fx.distractors["symptom_code"][0], "procedure_step": ["P-1.1"]},
        7: {"first_rejected_job": fx.distractors["first_rejected_job"][0]},
        8: {"peak_queue_wait_s": "about half an hour"},
    }
    s = score_session(fx, replies_for(fx, wrong))
    assert s["points"] == 0
    st = statuses(s)
    assert (
        st["rejecting_component"]
        == st["first_rejection_time"]
        == st["faulty_change"]
        == "distractor"
    )
    assert st["symptom_code"] == st["first_rejected_job"] == "distractor"
    assert st["evidence_event"] == "wrong_line"  # a real line of that journal, without the cause
    assert st["adjacent_change"] == "not_in_evidence"
    assert st["procedure_step"] == st["peak_queue_wait_s"] == "malformed"
    assert st["rejected_jobs"] in ("other_evidence", "not_in_evidence")


def test_quotes_must_be_real_long_enough_and_show_the_cause(fx: Any) -> None:
    def quote_status(q: str) -> str:
        return statuses(score_session(fx, replies_for(fx, {4: {"evidence_event": q}})))[
            "evidence_event"
        ]

    true = true_answers(fx, questions(fx)[3])["evidence_event"]
    setting = f"{fx.mechanism.setting}={fx.mechanism.new}"
    assert (
        quote_status(true[true.index(setting) - 4 : true.index(setting) + len(setting)])
        == "correct"
    )
    assert quote_status(setting[-12:]) == "too_short"
    assert quote_status(f"the logs say {setting} caused every failure") == "not_a_journal_line"
    other = next(line for (c, h), lines in fx.journals.items() if c != fx.root() for line in lines)
    assert quote_status(other) == "wrong_line"


def test_missing_and_malformed_answers() -> None:
    fx = instance("docthread-dev-1")
    replies = replies_for(fx)
    replies[0] = "I think it is the ocr stage."  # no block
    replies[1] = '```json\n{"faulty_change": \n```'  # unparsable JSON
    replies[2] = '```json\n{"other_key": 5}\n```'
    s = score_session(fx, [*replies[:6], None])  # turn 7 unfinished, turn 8 absent
    st = statuses(s)
    assert extract_answer(replies[0]) is None and s["questions"][0]["parsed"] is False
    assert st["rejecting_component"] == st["faulty_change"] == st["rejected_jobs"] == "missing"
    assert st["first_rejected_job"] == st["peak_queue_wait_s"] == "missing"
    assert [t["finished"] for t in s["questions"]] == [True] * 6 + [False, False]
    assert s["points"] == 10 - 6


def test_memory_diagnostics_record_carried_facts_and_refetches() -> None:
    fx = instance("docthread-dev-1")
    replies = replies_for(fx)
    replies[0] = replies[0].replace(
        "Findings.", f"Findings (the first rejected job was {fx.facts['first_rejected_job']})."
    )
    source = fx.sources["peak_queue_wait_s"][0]
    calls = [[] for _ in range(8)]
    calls[7] = [source]
    fields = {
        k: v for t in score_session(fx, replies, calls)["questions"] for k, v in t["values"].items()
    }
    assert fields["first_rejected_job"]["carried_in_turn"] == 1
    assert fields["adjacent_change"]["carried_in_turn"] is None
    assert (
        fields["peak_queue_wait_s"]["refetched"] is True
        and fields["adjacent_change"]["refetched"] is False
    )


# ---------------------------------------------------------------------- tools


def test_desk_counts_refetches_and_answers_errors_neutrally() -> None:
    fx = instance("docthread-dev-1")
    desk = EvidenceService(fx)
    desk.fetch("read_playbook", {"section": "Contents"})
    desk.fetch("read_playbook", {"section": " contents "})
    assert desk.repeat_calls == 1
    err = desk.fetch("read_journal", {"component": fx.root(), "hour": "08:00", "page": 9})
    assert err.is_error and err.content == "page 9 does not exist; this journal has 3 pages"
    assert all(t.spec.side_effects == "none" and t.spec.category == "task" for t in desk.tools())


# ---------------------------------------------------------------------- offline sessions


@pytest.fixture(scope="module")
def sessions(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("docthread")
    return {
        "tidy": (run(Plan(policy="tidy"), out), out / "docthread-dev-1-tidy"),
        "keep": (run(Plan(policy="keep"), out), out / "docthread-dev-1-keep"),
        "fixed": (
            run(Plan(instance="docthread-dev-2", synthetic_usage=False), out, "fixed"),
            out / "fixed",
        ),
        "provider_failure": (run(Plan(fail_at_attempt=20), out, "failure"), out / "failure"),
        "spend_refused": (run(Plan(refuse_after_turn=5), out, "refusal"), out / "refusal"),
    }


def started_prompts(run_dir: Path) -> list[str]:
    out = []
    for a in read_jsonl(run_dir / "attempts.jsonl"):
        if a["event"] == "started":
            parts = [p for m in a["messages"] for p in m["parts"]]
            out.append(next(p["content"] for p in parts if p.get("part_kind") == "user-prompt"))
    return out


def test_scripted_sessions_complete_all_eight_turns(sessions: dict[str, Any]) -> None:
    for name in ("tidy", "keep", "fixed"):
        result, _ = sessions[name]
        assert result["endings"] == ["completed"] * 8, name
        assert result["score"]["points"] == 10, name
    tidy, keep = sessions["tidy"][0]["measurements"], sessions["keep"][0]["measurements"]
    assert (
        tidy["refetches"] >= 3 and tidy["held_back"] == 0
    )  # tidy deletes pages, so memory turns refetch
    assert (
        keep["peak_estimated_tokens"] > 4 * tidy["peak_estimated_tokens"]
        and keep["requests_with_pressure"] > 0
    )
    assert keep["held_back"] >= 1


def test_conversation_continuity_edits_and_protected_records(sessions: dict[str, Any]) -> None:
    result, run_dir = sessions["tidy"]
    prompts = started_prompts(run_dir)
    fx = instance("docthread-dev-1")
    first_q, last_q = questions(fx)[0].text, questions(fx)[7].text
    assert first_q in prompts[0] and "Current request:" not in prompts[0].split("</task>")[0]
    assert first_q in prompts[-1] and last_q in prompts[-1].split("Current request:\n")[1]
    # An accepted delete reaches the next request: the deleted page entry is gone, the receipt is there.
    edits = [r for r in read_jsonl(run_dir / "trace.jsonl") if r["type"] == "lite_edit"]
    assert edits and all(e["status"] == "accepted" for e in edits)
    deleted = json.loads(edits[0]["code"])[0]["ids"]
    later = next(p for p in prompts if f"s{edits[0]['step'] + 1}.rcpt" in p)
    assert all(f'"id": "{i}"' not in later for i in deleted)
    # Earlier answers are protected entries in later turns' requests.
    assert (
        '"id": "t1.answer", "role": "assistant"' in prompts[-1]
        and '"protected": true' in prompts[-1]
    )
    assert result["measurements"]["requests"] == len(prompts)


def test_failure_and_refusal_endings_are_explicit_and_scored_as_unfinished(
    sessions: dict[str, Any],
) -> None:
    failure = sessions["provider_failure"][0]
    assert failure["endings"][-1] == "provider_failure" and len(failure["endings"]) < 8
    refusal = sessions["spend_refused"][0]
    assert refusal["endings"] == ["completed"] * 5 + ["spend_refused"]
    unfinished = [t for t in refusal["score"]["questions"] if not t["finished"]]
    assert [t["question"] for t in unfinished] == [6, 7, 8]
    assert all(f["status"] == "missing" for t in unfinished for f in t["values"].values())


def test_session_records_export_with_task_provenance(
    sessions: dict[str, Any], tmp_path: Path
) -> None:
    result, run_dir = sessions["keep"]
    dest = export_session(run_dir, tmp_path / "export")
    assert verify_export(dest) == []
    session = json.loads((dest / "session.json").read_text())
    assert session["evidence"] == "scripted-offline"
    assert session["task"] | {} == {
        "generator": "docthread-gen/1",
        "task": "docthread-task/1",
        "scorer": "docthread-score/1",
        "instance": "docthread-dev-1",
        "seed": 7301,
        "mechanism": "lease",
        "policy": "scripted-investigator:keep",
        "synthetic_usage": True,
    }
    assert session["score"]["points"] == 10 and session["measurements"] == result["measurements"]
