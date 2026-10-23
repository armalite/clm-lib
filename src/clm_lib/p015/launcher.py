"""The resumable 015 launcher (`p015-launch/1`): runs planned sessions of the three core arms, independently of any
chat session, with durable state on disk and spending controls (`spend.py`).

    python -m clm_lib.p015.launcher plan SPEC.json LAUNCH_DIR
    python -m clm_lib.p015.launcher run LAUNCH_DIR [--max-sessions N]
    python -m clm_lib.p015.launcher status LAUNCH_DIR
    python -m clm_lib.p015.launcher stop LAUNCH_DIR [--now | --clear]
    python -m clm_lib.p015.launcher resolve LAUNCH_DIR SESSION --as abandoned|rerun --reason TEXT
    python -m clm_lib.p015.launcher export LAUNCH_DIR DEST_DIR

**The launch contract** (`contract.json`, written once by `plan`, with its digest in `contract.sha256`) fixes the
session identities, their run order, each session's cache tag, the arms' settings, the model configuration, the
ledger and authorisation paths, and the fingerprints of the configuration and of the loaded source (clm-lib,
guarded-clm and the workload versions). `run` refuses if the contract, its digest or a fingerprint differs.

**State:** `status/<session>.json` (atomic writes) and the append-only `events.jsonl`. Each session's run is a
recorder directory, `runs/<session>/r<k>`. A file lock (`launcher.lock`) stops a second launcher from dispatching
anything while one runs.

**Restart:**
- finished sessions are skipped;
- failures are kept;
- a session found `running` was interrupted. If an attempt had started without finishing, its outcome is unknown,
  and the session is marked `uncertain`; otherwise it is `interrupted`.

Neither is redispatched automatically: `resolve` marks it `abandoned` (a missing cell) or authorises a rerun, which
gets a new run directory while the old one is kept. Provider execution after an uncertain network outcome is never
assumed to be exactly-once; the ledger charges such an attempt at its full reservation.

**Task and turn endings (2026-10-19):** the contract records `task_version` (`docthread-task/1` or `/2`, the same
wording for every arm) and `turn_endings`:
- `p015-turn-endings/1`: the first turn that does not complete ends the session; its unfinished questions score as
  missing;
- `p015-turn-endings/2`: the same, except that a turn ending at the main model's step or call limit ends only that
  turn (its fields score as unanswered) and the next scheduled turn runs in the same session. Only runners that
  support it may be planned with it (`CONTINUATION_ARMS`).

In both, a response stopped at the output limit ends the session as `output_truncated` (`p015-truncation/1`); it is
never retried, excluded or replaced automatically. A contract without these fields predates them (task version 1,
every incomplete turn ending the session, no truncation ending). Unsupported versions are refused.

**Stopping:** `stop` stops between sessions; `stop --now`, SIGTERM or SIGINT stop before the next provider attempt
or turn, and the session is marked `stopped`.

**Spending:** before every turn and every provider attempt (summariser attempts and retries included), the
authorisation is reread and the programme ceiling and block stop are checked against confirmed, assumed and open
spend. A refused attempt never reaches the model. Nothing here creates or raises an authorisation or a ceiling.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import math
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from ..budget import Ledger, ModelPrice, load_prices
from .accounting import RETRY_VERSION, TRUNCATION_VERSION, LaunchStopped, SpendRefused
from .arms import CONTINUATION_BLOCKER, TOOLS_CONTINUATION_BLOCKER, ArmSession
from .docthread.agent import Investigator
from .docthread.fixture import DEV_INSTANCES, GENERATOR_VERSION, generate
from .docthread.offline import SYNTHETIC_CHARS_PER_TOKEN, SyntheticUsageModel
from .docthread.plain_agent import PlainInvestigator
from .docthread.scoring import SCORER_VERSION, score_session
from .docthread.task import SYSTEM_PROMPT, TASK_V1, TASK_VERSIONS, questions, user_message
from .docthread.tools import EvidenceService
from .export import export_session, verify_export
from .live import (
    ModelConfig,
    credential_source_present,
    live_model,
    new_session_tag,
    tagged_system_prompt,
)
from .paper.capability import PaperSettings
from .papertools import TOOLS_VERSION, ToolsSettings
from .provenance import library_provenance, tree_fingerprint, versions
from .records import read_jsonl
from .session import (
    CONTINUABLE_ENDINGS,
    REQUEST_LAYOUT_VERSION,
    ContinuationUnsupported,
    GuardedSession,
    GuardedSettings,
    guarded_option_problems,
    obsolete_setting_problems,
)
from .spend import SPEND_VERSION, SpendGate
from .summary.capability import SummarySettings

LAUNCH_VERSION = "p015-launch/1"
ARMS = ("guarded_clm", "paper_clm_shell", "token_tail_p015")  # the comparison set, and the default
TOOLS_ARM = "paper_clm_tools"  # planned only by name (paper-clm-tools/1); not in the default set
KNOWN_ARMS = (*ARMS, TOOLS_ARM)
EXIT_OK, EXIT_LOCKED, EXIT_CONTRACT, EXIT_SPEND, EXIT_STOPPED, EXIT_USAGE = 0, 3, 4, 5, 6, 2
TERMINAL = ("finished", "uncertain", "interrupted", "stopped", "abandoned")
ARM_FAILURES = ("context_limit", "step_limit", "call_cap", "summary_call_cap", "model_behaviour")
HARNESS_DEFECTS = (
    "harness_defect",
    "sandbox_unavailable",
    "unclassified_error",
    "tool_failure",
    "request_incompatible",
)
RERUNNABLE_OUTCOMES = ("harness_defect", "provider_failure", "spend_stopped")
TURN_ENDINGS_V1, TURN_ENDINGS_V2 = "p015-turn-endings/1", "p015-turn-endings/2"
TURN_ENDINGS = (TURN_ENDINGS_V1, TURN_ENDINGS_V2)
CONTINUATION_ARMS = (
    "guarded_clm",
)  # runners whose existing interface can start a turn after a limit-ended one

# A live plan must state every experimental choice explicitly (2026-10-16): code defaults are proposals, not
# approvals, and must never be adopted silently. Scripted plans may rely on defaults.
LIVE_MODEL_FIELDS = {
    "thinking": '"adaptive" or "disabled"',
    "effort": 'an effort level, or null for "not sent"',
    "max_tokens": "an integer",
    "cache_ttl": '"5m", "1h", or null for no caching',
    "cache_breakpoints": 'a list drawn from "automatic", "tool_definitions", "instructions" (may be empty)',
    "max_retries": "the bounded retry count of p015-retry/1",
}
# A live plan names the guarded-clm implementation it was made for; plan() checks it against what is loaded.
LIBRARY_PIN_FIELDS = ("version", "revision", "source_sha256")
# guarded-clm's request layout and context cache point (p015-request-layout/1): a live plan states both.
GUARDED_LIVE_FIELDS = {
    "request_layout": '"single", "segmented" or "segmented_task_last"',
    "cache_point": '"5m", "1h", or null for no context cache point',
}


def library_pin_problems(spec: dict[str, Any]) -> list[str]:
    """A stated guarded-clm pin that differs from the loaded library (any mode)."""
    pin = (spec.get("library") or {}).get("guarded_clm")
    if not pin:
        return []
    loaded = library_provenance()
    actual = {"version": loaded.get("version"), "revision": (loaded.get("git") or {}).get("revision"),
              "source_sha256": loaded.get("source_sha256")}  # fmt: skip
    out = [f"library.guarded_clm.{k} is {pin[k]!r} but the loaded guarded-clm has {actual[k]!r}"
           for k in LIBRARY_PIN_FIELDS if k in pin and pin[k] != actual[k]]  # fmt: skip
    if (loaded.get("git") or {}).get("package_dirty"):
        out.append(
            "the loaded guarded-clm package has uncommitted changes, so it cannot match a pinned revision"
        )
    return out


# Rules the paper arm implements in code without a setting; a live plan must acknowledge each one explicitly.
# paper_clm_tools: the settings a live plan must state (the code defaults are the structured-tools design's).
TOOLS_LIVE_FIELDS = {
    "version": f'"{TOOLS_VERSION}" or "paper-clm-tools/2"',
    "budget_tokens": "an integer (the enforced limit is this minus reserve_tokens)",
    "max_retries": "an integer: rollback retries per session (0 ends the turn at the first over-limit request)",
    "readouts": "true or false: whether every tool result ends with the context size",
}
PAPER_RULES_IMPLEMENTED = {
    "reminder_point": "limit_minus_reminder_before_tokens",
    "reminder_repeat": "once_per_crossing",
    "rejected_edit_free_step": "free",
}


def live_spec_problems(spec: dict[str, Any]) -> list[str]:
    """What a live plan leaves implicit; empty when every choice is stated (or the plan is scripted)."""
    if spec.get("mode", "scripted") != "live":
        return []
    out = []
    model = spec.get("model") or {}
    for key, allowed in LIVE_MODEL_FIELDS.items():
        if key not in model:
            out.append(f"model.{key} is not set ({allowed})")
    if spec.get("retry_policy") != RETRY_VERSION:
        out.append(f'retry_policy must be stated as "{RETRY_VERSION}" (with model.max_retries)')
    if "task_version" not in spec:
        out.append(f"task_version is not set (one of {', '.join(TASK_VERSIONS)})")
    if "turn_endings" not in spec:
        out.append(f"turn_endings is not set (one of {', '.join(TURN_ENDINGS)})")
    arms = list(spec.get("arms", ARMS))
    settings = spec.get("arm_settings") or {}
    if "guarded_clm" in arms:
        pin = (spec.get("library") or {}).get("guarded_clm") or {}
        for key in LIBRARY_PIN_FIELDS:
            if not pin.get(key):
                out.append(f"library.guarded_clm.{key} is not set (the accepted guarded-clm {key})")
        guarded = settings.get("guarded_clm", {})
        for key, allowed in GUARDED_LIVE_FIELDS.items():
            if key not in guarded:
                out.append(f"arm_settings.guarded_clm.{key} is not set ({allowed})")
    if "paper_clm_shell" in arms:
        paper = settings.get("paper_clm_shell", {})
        for key, allowed in (
            ("profile", '"paper" or "readme"'),
            ("reminder_before_tokens", "an integer"),
            ("version", '"paper-clm-shell/1" or "paper-clm-shell/2"'),
        ):
            if key not in paper:
                out.append(f"arm_settings.paper_clm_shell.{key} is not set ({allowed})")
        rules = spec.get("paper_rules") or {}
        for key, implemented in PAPER_RULES_IMPLEMENTED.items():
            if key not in rules:
                out.append(
                    f'paper_rules.{key} is not set (the implemented rule is "{implemented}")'
                )
            elif rules[key] != implemented:
                out.append(
                    f'paper_rules.{key} = "{rules[key]}" is not implemented (only "{implemented}"); '
                    "choosing it needs a reviewed change to the arm"
                )
    if TOOLS_ARM in arms:
        tools = settings.get(TOOLS_ARM, {})
        for key, allowed in TOOLS_LIVE_FIELDS.items():
            if key not in tools:
                out.append(f"arm_settings.{TOOLS_ARM}.{key} is not set ({allowed})")
    if "token_tail_p015" in arms and "summary_call_cap" not in settings.get("token_tail_p015", {}):
        out.append(
            "arm_settings.token_tail_p015.summary_call_cap is not set (the summariser allowance per turn)"
        )
    return out


ModelHook = Callable[
    [str, int, list[ModelMessage]], None
]  # (session, call index, request): tests only


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------------- planning


def balanced_order(instances: list[str], arms: list[str]) -> list[tuple[str, str]]:
    """Instance-major; instance k runs the arms rotated by k (a cyclic Latin square), so over a multiple of
    len(arms) instances every arm holds every position equally often."""
    out = []
    for k, inst in enumerate(instances):
        rot = k % len(arms)
        out += [(inst, arm) for arm in arms[rot:] + arms[:rot]]
    return out


def version_problems(spec: dict[str, Any]) -> list[str]:
    """Unsupported task or turn-ending versions, and a continuation policy an arm cannot follow (any mode)."""
    out = []
    task = spec.get("task_version", TASK_V1)
    if task not in TASK_VERSIONS:
        out.append(
            f"task_version {task!r} is not supported (supported: {', '.join(TASK_VERSIONS)})"
        )
    endings = spec.get("turn_endings", TURN_ENDINGS_V1)
    if endings not in TURN_ENDINGS:
        out.append(
            f"turn_endings {endings!r} is not supported (supported: {', '.join(TURN_ENDINGS)})"
        )
    elif endings == TURN_ENDINGS_V2:
        for arm in spec.get("arms", ARMS):
            if arm not in CONTINUATION_ARMS:
                blocker = TOOLS_CONTINUATION_BLOCKER if arm == TOOLS_ARM else CONTINUATION_BLOCKER
                out.append(f"turn_endings {endings} is not supported by {arm}: {blocker}")
    return out


def fingerprints(contract_body: dict[str, Any]) -> dict[str, Any]:
    import clm_lib

    return {
        "config_sha256": hashlib.sha256(_canonical(contract_body).encode()).hexdigest(),
        "clm_lib_source_sha256": tree_fingerprint(Path(clm_lib.__file__).resolve().parent)[
            "sha256"
        ],
        "guarded_clm_source_sha256": library_provenance()["source_sha256"],
        "workload": {
            "generator": GENERATOR_VERSION,
            "task": contract_body.get("task_version", TASK_V1),
            "scorer": SCORER_VERSION,
        },
        "dependencies": versions(),
    }


def plan(spec: dict[str, Any], launch_dir: Path) -> dict[str, Any]:
    if (launch_dir / "contract.json").exists():
        raise FileExistsError(
            f"{launch_dir} already has a launch contract; contracts are immutable"
        )
    obsolete = obsolete_setting_problems((spec.get("arm_settings") or {}).get("guarded_clm"))
    if obsolete:
        raise ValueError(
            "the spec uses settings that no longer exist; nothing was written. Migrate it:\n- "
            + "\n- ".join(obsolete)
        )
    arms = list(spec.get("arms", ARMS))
    if "guarded_clm" in arms:
        invalid = guarded_option_problems(
            (spec.get("arm_settings") or {}).get("guarded_clm"), spec.get("model")
        )
        if invalid:
            raise ValueError("the guarded-clm request options are invalid; nothing was written:\n- "
                             + "\n- ".join(invalid))  # fmt: skip
    pin = library_pin_problems(spec)
    if pin:
        raise ValueError("the spec's guarded-clm pin does not match the loaded library; nothing was written:\n- "
                         + "\n- ".join(pin))  # fmt: skip
    unsupported = version_problems(spec)
    if unsupported:
        raise ValueError("the spec states an unsupported version; nothing was written:\n- "
                         + "\n- ".join(unsupported))  # fmt: skip
    missing = live_spec_problems(spec)
    if missing:
        raise ValueError(
            "a live plan must state every experimental choice explicitly; nothing was written. "
            "Missing or invalid:\n- " + "\n- ".join(missing)
        )
    arms = list(spec.get("arms", ARMS))
    for arm in arms:
        if arm not in KNOWN_ARMS:
            raise ValueError(f"unknown arm {arm!r}")
    instances = spec["instances"]
    defs = {}
    for inst in instances:
        if isinstance(inst, dict):
            defs[inst["name"]] = {"seed": inst["seed"], "mechanism": inst["mechanism"]}
        else:
            seed, mechanism = DEV_INSTANCES[inst]
            defs[inst] = {"seed": seed, "mechanism": mechanism}
    names = [i["name"] if isinstance(i, dict) else i for i in instances]
    sessions = []
    for order, (inst, arm) in enumerate(balanced_order(names, arms), start=1):
        sid = f"s{order:02d}-{inst}-{arm}"
        sessions.append(
            {
                "session_id": sid,
                "order": order,
                "instance": inst,
                "arm": arm,
                "tag": new_session_tag(),
            }
        )
    body = {
        "format": LAUNCH_VERSION,
        "spend": SPEND_VERSION,
        "launch_id": spec["launch_id"],
        "experiment": spec.get("experiment", "015"),
        "mode": spec.get("mode", "scripted"),
        "created_at": _now(),
        "questions": int(spec.get("questions", 8)),
        "instances": defs,
        "sessions": sessions,
        "model": dataclasses.asdict(ModelConfig(**spec.get("model", {}))),
        "arm_settings": spec.get("arm_settings", {}),
        "retry_policy": spec.get("retry_policy", RETRY_VERSION),
        "task_version": spec.get("task_version", TASK_V1),
        "turn_endings": spec.get("turn_endings", TURN_ENDINGS_V1),
        "truncation": TRUNCATION_VERSION,
        "paper_rules": spec.get("paper_rules", PAPER_RULES_IMPLEMENTED),
        **({"guarded_request": resolved_request(spec)} if "guarded_clm" in arms else {}),
        "library": spec.get(
            "library"
        ),  # the stated guarded-clm pin, if any (required for live plans)
        "settings_status": spec.get(
            "settings_status"
        ),  # informational: approved versus proposed choices
        "scripted": spec.get("scripted", {}),
        "price": spec.get("price"),
        "ledger": spec["ledger"],
        "ledger_ceiling_usd": spec.get("ledger_ceiling_usd"),
        "authorisation": spec["authorisation"],
    }
    if body["mode"] not in ("scripted", "live"):
        raise ValueError("mode is scripted or live")
    if "." in body["launch_id"] or "/" in body["launch_id"]:
        raise ValueError("a launch id has no dots or slashes")
    contract = {**body, "fingerprints": fingerprints(body)}
    launch_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("status", "runs"):
        (launch_dir / sub).mkdir(exist_ok=True)
    text = json.dumps(contract, indent=1, sort_keys=True) + "\n"
    (launch_dir / "contract.json").write_text(text, encoding="utf-8")
    (launch_dir / "contract.sha256").write_text(
        hashlib.sha256(text.encode()).hexdigest() + "\n", encoding="utf-8"
    )
    for s in sessions:
        _write_atomic(launch_dir / "status" / f"{s['session_id']}.json",
                      json.dumps({"session_id": s["session_id"], "state": "planned", "runs": []}, indent=1))  # fmt: skip
    _event(launch_dir, "planned", sessions=len(sessions))
    return contract


def resolved_request(spec: dict[str, Any]) -> dict[str, Any]:
    """The guarded arm's request options as they will run (the stated values, or the library's defaults)."""
    settings = GuardedSettings(**((spec.get("arm_settings") or {}).get("guarded_clm") or {}))
    return {"version": REQUEST_LAYOUT_VERSION, "request_layout": settings.request_layout,
            "cache_point": settings.cache_point}  # fmt: skip


def contract_problems(launch_dir: Path) -> list[str]:
    out = []
    text = (launch_dir / "contract.json").read_text(encoding="utf-8")
    recorded = (launch_dir / "contract.sha256").read_text(encoding="utf-8").strip()
    if hashlib.sha256(text.encode()).hexdigest() != recorded:
        out.append("contract.json does not match its recorded digest")
    contract = json.loads(text)
    out += obsolete_setting_problems((contract.get("arm_settings") or {}).get("guarded_clm"))
    if any(s["arm"] == "guarded_clm" for s in contract.get("sessions", [])):
        out += guarded_option_problems(
            (contract.get("arm_settings") or {}).get("guarded_clm"), contract.get("model")
        )
    out += version_problems({**contract, "arms": [s["arm"] for s in contract.get("sessions", [])]})
    body = {k: v for k, v in contract.items() if k != "fingerprints"}
    now = fingerprints(body)
    for key, value in contract["fingerprints"].items():
        if now.get(key) != value:
            out.append(f"fingerprint {key} differs from the launch contract")
    return out


# ---------------------------------------------------------------------------------------------------- state


def _event(launch_dir: Path, event: str, **fields: Any) -> None:
    line = _canonical({"ts": _now(), "pid": os.getpid(), "event": event, **fields})
    with (launch_dir / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_status(launch_dir: Path, sid: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (launch_dir / "status" / f"{sid}.json").read_text(encoding="utf-8")
    )
    return data


def write_status(launch_dir: Path, status: dict[str, Any]) -> None:
    _write_atomic(
        launch_dir / "status" / f"{status['session_id']}.json",
        json.dumps(status, indent=1, sort_keys=True),
    )


@contextlib.contextmanager
def launch_lock(launch_dir: Path) -> Iterator[bool]:
    fd = os.open(launch_dir / "launcher.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def in_flight_attempts(run_dir: Path) -> list[int]:
    path = run_dir / "attempts.jsonl"
    if not path.exists():
        return []
    attempts = read_jsonl(path)
    started = {a["attempt"] for a in attempts if a["event"] == "started"}
    finished = {a["attempt"] for a in attempts if a["event"] == "finished"}
    return sorted(started - finished)


def recover(launch_dir: Path) -> list[str]:
    """Sessions left `running` by a launcher that is gone: `uncertain` if an attempt was in flight."""
    changed = []
    for path in sorted((launch_dir / "status").glob("*.json")):
        st = json.loads(path.read_text(encoding="utf-8"))
        if st["state"] != "running":
            continue
        run = st["runs"][-1]
        flight = in_flight_attempts(launch_dir / run["dir"])
        st["state"] = "uncertain" if flight else "interrupted"
        run.update(ended_at=_now(), recovered=True, in_flight_attempts=flight)
        write_status(launch_dir, st)
        _event(
            launch_dir,
            f"recovered_{st['state']}",
            session=st["session_id"],
            in_flight_attempts=flight,
        )
        changed.append(st["session_id"])
    return changed


# ---------------------------------------------------------------------------------------------------- one session


def _scripted_model(
    agent: Any, synthetic: bool, faults: dict[str, Any], sid: str, hook: ModelHook | None
) -> Any:
    calls = [0]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        index = calls[0]
        calls[0] += 1
        if hook is not None:
            hook(sid, index, messages)
        if faults.get("delay_s"):
            time.sleep(
                float(faults["delay_s"])
            )  # a slow scripted model, so concurrent launchers overlap in tests
        if faults.get("hang_at_attempt") == index:
            time.sleep(3600)  # a scripted in-flight hang, for interruption tests
        if faults.get("fail_at_attempt") == index:
            raise ModelHTTPError(
                int(faults.get("fail_status", 503)), "scripted", {"error": "scripted failure"}
            )
        response: ModelResponse = agent.respond(messages, info)
        if (
            faults.get("truncate_at_attempt") == index
        ):  # a scripted output-limit stop, as the adapter reports it
            response.finish_reason, response.provider_details = (
                "length",
                {"finish_reason": "max_tokens"},
            )
        if synthetic:
            chars = sum(len(str(getattr(p, "content", ""))) for m in messages for p in m.parts)
            chars += len(info.instructions or "") + len(
                json.dumps([t.parameters_json_schema for t in info.function_tools])
            )
            response.usage = RequestUsage(
                input_tokens=math.ceil(chars / SYNTHETIC_CHARS_PER_TOKEN), output_tokens=40
            )
        return response

    cls = SyntheticUsageModel if synthetic else FunctionModel
    return cls(respond, model_name="scripted-investigator")


def run_session(
    launch_dir: Path,
    contract: dict[str, Any],
    session: dict[str, Any],
    run_index: int,
    ledger: Ledger,
    gate: SpendGate,
    hook: ModelHook | None = None,
) -> dict[str, Any]:
    sid, arm = session["session_id"], session["arm"]
    inst = contract["instances"][session["instance"]]
    fx = generate(session["instance"], inst["seed"], inst["mechanism"])
    desk = EvidenceService(fx)
    config = ModelConfig(**contract["model"])
    run_rel = f"runs/{sid}/r{run_index}"
    run_id = f"{contract['launch_id']}.{sid}.r{run_index}"
    mode = contract["mode"]
    scripted = contract.get("scripted", {})
    task_version = contract.get("task_version", TASK_V1)
    turn_endings = contract.get("turn_endings", TURN_ENDINGS_V1)
    if mode == "live":
        price = load_prices(Path("configs/prices.toml")).get(config.model)
        live_ok = bool(gate.authorisation().get("live")) and credential_source_present()
        model: Any = live_model(config)
    else:
        p = contract.get("price") or {}
        price = ModelPrice(p.get("model", "scripted"), p.get("input", 2.0), p.get("cache_write_5m", 2.5),
                           p.get("cache_write_1h", 4.0), p.get("cache_read", 0.2), p.get("output", 10.0),
                           "offline placeholder", "offline")  # fmt: skip
        live_ok = False
        default_policy = {"token_tail_p015": "none", TOOLS_ARM: "tools_keep"}.get(arm, "keep")
        policy = scripted.get("policies", {}).get(arm, default_policy)
        agent: Any = (
            Investigator(fx, policy, task_version=task_version)
            if arm == "guarded_clm"
            else PlainInvestigator(fx, policy, task_version=task_version)
        )
        faults = scripted.get("faults", {}).get(sid, {})
        model = _scripted_model(agent, scripted.get("synthetic_usage", True), faults, sid, hook)
    common: dict[str, Any] = {
        "run_dir": launch_dir / run_rel,
        "run_id": run_id,
        "ledger": ledger,
        "price": price,
        "system_prompt": tagged_system_prompt(
            SYSTEM_PROMPT, session["tag"] if config.session_tag else None
        ),
        "task_tools": desk.tools(),
        "model_settings": config.model_settings(),
        "conversation_id": run_id,
        "mode": mode,
        "live_authorised": live_ok,
        "evidence": "scripted-offline" if mode == "scripted" else "live",
        "gate": gate,
        "retry": config.retry_policy(),
    }
    settings = contract.get("arm_settings", {}).get(arm, {})
    (launch_dir / run_rel).parent.mkdir(parents=True, exist_ok=True)
    runner: Any
    if arm == "guarded_clm":
        runner = GuardedSession(model, settings=GuardedSettings(**settings), **common)
    elif arm == "paper_clm_shell":
        runner = ArmSession(arm, model, paper=PaperSettings(**settings), **common)
    elif arm == TOOLS_ARM:
        runner = ArmSession(arm, model, tools=ToolsSettings(**settings), **common)
    else:
        runner = ArmSession(arm, model, summary=SummarySettings(**settings), **common)
    runner.recorder.write_meta(launch={"launch_id": contract["launch_id"], "session_id": sid,
                                       "order": session["order"], "run_index": run_index, "tag": session["tag"],
                                       "task_version": task_version, "turn_endings": turn_endings,
                                       "truncation": TRUNCATION_VERSION})  # fmt: skip
    replies: list[str | None] = []
    calls: list[list[str]] = []
    endings: list[str] = []
    try:  # the session's loop and clients are closed however the turns end (p015-session-loop/1)
        for q in questions(fx, task_version)[: contract["questions"]]:
            try:
                gate.check_turn()
            except (SpendRefused, LaunchStopped) as exc:
                ending = "stopped" if isinstance(exc, LaunchStopped) else "spend_refused"
                runner.turns += 1
                runner.recorder.turn(
                    {"turn": runner.turns, "ending": ending, "error": f"before the turn: {exc}"}
                )
                endings.append(ending)
                break
            start = len(desk.order)
            outcome = runner.run_turn(user_message(q, task_version))
            calls.append(desk.order[start:])
            endings.append(outcome.ending)
            replies.append(outcome.output if outcome.ending == "completed" else None)
            if outcome.ending == "completed":
                continue
            if turn_endings == TURN_ENDINGS_V2 and outcome.ending in CONTINUABLE_ENDINGS:
                try:
                    kept = runner.continue_after_limit()
                except ContinuationUnsupported as exc:
                    _event(launch_dir, "continuation_refused", session=sid, turn=outcome.turn,
                           ending=outcome.ending, reason=str(exc))  # fmt: skip
                    break
                _event(launch_dir, "turn_continued", session=sid, turn=outcome.turn,
                       ending=outcome.ending, kept_messages=kept)  # fmt: skip
                continue
            break
    finally:
        runner.close()
    score = score_session(fx, replies, calls)
    attempts = read_jsonl(launch_dir / run_rel / "attempts.jsonl")
    finished = [a for a in attempts if a["event"] == "finished"]
    result = {
        "endings": endings,
        "outcome": classify_outcome(endings, contract["questions"]),
        "points": score["points"],
        "out_of": score["out_of"],
        "recall_points": score["recall_points"],
        "attempts": len(finished),
        "attempts_by_category": _count(a["category"] for a in finished),
        "refused_attempts": sum(1 for a in finished if a["outcome"] == "refused"),
        "charged_usd": round(sum(a.get("charged_usd", 0.0) for a in finished), 6),
    }
    runner.recorder.write_meta(score=score, calls_by_turn=calls, launch_result=result)
    return {"dir": run_rel, "run_id": run_id, **result}


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return dict(sorted(out.items()))


def classify_outcome(endings: list[str], questions_planned: int) -> str:
    if endings == ["completed"] * questions_planned:
        return "complete"
    last = endings[-1] if endings else "none"
    if last in ARM_FAILURES or (last == "completed" and any(e in ARM_FAILURES for e in endings)):
        return "arm_failure"  # including a run that continued past a limit-ended turn (p015-turn-endings/2)
    if last == "output_truncated":
        return "output_truncated"
    if last == "spend_refused":
        return "spend_stopped"
    if last == "stopped":
        return "stopped"
    if last == "provider_failure":
        return "provider_failure"
    return "harness_defect"  # HARNESS_DEFECTS, or any ending the harness cannot attribute


# ---------------------------------------------------------------------------------------------------- run


def open_ledger(contract: dict[str, Any]) -> Ledger:
    path = Path(contract["ledger"])
    if path.exists():
        stored = float(json.loads(path.read_text(encoding="utf-8"))["ceiling_usd"])
        return Ledger.open(path, stored)  # the stored ceiling, never a lower configured one
    if contract.get("ledger_ceiling_usd") is None:
        raise FileNotFoundError(f"no ledger at {path}, and the contract does not create one")
    return Ledger.open(path, float(contract["ledger_ceiling_usd"]))


def run(launch_dir: Path, *, max_sessions: int | None = None, hook: ModelHook | None = None) -> int:
    if (launch_dir / "STOP").exists():
        print("a stop is pending (launcher stop --clear removes it)", file=sys.stderr)
        return EXIT_STOPPED
    with launch_lock(launch_dir) as locked:
        if not locked:
            print("another launcher holds the lock", file=sys.stderr)
            return EXIT_LOCKED
        problems = contract_problems(launch_dir)
        if problems:
            _event(launch_dir, "refused_contract", problems=problems)
            print("refused: " + "; ".join(problems), file=sys.stderr)
            return EXIT_CONTRACT
        contract = json.loads((launch_dir / "contract.json").read_text(encoding="utf-8"))
        _event(launch_dir, "started")
        recover(launch_dir)
        ledger = open_ledger(contract)
        gate = SpendGate(ledger, Path(contract["authorisation"]), contract["launch_id"], contract["experiment"],
                         stop_path=launch_dir / "STOP")  # fmt: skip

        def on_signal(signum: int, frame: Any) -> None:
            del frame
            (launch_dir / "STOP").write_text("now\n", encoding="utf-8")
            _event(launch_dir, "signal", signum=signum)

        previous = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
        code, done = EXIT_OK, 0
        try:
            for session in contract["sessions"]:
                st = read_status(launch_dir, session["session_id"])
                if st["state"] != "planned":
                    continue
                if (launch_dir / "STOP").exists():
                    code = EXIT_STOPPED
                    break
                if max_sessions is not None and done >= max_sessions:
                    break
                try:
                    gate.check_turn()
                except SpendRefused as exc:
                    _event(
                        launch_dir,
                        "spend_refused_before_session",
                        session=session["session_id"],
                        reason=str(exc),
                    )
                    code = EXIT_SPEND
                    break
                except LaunchStopped:
                    code = EXIT_STOPPED
                    break
                run_index = len(st["runs"]) + 1
                st["state"] = "running"
                st["runs"].append({"index": run_index, "dir": f"runs/{session['session_id']}/r{run_index}",
                                   "pid": os.getpid(), "started_at": _now()})  # fmt: skip
                write_status(launch_dir, st)
                _event(launch_dir, "session_started", session=session["session_id"], run=run_index)
                try:
                    result = run_session(
                        launch_dir, contract, session, run_index, ledger, gate, hook
                    )
                except Exception as exc:  # a failure of the harness itself, kept as a finished run
                    result = {"dir": st["runs"][-1]["dir"], "outcome": "harness_defect",
                              "error": f"{type(exc).__name__}: {exc}"}  # fmt: skip
                st["runs"][-1].update(ended_at=_now(), **result)
                st["state"] = "stopped" if result["outcome"] == "stopped" else "finished"
                write_status(launch_dir, st)
                _event(
                    launch_dir,
                    "session_ended",
                    session=session["session_id"],
                    outcome=result["outcome"],
                )
                done += 1
                if result["outcome"] == "spend_stopped":
                    code = EXIT_SPEND
                    break
                if result["outcome"] == "stopped":
                    code = EXIT_STOPPED
                    break
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)
            _event(launch_dir, "ended", code=code, spend=gate.position())
        return code


# ---------------------------------------------------------------------------------------------------- other commands


def status(launch_dir: Path) -> dict[str, Any]:
    contract = json.loads((launch_dir / "contract.json").read_text(encoding="utf-8"))
    rows = []
    for s in contract["sessions"]:
        st = read_status(launch_dir, s["session_id"])
        last = st["runs"][-1] if st["runs"] else {}
        rows.append({"session": s["session_id"], "state": st["state"], "runs": len(st["runs"]),
                     "outcome": last.get("outcome"), "points": last.get("points")})  # fmt: skip
    out: dict[str, Any] = {"launch_id": contract["launch_id"], "sessions": rows}
    path = Path(contract["ledger"])
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        block = [
            e
            for e in data["entries"]
            if str(e.get("run_id", "")).startswith(f"{contract['launch_id']}.")
        ]
        out["block_spend"] = {
            "confirmed_usd": round(
                sum(e["charged_usd"] for e in block if e["cost_basis"] == "provider_usage"), 6
            ),
            "assumed_usd": round(
                sum(e["charged_usd"] for e in block if e["cost_basis"] != "provider_usage"), 6
            ),
            "pending_reservations": len(data.get("pending", [])),
        }
    return out


def resolve(launch_dir: Path, sid: str, as_: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("a resolution needs a reason")
    st = read_status(launch_dir, sid)
    last = st["runs"][-1] if st["runs"] else {}
    if as_ == "abandoned":
        if st["state"] not in ("uncertain", "interrupted", "stopped", "planned"):
            raise ValueError(
                f"{sid} is {st['state']}; only uncertain, interrupted, stopped or planned can be abandoned"
            )
        st["state"] = "abandoned"
    elif as_ == "rerun":
        ok = st["state"] in ("uncertain", "interrupted", "stopped") or (
            st["state"] == "finished" and last.get("outcome") in RERUNNABLE_OUTCOMES
        )
        if not ok:
            raise ValueError(
                f"{sid} ({st['state']}, {last.get('outcome')}) is a result and is not rerun"
            )
        st["state"] = "planned"
    else:
        raise ValueError("resolve as abandoned or rerun")
    st.setdefault("resolutions", []).append(
        {"ts": _now(), "as": as_, "reason": reason, "run": last.get("index")}
    )
    write_status(launch_dir, st)
    _event(launch_dir, "resolved", session=sid, as_=as_, reason=reason)
    return st


def export_launch(launch_dir: Path, dest: Path) -> dict[str, Any]:
    """Every run of every session: complete runs as verified exports; incomplete ones (uncertain, interrupted,
    stopped) as partial exports whose manifests list what is missing. A launch manifest ties them together."""
    contract = json.loads((launch_dir / "contract.json").read_text(encoding="utf-8"))
    dest.mkdir(parents=True, exist_ok=True)
    runs = []
    for s in contract["sessions"]:
        st = read_status(launch_dir, s["session_id"])
        for r in st["runs"]:
            run_dir = launch_dir / r["dir"]
            if not (run_dir / "session.json").exists():
                runs.append({"session": s["session_id"], "run": r["index"], "exported": False,
                             "reason": "no session record"})  # fmt: skip
                continue
            partial = st["state"] in ("uncertain", "interrupted", "stopped") and r is st["runs"][-1]
            out = export_session(
                run_dir, dest / "sessions", partial=partial or bool(in_flight_attempts(run_dir))
            )
            problems = verify_export(out)
            runs.append({"session": s["session_id"], "run": r["index"], "exported": out.name,
                         "partial": partial, "verify_problems": problems, "outcome": r.get("outcome")})  # fmt: skip
    home = str(Path.home())
    problems = [
        f"{name} contains a local home-directory path (use paths relative to the clm-lib checkout in the spec)"
        for name in ("contract.json", "events.jsonl")
        if home in (launch_dir / name).read_text(encoding="utf-8")
    ]
    manifest = {
        "format": "p015-launch-export/1",
        "problems": problems,
        "launch_id": contract["launch_id"],
        "contract_sha256": (launch_dir / "contract.sha256").read_text(encoding="utf-8").strip(),
        "status": status(launch_dir),
        "runs": runs,
    }
    shutil.copy2(launch_dir / "contract.json", dest / "contract.json")
    shutil.copy2(launch_dir / "events.jsonl", dest / "events.jsonl")
    (dest / "launch-manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    names = ["contract.json", "events.jsonl", "launch-manifest.json"]
    (dest / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256((dest / n).read_bytes()).hexdigest()}  {n}\n" for n in names),
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m clm_lib.p015.launcher")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("spec", type=Path)
    p.add_argument("launch_dir", type=Path)
    p = sub.add_parser("run")
    p.add_argument("launch_dir", type=Path)
    p.add_argument("--max-sessions", type=int)
    p = sub.add_parser("status")
    p.add_argument("launch_dir", type=Path)
    p = sub.add_parser("stop")
    p.add_argument("launch_dir", type=Path)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--now", action="store_true")
    g.add_argument("--clear", action="store_true")
    p = sub.add_parser("resolve")
    p.add_argument("launch_dir", type=Path)
    p.add_argument("session")
    p.add_argument("--as", dest="as_", required=True, choices=["abandoned", "rerun"])
    p.add_argument("--reason", required=True)
    p = sub.add_parser("export")
    p.add_argument("launch_dir", type=Path)
    p.add_argument("dest", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "plan":
        contract = plan(json.loads(args.spec.read_text(encoding="utf-8")), args.launch_dir)
        print(f"planned {len(contract['sessions'])} sessions in {args.launch_dir}")
        return EXIT_OK
    if args.cmd == "run":
        return run(args.launch_dir, max_sessions=args.max_sessions)
    if args.cmd == "status":
        print(json.dumps(status(args.launch_dir), indent=1))
        return EXIT_OK
    if args.cmd == "stop":
        stop = args.launch_dir / "STOP"
        if args.clear:
            stop.unlink(missing_ok=True)
        else:
            stop.write_text("now\n" if args.now else "between\n", encoding="utf-8")
        _event(
            args.launch_dir, "stop_cleared" if args.clear else "stop_requested", now=bool(args.now)
        )
        return EXIT_OK
    if args.cmd == "resolve":
        resolve(args.launch_dir, args.session, args.as_, args.reason)
        return EXIT_OK
    if args.cmd == "export":
        manifest = export_launch(args.launch_dir, args.dest)
        bad = [r for r in manifest["runs"] if r.get("verify_problems")] + manifest["problems"]
        print(f"exported {len(manifest['runs'])} runs; problems: {bad or 'none'}")
        return 1 if bad else EXIT_OK
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
