"""W3 register workload (export-gen/2, export-checks/2, register-checks/1; experiment 014
comparison phase): generation, information boundary, code-scorer validity, register-scorer
validity (correct alternatives and targeted faults), assessment timing, and replay attribution.

Library tests only. Faulty solutions and hand-made registers are offline scorer validation and
never enter an agent run; the scripted Jev is a fake on the host.
"""

from __future__ import annotations

import io
import json
import re
import sys
import unittest
from typing import Any

import pytest

from clm_lib import register_score as rs
from clm_lib import w3
from clm_lib.config import Config
from clm_lib.jev_primitive import build_body
from clm_lib.replay import (
    _Scripted,
    answer_table,
    audit_code,
    classify,
    classify_substitution,
    replay_run,
)
from clm_lib.tasks import INSTANCES, generate

from .test_jev_primitive import ScriptedJev, cfg_prim, runner_with
from .w2_solutions import write_core_code
from .w3_solutions import INDEPENDENT_SOURCE, faulty_core, independent_core

DEV = ["w3-dev-1", "w3-dev-2"]
ALL = [n for n in INSTANCES if n.startswith("w3-")]
ADVANCE = {"action": "advance"}
FINAL = {"action": "final", "answer": {"summary": "done"}}
# Field names of the evaluator-only truth (rule texts may use ordinary words such as "withdraws").
EVALUATOR_WORDS = (
    '"effect"',
    "in_force",
    "amended_by",
    "replaced_by",
    "activated",
    "probes",
    "register-checks",
)


def _ns() -> dict[str, Any]:
    ns: dict[str, Any] = {}
    exec(compile(INDEPENDENT_SOURCE, "independent", "exec"), ns)  # our own test code
    return ns


def _indep(
    ns: dict[str, Any], c: dict[str, Any], cfg: dict[str, int], vals: dict[str, Any]
) -> dict[str, Any]:
    try:
        text = ns["export_for"](cfg, vals, *json.loads(json.dumps(c["args"])))
    except PermissionError:
        return {"raises": "PermissionError"}
    if c["mode"] == "csv":
        return {"text": text}
    parsed = json.loads(text)
    return {"json": {k: parsed.get(k) for k in c["keys"]}}


def _host(
    checks: list[dict[str, Any]],
    cfg: dict[str, int],
    vals: dict[str, Any],
    faults: frozenset[str] = frozenset(),
) -> str:
    """Simulated evaluator output for the reference with ``cfg``/``faults`` (host-side)."""
    out = []
    for c in checks:
        obs = w3.observe(c["args"], cfg, vals, c["keys"], faults)
        if c["mode"] == "csv" and "text" not in obs and "raises" not in obs:
            obs = {"not_json_object": "x"}
        passed = obs == c["expect"]
        stale = sorted(m for m, old in c["stale"].items() if not passed and obs == old)
        out.append({"id": c["id"], "observed": obs, "passed": passed, "stale": stale})
    return w3.EVAL_MARKER + json.dumps({"import_error": None, "results": out})


# ------------------------------------------------------------- generation and boundary
def test_generation_is_deterministic_and_has_the_w3_ingredients() -> None:
    for name in ALL:
        a, b = generate(name), generate(name)
        assert a.files == b.files and a.truth.checks == b.truth.checks
    for name in DEV:
        t = generate(name)
        tr = t.truth
        assert len(t.stages) == 7 and t.generator_version == "export-gen/2"
        effects = [e["effect"] for v in tr.effects.values() for e in v]
        for kind in ("replaces", "amends", "activates", "none"):
            assert kind in effects, (name, kind)
        kinds = {i["kind"] for i in tr.items}
        assert {
            "requirement",
            "obligation",
            "exception",
            "withdrawal",
            "decision",
            "information",
        } <= kinds
        cats = {c["category"] for c in tr.checks}
        assert cats == set(w3.CATEGORIES), (name, cats)
        # An amendment of an amendment (FORMAT 3 amends FORMAT 2, which replaced FORMAT 1).
        fmt = [i["id"] for i in tr.items if i["module"] == "FORMAT"]
        assert [e["effect"] for e in tr.effects[fmt[2]]] == ["amends"]
    assert generate("w3-dev-1").truth.schedule != generate("w3-dev-2").truth.schedule


