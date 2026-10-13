"""jev-primitive/1 (experiment 014, planned): Jev called from model-written code mid-execution.

Mechanism tests only, in the real Docker sandbox with a SCRIPTED fake Jev on the host and a
scripted model. They show the path works; they are not evidence that a real model writes useful
helpers or that Jev improves outcomes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from clm_lib.config import Config, load_config
from clm_lib.executor import DockerExecutor
from clm_lib.jev import JevError, JevPrice, parse_response
from clm_lib.jev_primitive import PrimitiveLimits, build_body, validate
from clm_lib.prompts import JEV_PRIMITIVE_CLAUSE
from clm_lib.runner import parse_action
from clm_lib.tasks import generate

from .conftest import execute, working_block

REPO = Path(__file__).resolve().parents[1]
FINAL = {
    "action": "final",
    "answer": {"root_cause": "x: y", "required_value": "0", "remedy": "x: y", "evidence_refs": []},
}


@dataclass
class ScriptedJev:
    """SCRIPTED fake Jev: probability 0.95 when the item mentions ``topic``, else 0.05."""

    topic: str = "format"
    fail_with: list[JevError] = field(default_factory=list)
    delay_s: float = 0.0
    name: str = "fake"
    model: str = "scripted-fake-jev"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def call(self, body: dict[str, Any]) -> tuple[Any, float]:
        self.calls.append(body)
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail_with:
            raise self.fail_with.pop(0)
        ans = {
            k: {
                "type": "noul",
                "noul": 0.95 if self.topic in q["instructions"]["item"].lower() else 0.05,
            }
            for k, q in body["questions"].items()
        }
        usage = {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": len(ans)}
        return parse_response(
            {"model": self.model, "answers": ans, "usage": usage}, list(body["questions"])
        ), 0.01


SETUP = r"""
import json
ctx = {"format": "clm-context/v1", "entries": [
    {"id": "n1", "role": "note", "body": "R1 (current): exports are written as CSV with a header row."},
    {"id": "n2", "role": "note", "body": "R2 (current): viewers may not export; admins export everything."},
    {"id": "n3", "role": "note", "body": "R3 (current): archived records are excluded unless include_archived."},
    {"id": "h1", "role": "observation", "body": "HISTORICAL (keep): round-1 log showed 3 timeouts at 10:02."},
    {"id": "u1", "role": "note", "body": "UPDATE: the export format changes from CSV to JSON; other rules unchanged."},
]}
json.dump(ctx, open("context.json", "w"))
open("ctxhelp.py", "w").write(HELPER)
print("setup done")
"""
# The model's own helper: it chooses the entries, the question and what to do with the answers.
HELPER = r"""
import json
import clm_jev

def revise(update_id):
    ctx = json.load(open("context.json"))
    by = {e["id"]: e for e in ctx["entries"]}
    update = by[update_id]["body"]
    reqs = {e["id"]: e["body"] for e in ctx["entries"] if e["role"] == "note" and e["body"].startswith("R")}
    r = clm_jev.judge("Does this update change the requirement stated in this entry?", reqs, background=update)
    if r["status"] != "ok":
        print("jev unavailable:", r["error"], "- context left unchanged")
        return
    changed = [k for k, p in r["answers"].items() if p is not None and p >= 0.5]
    for k in changed:
        by[k]["body"] = by[k]["body"].replace("(current)", "(superseded)") + " -> see " + update_id
    ctx["entries"] = [e for e in ctx["entries"] if e["id"] != update_id] + [
        {"id": update_id + ".applied", "role": "note", "body": "Applied: " + update + " Revised: " + (", ".join(changed) or "none")}
    ]
    json.dump(ctx, open("context.json", "w"))
    print("revised", changed, "kept", sorted(set(reqs) - set(changed)))
