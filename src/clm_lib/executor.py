"""Isolated execution of model-generated Python.

Only :class:`DockerExecutor` is provided for real use. There is deliberately no
host-subprocess fallback: if Docker is unavailable, execution is refused.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

SANDBOX_FIXTURES = "/task/fixtures"
SANDBOX_WORKSPACE = "/task/workspace"


class ExecutorUnavailable(RuntimeError):
    """No isolated executor can be used; generated code must not run."""


@dataclass
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    stdout_bytes: int
    stderr_bytes: int
    duration_s: float
    error: str | None = None

    @property
    def truncated(self) -> bool:
        return self.stdout_bytes > len(self.stdout.encode()) or self.stderr_bytes > len(
            self.stderr.encode()
        )


class Executor(Protocol):
    isolation: str

    def run(self, code: str, workspace: Path, fixtures: Path) -> ExecResult: ...

    def describe(self) -> dict[str, object]: ...


def _bounded_reader(stream: IO[bytes], cap: int, sink: list[bytes], total: list[int]) -> None:
    kept = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        total[0] += len(chunk)
        if kept < cap:
            part = chunk[: cap - kept]
            sink.append(part)
            kept += len(part)


def _decode(parts: list[bytes]) -> str:
    return b"".join(parts).decode("utf-8", errors="replace")


@dataclass
class DockerExecutor:
    image: str = "python:3.12-slim"
    timeout_s: float = 30.0
    memory: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 128
    output_cap_bytes: int = 6000
    tmpfs_size: str = "64m"
    docker_bin: str = "docker"
    isolation: str = "docker"

    def describe(self) -> dict[str, object]:
        return {
            "kind": "docker",
            "image": self.image,
            "timeout_s": self.timeout_s,
            "memory": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "output_cap_bytes": self.output_cap_bytes,
            "network": "none",
            "user": f"{os.getuid()}:{os.getgid()} (non-root)",
            "capabilities": "all dropped; no-new-privileges",
            "root_fs": "read-only; /tmp tmpfs",
            "mounts": {SANDBOX_FIXTURES: "ro", SANDBOX_WORKSPACE: "rw"},
            "env": "HOME=/tmp only (host environment not forwarded)",
        }

    def _docker_env(self) -> dict[str, str]:
        # The docker CLI itself needs only these; nothing secret is forwarded.
        keep = (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_CERT_PATH",
        )
        return {k: v for k, v in os.environ.items() if k in keep}

    def check(self) -> tuple[bool, str]:
        if shutil.which(self.docker_bin) is None:
            return False, "docker CLI not found on PATH"
        try:
            info = subprocess.run(
                [self.docker_bin, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=20,
                env=self._docker_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"docker daemon not reachable: {exc}"
        if info.returncode != 0:
            return False, f"docker daemon not reachable: {info.stderr.strip()[:200]}"
        img = subprocess.run(
            [self.docker_bin, "image", "inspect", self.image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=20,
            env=self._docker_env(),
        )
        if img.returncode != 0:
            return False, f"sandbox image {self.image} not present; run: docker pull {self.image}"
        return True, f"docker {info.stdout.strip()}, image {self.image} {img.stdout.strip()[:19]}"

    def command(self, name: str, workspace: Path, fixtures: Path) -> list[str]:
        ws = workspace.resolve()
        fx = fixtures.resolve()
        return [
            self.docker_bin,
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids_limit),
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,size={self.tmpfs_size},mode=1777",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--mount",
            f"type=bind,source={fx},target={SANDBOX_FIXTURES},readonly",
            "--mount",
            f"type=bind,source={ws},target={SANDBOX_WORKSPACE}",
            "--workdir",
            SANDBOX_WORKSPACE,
            self.image,
            # In-container time limit; the host keeps a backstop kill.
            "timeout",
            "--kill-after=2",
            str(int(self.timeout_s)),
            # -E/-s: ignore PYTHON* env and user site; cwd stays importable for helpers.
            "python",
            "-E",
            "-s",
            "-",
        ]

    def run(self, code: str, workspace: Path, fixtures: Path) -> ExecResult:
        ok, why = self.check_cached()
        if not ok:
            raise ExecutorUnavailable(why)
        name = f"clm-exec-{uuid.uuid4().hex[:12]}"
        cmd = self.command(name, workspace, fixtures)
        start = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._docker_env(),
        )
        assert proc.stdin and proc.stdout and proc.stderr
        out_parts: list[bytes] = []
        err_parts: list[bytes] = []
        out_total = [0]
        err_total = [0]
        readers = [
            threading.Thread(
                target=_bounded_reader,
                args=(proc.stdout, self.output_cap_bytes, out_parts, out_total),
                daemon=True,
            ),
            threading.Thread(
                target=_bounded_reader,
                args=(proc.stderr, self.output_cap_bytes, err_parts, err_total),
                daemon=True,
            ),
        ]
        for t in readers:
            t.start()
        try:
            proc.stdin.write(code.encode("utf-8"))
            proc.stdin.close()
        except BrokenPipeError:
            pass
        timed_out = False
        try:
            # Backstop above the in-container limit (container start-up is not counted there).
            proc.wait(timeout=self.timeout_s + 20)
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(
                [self.docker_bin, "kill", name],
                capture_output=True,
                timeout=30,
                env=self._docker_env(),
            )
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for t in readers:
            t.join(timeout=5)
        duration = time.monotonic() - start
        error = None
        if proc.returncode == 124:
            timed_out = True
        elif proc.returncode == 137 and not timed_out:
            error = "process killed (SIGKILL; memory or pid limit, or timeout escalation)"
        elif proc.returncode == 125:
            error = "docker failed to start the sandbox container"
        return ExecResult(
            exit_code=proc.returncode,
            stdout=_decode(out_parts),
            stderr=_decode(err_parts),
            timed_out=timed_out,
            stdout_bytes=out_total[0],
            stderr_bytes=err_total[0],
            duration_s=round(duration, 3),
            error=error,
        )

    _checked: tuple[bool, str] | None = None

    def check_cached(self) -> tuple[bool, str]:
        if self._checked is None or not self._checked[0]:
            self._checked = self.check()
        return self._checked