def test_notices_do_not_name_affected_items_or_restate_obligations() -> None:
    for name in ALL:
        t = generate(name)
        for k in range(3, len(t.stages) + 1):
            doc = t.stage_updates[k - 1]
            cited = set(re.findall(r"\bS(\d+)\.\d+", doc))
            assert cited <= {str(k)}, (name, k, cited)  # only the notice's own ids
            if any(
                i["module"] == "FORMAT" and i["version"] == 2 and i["stage"] == k
                for i in t.truth.items
            ):
                assert "exported_by" not in doc  # the obligation is not restated when triggered
            if any(
                i["module"] == "CONF" and i["version"] == 2 and i["stage"] == k
                for i in t.truth.items
            ):
                assert "confidential_count" not in doc


def test_truth_never_reaches_model_visible_material() -> None:
    for name in ALL:
        t = generate(name)
        visible = t.prompt + "".join(t.files.values()) + "".join(t.stage_updates)
        for word in EVALUATOR_WORDS:
            assert word not in visible, (name, word)
        hidden_args = {repr(c["args"]) for c in t.truth.checks}
        assert not any(a in visible for a in hidden_args)
        assert "register-checks" not in visible and "truth" not in t.prompt.lower()


def test_register_truth_for_the_partial_change_example() -> None:
    tr = generate("w3-dev-1").truth
    ids = {(i["module"], i["version"]): i["id"] for i in tr.items if i["module"]}
    scope1, scope2 = ids[("SCOPE", 1)], ids[("SCOPE", 2)]
    s3 = tr.register[3]
    assert s3[scope1]["in_force"] and s3[scope1]["amended_by"] == [scope2]
    assert not tr.register[3][ids[("FORMAT", 1)]]["in_force"]  # replaced by JSON
    assert tr.register[3][ids[("BY", 1)]]["activated"]  # triggered, not restated
    s7 = tr.register[7]
    assert not s7[ids[("REGION", 1)]]["in_force"] and not s7[ids[("REGION", 2)]]["in_force"]
    assert s7[ids[("FORMAT", 2)]]["in_force"] and s7[ids[("FORMAT", 2)]]["amended_by"] == [
        ids[("FORMAT", 3)]
    ]


# ------------------------------------------------------------- code scorer
def test_reference_matches_independent_implementation() -> None:
    ns = _ns()
    for name in ALL:
        tr = generate(name).truth
        for c in tr.checks:
            assert _indep(ns, c, tr.final_config, tr.values) == c["expect"], (name, c["id"])
        for stage, checks in tr.snapshot_checks.items():
            cfg = w3.config_at(tr.schedule, stage)
            for c in checks:
                assert _indep(ns, c, cfg, tr.values) == c["expect"], (name, c["id"])


def test_visible_tests_pass_on_the_independent_implementation() -> None:
    ns = _ns()
    for name in DEV:
        inst = generate(name)
        tr = inst.truth
        for k, st in enumerate(inst.stages, start=1):
            cfg = w3.config_at(tr.schedule, k)
            mod = type(json)("exporter")
            mod.export_records = lambda r, u, p, cfg=cfg, vals=tr.values: ns["export_for"](
                cfg, vals, r, u, p
            )  # type: ignore[attr-defined]
            scope: dict[str, Any] = {"__name__": "visible"}
            sys.modules["exporter"] = mod
            try:
                exec(compile(st["current-tests/test_stage.py"], "visible", "exec"), scope)
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(scope["TestStage"])
                result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
                assert result.wasSuccessful(), (name, k, result.failures[:1])
            finally:
                del sys.modules["exporter"]


