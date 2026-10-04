"""Helper-use verification and content-level edit evidence (real Docker executor)."""

from __future__ import annotations

from typing import Any

import pytest

from clm_lib.report import edit_evidence
from clm_lib.tasks import generate

from .conftest import FINAL, execute

pytestmark = pytest.mark.docker

HELPER = (
    "import os\nos.makedirs('helpers', exist_ok=True)\n"
    "open('helpers/ctx.py', 'w').write('''\n"
    "import json\n"
    "def drop_observations(path='context.json', keep_last=0):\n"
    "    d = json.load(open(path))\n"
    "    obs = [e for e in d['entries'] if e['role'] == 'observation']\n"
    "    drop = {e['id'] for e in obs[:len(obs) - keep_last]}\n"
    "    d['entries'] = [e for e in d['entries'] if e['id'] not in drop]\n"
    "    json.dump(d, open(path, 'w'))\n"
    "    return sorted(drop)\n"
    "''')\nprint('helper written')\n"
)
USE = "from helpers.ctx import drop_observations\nprint('dropped', drop_observations())\n"


def test_helper_use_is_verified_not_just_inferred(make_runner: Any) -> None:
    script = [
        execute(HELPER),
        execute(USE),
        execute("# mentions helpers/ctx.py but never runs it\nprint('just a comment')"),
        execute("import helpers.ctx\nraise SystemExit(3)"),  # loads, then fails
        execute(USE),
        FINAL,
    ]
    runner, _ = make_runner(script)
    res = runner.run(generate("dev"), "clm")
    h = res.metrics["helpers"]
    assert h["created"] == {"helpers/ctx.py": 1}
    assert h["candidate_invocation_steps"] == [2, 3, 4, 5]
    assert h["function_execution_steps"] == {"helpers/ctx.py": [2, 5]}
    assert h["helper_written_accepted_edit_steps"] == {"helpers/ctx.py": [2, 5]}
    assert h["functions_executed_in_2plus_steps"]
    assert h["helper_written_accepted_edits_in_2plus_steps"]
    uses = {u["step"]: u for u in h["uses"]}
    f2 = uses[2]["files"]["helpers/ctx.py"]
    assert f2["functions_executed"] == {"drop_observations": 1}
    assert f2["wrote_context_from"] == ["drop_observations"]
    assert uses[2]["context_edit"] == "accepted"
    f3 = uses[3]["files"]["helpers/ctx.py"]
    assert f3["candidate"] and not f3["loaded"] and not f3["functions_executed"]
    f4 = uses[4]["files"]["helpers/ctx.py"]
    assert f4["loaded"] and f4["module_body_executed"] and not f4["functions_executed"]
    assert not uses[4]["exit_ok"]
    # The runtime line is stripped from what the model sees.
    assert "@@CLM-RUNTIME" not in (res.run_dir / "requests" / "0006.json").read_text()


def test_compile_or_import_alone_is_not_helper_execution(make_runner: Any) -> None:
    model_edit = (
        "import helpers.ctx, json\n"  # imported, but the edit below is the model's own code
        "d = json.load(open('context.json'))\n"
        "d['entries'] = d['entries'][-1:]\n"
        "json.dump(d, open('context.json', 'w'))\n"
    )
    script = [
        execute(HELPER),
        execute(
            "src = open('helpers/ctx.py').read()\ncompile(src, 'helpers/ctx.py', 'exec')\nprint('ok')"
        ),
        execute(model_edit),
        FINAL,
    ]
    runner, _ = make_runner(script)
    h = runner.run(generate("dev"), "clm").metrics["helpers"]
    uses = {u["step"]: u for u in h["uses"]}
    f2 = uses[2]["files"]["helpers/ctx.py"]
    assert f2["loaded"] and not f2["functions_executed"] and not f2["module_body_executed"]
    f3 = uses[3]["files"]["helpers/ctx.py"]
    assert f3["loaded"] and f3["module_body_executed"] and not f3["functions_executed"]
    assert f3["wrote_context_from"] == [] and uses[3]["context_writes_unattributed"] >= 1
    assert uses[3]["context_edit"] == "accepted"
    assert h["function_execution_steps"] == {} and h["helper_written_accepted_edit_steps"] == {}


MARK = "'REMOVED-' + 'CONTENT-LINE-' + 'x' * 30"


