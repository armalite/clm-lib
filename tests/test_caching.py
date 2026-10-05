"""Experiment 006: request layout blocks/1, prompt-caching controls, run isolation and
cache-aware accounting. Provider caching itself is simulated here (CachingProvider); live
cache behaviour is measured in calibration, not in these tests."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from clm_lib.budget import Ledger, ModelPrice
from clm_lib.cli import WILLIAMS_4, condition_schedule, parse_conditions, run_matrix
from clm_lib.config import Config, RequestConfig, load_config
from clm_lib.prompts import render_user
from clm_lib.provider import ModelRequest, ModelResponse, ScriptedProvider, Usage
from clm_lib.runner import Runner
from clm_lib.tasks import generate

from .conftest import FINAL, TEST_PRICE, FakeExecutor, execute, working_block, working_ids

REPO = Path(__file__).resolve().parents[1]
ADVANCE = {"action": "advance"}
CODING_FINAL = {"action": "final", "answer": {"summary": "done"}}


def blocks_cfg(caching: bool = True, **limits: Any) -> Config:
    cfg = Config()
    cfg.request = RequestConfig(
        layout="blocks/1", prompt_caching=caching, run_isolation="run-tag/1"
    )
    cfg.baseline.policy = "token-tail/1"
    for k, v in limits.items():
        setattr(cfg.limits, k, v)
    return cfg


def strip_cache(payload: dict[str, Any]) -> dict[str, Any]:
    p = copy.deepcopy(payload)
    for msg in p["messages"]:
        if isinstance(msg["content"], list):
            for block in msg["content"]:
                block.pop("cache_control", None)
    return p


def saved_payloads(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((run_dir / "requests").glob("*.json"))]


@dataclass
class CachingProvider(ScriptedProvider):
    """Scripted double that simulates prefix caching: entries are written at breakpoints and
    read by a 20-block lookback, as the provider documents. Tokens = chars // 3."""

    cache: set[str] = field(default_factory=set)

    def complete(self, req: ModelRequest) -> ModelResponse:
        resp = super().complete(req)
        if req.system_blocks is None or req.user_blocks is None:
            return resp
        flat = [*req.system_blocks, *req.user_blocks]
        offset = len(req.system_blocks)
        marks = [offset + i for i in req.cache_breakpoints] if req.cache_ttl else []

        def key(n: int) -> str:
            return hashlib.sha256("\0".join(flat[: n + 1]).encode()).hexdigest()

        def toks(n: int) -> int:
            return sum(len(b) for b in flat[: n + 1]) // 3

        total = sum(len(b) for b in flat) // 3
        hit = -1
        for m in marks:
            for q in range(m, max(-1, m - 20), -1):
                if key(q) in self.cache:
                    hit = max(hit, q)
                    break
        read = toks(hit) if hit >= 0 else 0
        last = max(marks) if marks else -1
        write = max(0, toks(last) - read) if last >= 0 else 0
        for m in marks:
            self.cache.add(key(m))
        usage = Usage(
            input_tokens=total - read - write,
            output_tokens=resp.usage.output_tokens,
            cache_read_input_tokens=read,
            cache_creation_input_tokens=write,
            cache_creation_5m=write,
            cache_creation_1h=0,
        )
        return replace(resp, usage=usage, usage_source="scripted-estimate")


def make(
    tmp_path: Path, cfg: Config, script: list[Any], provider: Any = None, executor: Any = None
) -> tuple[Runner, Any]:
    prov = provider or CachingProvider(list(script))
    runner = Runner(
        cfg,
        prov,
        executor or FakeExecutor(),
        Ledger.open(tmp_path / "ledger.json", 100.0),
        TEST_PRICE,
        live=False,
        runs_dir=tmp_path / "runs",
        sleep=lambda s: None,
    )
    return runner, prov


# ------------------------------------------------------------- historical behaviour
def test_historical_configs_keep_the_single_user_layout(tmp_path: Path) -> None:
    for name in ("default", "exp003", "exp004", "exp005"):
        r = load_config(REPO / "configs" / f"{name}.toml").request
        assert (r.layout, r.prompt_caching, r.run_isolation) == ("single-user/1", False, "none")
    runner, prov = make(tmp_path, Config(), [execute("print(1)"), FINAL], ScriptedProvider([]))
    prov.script.extend([execute("print(1)"), FINAL])
    res = runner.run(generate("dev"), "clm")
    first = saved_payloads(res.run_dir)[0]["payload"]
    assert isinstance(first["system"], str) and isinstance(first["messages"][0]["content"], str)
    assert "cache_control" not in json.dumps(first) and "run-ref:" not in json.dumps(first)
    req = prov.requests[1]
    assert req.user_blocks is None and req.user.index("<runtime_status>") < req.user.index(
        "<working_context>"
    )
    assert "-cache" not in res.run_id
    assert res.metrics["request"]["layout"] == "single-user/1"
    with pytest.raises(ValueError, match="blocks/1"):
        runner.run(generate("dev"), "clm", caching=True)