def test_code_scorer_detects_each_class_of_mistake() -> None:
    for name in DEV:
        tr = generate(name).truth
        final, vals = tr.final_config, tr.values
        ok = w3.score_export(tr, _host(tr.checks, final, vals), True)
        assert ok.outcome == "correct" and ok.strict_success
        for m, cat in tr.categories.items():
            if m in final and m != "FORMAT" and cat != "replaced":
                s = w3.score_export(tr, _host(tr.checks, w3._with(final, m, None), vals), True)
                cat_or_kept = cat if cat != "amended" else "kept_part"
                assert s.by_category[cat_or_kept]["passed"] < s.by_category[cat_or_kept]["total"], (
                    name,
                    "forget",
                    m,
                )
            prev = w3.previous_state(tr.schedule, m)
            if prev is not None:
                s = w3.score_export(tr, _host(tr.checks, w3._with(final, m, prev), vals), True)
                assert s.stale_modules.get(m, 0) >= 2, (name, "stale", m, s.stale_modules)
            if cat == "amended" or (m == "BY" and len(w3.history_of(tr.schedule)[m]) > 1):
                s = w3.score_export(
                    tr, _host(tr.checks, final, vals, frozenset({f"{m}:replace"})), True
                )
                assert s.by_category["kept_part"]["passed"] < s.by_category["kept_part"]["total"], (
                    name,
                    m,
                )
        s = w3.score_export(tr, _host(tr.checks, final, vals, frozenset({"CONF:none"})), True)
        assert s.by_category["exception"]["passed"] < s.by_category["exception"]["total"]
        for ob in ("BY", "CC"):  # a lost obligation fails only obligation checks
            s = w3.score_export(tr, _host(tr.checks, w3._with(final, ob, None), vals), True)
            failed = {c for c, v in s.by_category.items() if v["passed"] < v["total"]}
            assert failed <= {"deferred", "amended", "kept_part"} and "deferred" in failed, (
                name,
                ob,
                failed,
            )


def _script(core: str, n: int) -> list[Any]:
    return [
        {"action": "execute", "thought": "", "code": write_core_code(core)},
        *[ADVANCE] * (n - 1),
        FINAL,
    ]


@pytest.mark.docker
@pytest.mark.parametrize("name", DEV)
def test_independent_solution_is_strictly_successful_in_sandbox(
    make_runner: Any, name: str
) -> None:
    inst = generate(name)
    runner, _ = make_runner(
        _script(independent_core(inst.truth.final_config, inst.truth.values), 7)
    )
    res = runner.run(inst, "clm")
    s = res.score
    assert res.status == "completed" and s.outcome == "correct" and s.strict_success, (
        s.failed_modules
    )
    score = json.loads((res.run_dir / "evaluator" / "score.json").read_text())
    assert score["scorer_version"] == "export-checks/2"
    assert sorted(s.snapshots) == [str(k) for k in range(1, 7)]


@pytest.mark.docker
@pytest.mark.parametrize(
    ("label", "drop", "faults", "category"),
    [
        ("amendment applied as a replacement", None, ("SCOPE:replace",), "kept_part"),
        ("exception lost", None, ("CONF:none",), "exception"),
        ("deferred obligation lost", "CC", (), "deferred"),
        ("unchanged rule dropped", "SINCE", (), "retained"),
    ],
)
def test_faulty_solutions_are_detected_in_sandbox(
    make_runner: Any, label: str, drop: str | None, faults: tuple[str, ...], category: str
) -> None:
    inst = generate("w3-dev-1")
    cfg = w3._with(inst.truth.final_config, drop, None) if drop else dict(inst.truth.final_config)
    runner, _ = make_runner(_script(faulty_core(cfg, inst.truth.values, frozenset(faults)), 7))
    s = runner.run(inst, "clm").score
    assert s.outcome == "failed_checks", label
    assert s.by_category[category]["passed"] < s.by_category[category]["total"], label


# ------------------------------------------------------------- register scorer
def _text(tr: Any) -> dict[str, str]:
    return {i["id"]: i["text"] for i in tr.items}


def ideal_registers(tr: Any, stage: int) -> dict[str, list[dict[str, str]]]:
    """Several correct representations of the register after ``stage`` (built from truth)."""
    reg, text = tr.register[stage], _text(tr)
    cur = [i for i, t in reg.items() if t["in_force"]]
    old = [i for i, t in reg.items() if not t["in_force"] and t["kind"] not in ("information",)]
    per_item = [{"id": f"r{i}", "role": "note", "body": f"CURRENT [{i}] {text[i]}"} for i in cur]
    per_item += [{"id": f"h{i}", "role": "note", "body": f"HISTORY [{i}] {text[i]}"} for i in old]
    modules: dict[str, list[str]] = {}
    for i in cur:
        modules.setdefault(reg[i]["module"], []).append(i)
    merged = [
        {
            "id": f"m{m}",
            "role": "note",
            "body": f"CURRENT [{', '.join(ids)}] " + " ".join(text[i] for i in ids),
        }
        for m, ids in modules.items()
    ]
    merged.append(
        {
            "id": "hist",
            "role": "note",
            "body": "HISTORY [" + ", ".join(old) + "] " + " ".join(text[i] for i in old),
        }
    )
    one_note = f"Register after stage {stage}\nCURRENT\n"
    one_note += "\n".join(
        f"- [{', '.join(ids)}] " + " ".join(text[i] for i in ids) for ids in modules.values()
    )
    one_note += "\nHISTORY\n" + "\n".join(f"- [{i}] {text[i]}" for i in old)
    return {
        "one entry per item": per_item,
        "merged per rule": merged,
        "one note with sections": [{"id": "reg", "role": "note", "body": one_note}],
    }