def test_edit_evidence_checks_content_and_reappearance(make_runner: Any) -> None:
    drop = (
        "import json\nd = json.load(open('context.json'))\n"
        "d['entries'] = [e for e in d['entries'] if e['id'] != 's1.obs']\n"
        "for e in d['entries']:\n"
        "    if e['id'] == 's1.act':\n"
        "        e['body'] = 'rewritten act'\n"
        "json.dump(d, open('context.json', 'w'))\n"
    )
    clean = [execute(f"print({MARK})"), execute(drop), FINAL]
    runner, _ = make_runner(clean)
    ev = edit_evidence(runner.run(generate("dev"), "clm").run_dir)
    assert len(ev) == 1
    rec = ev[0]
    assert rec["structural_removed_absent"] and rec["content_prefix_equals_accepted_revision"]
    assert rec["rewritten"] == ["s1.act"] and rec["rewritten_verified"]
    assert rec["appended_after_edit"] == ["s2.act", "s2.obs", "s2.rcpt"]
    assert rec["content_verdict"].startswith("replaced; no checked removed text found")
    assert rec["removed_text"]["s1.obs"]["strong_hits"] == 0

    # Same edit, but the step also prints the removed text again: it reappears via s2.obs.
    leaky = [execute(f"print({MARK})"), execute(drop + f"print({MARK})\n"), FINAL]
    runner2, _ = make_runner(leaky)
    rec2 = edit_evidence(runner2.run(generate("dev"), "clm").run_dir)[0]
    assert rec2["structural_removed_absent"]  # the id check alone would look clean
    assert rec2["removed_text"]["s1.obs"]["in"] == ["s2.obs"]
    assert rec2["content_verdict"] == "replaced; removed text reappears in the next request"


def test_short_removed_text_reappearing_is_reported(make_runner: Any) -> None:
    short = "'pool' + '=12 ok'"  # an 11-character line, below the 40-character threshold
    drop = (
        "import json\nd = json.load(open('context.json'))\n"
        "d['entries'] = [e for e in d['entries'] if e['id'] != 's1.obs']\n"
        f"json.dump(d, open('context.json', 'w'))\nprint({short})\n"
    )
    runner, _ = make_runner([execute(f"print({short})"), execute(drop), FINAL])
    rec = edit_evidence(runner.run(generate("dev"), "clm").run_dir)[0]
    check = rec["removed_text"]["s1.obs"]
    assert check["weak_hits"] == 1 and check["strong_hits"] == 0 and check["in"] == ["s2.obs"]
    assert check["coverage"] < 1.0  # boilerplate framing is not counted as checked
    assert (
        rec["content_verdict"] == "replaced; short removed lines reappear (possibly coincidental)"
    )


def test_valid_empty_accepted_revision_passes_prefix_check(make_runner: Any) -> None:
    empty = (
        "import json\nd = json.load(open('context.json'))\nd['entries'] = []\n"
        "json.dump(d, open('context.json', 'w'))\n"
    )
    runner, _ = make_runner(
        [execute("print('hello world, some output here')"), execute(empty), FINAL]
    )
    rec = edit_evidence(runner.run(generate("dev"), "clm").run_dir)[0]
    assert rec["accepted_revision_entries"] == 0
    assert rec["content_prefix_equals_accepted_revision"] is True
    assert rec["appended_after_edit"] == ["s2.act", "s2.obs", "s2.rcpt"]
    assert rec["content_verdict"].startswith("replaced; no checked removed text found")


def test_missing_revision_artifact_is_unverifiable(make_runner: Any) -> None:
    drop = (
        "import json\nd = json.load(open('context.json'))\nd['entries'] = d['entries'][:1]\n"
        "json.dump(d, open('context.json', 'w'))\n"
    )
    runner, _ = make_runner([execute("print('x' * 50)"), execute(drop), FINAL])
    run_dir = runner.run(generate("dev"), "clm").run_dir
    rec = edit_evidence(run_dir)[0]
    (run_dir / "context" / f"rev-{rec['after_revision']:04d}.json").unlink()
    rec = edit_evidence(run_dir)[0]
    assert rec["content_prefix_equals_accepted_revision"] is None
    assert rec["content_verdict"] == "unverifiable: accepted revision artifact missing"