def test_invalid_request_settings_are_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[request]\nprompt_caching = true\n")
    with pytest.raises(ValueError, match="blocks/1"):
        load_config(bad)
    bad.write_text('[request]\nlayout = "blocks/2"\n')
    with pytest.raises(ValueError, match="layout"):
        load_config(bad)


# ------------------------------------------------------------- layout and controls
def test_blocks_layout_puts_stable_content_first_and_status_last(tmp_path: Path) -> None:
    runner, prov = make(tmp_path, blocks_cfg(), [execute("print('x')"), FINAL])
    runner.run(generate("dev"), "clm")
    req = prov.requests[1]
    assert req.user_blocks is not None and req.system_blocks is not None
    assert req.user_blocks[0].startswith("<task>") and req.user_blocks[0].endswith(
        "<working_context>\n"
    )
    assert req.user_blocks[-1].startswith("</working_context>\n\n<runtime_status>")
    # One block per entry; the joined text has the same working context as before.
    ids = working_ids(req.user)
    assert len(req.user_blocks) == len(ids) + 2
    assert req.cache_breakpoints == (0, len(ids))
    entries_text = "".join(req.user_blocks[1:-1])
    assert working_block(req.user) + "\n" == entries_text
    status = req.user_blocks[-1].split("<runtime_status>\n", 1)[1].split("\n</runtime_status>")[0]
    old = render_user(generate("dev").prompt, status, runner_entries(req))
    assert sorted(old) == sorted(req.user)  # same characters, reordered sections


def runner_entries(req: ModelRequest) -> list[Any]:
    from clm_lib.context import Entry

    return [Entry(**json.loads(b)) for b in req.user_blocks[1:-1]]  # type: ignore[index]