def test_correct_alternative_registers_score_without_errors() -> None:
    for name in ALL:
        tr = generate(name).truth
        for stage in tr.register:
            for label, entries in ideal_registers(tr, stage).items():
                s = rs.score_stage(entries, tr.register[stage], tr.probes, stage).to_dict()
                bad = {k: v for k, v in s["counts"].items() if v and k not in rs.OK}
                assert not bad, (
                    name,
                    stage,
                    label,
                    bad,
                    {k: v for k, v in s["by_item"].items() if set(v) - set(rs.OK)},
                )


def test_runtime_entries_and_unlabelled_text_are_not_register_items() -> None:
    entries = [
        {"id": "s3.obs", "role": "observation", "body": "CURRENT [S1.1] copied stage text"},
        {
            "id": "n1",
            "role": "note",
            "body": "notes: S1.1 is about the format\nCurrent format: JSON [S3.1]\nCurrent: [S3.1] JSON",
        },
    ]
    items = rs.parse_register(entries)
    assert [(i.label, sorted(i.ids)) for i in items] == [("CURRENT", ["S3.1"])]


def _ids(tr: Any) -> dict[tuple[str, int], str]:
    return {(i["module"], i["version"]): i["id"] for i in tr.items if i["module"]}


def _decision(tr: Any) -> str:
    return next(i["id"] for i in tr.items if i["kind"] == "decision")


def test_register_scorer_detects_targeted_faults() -> None:
    tr = generate("w3-dev-1").truth
    ids, text, stage = _ids(tr), _text(tr), 7
    good = ideal_registers(tr, stage)["one entry per item"]

    def score(entries: list[dict[str, str]]) -> dict[str, list[str]]:
        return rs.score_stage(entries, tr.register[stage], tr.probes, stage).by_item

    def without(*iids: str) -> list[dict[str, str]]:
        return [e for e in good if not any(f"[{i}]" in e["body"] for i in iids)]

    viewer1, _viewer2 = ids[("VIEWER", 1)], ids[("VIEWER", 2)]
    scope1, scope2 = ids[("SCOPE", 1)], ids[("SCOPE", 2)]
    by1, conf1, conf2 = ids[("BY", 1)], ids[("CONF", 1)], ids[("CONF", 2)]
    since1, dec = ids[("SINCE", 1)], _decision(tr)
    cases = {
        # a replaced rule still presented as current
        viewer1: (
            [
                *without(viewer1),
                {"id": "x", "role": "note", "body": f"CURRENT [{viewer1}] {text[viewer1]}"},
            ],
            "stale_superseded",
        ),
        # a partial amendment missing: the base is current, its amendment is absent
        scope1: (without(scope2), "stale_unamended"),
        # an amended rule retired as if replaced
        scope1 + "r": (
            [
                *without(scope1),
                {"id": "x", "role": "note", "body": f"HISTORY [{scope1}] {text[scope1]}"},
            ],
            "incorrect_retirement",
        ),
        # an obligation filed as done
        by1: (
            [
                *without(by1),
                {
                    "id": "x",
                    "role": "note",
                    "body": f"HISTORY [{by1}] exported_by obligation fulfilled",
                },
            ],
            "lost_obligation",
        ),
        # the confidential exception dropped
        conf1: (without(conf1, conf2), "lost_exception"),
        # a decision record presented as a current requirement
        dec: (
            [*without(dec), {"id": "x", "role": "note", "body": f"CURRENT [{dec}] {text[dec]}"}],
            "history_as_current",
        ),
        # cited but stated without the item's distinctive content
        conf2: (
            [
                *without(conf2),
                {
                    "id": "x",
                    "role": "note",
                    "body": f"CURRENT [{conf2}] another purpose is allowed too",
                },
            ],
            "content_missing",
        ),
        # an unchanged rule left out
        since1: (without(since1), "omitted"),
    }
    for key, (entries, expected) in cases.items():
        got = score(entries)[key.rstrip("r")]
        assert expected in got, (key, expected, got)
    assert all(set(v) <= set(rs.OK) for v in score(good).values())


