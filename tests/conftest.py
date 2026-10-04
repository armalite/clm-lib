from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from clm_lib.budget import Ledger, ModelPrice
from clm_lib.config import Config
from clm_lib.executor import DockerExecutor, ExecResult
from clm_lib.provider import ScriptedProvider
from clm_lib.runner import Runner

_DOCKER: tuple[bool, str] | None = None


def docker_ok() -> tuple[bool, str]:
    global _DOCKER
    if _DOCKER is None:
        _DOCKER = DockerExecutor().check()
    return _DOCKER


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    ok, why = docker_ok()
    if ok:
        return
    skip = pytest.mark.skip(reason=f"Docker sandbox unavailable: {why}")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


TEST_PRICE = ModelPrice("scripted", 4.0, 5.0, 8.0, 0.2, 20.0, "test", "test")


@dataclass
class FakeExecutor:
    """Test double that never runs code: returns canned output, optionally writes files.

    Used only for runner-logic tests (summaries, limits, accounting). Every
    test about model-generated code uses the real DockerExecutor.
    """

    outputs: list[str] = field(default_factory=list)
    writes: dict[int, dict[str, str]] = field(default_factory=dict)
    isolation: str = "fake-test-double"
    calls: int = 0

    def describe(self) -> dict[str, object]:
        return {"kind": "fake-test-double"}

    def run(self, code: str, workspace: Path, fixtures: Path) -> ExecResult:
        self.calls += 1
        for rel, text in self.writes.get(self.calls, {}).items():
            (workspace / rel).write_text(text)
        out = self.outputs.pop(0) if self.outputs else "ok"
        return ExecResult(0, out, "", False, len(out.encode()), 0, 0.01)


def execute(code: str, thought: str = "") -> dict[str, Any]:
    return {"action": "execute", "thought": thought, "code": code}


FINAL = {
    "action": "final",
    "answer": {
        "root_cause": "DB_POOL_EXHAUSTED: x",
        "required_value": "0",
        "remedy": "RAISE_DB_POOL_LIMIT: x",
        "evidence_refs": [],
    },
}


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def make_runner(tmp_path: Path, cfg: Config) -> Any:
    def factory(
        script: list[Any],
        executor: Any = None,
        price: ModelPrice | None = None,
        ceiling: float = 0.0,
        config: Config | None = None,
    ) -> tuple[Runner, ScriptedProvider]:
        prov = ScriptedProvider(list(script))
        ledger = Ledger.open(tmp_path / "ledger.json", ceiling)
        runner = Runner(
            config or cfg,
            prov,
            executor if executor is not None else DockerExecutor(),
            ledger,
            price,
            live=False,
            runs_dir=tmp_path / "runs",
            sleep=lambda s: None,
        )
        return runner, prov

    return factory


def working_ids(user: str) -> list[str]:
    block = user.split("<working_context>\n", 1)[1].split("\n</working_context>", 1)[0]
    return [json.loads(line)["id"] for line in block.splitlines() if line.strip()]


def working_block(user: str) -> str:
    return user.split("<working_context>\n", 1)[1].split("\n</working_context>", 1)[0]
