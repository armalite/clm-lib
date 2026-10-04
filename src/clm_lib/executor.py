"""Isolated execution of model-generated Python.

Only :class:`DockerExecutor` is provided for real use. There is deliberately no
host-subprocess fallback: if Docker is unavailable, execution is refused.
"""

from __future__ import annotations

import json
import os
import re
import secrets
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
RUNTIME_MARKER = "@@CLM-RUNTIME"

# Runs inside the container as `python -E -s -B -c BOOTSTRAP <nonce>`; model code
# arrives on stdin. Instrumentation (tamper-evident evidence, not a security control):
# - an audit hook records workspace files opened/compiled and subprocess spawns, and,
#   when context.json is opened for writing or replaced, which workspace (helper) code
#   frames were on the call stack at that moment;
# - sys.monitoring (Python 3.12) counts PY_START events for code objects defined in
#   workspace files, i.e. helper functions/module bodies that actually began executing.
# An atexit hook reports the record on stderr behind a per-execution nonce; the host
# strips that line. Code that calls os._exit or is killed produces no record, and
# low-level os.open writes are not attributed.
BOOTSTRAP = r"""
import sys, os, json, atexit
_n = sys.argv[1]; sys.argv = ['-']
_ws = '/task/workspace/'
_ctx = _ws + 'context.json'
_rec = {'opened': [], 'compiled': [], 'spawned': [], 'calls': {}, 'context_writes': []}
def _is_ws(f):
    return isinstance(f, str) and (f.startswith(_ws) or not (f.startswith('/') or f.startswith('<')))
def _p(x):
    try:
        x = os.fsdecode(x)
        return None if x.startswith('<') else os.path.abspath(x)
    except Exception:
        return None
def _ws_frames():
    out = []
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        if _is_ws(fn):
            out.append(os.path.abspath(fn) + '::' + f.f_code.co_qualname)
        f = f.f_back
    return out
def _hook(ev, args):
    if ev == 'open' and args:
        p = _p(args[0])
        if p and p.startswith(_ws):
            mode = str(args[1]) if len(args) > 1 else ''
            if len(_rec['opened']) < 200:
                _rec['opened'].append([p, mode])
            if p == _ctx and any(c in mode for c in 'wax+') and len(_rec['context_writes']) < 50:
                _rec['context_writes'].append({'via': 'open:' + mode, 'ws_frames': _ws_frames()})
    elif ev == 'os.rename' and len(args) > 1:
        if _p(args[1]) == _ctx and len(_rec['context_writes']) < 50:
            _rec['context_writes'].append({'via': 'rename', 'ws_frames': _ws_frames()})
    elif ev == 'compile' and len(args) > 1 and isinstance(args[1], str):
        p = _p(args[1])
        if p and p.startswith(_ws) and p not in _rec['compiled']:
            _rec['compiled'].append(p)
    elif ev in ('subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'):
        if len(_rec['spawned']) < 50:
            _rec['spawned'].append(repr(args)[:300])
_calls = {}
_mon = getattr(sys, 'monitoring', None)
if _mon is not None:
    try:
        _mon.use_tool_id(5, 'clm-evidence')
        def _start(code, offset):
            f = code.co_filename
            if _is_ws(f):
                k = (f, code.co_qualname)
                _calls[k] = _calls.get(k, 0) + 1
                return None
            return _mon.DISABLE
        _mon.register_callback(5, _mon.events.PY_START, _start)
        _mon.set_events(5, _mon.events.PY_START)
    except Exception:
        _mon = None
def _emit():
    if _mon is not None:
        _mon.set_events(5, 0)
    mods = []
    for m in list(sys.modules.values()):
        f = getattr(m, '__file__', None)
        if isinstance(f, str):
            f = os.path.abspath(f)
            if f.startswith(_ws):
                mods.append(f)
    _rec['imported'] = sorted(set(mods))
    _rec['calls'] = {os.path.abspath(f) + '::' + q: c for (f, q), c in _calls.items()}
    _rec['monitoring'] = _mon is not None
    try:
        sys.__stderr__.write('\n@@CLM-RUNTIME ' + _n + ' ' + json.dumps(_rec) + '\n')
        sys.__stderr__.flush()
    except Exception:
        pass
_src = sys.stdin.read()
atexit.register(_emit)
sys.addaudithook(_hook)
exec(compile(_src, '<model-code>', 'exec'), {'__name__': '__main__', '__builtins__': __builtins__})
"""


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
    # In-container audit record (imported/compiled/opened/spawned workspace files), or
    # None if the process ended without emitting it.
    runtime_trace: dict[str, object] | None = None

    @property
    def truncated(self) -> bool:
        return self.stdout_bytes > len(self.stdout.encode()) or self.stderr_bytes > len(
            self.stderr.encode()
        )


