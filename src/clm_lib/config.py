"""Configuration loading (TOML) with documented defaults."""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class ProviderConfig:
    name: str = "anthropic"
    model: str = "claude-opus-5-5"
    base_url: str = ""  # empty: SDK default (or ANTHROPIC_BASE_URL)
    effort: str = "low"
    structured_output: bool = True
    timeout_s: float = 120.0
    api_retries: int = 2


@dataclass
class Limits:
    max_calls: int = 20  # all provider attempts per task: actions, repairs, summaries, retries
    max_output_tokens: int = 2048
    exec_timeout_s: int = 30
    output_cap_chars: int = 6000
    context_budget_tokens: int = 8000  # whole request input (system + task + status + context)
    pressure_ratio: float = 0.70
    recovery_reserve_tokens: int = 1000
    spill_min_chars: int = 1500
    initial_chars_per_token: float = 3.0
    max_code_chars: int = 20_000
    # Experiments 001-006 count summary calls against max_calls. With this false, max_calls
    # covers task calls only (actions, repairs, their retries) and summary calls have their own
    # cap, so the summary baseline is not denied investigation steps.
    summary_calls_in_max_calls: bool = True
    max_summary_calls: int = 0
    # Defect fix found in experiment-007 calibration: when a CLM step both edits the context and
    # prints a large observation, the runtime appends a receipt after the observation, and the
    # spill rule (which only looked at the last entry) could not move the observation out. True
    # also spills an observation followed directly by its receipt. False keeps the original rule.
    spill_past_receipt: bool = False
    # Defect fix found in the revised experiment-007 calibration: the CLM PRESSURE notice is added
    # after the hard-limit checks, and could itself push a request just over the hard limit, ending
    # the run without the spill or recovery handling. True re-checks after adding the notice.
    recheck_after_notice: bool = False


@dataclass
class BaselineConfig:
    # "fixed-tail/1": experiments 001/002 policy (keep the newest `tail_entries` verbatim).
    # "token-tail/1": experiment 003 policy (token-bounded tail, room-leaving summaries).
    policy: str = "fixed-tail/1"
    tail_entries: int = 4
    summary_max_chars: int = 3000
    summary_retries: int = 1
    # token-tail/1 only (fractions of the request budget)
    tail_ratio: float = 0.15
    newest_ratio: float = 0.25
    target_ratio: float = 0.50
    summary_min_chars: int = 600


@dataclass
class RequestConfig:
    # "single-user/1": experiments 001-005 (one system string; one user string with <task>,
    # <runtime_status>, then <working_context>). "blocks/1": experiment 006 (text content
    # blocks; stable task first, one block per context entry, runtime status last).
    layout: str = "single-user/1"
    # Provider prompt caching (blocks/1 only): explicit 5-minute breakpoints on the task block
    # and on the last working-context block; off sends no cache_control at all.
    prompt_caching: bool = False
    cache_ttl: str = "5m"
    # "run-tag/1": a fixed-length random per-run tag is the first system block, so cache
    # prefixes (and any cache entry) are unique to the run. "none": no tag.
    run_isolation: str = "none"


@dataclass
class BudgetConfig:
    ceiling_usd: float = 10.0
    min_run_reserve_usd: float = 0.75
    ledger: str = "runs/ledger.json"
    prices: str = "configs/prices.toml"


@dataclass
class SandboxConfig:
    image: str = "python:3.12-slim"
    memory: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 128


@dataclass
class Config:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    limits: Limits = field(default_factory=Limits)
    baseline: BaselineConfig = field(default_factory=BaselineConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    request: RequestConfig = field(default_factory=RequestConfig)
    runs_dir: str = "runs"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fill(cls: type, data: dict[str, Any]) -> Any:
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**data)


def load_config(path: Path | None) -> Config:
    if path is None:
        return Config()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    sections = {
        "provider": ProviderConfig,
        "limits": Limits,
        "baseline": BaselineConfig,
        "budget": BudgetConfig,
        "sandbox": SandboxConfig,
        "request": RequestConfig,
    }
    kwargs: dict[str, Any] = {}
    for key, cls in sections.items():
        if key in data:
            kwargs[key] = _fill(cls, data.pop(key))
    if "runs_dir" in data:
        kwargs["runs_dir"] = str(data.pop("runs_dir"))
    if data:
        raise ValueError(f"unknown config sections: {sorted(data)}")
    cfg = Config(**kwargs)
    validate_request(cfg.request)
    return cfg


LAYOUTS = ("single-user/1", "blocks/1")


def validate_request(r: RequestConfig) -> None:
    if r.layout not in LAYOUTS:
        raise ValueError(f"request.layout must be one of {LAYOUTS}")
    if r.run_isolation not in ("none", "run-tag/1"):
        raise ValueError("request.run_isolation must be 'none' or 'run-tag/1'")
    if r.cache_ttl not in ("5m", "1h"):
        raise ValueError("request.cache_ttl must be '5m' or '1h'")
    if r.prompt_caching and r.layout == "single-user/1":
        raise ValueError("prompt caching requires request.layout = 'blocks/1'")
    if r.run_isolation != "none" and r.layout == "single-user/1":
        raise ValueError("run isolation requires request.layout = 'blocks/1'")