def test_viewer_replacement_and_withdrawal_history_alternatives() -> None:
    """A CURRENT item that cites a replaced rule together with its successor is not stale."""
    tr = generate("w3-dev-1").truth
    ids, text = _ids(tr), _text(tr)
    good = ideal_registers(tr, 7)["one entry per item"]
    v1, v2 = ids[("VIEWER", 1)], ids[("VIEWER", 2)]
    r1, r2, r0 = ids[("REGION", 1)], ids[("REGION", 2)], ids[("REGION", 0)]
    alt = [e for e in good if not any(f"[{i}]" in e["body"] for i in (v1, v2, r1, r2))]
    alt += [
        {
            "id": "v",
            "role": "note",
            "body": f"CURRENT [{v2}, {v1}] {text[v2]} (no longer a PermissionError)",
        },
        {
            "id": "r",
            "role": "note",
            "body": f"CURRENT [{r0}] no residency restriction now; HISTORY of [{r1}, {r2}] below",
        },
        {"id": "rh", "role": "note", "body": f"HISTORY [{r1}, {r2}] residency rule until stage 7"},
    ]
    s = rs.score_stage(alt, tr.register[7], tr.probes, 7).by_item
    assert all(set(v) <= set(rs.OK) for v in s.values()), {
        k: v for k, v in s.items() if set(v) - set(rs.OK)
    }


# ------------------------------------------------------------- assessment timing (runner)
def _register_code(tr: Any, stage: int, stale_from: int | None = None) -> str:
    use = stage if stale_from is None else min(stage, stale_from)
    entries = ideal_registers(tr, use)["one entry per item"]
    return (
        "import json\nc = json.load(open('context.json'))\n"
        f"c['entries'] = [e for e in c['entries'] if not e['id'].startswith(('r', 'h', 's'))] + {entries!r}\n"
        "json.dump(c, open('context.json', 'w'))\nprint('register written')\n"
    )


@pytest.mark.docker
def test_register_is_assessed_after_each_stage_was_processed(make_runner: Any) -> None:
    inst = generate("w3-smoke-1")
    tr = inst.truth
    n = len(inst.stages)
    script: list[Any] = []
    for k in range(1, n + 1):
        # stale_from=3: the register is not updated after stage 3 (stages 4-5 then show errors)
        script.append(
            {"action": "execute", "thought": "", "code": _register_code(tr, k, stale_from=3)}
        )
        script.append(ADVANCE if k < n else FINAL)
    cfg = Config()
    cfg.limits.context_budget_tokens = 12000  # as configs/exp014c.toml
    runner, _ = make_runner(script, config=cfg)
    res = runner.run(inst, "clm_helpers")
    assert res.status == "completed", res.reason
    reg = json.loads((res.run_dir / "evaluator" / "register_score.json").read_text())
    assert [s["stage"] for s in reg["stages"]] == list(range(1, n + 1))
    assert [s["point"] for s in reg["stages"]] == ["release"] * (n - 1) + ["final"]
    by_stage = {s["stage"]: s for s in reg["stages"]}
    assert all(by_stage[k]["harm"] == 0 and by_stage[k]["incomplete"] == 0 for k in (1, 2, 3))
    assert by_stage[4]["harm"] > 0 and by_stage[5]["harm"] > 0
    assert res.metrics["register"]["missing_stages"] == []
    pre = sorted(p.name for p in (res.run_dir / "workspace_pre").iterdir())
    assert pre and all(p.startswith("step-") for p in pre)
    sent = "".join(p.read_text() for p in (res.run_dir / "requests").iterdir())
    for word in ("in_force", "amended_by", "activates", "register-checks", "probes"):
        assert word not in sent, word  # evaluator truth never reaches a request


