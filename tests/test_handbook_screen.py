"""Experiment 012: handbook workload, scorer validation and result-screen/1 (offline; Jev faked).

A: ordinary CLM with unscreened search results. B: the same with Jev screening of eligible
structured search results. The faulty answers below are scorer tests only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from clm_lib.config import load_config
from clm_lib.handbook import (
    QTYPES,
    build_handbook,
    norm_citations,
    passages_from_files,
    score_handbook,
)
from clm_lib.jev import JevError, parse_response
from clm_lib.jev_screen import (
    MIN_CHARS,
    build_screen_request,
    eligible,
    notice_text,
    parse_blocks,
    render_block,
    repair_truncated,
    select,
    term_select,
)
from clm_lib.jev_screen_diag import run_screen_diagnostic
from clm_lib.prompts import SEARCH_SCREEN_CLAUSE
from clm_lib.tasks import generate

from .conftest import execute, working_block


def bodies(user: str) -> str:
    """The decoded bodies of the working-context entries of a request."""
    return "\n".join(json.loads(x)["body"] for x in working_block(user).splitlines() if x.strip())


REPO = Path(__file__).resolve().parents[1]
TOOLS = (
    "import sys; sys.path.insert(0, '/task/fixtures')\nfrom handbook_tools import search, show\n"
)


def answer_from_truth(qs: list[dict[str, Any]], set_index: int = 0) -> dict[str, Any]:
    return {
        "answers": [
            {
                "question": q["qid"],
                "value": f"{q['value']} {q['unit']}",
                "citations": q["support_sets"][min(set_index, len(q["support_sets"]) - 1)],
            }
            for q in qs
        ]
    }


# ------------------------------------------------------------- workload and scorer
def test_handbook_generation_has_versions_exceptions_and_alternatives() -> None:
    files, truth = build_handbook(9101)
    assert files == build_handbook(9101)[0]
    assert len([f for f in files if f.startswith("handbook/")]) == 40
    ids = {p["id"] for p in passages_from_files(files)}
    qs = truth["questions"]
    assert {q["type"] for q in qs} == set(QTYPES) and len(qs) == 6
    for q in qs:
        assert all(x in ids for s in q["support_sets"] for x in s)
        assert q["old_value"] != q["facts"]["v2"]
    assert any(len(s) == 2 for q in qs for s in q["support_sets"])  # multi-passage answers
    assert generate("handbook-dev-1").truth.questions != generate("handbook-dev-2").truth.questions


def test_scorer_accepts_valid_routes_and_rejects_wrong_versions_exceptions_and_citations() -> None:
    t = generate("handbook-dev-1").truth
    qs = t.questions
    ok = score_handbook(answer_from_truth(qs), t)
    assert ok.strict_success and ok.outcome == "correct" and ok.passed == 6
    assert score_handbook(answer_from_truth(qs, 1), t).strict_success  # alternative support sets
    spelled = answer_from_truth(qs)
    for a in spelled["answers"]:
        a["citations"] = [c.replace("HB-0", "HB-").replace(" §", "#") for c in a["citations"]]
        a["value"] = "EUR " + a["value"].split()[0]
    assert score_handbook(spelled, t).strict_success  # other spellings of ids and values
    for q in qs:
        bad = answer_from_truth(qs)
        entry = next(a for a in bad["answers"] if a["question"] == q["qid"])
        if q["type"] != "past":  # wrong (superseded) version
            entry["value"] = str(q["old_value"])
            s = score_handbook(bad, t)
            assert not s.strict_success and s.stale == 1, q["type"]
        if q["type"] in ("exception", "carveout", "additive"):  # missed exception or addition
            entry["value"] = str(q["facts"]["v2"] if q["type"] == "exception" else q["facts"]["vx"])
            assert not score_handbook(bad, t).strict_success, q["type"]
        entry.update(value=str(q["value"]), citations=["HB-99 §1"])  # unsupported citation
        s = score_handbook(bad, t)
        assert not s.strict_success and s.unsupported_citations == 1
        if any(len(st) == 2 for st in q["support_sets"]):  # half of a two-passage set
            entry["citations"] = [q["support_sets"][0][0]]
            assert not score_handbook(bad, t).strict_success
    assert score_handbook(None, t).outcome == "no_submission"
    assert norm_citations(["see HB-7 section 2 and HB-12#3"]) == {"HB-07 §2", "HB-12 §3"}


# ------------------------------------------------------------- the screening policy
def _block(n: int = 5, size: int = 450) -> str:
    lines = [
        f'<<search-result SR-001 query="lodging limit Geneva" k=5 returned={n} '
        "saved=/task/workspace/search-results/SR-001.txt>>"
    ]
    for i in range(n):
        lines += [
            f"=== HB-{i + 1:02d} §2 | Title {i} — Heading | version: 1 · status: current",
            f"text {i} " * (size // 7),
        ]
    lines.append("<<end search-result SR-001>>")
    return "\n".join(lines) + "\n"


def test_parse_render_eligibility_and_cap_repair(tmp_path: Path) -> None:
    out = "before\n" + _block() + "after\n"
    (b,) = parse_blocks(out)
    assert b.complete and len(b.passages) == 5 and eligible(b) and b.chars >= MIN_CHARS
    assert out == "before\n" + render_block(b, b.passages) + "after\n"
    assert not eligible(parse_blocks(_block(2))[0])  # fewer than 3 passages
    assert not eligible(parse_blocks(_block(3, 200))[0])  # under the minimum size
    (tmp_path / "search-results").mkdir()
    saved = {"passages": [{"id": p["id"]} for p in b.passages]}
    (tmp_path / "search-results" / "SR-001.json").write_text(json.dumps(saved))
    cut = out[: len(out) // 2]
    text, recs = repair_truncated(cut, tmp_path)
    (r,) = parse_blocks(text)
    assert r.complete and all(p["text"].endswith(" ") for p in r.passages)  # whole passages only
    assert recs[0]["not_shown"] == [p["id"] for p in b.passages[len(r.passages) :]]
    assert "[output cap:" in text and "not shown" in text


def test_selection_threshold_minimum_missing_answers_and_baseline() -> None:
    ps = parse_blocks(_block())[0].passages
    kept, missing = select(ps, [0.1, 0.9, 0.2, 0.05, 0.31])
    assert kept == [1, 4] and missing == []
    kept, _ = select(ps, [0.1, 0.05, 0.2, 0.01, 0.02])
    assert kept == [0, 2]  # at least two, highest first, original order kept
    kept, missing = select(ps, [0.1, None, 0.05, 0.01, 0.02])
    assert 1 in kept and missing == [1]  # a missing answer keeps its passage
    assert term_select("lodging limit Geneva", ps) == [0, 1]  # nothing matches: top two by rank
    b = parse_blocks(_block())[0]
    assert "all 5 passages kept" in notice_text(b, [0, 1, 2, 3, 4], 100, 100)
    n = notice_text(b, [1, 4], 2300, 900)
    assert "omitted HB-01 §2, HB-03 §2, HB-04 §2" in n and "SR-001.txt" in n and "show(" in n


def test_screen_request_holds_passage_text_and_no_labels() -> None:
    ps = parse_blocks(_block())[0].passages
    body = build_screen_request("TASK", "lodging limit Geneva", "find the limit", ps, "jev-1.13.0")
    assert set(body["state"]) == {"task", "query", "step_intent", "passages"}
    assert [p["text"] for p in body["state"]["passages"]] == [p["text"] for p in ps]
    for i, p in enumerate(ps):
        q = body["questions"][f"p{i}"]
        assert (
            q["instructions"]["passage_id"] == p["id"] and p["text"] in q["instructions"]["passage"]
        )
    cases = (
        json.loads(
            (
                REPO.parent
                / "clm-lib-test-results"
                / "experiments"
                / "012-jev-result-screening"
                / "screen-cases-v1.json"
            ).read_text()
        )
        if (REPO.parent / "clm-lib-test-results").exists()
        else None
    )
    if cases:
        c = cases["cases"][0]
        body = json.dumps(
            build_screen_request(c["task"], c["query"], c["intent"], c["passages"], "m")
        )
        for leak in ('"labels"', "support_sets", '"support"', "irrelevant", c["id"]):
            assert leak not in body


# ------------------------------------------------------------- runtime (Docker)
@dataclass
class ScreenJev:
    """Fake Jev for screening: keeps passages whose text contains ``keep``."""

    keep: str = "lodging"
    missing: str = ""
    fail_with: list[JevError] = field(default_factory=list)
    name: str = "fake"
    model: str = "fake-jev"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def call(self, body: dict[str, Any]) -> tuple[Any, float]:
        self.calls.append(body)
        if self.fail_with:
            raise self.fail_with.pop(0)
        ans = {}
        for k, q in body["questions"].items():
            text = q["instructions"]["passage"]
            if self.missing and self.missing in text:
                ans[k] = {"type": "noul"}
            else:
                ans[k] = {"type": "noul", "noul": 0.9 if self.keep in text.lower() else 0.05}
        return parse_response(
            {"model": self.model, "answers": ans, "usage": {"input_tokens": 500}},
            list(body["questions"]),
        ), 0.03


def _run(
    make_runner: Any, script: list[Any], arm: str, adv: Any = None, task: str = "handbook-dev-1"
) -> Any:
    runner, prov = make_runner(script, ceiling=10.0)
    runner.jev, runner.jev_price = adv, None
    return runner.run(generate(task), "clm_helpers", result_screen=arm), prov


def _final(task: str = "handbook-dev-1") -> dict[str, Any]:
    return {"action": "final", "answer": answer_from_truth(generate(task).truth.questions)}


SEARCH = TOOLS + "search('nightly lodging limit business trip Geneva contractor', k=6)\n"


@pytest.mark.docker
def test_condition_a_presents_unscreened_and_b_screens_the_same_input(make_runner: Any) -> None:
    res_a, prov_a = _run(make_runner, [execute(SEARCH, "find the lodging limit"), _final()], "off")
    obs_a = bodies(prov_a.requests[1].user)
    assert "<<search-result SR-001" in obs_a and "result-screen/1" not in obs_a
    assert SEARCH_SCREEN_CLAUSE.strip()[:30] not in prov_a.requests[0].system
    ra = res_a.metrics["result_screen"]
    assert (
        ra["mode"] == "off"
        and ra["blocks"] == 1
        and ra["screened"] == 0
        and ra["provenance_ok"] == 1
    )
    assert res_a.metrics["score"]["strict_success"]
    adv = ScreenJev(keep="geneva")
    res_b, prov_b = _run(
        make_runner, [execute(SEARCH, "find the lodging limit"), _final()], "jev", adv
    )
    assert "-scr-" in res_b.run_id and SEARCH_SCREEN_CLAUSE in prov_b.requests[0].system
    # Jev saw exactly the passages A was shown, with their text.
    a_block = parse_blocks(json.loads('"' + "x" + '"') and _stdout(res_a, 1))[0]
    sent = adv.calls[0]["state"]["passages"]
    assert [p["id"] for p in sent] == [p["id"] for p in a_block.passages]
    assert [p["text"] for p in sent] == [p["text"] for p in a_block.passages]
    rb = res_b.metrics["result_screen"]
    assert (
        rb["screened"] == 1
        and rb["passages_omitted"] > 0
        and rb["chars_kept"] < rb["chars_screened"]
    )
    obs_b = bodies(prov_b.requests[1].user)
    log = rb["block_log"][0]
    for pid in log["omitted"]:
        assert f"=== {pid} " not in obs_b
    assert "[result-screen/1: kept" in obs_b and "SR-001.txt" in obs_b
    # The saved original is complete in both conditions.
    for res in (res_a, res_b):
        saved = (res.run_dir / "workspace" / "search-results" / "SR-001.txt").read_text()
        assert all(p["id"] in saved for p in a_block.passages)
    assert len(list((res_b.run_dir / "jev").glob("*.json"))) == 1  # every attempt recorded
    assert (res_b.run_dir / "result_screen" / "001-SR-001.json").exists()


def _stdout(res: Any, step: int) -> str:
    for line in (res.run_dir / "events.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e["event"] == "execution_result" and e["step"] == step:
            return e["stdout"]
    raise AssertionError("no execution")


@pytest.mark.docker
def test_recovery_reads_are_never_screened_and_are_counted(make_runner: Any) -> None:
    adv = ScreenJev(keep="geneva")
    script = [
        execute(SEARCH, "find the lodging limit"),
        execute(TOOLS + "show(OMITTED)\n"),
        _final(),
    ]
    runner, prov = make_runner(script, ceiling=10.0)
    runner.jev, runner.jev_price = adv, None
    # Fill in the ids omitted at step 1 before step 2 runs (scripted provider reads lazily).
    orig = runner.executor.run

    def run(code: str, ws: Path, fx: Path) -> Any:
        if "OMITTED" in code:
            rec = json.loads(next((ws.parent).glob("result_screen/*.json")).read_text())
            code = code.replace("OMITTED", repr(rec["omitted"]))
        return orig(code, ws, fx)

    runner.executor.run = run  # type: ignore[method-assign]
    res = runner.run(generate("handbook-dev-1"), "clm_helpers", result_screen="jev")
    rs = res.metrics["result_screen"]
    assert len(adv.calls) == 1  # the show() output was not screened
    omitted = rs["block_log"][0]["omitted"]
    assert omitted and rs["recovery"]["show_calls"] == 1
    assert rs["recovery"]["omitted_recovered"] == sorted(omitted)
    obs = bodies(prov.requests[2].user)
    assert all(f"--- {pid} " in obs for pid in omitted)


@pytest.mark.docker
def test_service_errors_and_missing_answers_fail_open(make_runner: Any) -> None:
    err = JevError("timeout", "request timed out", retryable=True, billing="unknown")
    adv = ScreenJev(fail_with=[err, err])
    res, prov = _run(make_runner, [execute(SEARCH), _final()], "jev", adv)
    obs = bodies(prov.requests[1].user)
    assert "screening unavailable" in obs and "nothing omitted" in obs
    rs = res.metrics["result_screen"]
    assert rs["errors"] == 1 and rs["screened"] == 0 and len(adv.calls) == 2
    assert len(list((res.run_dir / "jev").glob("*.json"))) == 2
    adv2 = ScreenJev(keep="zzz", missing="§2")
    res2, _ = _run(make_runner, [execute(SEARCH), _final()], "jev", adv2)
    log = res2.metrics["result_screen"]["block_log"][0]
    assert log["missing_answers"] and set(log["missing_answers"]) <= set(log["kept"])


@pytest.mark.docker
def test_screening_is_opt_in_limited_and_clm_edits_still_drive_requests(make_runner: Any) -> None:
    for p in [*sorted((REPO / "configs").glob("exp*.toml")), REPO / "configs" / "default.toml"]:
        assert load_config(p).result_screen.enabled is False, p.name
    runner, _ = make_runner([_final()], ceiling=10.0)
    with pytest.raises(ValueError, match=r"\(handbook tasks\) only"):
        runner.run(generate("dev"), "clm_helpers", result_screen="jev")
    with pytest.raises(ValueError, match="no Jev advisor"):
        runner.run(generate("handbook-dev-1"), "clm_helpers", result_screen="jev")
    # Arbitrary output that is not a structured search result is never screened.
    adv = ScreenJev()
    edit = (
        "import json\nc=json.load(open('context.json'))\n"
        "c['entries']=[{'id':'n1','role':'note','body':'lodging note'}]\n"
        "json.dump(c,open('context.json','w'))\nprint('lodging ' * 400)\n"
    )
    res, prov = _run(make_runner, [execute(edit), _final()], "jev", adv)
    assert adv.calls == [] and res.metrics["result_screen"]["blocks"] == 0
    assert prov.requests[1].user.count('"id": "n1"') == 1  # the CLM edit set the next context


# ------------------------------------------------------------- component diagnostic
def test_diagnostic_scores_recall_complete_sets_and_gate(tmp_path: Path) -> None:
    from clm_lib.budget import Ledger
    from clm_lib.jev import JevPrice

    ps = parse_blocks(_block())[0].passages
    case = {
        "id": "x:Q1:specific",
        "split": "dev",
        "instance": "x",
        "question": "Q1",
        "eligible": True,
        "task": "T",
        "query": "lodging limit Geneva",
        "intent": "find it",
        "passages": ps,
        "labels": {
            ps[0]["id"]: "support",
            ps[1]["id"]: "support",
            ps[2]["id"]: "context",
            ps[3]["id"]: "irrelevant",
            ps[4]["id"]: "irrelevant",
        },
        "support_sets_present": [[ps[0]["id"], ps[1]["id"]]],
        "support_sets": [[ps[0]["id"], ps[1]["id"]]],
    }
    f = tmp_path / "cases.json"
    f.write_text(json.dumps({"version": "jev-screen-diag/1", "cases": [case]}))

    class Adv:
        name, model = "fake", "fake"

        def call(self, body: dict[str, Any]) -> tuple[Any, float]:
            ans = {
                k: {
                    "type": "noul",
                    "noul": 0.9
                    if q["instructions"]["passage_id"] in (ps[0]["id"], ps[2]["id"])
                    else 0.1,
                }
                for k, q in body["questions"].items()
            }
            assert "labels" not in json.dumps(body)
            return parse_response(
                {"model": "fake", "answers": ans, "usage": {"input_tokens": 1000}},
                list(body["questions"]),
            ), 0.1

    price = JevPrice("fake", 0.042, 0.0, "t", "t")
    ledger = Ledger.open(tmp_path / "l.json", 1.0)
    out = run_screen_diagnostic(Adv(), price, ledger, f, "dev", tmp_path, "m", sleep=lambda s: None)
    s = json.loads((out / "summary.json").read_text())
    assert s["jev"]["support_recall"] == "1/2" and s["jev"]["complete_set_lost"] == [
        "x:Q1:specific"
    ]
    assert s["gate"]["passed"] is False and len(ledger.entries) == 1
    assert s["term_selector"]["cases"] == 1


def test_recovery_read_detection_counts_direct_document_access() -> None:
    from clm_lib.jev_screen import recovery_reads

    grep = "import subprocess\nsubprocess.run('grep -n lodging /task/fixtures/handbook/*.md', shell=True)"
    assert recovery_reads("", grep)["doc_reads"] == 1
    tool = "import sys; sys.path.insert(0, '/task/fixtures')\nfrom handbook_tools import search"
    assert recovery_reads("", tool) == {
        "show_calls": 0,
        "show_ids": [],
        "saved_reads": 0,
        "doc_reads": 0,
    }
    saved = "print(open('/task/workspace/search-results/SR-001.txt').read())"
    assert recovery_reads("", saved)["saved_reads"] == 1


def test_export_rescore_keeps_the_recorded_handbook_score(tmp_path: Path) -> None:
    from clm_lib.report import rescore

    t = generate("handbook-dev-1").truth
    (tmp_path / "evaluator").mkdir()
    (tmp_path / "evaluator" / "truth.json").write_text(json.dumps(t.to_dict()))
    score = score_handbook(answer_from_truth(t.questions), t).to_dict()
    (tmp_path / "evaluator" / "score.json").write_text(json.dumps(score))
    (tmp_path / "events.jsonl").write_text("")
    assert rescore(tmp_path)["strict_success"] is True