"""
SETUP_CODE = "HELPER = " + repr(HELPER) + "\n" + SETUP
USE = "import ctxhelp\nctxhelp.revise('u1')\n"
SECOND = (
    "import json, ctxhelp\nc = json.load(open('context.json'))\n"
    "c['entries'].append({'id': 'u2', 'role': 'note', 'body': 'UPDATE: viewers may now export their own records; permissions otherwise unchanged.'})\n"
    "json.dump(c, open('context.json', 'w'))\nctxhelp.revise('u2')\n"
)


def cfg_prim(**lim: int) -> Config:
    c = Config()
    c.jev_primitive.enabled = True
    for k, v in lim.items():
        setattr(c.jev_primitive, k, v)
    return c


def runner_with(
    make_runner: Any, script: list[Any], adv: Any, cfg: Config, executor: Any = None
) -> tuple[Any, Any]:
    runner, prov = make_runner(script, executor=executor, ceiling=10.0, config=cfg)
    runner.jev, runner.jev_price = adv, JevPrice("scripted-fake-jev", 0.042, 0.0, "test", "test")
    return runner, prov


def bodies(user: str) -> dict[str, str]:
    return {
        json.loads(x)["id"]: json.loads(x)["body"]
        for x in working_block(user).splitlines()
        if x.strip()
    }


# ------------------------------------------------------------- protocol units
def test_request_validation_and_body_carry_only_code_supplied_text() -> None:
    lim = PrimitiveLimits()
    req = {
        "version": "jev-primitive/1",
        "id": "ab12cd34",
        "question": "Q?",
        "background": "B",
        "items": [{"key": "n1", "text": "T"}],
    }
    ok, _ = validate(json.dumps(req).encode(), lim)
    assert ok
    body = build_body(ok, "m")
    assert body["state"] == {"background": "B", "items": [{"key": "n1", "text": "T"}]}
    assert body["questions"]["i0"]["instructions"]["item"] == "T"
    for bad in (
        {**req, "path": "/etc/passwd"},  # no file-reading or extra fields
        {**req, "items": []},
        {**req, "items": [{"key": "a", "text": "x"}, {"key": "a", "text": "y"}]},
        {**req, "question": "q" * 501},
        {**req, "version": "other"},
    ):
        assert validate(json.dumps(bad).encode(), lim)[0] is None
    assert validate(b"x" * 100_001, lim)[0] is None


def test_off_by_default_and_historical_configs_unchanged(make_runner: Any) -> None:
    for p in [*sorted((REPO / "configs").glob("exp*.toml")), REPO / "configs" / "default.toml"]:
        assert load_config(p).jev_primitive.enabled is False, p.name
    runner, prov = make_runner([FINAL], executor=_NoRun(), ceiling=10.0)
    res = runner.run(generate("dev"), "clm_helpers")
    assert JEV_PRIMITIVE_CLAUSE.strip()[:20] not in prov.requests[0].system
    assert not (res.run_dir / "workspace" / "clm_jev.py").exists()
    assert res.metrics["jev_primitive"]["enabled"] is False
    with pytest.raises(ValueError, match="no Jev advisor"):
        runner.run(generate("dev"), "clm_helpers", jev_primitive=True)
    assert parse_action(json.dumps(execute("import clm_jev")), 1000)[0]  # no new action exists


class _NoRun:
    isolation = "none"

    def describe(self) -> dict[str, object]:
        return {"kind": "none"}

    def run(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("not executed")


# ------------------------------------------------------------- the complete path (Docker)
@pytest.mark.docker
def test_helper_calls_jev_mid_execution_and_its_edit_drives_the_next_request(
    make_runner: Any,
) -> None:
    adv = ScriptedJev(topic="csv")
    script = [execute(SETUP_CODE), execute(USE), FINAL]
    runner, prov = runner_with(make_runner, script, adv, cfg_prim())
    res = runner.run(generate("dev"), "clm_helpers")
    assert "-jprim-" in res.run_id and JEV_PRIMITIVE_CLAUSE.split("{")[0] in prov.requests[0].system
    # One model call per scripted action: the Jev exchange added none.
    assert len(prov.requests) == 3 and len(adv.calls) == 1
    sent = adv.calls[0]
    assert sent["state"]["background"].startswith("UPDATE: the export format")
    assert [i["key"] for i in sent["state"]["items"]] == ["n1", "n2", "n3"]  # the helper's choice
    ctx = bodies(prov.requests[2].user)
    assert ctx["n1"].startswith("R1 (superseded)") and ctx["n1"].endswith("-> see u1")
    assert ctx["n2"].startswith("R2 (current)") and ctx["n3"].startswith("R3 (current)")
    assert ctx["h1"].startswith("HISTORICAL (keep)") and "u1" not in ctx and "u1.applied" in ctx
    assert "revised ['n1'] kept ['n2', 'n3']" in ctx["s2.obs"]
    ev = [json.loads(x) for x in (res.run_dir / "events.jsonl").read_text().splitlines()]
    order = [
        e["event"]
        for e in ev
        if e.get("step") == 2
        and e["event"] in ("execution_start", "jev_primitive", "execution_result", "context_edit")
    ]
    assert order == ["execution_start", "jev_primitive", "execution_result", "context_edit"]
    jp = res.metrics["jev_primitive"]
    assert jp["calls"] == 1 and jp["by_status"] == {"ok": 1} and jp["steps_with_calls"] == [2]
    rec = json.loads((res.run_dir / "jev_primitive" / "0001.json").read_text())
    assert (
        rec["request"]["question"].startswith("Does this update")
        and rec["response"]["answers"]["n1"] == 0.95
    )
    assert len(list((res.run_dir / "jev").glob("*.json"))) == 1  # the recorded attempt
    assert res.metrics["cost"]["by_provider"]["typesafe"] > 0  # charged through the ledger
    assert not list((res.run_dir / "workspace" / ".clm-jev").iterdir())  # mailbox emptied


@pytest.mark.docker
def test_saved_helper_is_reused_and_different_answers_take_different_branches(
    make_runner: Any,
) -> None:
    adv = ScriptedJev(topic="csv")
    script = [execute(SETUP_CODE), execute(USE), execute(SECOND), FINAL]
    runner, prov = runner_with(make_runner, script, adv, cfg_prim())
    orig = adv.call

    def call(body: dict[str, Any]) -> Any:  # the second update concerns permissions
        adv.topic = "viewers" if len(adv.calls) >= 1 else "csv"
        return orig(body)

    adv.call = call  # type: ignore[method-assign]
    res = runner.run(generate("dev"), "clm_helpers")
    ctx = bodies(prov.requests[3].user)
    assert ctx["n1"].startswith("R1 (superseded)") and ctx["n2"].startswith("R2 (superseded)")
    assert ctx["n3"].startswith("R3 (current)") and ctx["h1"].startswith("HISTORICAL")
    assert "revised ['n2'] kept ['n1', 'n3']" in ctx["s3.obs"]
    assert (
        res.metrics["jev_primitive"]["calls"] == 2
        and res.metrics["counts"]["edits_accepted_changed"] == 3
    )


@pytest.mark.docker
def test_service_errors_and_limits_are_explicit(make_runner: Any) -> None:
    err = JevError("auth", "unauthorized", retryable=False, billing="none")
    runner, prov = runner_with(
        make_runner,
        [execute(SETUP_CODE), execute(USE), FINAL],
        ScriptedJev(fail_with=[err]),
        cfg_prim(),
    )
    res = runner.run(generate("dev"), "clm_helpers")
    ctx = bodies(prov.requests[2].user)
    assert "jev unavailable: auth: unauthorized - context left unchanged" in ctx["s2.obs"]
    assert ctx["n1"].startswith("R1 (current)") and res.metrics["jev_primitive"]["by_status"] == {
        "error": 1
    }
    twice = "import clm_jev\nfor i in range(2):\n    print(clm_jev.judge('Q?', {'a': 'x'})['status'], end=' ')\n"
    runner, prov = runner_with(
        make_runner, [execute(twice), FINAL], ScriptedJev(), cfg_prim(max_calls_per_execution=1)
    )
    res = runner.run(generate("dev"), "clm_helpers")
    assert "ok error" in bodies(prov.requests[1].user)["s1.obs"]
    assert res.metrics["jev_primitive"]["by_status"] == {"ok": 1, "limit_execution": 1}
    bad = "import json, os\nos.makedirs('.clm-jev', exist_ok=True)\njson.dump({'version': 'jev-primitive/1', 'id': 'abcdef12', 'question': 'q', 'items': [], 'path': '/etc'}, open('.clm-jev/req-abcdef12.json', 'w'))\nimport time; time.sleep(1)\nprint(open('.clm-jev/resp-abcdef12.json').read()[:80])\n"
    runner, prov = runner_with(
        make_runner, [execute(bad), FINAL], adv2 := ScriptedJev(), cfg_prim()
    )
    res = runner.run(generate("dev"), "clm_helpers")
    assert "invalid request" in bodies(prov.requests[1].user)["s1.obs"] and adv2.calls == []


@pytest.mark.docker
def test_timeout_cancellation_and_unanswered_requests(make_runner: Any) -> None:
    slow = ScriptedJev(delay_s=4.0)
    wait = "import clm_jev\nprint(clm_jev.judge('Q?', {'a': 'x'}, timeout=60))\n"
    runner, _ = runner_with(
        make_runner, [execute(wait), FINAL], slow, cfg_prim(), DockerExecutor(timeout_s=2)
    )
    res = runner.run(generate("dev"), "clm_helpers")
    ev = [json.loads(x) for x in (res.run_dir / "events.jsonl").read_text().splitlines()]
    result = next(e for e in ev if e["event"] == "execution_result")
    assert result["timed_out"] and res.metrics["jev_primitive"]["calls"] == 1  # charged, recorded
    assert not list((res.run_dir / "workspace" / ".clm-jev").iterdir())
    fire = "import json, os\nos.makedirs('.clm-jev', exist_ok=True)\njson.dump({'version': 'jev-primitive/1', 'id': 'feedbeef', 'question': 'q', 'items': [{'key': 'a', 'text': 'x'}]}, open('.clm-jev/req-feedbeef.json', 'w'))\n"
    adv = ScriptedJev()
    runner, _ = runner_with(make_runner, [execute(fire), FINAL], adv, cfg_prim())
    res = runner.run(generate("dev"), "clm_helpers")
    jp = res.metrics["jev_primitive"]
    assert (
        len(adv.calls) + len(jp["unanswered_at_exit"]) == 1
    )  # answered in flight, or left unprocessed
    if jp["unanswered_at_exit"]:
        assert jp["calls"] == 0
