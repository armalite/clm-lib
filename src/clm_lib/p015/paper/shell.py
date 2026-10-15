"""The paper arm's shell: one bash command per call, in a fresh Docker container.

The container uses the isolation of clm-lib's executor: no network, a non-root user, all capabilities dropped,
`no-new-privileges`, memory, CPU and process limits, a read-only root with a private `/tmp`, and nothing from the
host environment except `HOME=/tmp`. It sees only two host directories:

- the mirror directory, at `/tmp/.live_ctx` (read-write), holding `LIVE_CTX_MAIN.txt`;
- a per-session scratch directory, at `/work` (read-write, the working directory), which persists between commands.

No task evidence, fixture, scorer data, credential or other host path is mounted: the docthread evidence is reached
only through the shared task tools, as in every arm. The Docker socket is never mounted, and there is no host
fallback: if Docker is unavailable, the command is refused.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ...executor import ExecResult, ExecutorUnavailable, _bounded_reader, _decode

MIRROR_DIR = "/tmp/.live_ctx"
MIRROR_FILE = f"{MIRROR_DIR}/LIVE_CTX_MAIN.txt"
WORK_DIR = "/work"


@dataclass
class ShellSandbox:
    mirror_dir: Path
    work_dir: Path
    image: str = "python:3.12-slim"
    timeout_s: int = 30
    memory: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 128
    capture_bytes: int = 32_004  # tool-output-cap/1 needs at least 4 x 8,000 + 4 bytes per stream
    docker_bin: str = "docker"

    def _env(self) -> dict[str, str]:
        keep = (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_CERT_PATH",
        )
        return {k: v for k, v in os.environ.items() if k in keep}

    def command(self, name: str, script: str) -> list[str]:
        return [
            self.docker_bin, "run", "--rm", "-i", "--name", name,
            "--network", "none",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", self.memory, "--memory-swap", self.memory,
            "--cpus", str(self.cpus), "--pids-limit", str(self.pids_limit),
            "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--env", "HOME=/tmp",
            "--mount", f"type=bind,source={self.mirror_dir.resolve()},target={MIRROR_DIR}",
            "--mount", f"type=bind,source={self.work_dir.resolve()},target={WORK_DIR}",
            "--workdir", WORK_DIR,
            self.image,
            "timeout", "--kill-after=2", str(self.timeout_s), "bash", "-c", script,
        ]  # fmt: skip

    def run(self, script: str) -> ExecResult:
        try:
            check = subprocess.run(
                [self.docker_bin, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=20,
                env=self._env(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutorUnavailable(
                f"docker is not available ({type(exc).__name__}); the shell is refused"
            ) from exc
        if check.returncode != 0:
            raise ExecutorUnavailable("docker daemon not reachable; the shell is refused")
        name = f"clm-shell-{uuid.uuid4().hex[:12]}"
        start = time.monotonic()
        proc = subprocess.Popen(
            self.command(name, script),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
        )
        assert proc.stdout and proc.stderr
        out: list[bytes] = []
        err: list[bytes] = []
        out_total, err_total = [0], [0]
        readers = [
            threading.Thread(
                target=_bounded_reader,
                args=(proc.stdout, self.capture_bytes, out, out_total),
                daemon=True,
            ),
            threading.Thread(
                target=_bounded_reader,
                args=(proc.stderr, self.capture_bytes, err, err_total),
                daemon=True,
            ),
        ]
        for t in readers:
            t.start()
        timed_out = False
        try:
            proc.wait(timeout=self.timeout_s + 20)
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(
                [self.docker_bin, "kill", name],
                capture_output=True,
                timeout=30,
                env=self._env(),
                check=False,
            )
            proc.wait(timeout=30)
        for t in readers:
            t.join(timeout=5)
        code = proc.returncode
        return ExecResult(
            exit_code=code,
            stdout=_decode(out),
            stderr=_decode(err),
            timed_out=timed_out or code == 124,
            stdout_bytes=out_total[0],
            stderr_bytes=err_total[0],
            duration_s=round(time.monotonic() - start, 3),
            error="docker failed to start the sandbox container" if code == 125 else None,
        )
