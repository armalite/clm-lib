"""Command-line interface. Every command uses the same ``clm_lib`` core."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .budget import BudgetExhausted, Ledger, ModelPrice, load_prices, reservation_input_tokens
from .config import Config, load_config
from .executor import DockerExecutor
from .provider import (
    ACTION_SCHEMA,
    AnthropicProvider,
    ModelRequest,
    ProviderError,
    ScriptedProvider,
)
from .runner import MODES, Runner
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
        {"action": "execute", "thought": "look around",
         "code": "import os\nprint('BULKY-' + 'OBSERVATION-MARKER')\nprint('x' * 3000)\nprint(sorted(os.listdir('/task/fixtures')))"},
        {"action": "execute", "thought": "drop the bulky observation, keep a note",
         "code": ("import json\nd = json.load(open('context.json'))\n"
                  "m = 'BULKY-' + 'OBSERVATION-MARKER'\n"
                  "d['entries'] = [e for e in d['entries'] if m not in e['body']]\n"
                  "d['entries'].append({'id': 'n1', 'role': 'note', 'body': 'fixtures listed; bulky output dropped'})\n"
                  "json.dump(d, open('context.json', 'w'))\nprint('edited')")},
        {"action": "final", "answer": {"root_cause": "DB_POOL_EXHAUSTED: scripted", "required_value": "0",
                                       "remedy": "RAISE_DB_POOL_LIMIT: scripted", "evidence_refs": []}},
    ]  # fmt: skip


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
    reserve = price.max_cost(reservation_input_tokens(len(json.dumps(payload))), req.max_tokens)
    try:
        res = ledger.reserve(reserve, run_id, "smoke")
    except BudgetExhausted as exc:
        raise SystemExit(str(exc)) from exc
    trace.save_request(payload, {"kind": "smoke", "reserved_usd": reserve})
    try:
        resp = prov.complete(req)
    except ProviderError as exc:
        entry = ledger.settle(
            res, actual_usd=0.0 if exc.billing == "none" else None, status=f"error:{exc.kind}"
        )
        trace.event("provider_error", message=str(exc)[:300], charged_usd=entry["charged_usd"])
        trace.close()
        print(f"smoke call FAILED: {exc}")
        if exc.kind == "auth":
            print(
                "  smallest fix: set ANTHROPIC_API_KEY, or run `ant auth login` to refresh the SDK profile"
            )
        return 2
    cost = price.cost(resp.usage)
    ledger.settle(res, actual_usd=cost, status="ok", usage=resp.usage)
    trace.event("response", text=resp.text, usage=resp.usage.to_dict(), stop_reason=resp.stop_reason,
                model=resp.model, request_id=resp.request_id, cost_usd=cost)  # fmt: skip
    trace.close()
    print(f"smoke OK: model={resp.model} stop={resp.stop_reason} usage={resp.usage.to_dict()}")
    print(f"  text: {resp.text[:200]}")
    print(f"  cost ${cost:.6f} (reserved ${reserve:.4f}); trace {trace.dir}")
    return 0


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
    print(
        f"  helpers created: {h['created']} | invocations: {h['invocations']} | revisions: {h['revised']}"
    )
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    cfg = _config(args)
    runner = _live_runner(cfg, args.max_usd)
    tasks = args.tasks.split(",") if args.tasks else list(HELDOUT)
    cells: list[dict[str, Any]] = []
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    out = _runs_dir(cfg) / "comparisons" / f"{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    pair = 0
    stopped = ""
    for rep in range(1, args.reps + 1):
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
                    stopped = (f"remaining ${runner.ledger.remaining_usd:.4f} below per-run reserve "
                               f"${cfg.budget.min_run_reserve_usd:.2f}")  # fmt: skip
                if stopped:
                    cell.update(status="missing", reason=stopped)
                else:
                    res = runner.run(
                        generate(task), mode, label=f"compare {stamp} pair {pair} rep {rep}"
                    )
                    _print_result(res)
                    m = res.metrics
                    cell.update(run_id=res.run_id, status=res.status, outcome=m["score"]["outcome"],
                                strict=m["score"]["strict_success"], cost_usd=m["cost"]["incurred_usd"],
                                valid=m["valid_for_comparison"])  # fmt: skip
                    if res.status == "budget_exhausted":
                        stopped = "budget exhausted during a run"
                cells.append(cell)
                out.write_text(json.dumps({"comparison": stamp, "frozen_prompt_version": runner_prompt_version(),
                                           "cells": cells}, indent=1))  # fmt: skip
            pair += 1
    done = sum(1 for c in cells if c["status"] != "missing")
    print(f"comparison {stamp}: {done}/{len(cells)} cells run; record {out}")
    return 0


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

    b = sub.add_parser("budget", help="show the persistent spend ledger")
    b.set_defaults(func=cmd_budget)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
