"""Experiment 011 component diagnostic: Jev checks proposed context rewrites (offline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clm_lib.budget import Ledger
from clm_lib.jev import JevError, JevPrice, parse_response
from clm_lib.jev_edit_check import (
    PAYLOAD_FIELDS,
    QUESTIONS,
    build_edit_request,
    check_new_numbers,
    check_ref_loss,
    run_edit_diagnostic,
)

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO.parent / "clm-lib-test-results" / "experiments"
CASES = RESULTS / "011-jev-edit-check" / "diagnostic-cases-v1.json"
PRICE = JevPrice("fake-jev", 0.042, 0.0, "test", "test")
LABEL_FIELDS = (
    "label",
    "category",
    "family",
    "provenance",
    "source",
    "justification",
    "dimensions",
    "lost_text",
    "changed_from",
    "changed_to",
)


class EditFake:
    """Answers per case by matching a marker in the proposed context; may raise first."""

    def __init__(
        self, answers: dict[str, dict[str, float | None]], fail_with: list[JevError] | None = None
    ) -> None:
        self.answers = answers
        self.fail_with = list(fail_with or [])
        self.calls: list[dict[str, Any]] = []

    def call(self, body: dict[str, Any]) -> tuple[Any, float]:
        self.calls.append(body)
        if self.fail_with:
            raise self.fail_with.pop(0)
        text = json.dumps(body["state"]["proposed_context"])
        probs = next(v for k, v in self.answers.items() if k in text)
        raw = {
            "model": "fake-jev",
            "answers": {
                k: ({"type": "noul", "noul": p} if p is not None else {"type": "noul"})
                for k, p in probs.items()
            },
            "usage": {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": 3},
        }
        return parse_response(raw, list(body["questions"])), 0.01


def case(
    cid: str, label: str, cur: list[str], prop: list[str], update: str = "", **extra: Any
) -> dict[str, Any]:
    note = lambda i, b: {"id": f"n{i}", "role": "note", "body": b}  # noqa: E731
    return {
        "id": cid,
        "split": "frozen",
        "family": "fam",
        "provenance": "constructed",
        "task": "Do the task.",
        "current": [note(i, b) for i, b in enumerate(cur, 1)],
        "update": update,
        "proposed": [note(i, b) for i, b in enumerate(prop, 1)],
        "label": label,
        "category": "cat",
        "justification": "why",
        **extra,
    }


def write_cases(tmp: Path, cases: list[dict[str, Any]]) -> Path:
    p = tmp / "cases.json"
    p.write_text(json.dumps({"version": "test", "reporting_threshold": 0.5, "cases": cases}))
    return p


# ------------------------------------------------------------- payload
def test_payload_has_only_checker_visible_fields_in_order() -> None:
    c = case("c01", "harmful", ["A (x.md:3)", "B"], ["B2", "A2"], update="U", lost_text="z")
    body = build_edit_request(c, "jev-1.13.0")
    assert tuple(body["state"]) == PAYLOAD_FIELDS and body["model"] == "jev-1.13.0"
    assert [e["body"] for e in body["state"]["current_context"]] == ["A (x.md:3)", "B"]
    assert [e["body"] for e in body["state"]["proposed_context"]] == ["B2", "A2"]
    assert set(body["questions"]) == set(QUESTIONS)
    text = json.dumps(body)
    for field in LABEL_FIELDS:
        assert f'"{field}"' not in text
    assert "c01" not in text and "why" not in text and "harmful" not in text


@pytest.mark.skipif(not CASES.exists(), reason="results repository not checked out alongside")
def test_real_case_file_payloads_leak_nothing_and_real_edits_match_010() -> None:
    doc = json.loads(CASES.read_text())
    for c in doc["cases"]:
        text = json.dumps(build_edit_request(c, "m")).lower()
        for word in (
            "harmful",
            "acceptable",
            "ambiguous",
            "injected",
            "deliberately",
            c["id"],
            c["family"],
        ):
            assert word.lower() not in text, (c["id"], word)
        assert c["justification"].lower() not in text
    v3 = RESULTS / "010-jev-initiated-review" / "artifacts" / "runs" / "v3" / "evaluation"
    real = [c for c in doc["cases"] if c["provenance"] == "real-unmodified"]
    assert len(real) == 3
    for c in real:
        suffix = c["source"].split("run …")[1].split(",")[0]
        before, after = (int(x) for x in c["source"].split("revision ")[1].split(" ")[0:3:2])
        run = next(p for p in v3.iterdir() if p.name.endswith(suffix))

        def notes(n: int, run: Path = run) -> list[dict[str, Any]]:
            rev = json.loads((run / "context" / f"rev-{n:04d}.json").read_text())
            return [e for e in rev["entries"] if e["role"] == "note"]

        assert c["current"] == notes(before) and c["proposed"] == notes(after)


# ------------------------------------------------------------- deterministic references
def test_deterministic_checks() -> None:
    c = case(
        "c01",
        "harmful",
        ["Fee 1.5% after 30 days (§5.2); see OPS-311 and a.log:12 [Lewis 2020]"],
        ["Fee 1.5% after 60 days"],
    )
    assert check_ref_loss(c) == ["OPS-311", "[Lewis2020]", "a.log:12", "§5.2"]
    assert check_new_numbers(c) == ["60"]
    ok = case(
        "c02",
        "acceptable",
        ["Rate 100/min (OPS-311)"],
        ["Rate 120/min (OPS-311)"],
        update="raised to 120",
    )
    assert check_ref_loss(ok) == [] and check_new_numbers(ok) == []


# ------------------------------------------------------------- run, parse, score, account
def test_run_scores_cases_and_records_every_attempt(tmp_path: Path) -> None:
    cases = [
        case("c01", "harmful", ["keep A"], ["H1 drop"]),
        case("c02", "harmful", ["keep B"], ["H2 drop"]),
        case("c03", "acceptable", ["x"], ["OK1"]),
        case("c04", "acceptable", ["y"], ["OK2"]),
        case("c05", "ambiguous", ["z"], ["AMB"]),
        case("c06", "harmful", ["w"], ["H3 none"]),
    ]
    fake = EditFake(
        {
            "H1": {"lost": 0.9, "changed": 0.1, "evidence": 0.1},
            "H2": {"lost": 0.2, "changed": None, "evidence": 0.3},  # partial, missed
            "OK1": {"lost": 0.1, "changed": 0.6, "evidence": 0.1},  # false flag via one question
            "OK2": {"lost": 0.1, "changed": 0.1, "evidence": 0.1},
            "AMB": {"lost": 0.7, "changed": 0.1, "evidence": 0.1},
            "H3": {"lost": None, "changed": None, "evidence": None},  # unanswered
        },
        fail_with=[JevError("http_529", "overloaded", retryable=True, billing="none")],
    )
    ledger = Ledger.open(tmp_path / "ledger.json", 10.0)
    out = run_edit_diagnostic(
        fake,
        PRICE,
        ledger,
        write_cases(tmp_path, cases),
        "frozen",
        tmp_path / "runs",
        "fake-jev",
        retries=1,
        sleep=lambda s: None,
    )
    s = json.loads((out / "summary.json").read_text())
    sc = s["score"]
    assert sc["jev"]["harmful_detected"] == ["c01"] and set(sc["jev"]["harmful_missed"]) == {
        "c02",
        "c06",
    }
    assert sc["jev"]["acceptable_flagged"] == ["c03"] and sc["jev"]["recall"] == "1/3"
    assert sc["unanswered"] == ["c06"] and sc["partial"] == ["c02"]
    assert sc["ambiguous"] == {"c05": {"lost": 0.7, "changed": 0.1, "evidence": 0.1}}
    assert sc["per_question"]["changed"]["acceptable_flagged"] == ["c03"]
    assert sc["gate"] is None  # the gate applies only to the 16 + 16 frozen structure
    assert sc["attempts"] == 7 and sc["errors"] == 1 and sc["requests"] == 6
    led = json.loads((tmp_path / "ledger.json").read_text())
    assert [e["kind"] for e in led["entries"]] == ["jev-diagnostic"] * 7
    assert led["entries"][0]["status"] == "error:http_529" and led["entries"][0]["charged_usd"] == 0
    saved = json.loads((out / "requests" / "c01.json").read_text())
    assert "label" not in json.dumps(saved["request"]) and saved["answers"]["lost"] == 0.9
    assert "harmful" not in json.dumps(fake.calls)


def test_gate_and_unanswered_conventions(tmp_path: Path) -> None:
    cases = [case(f"h{i:02d}", "harmful", ["a"], [f"H{i:02d}x"]) for i in range(16)]
    cases += [case(f"a{i:02d}", "acceptable", ["a"], [f"A{i:02d}x"]) for i in range(16)]
    answers = {
        f"H{i:02d}x": {"lost": 0.9 if i < 14 else 0.1, "changed": 0.1, "evidence": 0.1}
        for i in range(16)
    }
    answers |= {
        f"A{i:02d}x": {"lost": 0.6 if i < 2 else 0.1, "changed": 0.1, "evidence": 0.1}
        for i in range(16)
    }
    fake = EditFake(answers)
    out = run_edit_diagnostic(
        fake,
        PRICE,
        Ledger.open(tmp_path / "l.json", 10.0),
        write_cases(tmp_path, cases),
        "frozen",
        tmp_path / "r",
        "m",
        sleep=lambda s: None,
    )
    gate = json.loads((out / "summary.json").read_text())["score"]["gate"]
    assert gate == {
        "harmful_detected_min": 14,
        "acceptable_flagged_max": 2,
        "detected": 14,
        "of_harmful": 16,
        "flagged_acceptable": 2,
        "of_acceptable": 16,
        "passed": True,
    }
    # A failed call on an acceptable case counts against the gate (treated as flagged).
    fake2 = EditFake(
        answers, fail_with=[JevError("bad_request", "422", retryable=False, billing="none")]
    )
    cases2 = [cases[16], *cases[:16], *cases[17:]]
    out2 = run_edit_diagnostic(
        fake2,
        PRICE,
        Ledger.open(tmp_path / "l2.json", 10.0),
        write_cases(tmp_path, cases2),
        "frozen",
        tmp_path / "r2",
        "m",
        sleep=lambda s: None,
    )
    s2 = json.loads((out2 / "summary.json").read_text())["score"]
    assert (
        s2["unanswered"] == ["a00"]
        and s2["gate"]["flagged_acceptable"] == 2
        and "a00" in s2["jev"]["acceptable_flagged"]
    )


def test_diagnostic_export(tmp_path: Path) -> None:
    from clm_lib.export import export_experiment

    cases = [case("c01", "harmful", ["a"], ["H1"]), case("c02", "acceptable", ["b"], ["OK"])]
    fake = EditFake(
        {
            "H1": {"lost": 0.9, "changed": 0.1, "evidence": 0.1},
            "OK": {"lost": 0.1, "changed": 0.1, "evidence": 0.1},
        }
    )
    ledger_path = tmp_path / "ledger.json"
    out = run_edit_diagnostic(
        fake,
        PRICE,
        Ledger.open(ledger_path, 10.0),
        write_cases(tmp_path, cases),
        "frozen",
        tmp_path / "runs" / "jev-diagnostic",
        "m",
        sleep=lambda s: None,
    )
    exp = tmp_path / "exp"
    exp.mkdir()
    (exp / "experiment.json").write_text(
        json.dumps(
            {
                "id": "989-edit",
                "title": "t",
                "runs": [],
                "comparisons": [],
                "diagnostics": [out.name],
            }
        )
    )
    export_experiment(exp, tmp_path / "runs", ledger_path, tmp_path / "results")
    dest = (
        tmp_path / "results" / "experiments" / "989-edit" / "artifacts" / "diagnostics" / out.name
    )
    assert (dest / "summary.json").exists() and (dest / "requests" / "c01.json").exists()


# ================================================================ v2: evidence-complete checking
from clm_lib.jev_edit_check import (  # noqa: E402
    MAX_ENTRY_CHARS,
    PAYLOAD_FIELDS_V2,
    QUESTIONS_V2,
    build_edit_request_v2,
    check_new_numbers_v2,
    questions_v2_sha256,
    select_evidence,
    verdict_v2,
)


def case2(
    cid: str, label: str, cur: list[tuple[str, str]], prop: list[str], **extra: Any
) -> dict[str, Any]:
    entry = lambda i, rb: {"id": f"e{i}", "role": rb[0], "body": rb[1]}  # noqa: E731
    return {
        "id": cid,
        "split": "frozen",
        "family": "fam",
        "provenance": "constructed",
        "task": "Do the task.",
        "current": [entry(i, rb) for i, rb in enumerate(cur, 1)],
        "update": extra.pop("update", ""),
        "exec_output": extra.pop("exec_output", ""),
        "sources": extra.pop("sources", {}),
        "proposed": [{"id": f"n{i}", "role": "note", "body": b} for i, b in enumerate(prop, 1)],
        "label": label,
        "category": "cat",
        "justification": "secret why",
        **extra,
    }


SRC = {
    "r1/logs/a.log": "\n".join(
        f"line {i} ok" if i != 7 else "line 7 ERROR disk full" for i in range(1, 61)
    ),
    "r2/logs/a.log": "x\ny",
}


def test_v2_evidence_contract_is_deterministic_and_discloses_gaps() -> None:
    c = case2(
        "c01",
        "acceptable",
        [
            ("note", "old note"),
            ("observation", "step output: disk WARN"),
            ("receipt", "edit accepted"),
        ],
        [
            "disk full at r1/logs/a.log:7; also r1/logs/a.log:1-30 and r9/x.log:3 and a.log:2 and r1/logs/a.log:99"
        ],
        exec_output="",
        sources=SRC,
    )
    state, meta = select_evidence(c)
    assert tuple(state) == PAYLOAD_FIELDS_V2
    assert [e["role"] for e in state["current_context"]] == [
        "note",
        "observation",
        "receipt",
    ]  # whole revision
    refs = {x["reference"]: x["lines"] for x in state["cited_sources"]}
    assert refs["r1/logs/a.log:7"] == ["r1/logs/a.log:7: line 7 ERROR disk full"]
    assert len(refs["r1/logs/a.log:1-30"]) == 20
    notes = " | ".join(state["evidence_notes"])
    assert (
        "spans 30 lines; only the first 20" in notes and "r9/x.log:3 could not be resolved" in notes
    )
    assert "a.log:2 could not be resolved (ambiguous short path)" in notes
    assert "line 99 does not exist" in notes and "printed nothing" in notes
    assert meta["complete"] is False
    # References already present in the current context are not re-fetched.
    c2 = case2(
        "c02",
        "acceptable",
        [("note", "see r1/logs/a.log:7")],
        ["see r1/logs/a.log:7 (kept)"],
        exec_output="ok",
        sources=SRC,
    )
    s2, m2 = select_evidence(c2)
    assert s2["cited_sources"] == [] and m2["complete"] is True and s2["evidence_notes"] == []
    # Long entries are cut with a marker and a note; nothing is silently incomplete.
    c3 = case2(
        "c03", "acceptable", [("observation", "z" * (MAX_ENTRY_CHARS + 50))], ["n"], exec_output="o"
    )
    s3, _ = select_evidence(c3)
    assert s3["current_context"][0]["body"].endswith("[... truncated: 50 characters omitted]")
    assert any("current_context entry e1 truncated" in n for n in s3["evidence_notes"])


def test_v2_payload_leaks_no_labels_or_whole_source_files() -> None:
    c = case2(
        "c07",
        "harmful",
        [("note", "keep r1/logs/a.log:7")],
        ["dropped"],
        sources=SRC,
        lost_text="keep",
    )
    body, _ = build_edit_request_v2(c, "jev-1.13.0")
    assert (
        set(body["questions"])
        == set(QUESTIONS_V2)
        == {"lost", "changed", "evidence", "insufficient"}
    )
    text = json.dumps(body)
    for field in (*LABEL_FIELDS, "sources", "exec_output"):
        assert f'"{field}"' not in text
    for word in (
        "harmful",
        "secret why",
        "c07",
        "line 33 ok",
    ):  # no label, id or unrelated source lines
        assert word not in text


def test_v2_verdicts_abstention_and_gate(tmp_path: Path) -> None:
    assert (
        verdict_v2({"lost": 0.6, "changed": 0.1, "evidence": 0.1, "insufficient": 0.9}, 0.5)
        == "flagged"
    )
    assert (
        verdict_v2({"lost": 0.1, "changed": 0.1, "evidence": 0.1, "insufficient": 0.8}, 0.5)
        == "uncertain"
    )
    assert (
        verdict_v2({"lost": 0.1, "changed": 0.1, "evidence": 0.1, "insufficient": 0.2}, 0.5)
        == "clear"
    )
    assert (
        verdict_v2({"lost": None, "changed": None, "evidence": None, "insufficient": None}, 0.5)
        == "unanswered"
    )
    assert verdict_v2(None, 0.5) == "unanswered"
    cases = [case2(f"h{i:02d}", "harmful", [("note", "a")], [f"H{i:02d}x"]) for i in range(16)]
    cases += [case2(f"a{i:02d}", "acceptable", [("note", "a")], [f"A{i:02d}x"]) for i in range(16)]
    cases += [case2("g01", "insufficient", [("note", "a")], ["G01x"])]
    low = {"lost": 0.1, "changed": 0.1, "evidence": 0.1}
    answers = {
        f"H{i:02d}x": (
            {**low, "lost": 0.9, "insufficient": 0.1} if i < 14 else {**low, "insufficient": 0.9}
        )
        for i in range(16)
    }
    answers |= {
        f"A{i:02d}x": ({**low, "insufficient": 0.1} if i < 13 else {**low, "insufficient": 0.8})
        for i in range(16)
    }
    answers["G01x"] = {**low, "insufficient": 0.9}
    p = tmp_path / "cases2.json"
    p.write_text(
        json.dumps({"version": "jev-edit-diag/2", "reporting_threshold": 0.5, "cases": cases})
    )
    out = run_edit_diagnostic(
        EditFake(answers),
        PRICE,
        Ledger.open(tmp_path / "l.json", 10.0),
        p,
        "frozen",
        tmp_path / "r",
        "m",
        sleep=lambda s: None,
    )
    assert out.name.endswith("-edit2-frozen")
    s = json.loads((out / "summary.json").read_text())
    sc = s["score"]
    assert (
        s["question_version"] == "jev-edit-check/2" and s["evidence_version"] == "edit-evidence/1"
    )
    assert (
        sc["summary"]["harmful_flagged"] == "14/16" and sc["summary"]["harmful_uncertain"] == "2/16"
    )
    assert (
        sc["summary"]["acceptable_clear"] == "13/16"
        and sc["summary"]["acceptable_needing_inspection"] == "3/16"
    )
    assert sc["gate"]["passed"] is True and sc["gap_cases"]["g01"]["verdict"] == "uncertain"
    # Abstaining on acceptable cases cannot pass the coverage requirement.
    answers |= {f"A{i:02d}x": {**low, "insufficient": 0.8} for i in range(10, 16)}
    out2 = run_edit_diagnostic(
        EditFake(answers),
        PRICE,
        Ledger.open(tmp_path / "l2.json", 10.0),
        p,
        "frozen",
        tmp_path / "r2",
        "m",
        sleep=lambda s: None,
    )
    g2 = json.loads((out2 / "summary.json").read_text())["score"]["gate"]
    assert g2["acceptable_flagged"] == 0 and g2["acceptable_clear"] == 10 and g2["passed"] is False
    req = json.loads((out / "requests" / "h00.json").read_text())
    assert "evidence" in req and set(req["answers"]) == set(QUESTIONS_V2)
    summary = json.loads((out / "summary.json").read_text())
    assert summary["questions_sha256"] == questions_v2_sha256()


def test_v2_harm_questions_accept_support_from_all_supplied_material() -> None:
    # A completion or change justified by execution output or a cited source is not unsupported.
    for k in ("lost", "changed", "evidence"):
        text = json.dumps(QUESTIONS_V2[k])
        assert "execution output" in text and "cited sources" in text, k
        assert "the update supports" not in text and "the update does not" not in text, k


def test_v2_deterministic_checks_get_the_same_evidence() -> None:
    c = case2(
        "c01",
        "acceptable",
        [("note", "rate 100")],
        ["rate 120 per r1/logs/a.log:7"],
        exec_output="new rate 120",
        sources=SRC,
    )
    assert check_new_numbers_v2(c) == []  # 120 is in the execution output, 7 in the cited reference
    c2 = case2("c02", "harmful", [("note", "rate 100")], ["rate 150"], exec_output="new rate 120")
    assert check_new_numbers_v2(c2) == ["150"]


def test_v1_files_still_use_v1_and_both_export_separately(tmp_path: Path) -> None:
    from clm_lib.export import export_experiment

    v1 = write_cases(tmp_path, [case("c01", "harmful", ["a"], ["H1"])])
    fake = EditFake({"H1": {"lost": 0.9, "changed": 0.1, "evidence": 0.1, "insufficient": 0.1}})
    ledger_path = tmp_path / "ledger.json"
    root = tmp_path / "runs" / "jev-diagnostic"
    o1 = run_edit_diagnostic(
        fake, PRICE, Ledger.open(ledger_path, 10.0), v1, "frozen", root, "m", sleep=lambda s: None
    )
    s1 = json.loads((o1 / "summary.json").read_text())
    assert o1.name.endswith("-edit-frozen") and s1["question_version"] == "jev-edit-check/1"
    assert set(fake.calls[0]["questions"]) == {"lost", "changed", "evidence"}
    p2 = tmp_path / "v2.json"
    p2.write_text(
        json.dumps(
            {
                "version": "jev-edit-diag/2",
                "reporting_threshold": 0.5,
                "cases": [case2("c01", "harmful", [("note", "a")], ["H1"])],
            }
        )
    )
    o2 = run_edit_diagnostic(
        fake, PRICE, Ledger.open(ledger_path, 10.0), p2, "frozen", root, "m", sleep=lambda s: None
    )
    exp = tmp_path / "exp"
    exp.mkdir()
    (exp / "experiment.json").write_text(
        json.dumps(
            {
                "id": "988-edit",
                "title": "t",
                "runs": [],
                "comparisons": [],
                "diagnostics": [o1.name, o2.name],
            }
        )
    )
    export_experiment(exp, tmp_path / "runs", ledger_path, tmp_path / "results")
    d = tmp_path / "results" / "experiments" / "988-edit" / "artifacts" / "diagnostics"
    assert (
        (d / o1.name / "summary.json").exists()
        and (d / o2.name / "summary.json").exists()
        and o1.name != o2.name
    )
    led = json.loads(ledger_path.read_text())
    assert [e["kind"] for e in led["entries"]] == ["jev-diagnostic"] * 2
