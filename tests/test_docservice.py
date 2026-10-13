"""Experiment 012 v2: document-service workload, service boundary and screening (Jev faked).

A: ordinary CLM using the service. B: the same, with result-screen/1 on eligible search results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from clm_lib.config import Config
from clm_lib.docservice import DETAIL, DOMAINS, DocService, build_corpus
from clm_lib.handbook import FILLER, score_handbook
from clm_lib.jev import JevError, parse_response
from clm_lib.jev_screen import parse_blocks
from clm_lib.prompts import SEARCH_SCREEN_CLAUSE, SEARCH_SCREEN_SERVICE_CLAUSE
from clm_lib.runner import parse_action
from clm_lib.tasks import generate

from .conftest import FakeExecutor, execute, working_block
from .test_update_review import make

TASK = "docsvc-dev-1"


def bodies(user: str) -> str:
    return "\n".join(json.loads(x)["body"] for x in working_block(user).splitlines() if x.strip())


def final_for(task: str = TASK) -> dict[str, Any]:
    qs = generate(task).truth.questions
    return {
        "action": "final",
        "answer": {
            "answers": [
                {"question": q["qid"], "value": str(q["value"]), "citations": q["support_sets"][0]}
                for q in qs
            ]
        },
    }


@dataclass
class ScreenJev:
    keep: str = "geneva"
    fail_with: list[JevError] = field(default_factory=list)
    name: str = "fake"
    model: str = "fake-jev"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def call(self, body: dict[str, Any]) -> tuple[Any, float]:
        self.calls.append(body)
        if self.fail_with:
            raise self.fail_with.pop(0)
        ans = {
            k: {
                "type": "noul",
                "noul": 0.9 if self.keep in q["instructions"]["passage"].lower() else 0.05,
            }
            for k, q in body["questions"].items()
        }
        return parse_response(
            {"model": self.model, "answers": ans, "usage": {"input_tokens": 400}},
            list(body["questions"]),
        ), 0.03


def run(
    tmp_path: Path, script: list[Any], arm: str, adv: Any = None, executor: Any = None
) -> tuple[Any, Any]:
    runner, prov = make(tmp_path, script, Config(), adv, executor or FakeExecutor())
    return runner.run(generate(TASK), "clm_helpers", result_screen=arm), prov


SEARCH = {
    "action": "search",
    "query": "nightly lodging limit business trip Geneva",
    "k": 6,
    "thought": "find the Geneva lodging limit",
}


# ------------------------------------------------------------- corpus and scorer
def test_corpus_is_deterministic_varied_and_answerable_through_the_service() -> None:
    docs, truth = build_corpus(9501)
    assert docs == build_corpus(9501)[0] and len(docs) <= 99 and len(DOMAINS) == 16
    svc = DocService(docs)
    texts = " ".join(p["text"] for p in svc.passages)
    assert not any(f in texts for f in FILLER)  # no v1 generic filler
    assert any(d.split("{")[0] in texts for d in DETAIL)
    for q in truth["questions"]:
        for st in q["support_sets"]:
            assert all(svc.fetch([pid])[0][1] for pid in st)
    t = generate(TASK).truth
    assert score_handbook(final_for()["answer"], t).strict_success
    bad = final_for()["answer"]
    for a, q in zip(bad["answers"], t.questions, strict=True):
        if q["type"] == "exception":
            a["value"] = str(q["facts"]["v2"])  # missed exception
        elif q["type"] != "past":
            a["value"] = str(q["old_value"])  # superseded version
    s = score_handbook(bad, t)
    assert s.passed <= 1 and not s.strict_success


def test_service_actions_exist_only_for_the_service_task() -> None:
    assert parse_action(json.dumps(SEARCH), 1000)[0] is None
    assert parse_action(json.dumps(SEARCH), 1000, allow_service=True)[0]
    assert parse_action(json.dumps({**SEARCH, "k": 9}), 1000, allow_service=True)[0] is None
    assert (
        parse_action(json.dumps({"action": "fetch", "ids": []}), 1000, allow_service=True)[0]
        is None
    )
    assert parse_action(
        json.dumps({"action": "fetch", "ids": ["HB-01"]}), 1000, allow_service=True
    )[0]
    svc = DocService(build_corpus(9501)[0])
    got = svc.fetch(["HB-99 §1", svc.passages[0]["doc"]])
    assert got[0] == ("HB-99 §1", None) and all(p for _, p in got[1:])


# ------------------------------------------------------------- runtime (fake executor)
def test_a_and_b_share_presentation_and_b_screens_with_fetch_recovery(tmp_path: Path) -> None:
    res_a, prov_a = run(tmp_path / "a", [SEARCH, final_for()], "off")
    obs_a = bodies(prov_a.requests[1].user)
    assert "<<search-result SR-001" in obs_a and "result-screen/1" not in obs_a
    assert SEARCH_SCREEN_SERVICE_CLAUSE.strip()[:30] not in prov_a.requests[0].system
    saved = res_a.run_dir / "workspace" / "search-results"
    assert sorted(p.name for p in saved.iterdir()) == ["SR-001.json", "SR-001.txt"]
    a_block = parse_blocks(
        next(
            e["body"]
            for e in json.loads(
                (sorted((res_a.run_dir / "context").glob("rev-*.json"))[-1]).read_text()
            )["entries"]
            if e["id"] == "s1.obs"
        )
    )[0]
    adv = ScreenJev()
    res_b, prov_b = run(
        tmp_path / "b", [SEARCH, {"action": "fetch", "ids": ["OMITTED"]}, final_for()], "jev", adv
    )
    assert (
        SEARCH_SCREEN_SERVICE_CLAUSE in prov_b.requests[0].system
        and SEARCH_SCREEN_CLAUSE not in prov_b.requests[0].system
    )
    sent = adv.calls[0]["state"]["passages"]
    assert [p["id"] for p in sent] == [p["id"] for p in a_block.passages]  # identical input
    assert [p["text"] for p in sent] == [p["text"] for p in a_block.passages]
    rs = res_b.metrics["result_screen"]
    log = rs["block_log"][0]
    assert log["screened"] and log["omitted"] and "fetch([" in bodies(prov_b.requests[1].user)
    assert len(adv.calls) == 1  # the fetch was not screened
    assert rs["service"]["searches"] == 1 and rs["service"]["fetches"] == 1


def test_fetching_omitted_passages_returns_them_unscreened_and_is_counted(tmp_path: Path) -> None:
    adv = ScreenJev()
    runner, _ = make(tmp_path, [SEARCH], Config(), adv, FakeExecutor())
    from clm_lib.runner import _Run

    r = _Run(
        runner,
        generate(TASK),
        "clm_helpers",
        "unit",
        "",
        True,
        False,
        "off",
        "off",
        False,
        "stated",
        "off",
        "jev",
    )
    r.step = 1
    r.service_step(SEARCH)
    omitted = r.screen_log[-1]["omitted"]
    assert omitted
    r.step = 2
    r.service_step({"action": "fetch", "ids": omitted})
    obs = next(e.body for e in r.store.entries if e.id == "s2.obs")
    assert obs.startswith("<<passages (unscreened):") and all(f"--- {i} " in obs for i in omitted)
    assert r.result_screen_summary()["service"]["fetch_omitted_recovered"] == sorted(omitted)
    assert len(adv.calls) == 1


def test_service_errors_fail_open_and_execute_output_is_never_screened(tmp_path: Path) -> None:
    err = JevError("timeout", "request timed out", retryable=True, billing="unknown")
    adv = ScreenJev(fail_with=[err, err])
    res, prov = run(tmp_path / "e", [SEARCH, final_for()], "jev", adv)
    assert "screening unavailable" in bodies(prov.requests[1].user)
    assert (
        res.metrics["result_screen"]["errors"] == 1
        and len(list((res.run_dir / "jev").glob("*.json"))) == 2
    )
    block = (res.run_dir / "workspace" / "search-results" / "SR-001.txt").read_text()
    out = FakeExecutor(
        outputs=['<<search-result SR-009 query="x" k=5 returned=5 saved=/x>>\n' + block]
    )
    adv2 = ScreenJev()
    res2, _ = run(
        tmp_path / "x",
        [execute("print(open('search-results/SR-001.txt').read())"), final_for()],
        "jev",
        adv2,
        out,
    )
    assert adv2.calls == [] and res2.metrics["result_screen"]["blocks"] == 0


def test_context_edits_still_drive_requests_in_the_service_task(tmp_path: Path) -> None:
    from .test_initiated_review import NoteExecutor

    res, prov = run(
        tmp_path,
        [SEARCH, execute("NOTE:n1:Geneva lodging noted"), final_for()],
        "jev",
        ScreenJev(),
        NoteExecutor(),
    )
    assert '"id": "n1"' in prov.requests[2].user and "<<search-result" not in bodies(
        prov.requests[2].user
    )
    assert res.metrics["counts"]["edits_accepted_changed"] == 1


# ------------------------------------------------------------- the service boundary (Docker)
PROBE = (
    "import os, subprocess\n"
    "hits = [os.path.join(r, f) for r, _, fs in os.walk('/task') for f in fs]\n"
    "print('task_files', sorted(hits))\n"
    "g = subprocess.run(['grep', '-rIl', '--exclude-dir=proc', '--exclude-dir=sys', '--exclude-dir=dev', MARK, '/'], capture_output=True, text=True, timeout=25)\n"
    "print('corpus_hits', [x for x in g.stdout.split() if not x.startswith('/task/workspace/search-results')])\n"
)


@pytest.mark.docker
def test_the_corpus_is_not_reachable_from_the_sandbox(make_runner: Any) -> None:
    inst = generate(TASK)
    mark = inst.truth.corpus[sorted(inst.truth.corpus)[0]].splitlines()[4][:60]  # a section text
    runner, prov = make_runner([execute(f"MARK = {mark!r}\n" + PROBE), final_for()], ceiling=10.0)
    res = runner.run(inst, "clm_helpers")
    obs = bodies(prov.requests[1].user)
    assert "task_files ['/task/fixtures/SERVICE.md', '/task/workspace/context.json']" in obs
    assert "corpus_hits []" in obs
    assert res.metrics["score"]["strict_success"]
