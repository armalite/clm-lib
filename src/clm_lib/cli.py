"""Command-line interface. Every command uses the same ``clm_lib`` core."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .budget import BudgetExhausted, Ledger, ModelPrice, input_token_bound, load_prices
from .config import Config, load_config
from .executor import DockerExecutor
from .provider import (
    ACTION_SCHEMA,
    AnthropicProvider,
    ModelRequest,
    ProviderError,
    ScriptedProvider,
)
from .runner import MODES, Runner, parse_action
from .tasks import HELDOUT, INSTANCES, generate
from .tracing import RunTrace

REPO_ROOT = Path(__file__).resolve().parents[2]
CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")


def _resolve(path: str) -> Path:
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    return REPO_ROOT / p


def _config(args: argparse.Namespace) -> Config:
    path = Path(args.config) if args.config else _resolve("configs/default.toml")
    return load_config(path if path.exists() else None)


def _executor(cfg: Config) -> DockerExecutor:
    s, lim = cfg.sandbox, cfg.limits
    return DockerExecutor(
        image=s.image,
        timeout_s=lim.exec_timeout_s,
        memory=s.memory,
        cpus=s.cpus,
        pids_limit=s.pids_limit,
        output_cap_bytes=lim.output_cap_chars,
    )


def _provider(cfg: Config) -> AnthropicProvider:
    p = cfg.provider
    if p.name != "anthropic":
        raise SystemExit(f"provider {p.name!r} is not implemented (only 'anthropic')")
    return AnthropicProvider(
        model=p.model,
        base_url=p.base_url or None,
        timeout_s=p.timeout_s,
        effort=p.effort or None,
        structured_output=p.structured_output,
    )


def _price(cfg: Config) -> ModelPrice | None:
    path = _resolve(cfg.budget.prices)
    if not path.exists():
        return None
    return load_prices(path).get(cfg.provider.model)


def _ledger(cfg: Config, max_usd: float | None) -> Ledger:
    return Ledger.open(_resolve(cfg.budget.ledger), cfg.budget.ceiling_usd, max_usd)


def _runs_dir(cfg: Config) -> Path:
    d = _resolve(cfg.runs_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _live_preflight(
    cfg: Config, ledger: Ledger
) -> tuple[AnthropicProvider, ModelPrice, DockerExecutor]:
    price = _price(cfg)
    if price is None:
        raise SystemExit(
            f"live mode blocked: no dated price for {cfg.provider.model} in {cfg.budget.prices}; "
            "add one from the provider's official pricing page."
        )
    ex = _executor(cfg)
    ok, why = ex.check()
    if not ok:
        raise SystemExit(f"live mode blocked: isolated executor unavailable ({why})")
    prov = _provider(cfg)
    print(
        f"model={prov.model} effort={prov.effort} | ledger: spent ${ledger.spent_usd:.4f}, "
        f"remaining ${ledger.remaining_usd:.4f} of ${ledger.effective_ceiling:.2f}"
    )
    return prov, price, ex


def _print_result(res: Any) -> None:
    m = res.metrics
    s = m["score"]
    print(
        f"run {res.run_id}: status={res.status} ({res.stop_reason}) outcome={s['outcome']} "
        f"strict={s['strict_success']} cost=${m['cost']['incurred_usd']:.4f} "
        f"calls={m['counts']['provider_calls']} edits={m['counts']['edits_accepted_changed']} "
        f"summaries={m['counts']['summaries_applied']}"
    )
    print(f"  evidence: {res.run_dir}")


# ---------------------------------------------------------------- commands
def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _config(args)
    print(f"clm-lib {__version__} | python {sys.version.split()[0]}")
    ex = _executor(cfg)
    ok, why = ex.check()
    print(f"executor: {'OK' if ok else 'UNAVAILABLE'} - {why}")
    for name in CREDENTIAL_VARS:
        print(f"credential env {name}: {'set' if os.environ.get(name) else 'unset'}")
    try:
        from anthropic.lib.credentials._constants import _has_auto_discoverable_credentials

        print(
            f"anthropic SDK profile/credential discovery: {'found' if _has_auto_discoverable_credentials() else 'none found'}"
        )
    except Exception:
        print("anthropic SDK profile/credential discovery: not checkable with this SDK version")
    price = _price(cfg)
    print(
        f"model: {cfg.provider.model} | price: "
        + (
            f"${price.input}/{price.output} per MTok in/out (retrieved {price.retrieved})"
            if price
            else "UNAVAILABLE"
        )
    )
    led = _ledger(cfg, None)
    print(f"budget ledger: spent ${led.spent_usd:.4f} of ceiling ${led.ceiling_usd:.2f}")
    if ok and args.sandbox:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ws, fx = Path(tmp, "ws"), Path(tmp, "fx")
            ws.mkdir()
            fx.mkdir()
            os.environ["CLM_DOCTOR_DUMMY_SECRET"] = "dummy"
            code = (
                "import os,socket\n"
                "print('dummy_secret_visible=', 'CLM_DOCTOR_DUMMY_SECRET' in os.environ)\n"
                "print('uid=', os.getuid())\n"
                "try:\n socket.create_connection(('1.1.1.1', 53), timeout=2); print('network=open')\n"
                "except OSError: print('network=blocked')\n"
            )
            r = ex.run(code, ws, fx)
            os.environ.pop("CLM_DOCTOR_DUMMY_SECRET", None)
            print("sandbox self-test:", " ".join(r.stdout.split()))
    if args.probe:
        try:
            import anthropic

            kw: dict[str, Any] = {"max_retries": 0, "timeout": 20.0}
            if cfg.provider.base_url:
                kw["base_url"] = cfg.provider.base_url
            client = anthropic.Anthropic(**kw)
            info = client.models.retrieve(cfg.provider.model)
            print(
                f"probe (free models endpoint): OK id={info.id} max_input_tokens={getattr(info, 'max_input_tokens', '?')} max_tokens={getattr(info, 'max_tokens', '?')}"
            )
        except Exception as exc:
            msg = getattr(exc, "message", None) or str(exc)
            print(f"probe: FAILED ({type(exc).__name__}: {str(msg)[:200]})")
            print(
                "  smallest fix: set ANTHROPIC_API_KEY, or run `ant auth login` to refresh the SDK profile"
            )
            return 2
    return 0 if ok else 1


def cmd_fixtures(args: argparse.Namespace) -> int:
    inst = generate(args.task)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    inst.write_fixtures(out)
    print(
        f"wrote {len(inst.files)} fixture files for {args.task} (sha256 {inst.fixture_sha256[:16]}) to {out}"
    )
    print("ground truth is not written; it is used only by the scorer")
    return 0


def offline_demo_script() -> list[Any]:
    """A scripted 'model' that reads, then edits its context, then answers.

    The marker is built by concatenation so it appears only in program output,
    never in the code bodies that are themselves kept in the transcript.
    """
    return [
        {
            "action": "execute",
            "thought": "look around",
            "code": "import os\nprint('BULKY-' + 'OBSERVATION-MARKER')\nprint('x' * 3000)\nprint(sorted(os.listdir('/task/fixtures')))",
        },
        {
            "action": "execute",
            "thought": "drop the bulky observation, keep a note",
            "code": (
                "import json\nd = json.load(open('context.json'))\n"
                "m = 'BULKY-' + 'OBSERVATION-MARKER'\n"
                "d['entries'] = [e for e in d['entries'] if m not in e['body']]\n"
                "d['entries'].append({'id': 'n1', 'role': 'note', 'body': 'fixtures listed; bulky output dropped'})\n"
                "json.dump(d, open('context.json', 'w'))\nprint('edited')"
            ),
        },
        {
            "action": "final",
            "answer": {
                "root_cause": "DB_POOL_EXHAUSTED: scripted",
                "required_value": "0",
                "remedy": "RAISE_DB_POOL_LIMIT: scripted",
                "evidence_refs": [],
            },
        },
    ]


def cmd_demo_offline(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ex = _executor(cfg)
    ok, why = ex.check()
    if not ok:
        raise SystemExit(f"offline demo needs the isolated executor: {why}")
    runs = _runs_dir(cfg) / "offline"
    runs.mkdir(exist_ok=True)
    ledger = Ledger.open(runs / "offline-ledger.json", 0.0)
    prov = ScriptedProvider(offline_demo_script())
    runner = Runner(cfg, prov, ex, ledger, None, live=False, runs_dir=runs)
    res = runner.run(generate("dev"), "clm", label="offline-demo (scripted model)")
    _print_result(res)
    marker = "BULKY-OBSERVATION-MARKER"
    for i, req in enumerate(prov.requests, 1):
        wc = req.user.split("<working_context>")[1]
        print(f"  request {i}: marker in working context = {marker in wc}")
    print("NOTE: scripted model double; this demonstrates the mechanism only, not model behaviour.")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ledger = _ledger(cfg, args.max_usd)
    prov, price, _ = _live_preflight(cfg, ledger)
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-smoke"
    trace = RunTrace(_runs_dir(cfg) / run_id)
    req = ModelRequest(
        system="Reply with exactly one JSON object.",
        user='Return a final action whose answer has root_cause "smoke", required_value "ok", '
        'remedy "none" and an empty evidence_refs list.',
        max_tokens=512,
        purpose="smoke",
        json_schema=ACTION_SCHEMA,
    )
    payload = prov.payload(req)
    bound = input_token_bound(payload)
    reserve = price.max_cost(bound, req.max_tokens)
    try:
        res = ledger.reserve(reserve, run_id, "smoke", bound)
    except BudgetExhausted as exc:
        raise SystemExit(str(exc)) from exc
    trace.save_request(
        payload, {"kind": "smoke", "reserved_usd": reserve, "input_token_bound": bound}
    )
    try:
        resp = prov.complete(req)
    except ProviderError as exc:
        billed = getattr(exc, "response", None)
        actual = price.cost(billed.usage) if billed else (0.0 if exc.billing == "none" else None)
        entry = ledger.settle(res, actual_usd=actual, status=f"error:{exc.kind}")
        trace.event(
            "provider_error",
            message=str(exc)[:300],
            charged_usd=entry["charged_usd"],
            cost_basis=entry["cost_basis"],
        )
        trace.close()
        print(
            f"smoke call FAILED: {exc} (charged ${entry['charged_usd']:.6f}, {entry['cost_basis']})"
        )
        if exc.kind == "auth":
            print("  smallest fix: export a valid ANTHROPIC_API_KEY in the shell running clm-lib")
        return 2
    except BaseException:
        ledger.settle(res, actual_usd=None, status="interrupted")
        trace.close()
        raise
    cost = price.cost(resp.usage)
    entry = ledger.settle(res, actual_usd=cost, status="ok", usage=resp.usage)
    reported = (
        resp.usage.input_tokens
        + resp.usage.cache_read_input_tokens
        + resp.usage.cache_creation_input_tokens
    )
    action, err = parse_action(resp.text, 1000)
    expected = bool(
        action
        and action["action"] == "final"
        and action["answer"].get("required_value") == "ok"
        and action["answer"].get("evidence_refs") == []
    )
    counted = _count_tokens(prov, payload)
    checks = {
        "model_matches": resp.model.startswith(cfg.provider.model),
        "valid_action": action is not None,
        "expected_answer": expected,
        "stop_reason_end_turn": resp.stop_reason == "end_turn",
        "input_within_bound": reported <= bound,
        "output_within_max_tokens": resp.usage.output_tokens <= req.max_tokens,
        "ledger_settled_from_usage": entry["cost_basis"] == "provider_usage",
    }
    trace.event(
        "response",
        text=resp.text,
        usage=resp.usage.to_dict(),
        stop_reason=resp.stop_reason,
        model=resp.model,
        request_id=resp.request_id,
        cost_usd=cost,
        parse_error=err,
        input_token_bound=bound,
        count_tokens=counted,
        checks=checks,
    )
    trace.close()
    ok = all(checks.values())
    print(
        f"smoke {'OK' if ok else 'FAILED CHECKS'}: model={resp.model} stop={resp.stop_reason} "
        f"usage={resp.usage.to_dict()}"
    )
    print(f"  text: {resp.text[:200]}")
    print(f"  checks: {checks}")
    print(f"  input tokens reported {reported}; count_tokens {counted}; reservation bound {bound}")
    print(
        f"  cost ${cost:.6f} (reserved ${reserve:.4f}); ledger spent ${ledger.spent_usd:.6f}; trace {trace.dir}"
    )
    return 0 if ok else 3


def _count_tokens(prov: AnthropicProvider, payload: dict[str, Any]) -> int | str:
    """Free provider token count for the same system/messages, as a bound cross-check."""
    try:
        client = prov._get_client()
        r = client.messages.count_tokens(
            model=payload["model"], system=payload["system"], messages=payload["messages"]
        )
        return int(r.input_tokens)
    except Exception as exc:
        return f"unavailable ({type(exc).__name__})"


def _live_runner(cfg: Config, max_usd: float | None) -> Runner:
    ledger = _ledger(cfg, max_usd)
    prov, price, ex = _live_preflight(cfg, ledger)
    return Runner(cfg, prov, ex, ledger, price, live=True, runs_dir=_runs_dir(cfg))


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config(args)
    runner = _live_runner(cfg, args.max_usd)
    res = runner.run(generate(args.task), args.mode, label=args.label or "single run")
    _print_result(res)
    return 0


def cmd_guided(args: argparse.Namespace) -> int:
    cfg = _config(args)
    runner = _live_runner(cfg, args.max_usd)
    res = runner.run(generate(args.task), "guided", label="guided helper demo (prompted)")
    _print_result(res)
    h = res.metrics["helpers"]
    print(f"  helpers created: {h['created']} | revisions: {h['revised']}")
    print(
        f"  candidate invocation steps (inferred from code text): {h['candidate_invocation_steps']}"
    )
    print(f"  helper function executions (observed, exit 0): {h['function_execution_steps']}")
    print(f"  accepted edits written from helper code: {h['helper_written_accepted_edit_steps']}")
    return 0


# Run statuses after which no further comparison calls may be dispatched.
COMPARE_HALT = {
    "accounting_bound_violated": "accounting assumption failed (reported tokens exceeded the reservation bound)",
    "budget_exhausted": "budget exhausted during a run",
}


def source_patch() -> str:
    """Uncommitted tracked changes plus untracked files under src/configs/tests, as one patch."""
    import subprocess

    def git(*a: str) -> str:
        try:
            return subprocess.run(
                ["git", *a], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    paths = ["src", "configs", "tests", "pyproject.toml"]
    out = git("diff", "HEAD", "--", *paths)
    for f in git("ls-files", "--others", "--exclude-standard", "--", *paths).split():
        out += git("diff", "--no-index", "--", "/dev/null", f)
    return out


def code_state() -> dict[str, Any]:
    """Git HEAD plus a hash of uncommitted changes, to freeze the evaluated code."""
    import hashlib
    import subprocess

    def git(*a: str) -> str:
        try:
            return subprocess.run(
                ["git", *a], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    diff = git("diff", "HEAD", "--", "src", "configs", "pyproject.toml")
    untracked = git("ls-files", "--others", "--exclude-standard", "--", "src", "configs")
    return {
        "git_head": git("rev-parse", "HEAD").strip(),
        "uncommitted_diff_sha256": hashlib.sha256(diff.encode()).hexdigest() if diff else None,
        "untracked_src_files": untracked.split(),
    }


def run_matrix(
    runner: Runner, cfg: Config, tasks: list[str], reps: int, out: Path, stamp: str
) -> tuple[list[dict[str, Any]], int]:
    """Sequential comparison matrix; returns (cells, exit code). Never parallel."""
    from .prompts import PROMPT_VERSION
    from .tasks import SCORER_VERSION

    patch = source_patch()
    if patch:
        out.with_suffix(".patch").write_text(patch)
    frozen = {
        "source_patch": out.with_suffix(".patch").name if patch else None,
        "source_patch_sha256": hashlib.sha256(patch.encode()).hexdigest() if patch else None,
        "model": runner.provider.model,
        "provider": runner.provider.describe(),
        "prompt_version": PROMPT_VERSION,
        # Per-task generator versions (staged tasks use their own generator).
        "generator_version": sorted({generate(t).generator_version for t in tasks}),
        "scorer_version": SCORER_VERSION,
        "summary_policy": cfg.baseline.policy,
        "config": cfg.to_dict(),
        "code_state": code_state(),
    }
    cells: list[dict[str, Any]] = []
    pair = 0
    stopped = ""
    exit_code = 0
    for rep in range(1, reps + 1):
        for task in tasks:
            order = ["summary", "clm"] if pair % 2 == 0 else ["clm", "summary"]
            for mode in order:
                cell: dict[str, Any] = {
                    "task": task,
                    "rep": rep,
                    "mode": mode,
                    "pair": pair,
                    "order": order,
                }
                if not stopped and runner.ledger.remaining_usd < cfg.budget.min_run_reserve_usd:
                    stopped = (
                        f"remaining ${runner.ledger.remaining_usd:.4f} below per-run reserve "
                        f"${cfg.budget.min_run_reserve_usd:.2f}"
                    )
                if stopped:
                    cell.update(status="missing", reason=stopped)
                else:
                    res = runner.run(
                        generate(task), mode, label=f"compare {stamp} pair {pair} rep {rep}"
                    )
                    _print_result(res)
                    m = res.metrics
                    cell.update(
                        run_id=res.run_id,
                        status=res.status,
                        outcome=m["score"]["outcome"],
                        strict=m["score"]["strict_success"],
                        cost_usd=m["cost"]["incurred_usd"],
                        valid=m["valid_for_comparison"],
                    )
                    if res.status in COMPARE_HALT:
                        stopped = f"{COMPARE_HALT[res.status]} in run {res.run_id}"
                        if res.status == "accounting_bound_violated":
                            exit_code = 4
                cells.append(cell)
                out.write_text(
                    json.dumps(
                        {
                            "comparison": stamp,
                            "frozen": frozen,
                            "frozen_prompt_version": PROMPT_VERSION,
                            "halted": stopped or None,
                            "cells": cells,
                        },
                        indent=1,
                    )
                )
            pair += 1
    return cells, exit_code


def cmd_compare(args: argparse.Namespace) -> int:
    cfg = _config(args)
    runner = _live_runner(cfg, args.max_usd)
    tasks = args.tasks.split(",") if args.tasks else list(HELDOUT)
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    out = _runs_dir(cfg) / "comparisons" / f"{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    cells, code = run_matrix(runner, cfg, tasks, args.reps, out, stamp)
    done = sum(1 for c in cells if c["status"] != "missing")
    print(f"comparison {stamp}: {done}/{len(cells)} cells run; record {out}")
    if code:
        print("HALTED: accounting bound violated; no further calls were dispatched")
    return code


def runner_prompt_version() -> str:
    from .prompts import PROMPT_VERSION

    return PROMPT_VERSION


def cmd_report(args: argparse.Namespace) -> int:
    from .report import build_report

    cfg = _config(args)
    text = build_report(_runs_dir(cfg), _resolve(cfg.budget.ledger))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .export import ExportConflict, export_experiment

    cfg = _config(args)
    try:
        log = export_experiment(
            Path(args.experiment), _runs_dir(cfg), _resolve(cfg.budget.ledger), Path(args.dest)
        )
    except ExportConflict as exc:
        print(f"export refused: {exc}")
        return 5
    print(json.dumps(log, indent=1))
    return 0


def cmd_budget(args: argparse.Namespace) -> int:
    cfg = _config(args)
    print(json.dumps(_ledger(cfg, None).summary(), indent=1))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="clm-lib", description=__doc__)
    ap.add_argument("--config", help="TOML config (default: configs/default.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check executor, credentials (presence only), prices, budget")
    d.add_argument(
        "--probe", action="store_true", help="free models-endpoint call to verify access"
    )
    d.add_argument("--sandbox", action="store_true", help="run the sandbox boundary self-test")
    d.set_defaults(func=cmd_doctor)

    f = sub.add_parser("fixtures", help="write a task instance's fixture files (no ground truth)")
    f.add_argument("--task", choices=sorted(INSTANCES), default="dev")
    f.add_argument("--out", required=True)
    f.set_defaults(func=cmd_fixtures)

    o = sub.add_parser(
        "demo-offline", help="scripted-model demo through the real sandbox (no API calls)"
    )
    o.set_defaults(func=cmd_demo_offline)

    s = sub.add_parser("smoke", help="one minimal live provider call (paid, bounded)")
    s.add_argument("--max-usd", type=float, default=None)
    s.set_defaults(func=cmd_smoke)

    r = sub.add_parser("run", help="one live task run (paid, bounded)")
    r.add_argument("--mode", choices=MODES, default="clm")
    r.add_argument("--task", choices=sorted(INSTANCES), default="dev")
    r.add_argument("--label", default="")
    r.add_argument("--max-usd", type=float, default=None)
    r.set_defaults(func=cmd_run)

    g = sub.add_parser(
        "guided", help="live guided helper demonstration (prompted; not comparative)"
    )
    g.add_argument("--task", choices=sorted(INSTANCES), default="dev")
    g.add_argument("--max-usd", type=float, default=None)
    g.set_defaults(func=cmd_guided)

    c = sub.add_parser("compare", help="live summary-vs-CLM comparison on held-out tasks")
    c.add_argument("--reps", type=int, default=2)
    c.add_argument("--tasks", default="", help=f"comma list (default {','.join(HELDOUT)})")
    c.add_argument("--max-usd", type=float, default=None)
    c.set_defaults(func=cmd_compare)

    rp = sub.add_parser("report", help="markdown report from recorded runs and the ledger")
    rp.add_argument("--out", default="")
    rp.set_defaults(func=cmd_report)

    ex = sub.add_parser("export", help="copy an experiment's evidence into a results repository")
    ex.add_argument(
        "experiment", help="experiment directory, e.g. experiments/001-short-incident-pilot"
    )
    ex.add_argument("--dest", required=True, help="results repository root")
    ex.set_defaults(func=cmd_export)

    b = sub.add_parser("budget", help="show the persistent spend ledger")
    b.set_defaults(func=cmd_budget)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