# ------------------------------------------------------------- replay attribution
HELPER = r"""
import json, re
import clm_jev

def update(stage):
    notice = open(f"/task/fixtures/stage-{stage}/REQUIREMENTS.md").read()
    c = json.load(open("context.json"))
    reg = [e for e in c["entries"] if e["body"].startswith("CURRENT")]
    r = clm_jev.judge("Does this notice change the requirement in this entry?", {e["id"]: e["body"] for e in reg}, background=notice[:3900])
    if r["status"] != "ok":
        print("no answers; register left as it was")
        hit = []
    else:
        hit = [k for k, p in r["answers"].items() if p is not None and p > 0.5]
    for e in c["entries"]:
        if e["id"] in hit:
            e["body"] = MODE(e["body"])
    new = [{"id": "r" + i, "role": "note", "body": f"CURRENT [{i}] {t}"} for i, t in re.findall(r"- \[(S\d+\.\d+)\] (.*)", notice)]
    c["entries"] = [e for e in c["entries"] if not e["id"].startswith("s")] + new
    json.dump(c, open("context.json", "w"))
"""
MODES = {
    "substantive": "lambda b: 'HISTORY' + b[len('CURRENT'):]",
    "marker": "lambda b: b + ' (REVIEW)'",
}


def _replay_script(mode: str, n: int) -> list[Any]:
    helper = HELPER.replace("MODE(", f"({MODES[mode]})(")
    setup = f"open('ctxhelper.py', 'w').write({helper!r})\nimport ctxhelper\nctxhelper.update(1)\n"
    script: list[Any] = [{"action": "execute", "thought": "", "code": setup}, ADVANCE]
    for k in range(2, n + 1):
        script.append(
            {
                "action": "execute",
                "thought": "",
                "code": f"import ctxhelper\nctxhelper.update({k})\n",
            }
        )
        script.append(ADVANCE if k < n else FINAL)
    return script


@pytest.mark.docker
@pytest.mark.parametrize(
    ("mode", "expected"), [("substantive", "substantive"), ("marker", "marker_only")]
)
def test_replay_attributes_register_changes_to_jev_answers(
    make_runner: Any, tmp_path: Any, mode: str, expected: str
) -> None:
    inst = generate("w3-smoke-1")
    adv = ScriptedJev(topic="format")
    runner, _ = runner_with(
        make_runner,
        _replay_script(mode, len(inst.stages)),
        adv,
        cfg_prim(max_calls_per_execution=8),
    )
    res = runner.run(inst, "clm_helpers")
    assert res.metrics["jev_primitive"]["calls"] >= 2
    before = {p: p.read_bytes() for p in res.run_dir.rglob("*") if p.is_file()}
    out = replay_run(res.run_dir, tmp_path / "replay")
    assert {p: p.read_bytes() for p in res.run_dir.rglob("*") if p.is_file()} == before  # untouched
    classes = [s["dependence"]["class"] for s in out["steps"]]
    assert all(s["reproduced"] for s in out["steps"]), out["steps"]
    assert expected in classes, classes
    assert all(c in (expected, "no_register_dependence") for c in classes), classes
    dep = next(s["dependence"] for s in out["steps"] if s["dependence"]["class"] == expected)
    assert dep["edits_independent_of_answers"]["removed_either_way"]  # transcript removal
    # Valid substituted answers (same shape, keys, matching) are logged and classified.
    subs = [x["class"] for st in out["steps"] for x in st["substitutions"].values()]
    assert expected in subs, subs
    logged = out["steps"][0]["variants"]["inverted"]["substitution_log"]
    assert logged and all(
        i["served"] == round(1 - i["recorded"], 6) for r in logged for i in r["items"]
    )
    if expected == "substantive":
        assert out["stages_substantive_under_valid_substitution"]
    else:
        assert out["stages_substantive_under_valid_substitution"] == []


def test_replay_classifies_a_crash_without_answers_separately() -> None:
    pre = [{"id": "r1", "role": "note", "body": "CURRENT [S1.1] csv"}]
    rec = {
        "exit_code": 0,
        "timed_out": False,
        "entries": [{"id": "r1", "role": "note", "body": "HISTORY [S1.1] csv"}],
    }
    crash = {"exit_code": 1, "timed_out": False, "entries": pre}
    early = {"exit_code": 0, "timed_out": False, "entries": pre}
    assert classify(pre, rec, crash)["class"] == "crash_without_answers"
    assert classify(pre, rec, early)["class"] == "no_edit_without_answers"
    same = {
        "exit_code": 0,
        "timed_out": False,
        "entries": rec["entries"] + [{"id": "s9.obs", "role": "observation", "body": "x"}],
    }
    assert classify(pre, rec, same)["class"] == "no_register_dependence"