def test_caching_on_and_off_differ_only_in_cache_control(tmp_path: Path) -> None:
    script = [execute("print('a')"), execute("print('b')"), FINAL]
    on_runner, _ = make(tmp_path / "on", blocks_cfg(True), list(script))
    off_runner, _ = make(tmp_path / "off", blocks_cfg(False), list(script))
    on = on_runner.run(generate("dev"), "clm")
    off = off_runner.run(generate("dev"), "clm")
    assert "-cacheon-" in on.run_id and "-cacheoff-" in off.run_id
    p_on = [r["payload"] for r in saved_payloads(on.run_dir)]
    p_off = [r["payload"] for r in saved_payloads(off.run_dir)]
    assert len(p_on) == len(p_off) == 3
    tag_on = p_on[0]["system"][0]["text"].split()[1]
    tag_off = p_off[0]["system"][0]["text"].split()[1]
    for a, b in zip(p_on, p_off, strict=True):
        b = json.loads(json.dumps(b).replace(tag_off, tag_on))  # only the random tag differs
        assert strip_cache(a) == b
        assert "cache_control" not in json.dumps(b)
        marked = [i for i, blk in enumerate(a["messages"][0]["content"]) if "cache_control" in blk]
        assert marked[0] == 0 and len(marked) == (2 if len(a["messages"][0]["content"]) > 2 else 1)
        assert all(
            blk["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
            for blk in a["messages"][0]["content"]
            if "cache_control" in blk
        )
        assert "cache_control" not in a["messages"][0]["content"][-1]  # status never cached
    assert on.metrics["request"]["prompt_caching"] == "on"
    assert off.metrics["request"]["prompt_caching"] == "off"
    assert off.metrics["usage_provider_reported"]["cache_read_input_tokens"] == 0
    assert off.metrics["usage_provider_reported"]["cache_creation_input_tokens"] == 0


def test_run_tag_is_stable_within_a_run_and_unique_across_runs(tmp_path: Path) -> None:
    cfg = blocks_cfg(True, max_calls=30)
    cfg.limits.context_budget_tokens = 4000  # force summaries
    big = "print('x' * 2400)"
    script = [execute(big) for _ in range(6)] + [FINAL]

    def step(req: ModelRequest) -> Any:
        return "summary text " * 60 if req.purpose == "summary" else script.pop(0)

    tags = []
    for i in range(2):
        runner, _ = make(
            tmp_path / str(i), cfg, [step] * 20, executor=FakeExecutor(outputs=["y" * 2400] * 9)
        )
        script[:] = [execute(big) for _ in range(6)] + [FINAL]
        res = runner.run(generate("dev"), "summary")
        payloads = saved_payloads(res.run_dir)
        kinds = {p["meta"]["kind"] for p in payloads}
        assert "summary" in kinds and "action" in kinds
        first_blocks = {p["payload"]["system"][0]["text"] for p in payloads}
        assert len(first_blocks) == 1  # identical first block in every request of the run
        tag = res.metrics["request"]["run_tag"]
        assert first_blocks == {f"run-ref: {tag}\n"} and len(tag) == 32
        assert "dev" not in tag and "summary" not in tag
        tags.append(tag)
    assert tags[0] != tags[1]


# ------------------------------------------------------------- accounting
def test_cache_aware_cost_and_reservation() -> None:
    p = ModelPrice("m", 4.0, 5.0, 8.0, 0.2, 20.0, "s", "d")
    u = Usage(
        input_tokens=1000,
        output_tokens=100,
        cache_read_input_tokens=10_000,
        cache_creation_input_tokens=2000,
        cache_creation_5m=2000,
        cache_creation_1h=0,
    )
    expected = (1000 * 4 + 2000 * 5 + 10_000 * 0.2 + 100 * 20) / 1e6
    assert p.cost(u) == pytest.approx(expected)
    u1h = replace(u, cache_creation_5m=500, cache_creation_1h=1500)
    assert p.cost(u1h) == pytest.approx((1000 * 4 + 500 * 5 + 1500 * 8 + 2000 + 2000) / 1e6)
    # Reservations assume every input token at the dearest applicable input-side rate.
    assert p.max_cost(10_000, 100) == pytest.approx((10_000 * 5 + 100 * 20) / 1e6)
    assert p.max_cost(10_000, 100, "1h") == pytest.approx((10_000 * 8 + 100 * 20) / 1e6)


def test_cached_tokens_count_toward_budget_and_are_settled_per_kind(tmp_path: Path) -> None:
    script = [execute("print('a' * 300)"), execute("print('b' * 300)"), FINAL]
    runner, _ = make(tmp_path, blocks_cfg(True), script)
    res = runner.run(generate("dev"), "clm")
    events = [json.loads(x) for x in (res.run_dir / "events.jsonl").read_text().splitlines()]
    responses = [e for e in events if e["event"] == "response"]
    assert responses[0]["usage"]["cache_read_input_tokens"] == 0  # cold start
    assert responses[0]["usage"]["cache_creation_input_tokens"] > 0
    assert responses[1]["usage"]["cache_read_input_tokens"] > 0  # reuse within the run
    tl = res.metrics["management"]["timeline"]
    for t, r in zip(tl, responses, strict=True):
        u = r["usage"]
        total = u["input_tokens"] + u["cache_read_input_tokens"] + u["cache_creation_input_tokens"]
        assert t["reported_input_tokens"] == total  # cached tokens occupy the budget too
        assert t["cache_read_input_tokens"] == u["cache_read_input_tokens"]
    assert res.metrics["peak_request_input_tokens_reported"] == max(
        t["reported_input_tokens"] for t in tl
    )
    led = json.loads((tmp_path / "ledger.json").read_text())
    charged = sum(e["charged_usd"] for e in led["entries"])
    assert charged == pytest.approx(res.metrics["cost"]["incurred_usd"], abs=1e-6)
    by_kind = res.metrics["request"]["by_kind"]
    assert by_kind["action"]["calls"] == 3
    assert by_kind["action"]["cost_usd"] == pytest.approx(charged, abs=1e-5)
    assert (
        sum(e["usage"]["cache_read_input_tokens"] for e in led["entries"])
        == (res.metrics["usage_provider_reported"]["cache_read_input_tokens"])
    )
    assert res.metrics["request"]["first_call_cache"]["cache_read_input_tokens"] == 0


def test_summary_requests_use_the_same_layout_and_caching(tmp_path: Path) -> None:
    cfg = blocks_cfg(True, max_calls=30)
    cfg.limits.context_budget_tokens = 4000
    big = "print('x' * 2400)"
    queue = [execute(big) for _ in range(6)] + [FINAL]

    def step(req: ModelRequest) -> Any:
        return "summary text " * 60 if req.purpose == "summary" else queue.pop(0)

    runner, prov = make(tmp_path, cfg, [step] * 20, executor=FakeExecutor(outputs=["y" * 2400] * 9))
    res = runner.run(generate("dev"), "summary")
    sums = [r for r in prov.requests if r.purpose == "summary"]
    assert sums and res.metrics["counts"]["summaries_applied"] >= 1
    s = sums[0]
    assert s.cache_ttl == "5m" and s.system_blocks is not None and s.user_blocks is not None
    assert s.system_blocks[0].startswith("run-ref: ")
    assert s.user_blocks[0].endswith("<transcript_to_summarise>\n")
    assert s.user_blocks[-1].startswith("</transcript_to_summarise>")
    assert s.cache_breakpoints == (0, len(s.user_blocks) - 2)
    assert "summary" in res.metrics["request"]["by_kind"]
    # After a summary, the next action request repeats only the stable prefix.
    payloads = saved_payloads(res.run_dir)
    after = next(
        p
        for i, p in enumerate(payloads)
        if p["meta"]["kind"] == "action" and payloads[i - 1]["meta"]["kind"] == "summary"
    )
    pre = after["meta"]["prefix"]
    assert pre["family"] == "action" and pre["first_changed_block"] == pre["system_blocks"] + 1


@pytest.mark.docker
def test_context_edits_still_shape_the_next_request_under_blocks_layout(
    tmp_path: Path,
) -> None:
    from clm_lib.executor import DockerExecutor

    marker = "'UNIQ' + 'MARK-6c'"
    script = [
        execute(f"print({marker})\nprint('pad ' * 50)"),
        execute(
            "import json\nd = json.load(open('context.json'))\n"
            f"d['entries'] = [e for e in d['entries'] if {marker} not in e['body']]\n"
            "json.dump(d, open('context.json', 'w'))\nprint('edited')\n"
        ),
        FINAL,
    ]
    runner, prov = make(tmp_path, blocks_cfg(True), script, executor=DockerExecutor())
    res = runner.run(generate("dev"), "clm")
    assert res.status == "completed" and res.metrics["counts"]["edits_accepted_changed"] == 1
    r2, r3 = prov.requests[1], prov.requests[2]
    assert "UNIQMARK-6c" in working_block(r2.user) and "UNIQMARK-6c" not in r3.user
    assert "s1.obs" not in working_ids(r3.user)
    # Evidence of the changed prefix: the edit removed the first entry block (s1.act kept).
    pre = saved_payloads(res.run_dir)[2]["meta"]["prefix"]
    assert pre["first_changed_block"] == pre["system_blocks"] + 2
    events = [json.loads(x) for x in (res.run_dir / "events.jsonl").read_text().splitlines()]
    resp3 = [e for e in events if e["event"] == "response"][2]
    assert resp3["usage"]["cache_read_input_tokens"] > 0  # the unchanged prefix is still read


# ------------------------------------------------------------- four-condition schedule
def test_williams_schedule_balances_positions_and_instances() -> None:
    conds = parse_conditions("summary:off,summary:on,clm:off,clm:on")
    tasks = ["coding-eval-1", "coding-eval-2", "coding-eval-3", "coding-eval-4"]
    sched = condition_schedule(tasks, 3, conds)
    assert len(sched) == 48
    by_pos: dict[tuple[str, str, int], int] = {}
    for row in sched:
        k = (row["mode"], row["caching"], row["position"])
        by_pos[k] = by_pos.get(k, 0) + 1
    assert set(by_pos.values()) == {3} and len(by_pos) == 16  # each condition 3x per position
    for task in tasks:
        rows = {r["williams_row"] for r in sched if r["task"] == task}
        assert len(rows) == 3  # each instance sees three different orders
    pairs = set()
    for row in WILLIAMS_4:
        pairs |= {(row[i], row[i + 1]) for i in range(3)}
    assert len(pairs) == 12  # every ordered adjacent pair once
    with pytest.raises(ValueError):
        parse_conditions("summary:off,summary:off")


def test_four_condition_matrix_runs_each_condition(tmp_path: Path) -> None:
    cfg = blocks_cfg(False)
    runner, _ = make(tmp_path, cfg, [FINAL] * 40)
    conds = parse_conditions("summary:off,summary:on,clm:off,clm:on")
    out = tmp_path / "c.json"
    cells, code = run_matrix(runner, cfg, ["heldout-1", "dev"], 2, out, "t", conditions=conds)
    assert code == 0 and len(cells) == 16
    rec = json.loads(out.read_text())
    assert rec["frozen"]["mode_order"] == "williams" and len(rec["frozen"]["schedule"]) == 16
    for cell in cells:
        assert f"-cache{cell['caching']}-" in cell["run_id"]
        assert cell["run_id"].split("-")[1] == cell["mode"]


@pytest.mark.docker
def test_edit_evidence_report_reads_the_blocks_layout(tmp_path: Path) -> None:
    from clm_lib.executor import DockerExecutor
    from clm_lib.report import edit_evidence

    script = [
        execute("print('pad ' * 80)"),
        execute(
            "import json\nd = json.load(open('context.json'))\n"
            "d['entries'] = [{'id': 'n1', 'role': 'note', 'body': 'kept note'}]\n"
            "json.dump(d, open('context.json', 'w'))\nprint('edited')\n"
        ),
        FINAL,
    ]
    runner, _ = make(tmp_path, blocks_cfg(True), script, executor=DockerExecutor())
    res = runner.run(generate("dev"), "clm")
    ev = edit_evidence(res.run_dir)
    assert len(ev) == 1 and ev[0]["content_prefix_equals_accepted_revision"] is True
    assert ev[0]["structural_removed_absent"] and ev[0]["added"] == ["n1"]
