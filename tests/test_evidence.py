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
        execute("import helpers.ctx\nraise SystemExit(3)"),  # runs but fails
        execute(USE),
        FINAL,
    ]
    runner, _ = make_runner(script)
    res = runner.run(generate("dev"), "clm")
    h = res.metrics["helpers"]
    assert h["created"] == {"helpers/ctx.py": 1}
    assert h["candidate_invocation_steps"] == [2, 3, 4, 5]
    assert h["verified_execution_steps"] == {"helpers/ctx.py": [2, 5]}
    assert h["verified_execution_with_accepted_edit_steps"] == {"helpers/ctx.py": [2, 5]}
    assert h["executed_in_2plus_steps"] and h["reused_with_accepted_edit_in_2plus_steps"]
    step3 = next(u for u in h["uses"] if u["step"] == 3)
    assert step3["candidate"] == ["helpers/ctx.py"] and step3["executed"] == []
    step4 = next(u for u in h["uses"] if u["step"] == 4)
    assert step4["executed"] == ["helpers/ctx.py"] and not step4["exit_ok"]
    # The runtime line is stripped from what the model sees.
    assert "@@CLM-RUNTIME" not in (res.run_dir / "requests" / "0006.json").read_text()


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
    assert rec["content_verdict"] == "replaced; removed text absent from next request"

    # Same edit, but the step also prints the removed text again: it reappears via s2.obs.
    leaky = [execute(f"print({MARK})"), execute(drop + f"print({MARK})\n"), FINAL]
    runner2, _ = make_runner(leaky)
    rec2 = edit_evidence(runner2.run(generate("dev"), "clm").run_dir)[0]
    assert rec2["structural_removed_absent"]  # the id check alone would look clean
    assert rec2["removed_content_reappearance"]["s1.obs"]["in"] == ["s2.obs"]
    assert rec2["content_verdict"].startswith("replaced; some removed text reappears")