def _exchange(question: str, items: dict[str, float]) -> dict[str, Any]:
    req = {
        "question": question,
        "background": "B",
        "items": [{"key": k, "text": "t" + k} for k in items],
    }
    return {"request": req, "answers": dict(items), "status": "ok"}


def _body(question: str, keys: list[str]) -> dict[str, Any]:
    req = {
        "version": "jev-primitive/1",
        "id": "ab12cd34",
        "question": question,
        "background": "B",
        "items": [{"key": k, "text": "t" + k} for k in keys],
    }
    return build_body(req, "m")


def test_answers_are_matched_per_item_and_substitutions_are_valid() -> None:
    table = answer_table(
        [_exchange("Q1", {"a": 0.9, "b": 0.2, "c": 0.4}), _exchange("Q2", {"a": 0.1})]
    )
    assert table[next(k for k in table if '"Q1"' in k and '"a"' in k)]["permuted"] == 0.2  # rotated
    assert table[next(k for k in table if '"Q2"' in k)]["permuted"] == 0.1  # one item: unchanged
    # A recombined request of recorded items is answerable item by item...
    sub = _Scripted("inverted", table)
    out, err = sub(_body("Q1", ["c", "a"]))
    assert err is None and out.probabilities == {"i0": 0.6, "i1": 0.1}
    assert sub.log[0]["items"] == [
        {"key": "c", "recorded": 0.4, "served": 0.6},
        {"key": "a", "recorded": 0.9, "served": 0.1},
    ]
    # ...but a question the original run never asked about an item has no valid answer.
    out, err = sub(_body("Q2", ["b"]))
    assert out is None and sub.unmatched == 1


def test_substitution_classes_keep_failures_apart_from_selection() -> None:
    pre = [{"id": "r1", "role": "note", "body": "CURRENT [S1.1] csv"}]
    rec = {
        "exit_code": 0,
        "timed_out": False,
        "entries": [{"id": "r1", "role": "note", "body": "HISTORY [S1.1] csv"}],
    }

    def sub(**kw: Any) -> dict[str, Any]:
        return {
            "exit_code": 0,
            "timed_out": False,
            "entries": pre,
            "unmatched": 0,
            "answers_changed": True,
            **kw,
        }

    assert (
        classify_substitution(pre, rec, sub(unmatched=1))["class"] == "unsupported_counterfactual"
    )
    assert (
        classify_substitution(pre, rec, sub(answers_changed=False))["class"]
        == "substitution_identical"
    )
    assert classify_substitution(pre, rec, sub(exit_code=1))["class"] == "crash_under_substitution"
    assert classify_substitution(pre, rec, sub())["class"] == "no_edit_under_substitution"
    moved = [
        {"id": "r1", "role": "note", "body": "CURRENT [S1.1] csv"},
        {"id": "r2", "role": "note", "body": "CURRENT [S3.1] json"},
    ]
    assert classify_substitution(pre, rec, sub(entries=moved))["class"] == "substantive"


def test_code_audit_flags_register_literals_and_existing_item_ids() -> None:
    caller = "regmgr.apply(c, [], {'reg.S1.2': 'HISTORY [S1.2] replaced by S5.2'})\nupdate(5, notes={'S5.1': 'x'})"
    a = audit_code(caller, stage=5)
    assert a["register_literals"] == 1 and a["earlier_item_ids"] == ["S1.2"]  # S5.x are new items
    assert audit_code("import h\nh.update(5)\n", stage=5) == {
        "register_literals": 0,
        "earlier_item_ids": [],
    }


def test_last_request_substitution_serves_recorded_answers_before_it() -> None:
    """Post-hoc diagnostic: only the execution's last request gets substituted values."""
    table = answer_table([_exchange("Q1", {"a": 0.9, "b": 0.2}), _exchange("Q2", {"a": 0.3})])
    sub = _Scripted("inverted_last", table, substitute_from=2)
    first, _ = sub(_body("Q1", ["a", "b"]))
    second, _ = sub(_body("Q2", ["a"]))
    assert first.probabilities == {"i0": 0.9, "i1": 0.2}  # recorded
    assert second.probabilities == {"i0": 0.7}  # inverted