class Executor(Protocol):
    isolation: str

    def run(self, code: str, workspace: Path, fixtures: Path) -> ExecResult: ...

    def describe(self) -> dict[str, object]: ...


def _bounded_reader(
    stream: IO[bytes], cap: int, sink: list[bytes], total: list[int], tail: bytearray | None = None
) -> None:
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
        if tail is not None:
            tail.extend(chunk)
            del tail[:-16384]


def _extract_runtime(
    head: str, tail: bytes, total: int, nonce: str
) -> tuple[str, int, dict[str, object] | None]:
    """Remove the nonce-tagged runtime line from stderr; return (stderr, bytes, record)."""
    pat = re.compile(r"\n?" + re.escape(f"{RUNTIME_MARKER} {nonce} ") + r"(\{.*\})\n?")
    record = None
    text = tail.decode("utf-8", errors="replace")
    matches = list(pat.finditer(text))
    if matches:
        m = matches[-1]
        try:
            record = json.loads(m.group(1))
        except json.JSONDecodeError:
            record = None
        total -= len(m.group(0).encode("utf-8"))
    head = pat.sub("", head)
    return head, max(total, 0), record


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
            "instrumentation": "in-container audit hook records workspace imports/compiles/"
            "opens/spawns (tamper-evident, not a security control)",
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

    def command(self, name: str, workspace: Path, fixtures: Path, nonce: str) -> list[str]:
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
            # -E/-s: ignore PYTHON* env and user site; -B: no .pyc files in the workspace.
            # cwd ('' on sys.path with -c) stays importable for helpers.
            "python",
            "-E",
            "-s",
            "-B",
            "-c",
            BOOTSTRAP,
            nonce,
        ]

    def run(self, code: str, workspace: Path, fixtures: Path) -> ExecResult:
        ok, why = self.check_cached()
        if not ok:
            raise ExecutorUnavailable(why)
        name = f"clm-exec-{uuid.uuid4().hex[:12]}"
        nonce = secrets.token_hex(8)
        cmd = self.command(name, workspace, fixtures, nonce)
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
        err_tail = bytearray()
        readers = [
            threading.Thread(
                target=_bounded_reader,
                args=(proc.stdout, self.output_cap_bytes, out_parts, out_total),
                daemon=True,
            ),
            threading.Thread(
                target=_bounded_reader,
                args=(proc.stderr, self.output_cap_bytes, err_parts, err_total, err_tail),
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
        stderr, stderr_bytes, record = _extract_runtime(
            _decode(err_parts), bytes(err_tail), err_total[0], nonce
        )
        return ExecResult(
            exit_code=proc.returncode,
            stdout=_decode(out_parts),
            stderr=stderr,
            timed_out=timed_out,
            stdout_bytes=out_total[0],
            stderr_bytes=stderr_bytes,
            duration_s=round(duration, 3),
            error=error,
            runtime_trace=record,
        )

    _checked: tuple[bool, str] | None = None

    def check_cached(self) -> tuple[bool, str]:
        if self._checked is None or not self._checked[0]:
            self._checked = self.check()
        return self._checked
