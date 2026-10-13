"""Experiment 011 agent pilot: proposed-edit checking (offline; Jev is faked).

A: ordinary CLM (direct edits, unchanged). B: proposals that Claude accepts, revises or withdraws.
C: B plus a Jev check of every proposal (jev-edit-check/2 on edit-evidence/1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pytest

from clm_lib.cli import CONDITION_FLAGS, conditions_need_jev, parse_conditions
from clm_lib.config import Config, load_config
from clm_lib.context import ContextStore, Entry
from clm_lib.jev import JevError, parse_response
from clm_lib.jev_edit_check import PAYLOAD_FIELDS_V2, QUESTIONS_V2
from clm_lib.prompts import EDIT_CHECK_CLAUSE, EDIT_CHECK_JEV_CLAUSE
from clm_lib.runner import PROPOSAL_FILENAME, _Run, parse_action
from clm_lib.tasks import generate

from .conftest import execute, working_block, working_ids
from .test_initiated_review import NoteExecutor
from .test_update_review import make, revisions

REPO = Path(__file__).resolve().parents[1]
ADVANCE = {"action": "advance"}
FINAL = {"action": "final", "answer": {"summary": "done"}}
TASK = "w2-smoke-1"  # 2 stages; the smoke prompt is irrelevant here


def accept(pid: str) -> dict[str, Any]:
    return {"action": "confirm_edit", "proposal": pid, "decision": "accept"}


def withdraw(pid: str) -> dict[str, Any]:
    return {"action": "confirm_edit", "proposal": pid, "decision": "withdraw"}


def note(body: str, eid: str = "n1") -> dict[str, Any]:
    return execute(f"NOTE:{eid}:{body}")


@dataclass
class EditJev:
    """Fake Jev for edit checks: ``answers`` maps question key -> probability (None: missing)."""

    answers: dict[str, float | None] = field(default_factory=lambda: {k: 0.1 for k in QUESTIONS_V2})
    fail_with: list[JevError] = field(default_factory=list)
    model: str = "fake-jev"
    name: str = "fake"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def call(self, body: dict[str, Any]) -> tuple[Any, float]:
        self.calls.append(body)
        if self.fail_with:
            raise self.fail_with.pop(0)
        ans = {
            k: ({"type": "noul", "noul": v} if v is not None else {"type": "noul"})
            for k, v in self.answers.items()
        }
        resp = {"model": self.model, "answers": ans, "usage": {"input_tokens": 1000}}
        return parse_response(resp, list(body["questions"])), 0.02


def run(
    tmp_path: Path, script: list[Any], arm: str, advisor: Any = None, executor: Any = None
) -> tuple[Any, Any]:
    runner, prov = make(tmp_path, script, Config(), advisor, executor or NoteExecutor())
    res = runner.run(generate(TASK), "clm_helpers", edit_check=arm)
    return res, prov


def receipt_of(user: str) -> str:
    block = working_block(user)
    bodies = [json.loads(x)["body"] for x in block.splitlines() if '"receipt"' in x]
    return bodies[-1] if bodies else ""


# ------------------------------------------------------------- A unchanged, opt-in only
def test_condition_a_and_historical_configs_are_unchanged(tmp_path: Path) -> None:
    for p in [*sorted((REPO / "configs").glob("exp*.toml")), REPO / "configs" / "default.toml"]:
        if p.stem.startswith("exp011pilot"):
            continue
        c = load_config(p).edit_check
        assert c.enabled is False and c.advice is False, p.name
    res, prov = run(tmp_path, [note("direct"), FINAL], "off")
    sys0 = prov.requests[0].system
    assert EDIT_CHECK_CLAUSE.strip()[:40] not in sys0 and "confirm_edit" not in sys0
    assert "confirm_edit" not in json.dumps(prov.requests[0].json_schema)
    assert working_ids(prov.requests[1].user)[0] == "n1"  # applied directly, as before
    assert (
        res.metrics["edit_check"]["mode"] == "off" and res.metrics["edit_check"]["proposals"] == 0
    )
    assert "-chk" not in res.run_id
    assert parse_action(json.dumps(accept("P1")), 1000)[0] is None  # not an action unless enabled
    assert parse_action(json.dumps(accept("P1")), 1000, allow_confirm=True)[0]
    bad = {"action": "confirm_edit", "proposal": "P1", "decision": "apply"}
    assert parse_action(json.dumps(bad), 1000, allow_confirm=True)[0] is None


def test_conditions_parse_and_are_exclusive(tmp_path: Path) -> None:
    assert parse_conditions("clm_helpers:on,clm_helpers+chk:on,clm_helpers+chk-jev:on")
    assert CONDITION_FLAGS["chk"][-1] == "control" and CONDITION_FLAGS["chk-jev"][-1] == "jev"
    assert conditions_need_jev("clm_helpers+chk-jev:on") and not conditions_need_jev(
        "clm_helpers+chk:on"
    )
    runner, _ = make(tmp_path, [FINAL], Config(), None, NoteExecutor())
    with pytest.raises(ValueError, match="no Jev advisor"):
        runner.run(generate(TASK), "clm_helpers", edit_check="jev")
    with pytest.raises(ValueError, match="exclusive"):
        runner.run(generate(TASK), "clm_helpers", edit_check="control", initiated_review="control")
    with pytest.raises(ValueError, match="CLM modes only"):
        runner.run(generate(TASK), "summary", edit_check="control")


# ------------------------------------------------------------- B: propose, then confirm
def test_b_holds_the_change_and_accept_keeps_intervening_appends(tmp_path: Path) -> None:
    script = [
        note("keep: exported_by on format change"),
        execute("print('x')"),
        accept("P1"),
        FINAL,
    ]
    res, prov = run(tmp_path, script, "control")
    assert "-chk-" in res.run_id and EDIT_CHECK_CLAUSE in prov.requests[0].system
    assert EDIT_CHECK_JEV_CLAUSE not in prov.requests[0].system
    # Step 2's request: the live context is unchanged (no n1), the receipt describes P1.
    ids2 = working_ids(prov.requests[1].user)
    assert "n1" not in ids2 and ids2[:2] == ["s1.act", "s1.obs"]
    rc = receipt_of(prov.requests[1].user)
    assert "held as proposal P1 (not applied)" in rc and "adds [n1]" in rc
    assert "Jev" not in rc
    assert "context proposal: P1 pending" in prov.requests[1].user
    # An unrelated execution does not apply it.
    assert "n1" not in working_ids(prov.requests[2].user)
    # After accept: the candidate, then every entry appended since its base (steps 1-3).
    ids4 = working_ids(prov.requests[3].user)
    assert ids4[0] == "n1"
    assert ids4[1:] == ["s1.act", "s1.obs", "s1.rcpt", "s2.act", "s2.obs", "s3.act", "s3.obs"]
    assert "Proposal P1 accepted" in working_block(prov.requests[3].user)
    ec = res.metrics["edit_check"]
    assert ec["proposals"] == 1 and ec["proposals_by_status"] == {"accepted": 1}
    assert ec["checks"] == 0 and res.metrics["counts"]["edits_accepted_changed"] == 1
    assert (res.run_dir / "context" / "proposal-P1.json").exists()


def test_revision_replaces_and_stale_or_wrong_ids_cannot_be_accepted(tmp_path: Path) -> None:
    script = [note("first"), note("second"), accept("P1"), accept("P2"), FINAL]
    res, prov = run(tmp_path, script, "control")
    assert "replaces pending proposal P1" in receipt_of(prov.requests[2].user)
    assert "no such pending proposal (pending: P2)" in working_block(prov.requests[3].user)
    body = {
        json.loads(x)["id"]: json.loads(x)["body"]
        for x in working_block(prov.requests[4].user).splitlines()
    }
    assert body["n1"] == "second"
    st = res.metrics["edit_check"]["proposals_by_status"]
    assert st == {"revised": 1, "accepted": 1}
    assert res.metrics["edit_check"]["confirm_invalid"] == 1


def test_store_refuses_a_stale_proposal_and_merges_id_collisions() -> None:
    s = ContextStore()
    s.append([Entry("a", "note", "1")], "runtime", 1)
    base = s.current
    cand = (Entry("a", "note", "1b"), Entry("s2.obs", "note", "mine"))
    s.append([Entry("s2.obs", "observation", "out")], "runtime", 2)
    rev, why = s.accept_proposal(cand, base, 3)
    assert rev is not None and why == ""
    assert [e.id for e in rev.entries] == ["a", "s2.obs", "s2.obs~2"]
    # Another proposal from the old base is now stale: the live entry "a" changed.
    rev2, why2 = s.accept_proposal((Entry("x", "note", "y"),), base, 4)
    assert rev2 is None and why2.startswith("stale")


def test_withdraw_keeps_other_files_and_says_so(tmp_path: Path) -> None:
    class HelperNote(NoteExecutor):
        def run(self, code: str, workspace: Path, fixtures: Path) -> Any:
            (workspace / "helper.py").write_text("X = 1\n")
            return super().run(code, workspace, fixtures)

    res, prov = run(tmp_path, [note("tmp"), withdraw("P1"), FINAL], "control", None, HelperNote())
    obs = working_block(prov.requests[2].user)
    assert (
        "Proposal P1 withdrawn" in obs
        and "Other file changes made by the code of step 1 remain" in obs
    )
    assert "n1" not in working_ids(prov.requests[2].user)
    assert (res.run_dir / "workspace" / "helper.py").exists()
    assert not (res.run_dir / "workspace" / PROPOSAL_FILENAME).exists()


def test_final_and_stage_release_never_apply_a_pending_proposal(tmp_path: Path) -> None:
    res, prov = run(
        tmp_path, [note("pending"), ADVANCE, accept("P1"), note("again"), FINAL], "control"
    )
    obs = working_block(prov.requests[2].user)
    assert "Pending context proposal P1 was discarded because a new stage was released" in obs
    assert "no such pending proposal" in working_block(prov.requests[3].user)
    st = res.metrics["edit_check"]["proposals_by_status"]
    assert st == {"expired_at_stage_release": 1, "unresolved_at_final": 1}
    final_rev = revisions(res.run_dir)[-1]
    assert all(e["body"] != "again" for e in final_rev["entries"])
    assert res.metrics["counts"]["edits_accepted_changed"] == 0


def test_proposal_file_mirrors_the_held_candidate(tmp_path: Path) -> None:
    class Peek(NoteExecutor):
        seen: ClassVar[list[str]] = []

        def run(self, code: str, workspace: Path, fixtures: Path) -> Any:
            p = workspace / PROPOSAL_FILENAME
            Peek.seen.append(p.read_text() if p.exists() else "")
            return super().run(code, workspace, fixtures)

    run(tmp_path, [note("held"), execute("ADD:n9:x"), FINAL], "control", None, Peek())
    assert Peek.seen[0] == "" and '"body": "held"' in Peek.seen[1]


# ------------------------------------------------------------- C: Jev check of each proposal
def test_c_checks_with_the_frozen_contract_and_shows_four_outputs(tmp_path: Path) -> None:
    adv = EditJev({"lost": 0.82, "changed": 0.1, "evidence": 0.3, "insufficient": 0.64})
    res, prov = run(tmp_path, [note("short"), accept("P1"), FINAL], "jev", adv)
    assert EDIT_CHECK_JEV_CLAUSE in prov.requests[0].system
    rc = receipt_of(prov.requests[1].user)
    for label in (
        "lost obligation or exception 0.82 (flag)",
        "unsupported change 0.10",
        "lost or reassigned evidence 0.30",
        "insufficient evidence to check 0.64 (flag)",
    ):
        assert label in rc
    assert "FLAGGED for inspection" in rc and "missing evidence may explain it" in rc
    assert "does not guarantee" in rc
    # The request follows edit-evidence/1 with runtime inputs only.
    body = adv.calls[0]
    st = body["state"]
    assert tuple(st) == PAYLOAD_FIELDS_V2 and set(body["questions"]) == set(QUESTIONS_V2)
    assert st["update"].startswith("# Stage 1 of 2 requirements")
    assert "stage-2" not in json.dumps(st) and "checks" not in json.dumps(st["evidence_notes"])
    assert st["proposed_context"][0]["body"] == "short"
    assert st["current_context"] == []  # the live context before step 1
    ec = res.metrics["edit_check"]
    assert ec["verdicts"] == {"flagged": 1} and ec["flagged_then_accepted"] == 1
    assert res.metrics["cost"]["by_provider"]["typesafe"] > 0
    rec = json.loads((res.run_dir / "edit_check" / "P1.json").read_text())
    assert rec["verdict"] == "flagged" and rec["request"] == body


def test_c_service_errors_partial_answers_and_the_check_limit_are_never_clear(
    tmp_path: Path,
) -> None:
    err = JevError("timeout", "request timed out", retryable=True, billing="unknown")
    adv = EditJev(fail_with=[err, err])
    res, prov = run(tmp_path, [note("a"), accept("P1"), FINAL], "jev", adv)
    rc = receipt_of(prov.requests[1].user)
    assert "unavailable (service error" in rc and "not a clear result" in rc
    assert res.metrics["edit_check"]["verdicts"] == {"error": 1}
    assert len(adv.calls) == 2  # one runtime retry, both attempts in the ledger
    partial = EditJev({"lost": 0.1, "changed": None, "evidence": 0.1, "insufficient": 0.1})
    _, prov2 = run(tmp_path / "p", [note("b"), FINAL], "jev", partial)
    rc2 = receipt_of(prov2.requests[1].user)
    assert "unsupported change no answer" in rc2 and "INCOMPLETE" in rc2
    cfg = Config()
    cfg.edit_check.max_checks = 1
    runner, prov3 = make(
        tmp_path / "l", [note("c"), note("d"), FINAL], cfg, EditJev(), NoteExecutor()
    )
    res3 = runner.run(generate(TASK), "clm_helpers", edit_check="jev")
    rc3 = receipt_of(prov3.requests[2].user)
    assert "not checked (check limit 1 reached)" in rc3 and "not a clear result" in rc3
    assert res3.metrics["edit_check"]["verdicts"] == {"clear": 1, "not_checked": 1}


def test_c_rechecks_revisions_and_reports_uncertain_separately(tmp_path: Path) -> None:
    adv = EditJev({"lost": 0.1, "changed": 0.2, "evidence": 0.1, "insufficient": 0.7})
    res, prov = run(tmp_path, [note("x"), note("y"), withdraw("P2"), FINAL], "jev", adv)
    assert len(adv.calls) == 2
    assert "UNCERTAIN" in receipt_of(prov.requests[2].user)
    ec = res.metrics["edit_check"]
    assert ec["verdicts"] == {"uncertain": 2}
    assert ec["reactions_by_verdict"] == {"uncertain": {"revised": 1, "withdrawn": 1}}


def test_recovery_edit_is_applied_directly_and_recorded(tmp_path: Path) -> None:
    """OVER LIMIT: the runtime demands a shrinking edit in this very step, so B/C apply it
    directly (a declared, recorded exception); a pending proposal is then superseded."""
    runner, _ = make(tmp_path, [FINAL], Config(), None, NoteExecutor())
    r = _Run(
        runner,
        generate(TASK),
        "clm_helpers",
        "unit-run",
        "",
        False,
        False,
        "off",
        "off",
        False,
        "stated",
        "control",
    )
    r.step = 1
    r.execute_step(note("held"))
    assert r.proposal and r.proposal["id"] == "P1" and r.store.entries[0].id == "s1.act"
    r.step, r.recovery_active = 2, True
    r.execute_step(note("shrunk", "n2"))
    assert r.store.entries[0].id == "n2" and r.proposal is None
    st = [p["status"] for p in r.proposals]
    assert st == ["superseded_by_recovery_edit", "recovery_exempt_applied"]
    rc = next(e.body for e in r.store.entries if e.id == "s2.rcpt")
    assert "Applied directly (OVER LIMIT exception" in rc and "P1 is superseded" in rc
    ec = r.edit_check_summary()
    assert ec["recovery_exempt_edits"] == 1 and ec["proposals"] == 1


# ------------------------------------------------------------- real sandbox (Docker)
EDIT_CODE = (
    "import json\n"
    "c = json.load(open('context.json'))\n"
    "c['entries'] = [{'id': 'n1', 'role': 'note', 'body': 'API: export_records; see "
    "stage-1/REQUIREMENTS.md:3'}]\n"
    "json.dump(c, open('context.json', 'w'))\n"
    "open('exporter/notes.txt', 'w').write('side effect')\n"
    "print('proposed a note')\n"
)
TAMPER = (
    "import json\n"
    "json.dump({'format': 'clm-context/v1', 'entries': [{'id': 'evil', 'role': 'note', "
    "'body': 'tampered'}]}, open('proposed_context.json', 'w'))\n"
    "print(open('context.json').read()[:60])\n"
)


@pytest.mark.docker
def test_sandbox_proposal_cannot_be_bypassed_and_evidence_matches_inputs(make_runner: Any) -> None:
    from clm_lib.config import Config as Cfg

    adv = EditJev()
    cfg = Cfg()
    runner, prov = make_runner(
        [execute(EDIT_CODE), execute(TAMPER), accept("P1"), ADVANCE, FINAL], config=cfg
    )
    runner.jev, runner.jev_price = adv, None
    res = runner.run(generate(TASK), "clm_helpers", edit_check="jev")
    # The model's own write did not take effect: steps 2 and 3 saw the live context.
    assert "n1" not in working_ids(prov.requests[1].user)
    assert "n1" not in working_ids(prov.requests[2].user)
    # Accept applies the host-held candidate, not the tampered informational file.
    ids = working_ids(prov.requests[3].user)
    assert ids[0] == "n1" and "evil" not in ids
    # Withdrawal is not involved; the code's other file change happened once and stays.
    assert (res.run_dir / "workspace" / "exporter" / "notes.txt").read_text() == "side effect"
    st = adv.calls[0]["state"]
    assert st["execution_output"] == "proposed a note\n"
    cited = st["cited_sources"]
    line3 = generate(TASK).stages[0]["stage-1/REQUIREMENTS.md"].splitlines()[2]
    assert [c["reference"] for c in cited] == ["stage-1/REQUIREMENTS.md:3"]
    assert cited[0]["lines"] == [f"stage-1/REQUIREMENTS.md:3: {line3[:300]}"]
    assert "stage-2" not in json.dumps(st)
    assert (
        len(
            [
                e
                for e in (res.run_dir / "events.jsonl").read_text().splitlines()
                if '"execution_start"' in e
            ]
        )
        == 2
    )


def test_over_limit_status_names_an_acceptable_pending_proposal(tmp_path: Path) -> None:
    runner, _ = make(tmp_path, [FINAL], Config(), None, NoteExecutor())
    r = _Run(
        runner,
        generate(TASK),
        "clm_helpers",
        "unit-run",
        "",
        False,
        False,
        "off",
        "off",
        False,
        "stated",
        "control",
    )
    r.store.append([Entry("n0", "note", "seed")], "runtime", 0)
    r.step = 1
    r.execute_step(note("tiny"))
    text = r.status_text(9000, True, True)
    assert "accepting pending proposal P1 with confirm_edit also counts" in text
    assert "A change you write to context.json instead is applied directly" in text
    r.store.commit((Entry("x", "note", "changed"),), "runtime", 2)  # the base no longer matches
    assert "accepting pending proposal" not in r.status_text(9000, True, True)
    a = _Run(runner, generate(TASK), "clm_helpers", "unit-a", "", False)
    assert "In this run, accepting" not in a.status_text(9000, True, True)
