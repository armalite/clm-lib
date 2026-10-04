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
    }
    kwargs: dict[str, Any] = {}
    for key, cls in sections.items():
        if key in data:
            kwargs[key] = _fill(cls, data.pop(key))
    if "runs_dir" in data:
        kwargs["runs_dir"] = str(data.pop("runs_dir"))
    if data:
        raise ValueError(f"unknown config sections: {sorted(data)}")
    return Config(**kwargs)
