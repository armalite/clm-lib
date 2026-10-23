"""The p015 session event-loop and client lifecycle (`p015-session-loop/1`), against a real local HTTP server with
keep-alive connections (127.0.0.1 only; every other address stays blocked). The server returns synthetic Anthropic
Messages responses; no provider is contacted, and the key is a placeholder. Temporary ledgers only.

The pre-fix pattern (one client used across per-turn `asyncio.run` loops) reproduces the live smoke's failure; the
fixed sessions keep one loop for all turns, reuse pooled connections, never need a retry for it, and close their
clients on that loop."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError

from clm_lib.budget import Ledger, ModelPrice
from clm_lib.p015 import launcher as L
from clm_lib.p015.arms import ArmSession
from clm_lib.p015.docthread.fixture import instance
from clm_lib.p015.docthread.task import SYSTEM_PROMPT, questions, user_message
from clm_lib.p015.docthread.tools import EvidenceService
from clm_lib.p015.live import ModelConfig, live_model
from clm_lib.p015.records import read_jsonl
from clm_lib.p015.session import GuardedSession
from clm_lib.p015.summary.capability import SummarySettings

from . import harness as h

REPO = Path(__file__).resolve().parents[2]
PRICE = ModelPrice("claude-sonnet-5", 2.0, 2.5, 4.0, 0.2, 10.0, "test", "test")
_CONNECT, _CONNECT_EX, _CREATE, _GAI = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection,
                                        socket.getaddrinfo)  # fmt: skip


def _loopback(address: Any) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    return host in ("127.0.0.1", "localhost", "::1")


@pytest.fixture(autouse=True)
def loopback_only(no_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Narrows the suite's network block to allow 127.0.0.1 only."""

    def connect(self: socket.socket, address: Any) -> Any:
        if not _loopback(address):
            raise RuntimeError("only loopback connections are allowed in this test")
        return _CONNECT(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        if not _loopback(address):
            raise RuntimeError("only loopback connections are allowed in this test")
        return _CONNECT_EX(self, address)

    def getaddrinfo(host: Any, *a: Any, **k: Any) -> Any:
        if not _loopback((host,)):
            raise RuntimeError("only loopback names are allowed in this test")
        return _GAI(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", lambda a, *x, **k: _CREATE(a, *x, **k) if _loopback(a) else
                        (_ for _ in ()).throw(RuntimeError("loopback only")))  # fmt: skip


class Server:
    """A keep-alive HTTP/1.1 server answering the Messages API with synthetic replies: an agent request is answered
    alternately with a `tool_use` (a journal read) and a final answer; a summariser request with a short summary;
    `fail_next` makes the next agent request a 400."""

    def __init__(self, component: str) -> None:
        self.component = component
        self.requests: list[dict[str, Any]] = []
        self.next_kind = "tool"
        self.fail_next = False
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                status, reply = server.reply(body, self.client_address[1])
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reply(self, body: dict[str, Any], port: int) -> tuple[int, dict[str, Any]]:
        with self.lock:
            system = json.dumps(body.get("system"))
            summariser = "You compress an agent's working transcript" in system
            self.requests.append({"port": port, "summariser": summariser, "body": body})
            n = len(self.requests)
            tokens = max(
                1, len(json.dumps(body)) // 3
            )  # provider-like: proportional to the request
            if summariser:
                return 200, self.message(
                    [{"type": "text", "text": "summary: one journal page was read."}],
                    "end_turn",
                    tokens,
                )
            if self.fail_next:
                self.fail_next = False
                return 400, {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "synthetic"},
                }
            kind, self.next_kind = self.next_kind, ("text" if self.next_kind == "tool" else "tool")
            if kind == "tool":
                call = {"type": "tool_use", "id": f"toolu_{n}", "name": "read_journal",
                        "input": {"component": self.component, "hour": "11:00", "page": 1}}  # fmt: skip
                return 200, self.message([call], "tool_use", tokens)
            return 200, self.message(
                [{"type": "text", "text": 'Answer.\n\n```json\n{"x": 1}\n```'}], "end_turn", tokens
            )

    @staticmethod
    def message(content: list[dict[str, Any]], stop: str, tokens: int = 1_000) -> dict[str, Any]:
        return {"id": "msg_local", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
                "content": content, "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": tokens, "output_tokens": 20, "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0}}  # fmt: skip

    def agent_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if not r["summariser"]]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    fx = instance("docthread-dev-1")
    srv = Server(fx.names["intake"])
    monkeypatch.setenv("ANTHROPIC_BASE_URL", srv.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-placeholder-not-a-credential")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    yield srv
    srv.httpd.shutdown()


def session(kind: str, tmp: Path, name: str, summary: SummarySettings | None = None) -> Any:
    fx = instance("docthread-dev-1")
    common: dict[str, Any] = dict(
        run_dir=tmp / name, run_id=name, ledger=Ledger.open(tmp / f"{name}.json", 100.0), price=PRICE,
        system_prompt=SYSTEM_PROMPT, task_tools=EvidenceService(fx).tools(),
        model_settings=ModelConfig().model_settings(), conversation_id=name, mode="live", live_authorised=True,
        retry=ModelConfig().retry_policy(),
    )  # fmt: skip
    model = live_model(ModelConfig())
    if kind == "guarded_clm":
        return GuardedSession(model, **common)
    return ArmSession(kind, model, summary=summary, **common)  # type: ignore[arg-type]


def finished(run_dir: Path) -> list[dict[str, Any]]:
    return [a for a in read_jsonl(run_dir / "attempts.jsonl") if a["event"] == "finished"]


def prompts(n: int) -> list[str]:
    return [user_message(q) for q in questions(instance("docthread-dev-1"))[:n]]


# ---------------------------------------------------------------------- the old failure


def test_the_pre_fix_pattern_fails_on_the_second_turn(server: Server) -> None:
    """One client across two `asyncio.run` loops, as the runners did before the fix: the second turn's first request
    fails on the closed loop's connection, after the server has received it."""
    agent: Agent[None, str] = Agent(live_model(ModelConfig(max_retries=0)))
    asyncio.run(agent.run("one", model_settings={"max_tokens": 16}))
    server.next_kind = "text"
    received = len(server.requests)
    with pytest.raises(ModelAPIError) as exc:
        asyncio.run(agent.run("two", model_settings={"max_tokens": 16}))
    chain, e = [], exc.value
    while e is not None:
        chain.append(str(e))
        e = e.__cause__ or e.__context__  # type: ignore[assignment]
    assert any("Event loop is closed" in c for c in chain)
    assert len(server.requests) == received + 1  # the failed request reached the server


# ---------------------------------------------------------------------- the fixed lifecycle


@pytest.mark.parametrize("kind", ["guarded_clm", "paper_clm_shell", "token_tail_p015"])
def test_turns_reuse_one_loop_and_pool_without_extra_attempts(
    server: Server, tmp_path: Path, kind: str
) -> None:
    s = session(kind, tmp_path, kind)
    for prompt in prompts(3):
        assert s.run_turn(prompt).ending == "completed"
    attempts = finished(tmp_path / kind)
    assert [a["outcome"] for a in attempts] == [
        "ok"
    ] * 6  # two calls per turn, no failed first attempt
    assert all(a["retry"] == 0 for a in attempts)
    assert len(server.agent_requests()) == len(attempts)  # nothing extra reached the server
    assert (
        len({r["port"] for r in server.requests}) == 1
    )  # one pooled connection, reused across the turns
    client = s.model.wrapped.client
    s.close()
    assert client.is_closed() and s.loop.closed
    with pytest.raises(RuntimeError, match="the session is closed"):
        s.run_turn(prompts(1)[0])
    s.close()  # idempotent


def test_successive_sessions_each_own_their_loop_and_client(server: Server, tmp_path: Path) -> None:
    for i, kind in enumerate(["guarded_clm", "paper_clm_shell", "token_tail_p015"]):
        server.next_kind = "tool"
        with session(kind, tmp_path, f"s{i}") as s:
            for prompt in prompts(2):
                assert s.run_turn(prompt).ending == "completed"
        assert s.loop.closed and s.model.wrapped.client.is_closed()
        assert [a["outcome"] for a in finished(tmp_path / f"s{i}")] == ["ok"] * 4
    assert len(server.agent_requests()) == 12


def test_summariser_requests_share_the_session_loop(server: Server, tmp_path: Path) -> None:
    s = session("token_tail_p015", tmp_path, "summ", summary=SummarySettings(budget_tokens=5_500))
    for prompt in prompts(2):
        assert s.run_turn(prompt).ending == "completed"
    attempts = finished(tmp_path / "summ")
    summaries = [a for a in attempts if a["category"] == "summary"]
    assert summaries and all(a["outcome"] == "ok" for a in attempts)
    assert len(server.requests) == len(attempts)
    assert sum(r["summariser"] for r in server.requests) == len(summaries)
    assert s.summary_model.wrapped is s.model.wrapped  # one client, closed once
    s.close()
    assert s.model.wrapped.client.is_closed()


def test_error_and_stop_endings_close_cleanly(server: Server, tmp_path: Path) -> None:
    s = session("guarded_clm", tmp_path, "err")
    assert s.run_turn(prompts(1)[0]).ending == "completed"
    server.fail_next = True
    assert s.run_turn(prompts(2)[1]).ending == "provider_failure"  # a 400 is not retried
    s.close()
    assert s.model.wrapped.client.is_closed()
    # A stop requested before the next attempt: refused before dispatch, then closed.
    from clm_lib.p015.spend import SpendGate

    (tmp_path / "auth.json").write_text(json.dumps({"experiment": "015", "launch_id": "x", "programme_ceiling_usd": 5.0,
                                                    "block_stop_usd": 5.0}))  # fmt: skip
    (tmp_path / "STOP").write_text("now\n")
    gate = SpendGate(
        Ledger.open(tmp_path / "gate.json", 100.0),
        tmp_path / "auth.json",
        "x",
        stop_path=tmp_path / "STOP",
    )
    s2 = session("guarded_clm", tmp_path, "stop")
    s2.model.gate = gate
    before = len(server.requests)
    assert s2.run_turn(prompts(1)[0]).ending == "stopped"
    assert len(server.requests) == before
    s2.close()


def test_requests_are_identical_to_the_in_memory_transport(server: Server, tmp_path: Path) -> None:
    """Apart from the lifecycle, nothing changes: the request bodies over the real pool equal those sent through an
    in-memory transport (which has no pool), turn for turn."""
    captured: list[dict[str, Any]] = []
    mirror = Server.__new__(Server)
    mirror.component, mirror.requests, mirror.next_kind, mirror.fail_next = (
        server.component,
        [],
        "tool",
        False,
    )
    mirror.lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        status, reply = mirror.reply(body, 0)
        return httpx.Response(status, json=reply)

    fx = instance("docthread-dev-1")
    bodies = []
    for transport in ("pool", "memory"):
        model = (live_model(ModelConfig()) if transport == "pool" else
                 live_model(ModelConfig(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                            api_key="offline-placeholder"))  # fmt: skip
        with GuardedSession(model, run_dir=tmp_path / transport, run_id="same", ledger=Ledger.open(tmp_path / f"{transport}.json", 100.0),
                            price=PRICE, system_prompt=SYSTEM_PROMPT, task_tools=EvidenceService(fx).tools(),
                            model_settings=ModelConfig().model_settings(), conversation_id="same", mode="live",
                            live_authorised=True) as s:  # fmt: skip
            for prompt in prompts(3):
                assert s.run_turn(prompt).ending == "completed"
        bodies.append([r["body"] for r in server.requests] if transport == "pool" else captured)
    assert bodies[0] == bodies[1] and len(bodies[0]) == 6


def test_the_launcher_runs_three_arms_over_the_pool(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)  # the launcher reads configs/prices.toml from the checkout
    Ledger.open(tmp_path / "ledger.json", 100.0)
    (tmp_path / "auth.json").write_text(json.dumps({"experiment": "015", "launch_id": "loop1", "live": True,
                                                    "programme_ceiling_usd": 15.0, "block_stop_usd": 15.0}))  # fmt: skip
    spec = h.as_current(json.loads((REPO / "configs/p015/smoke-dev2.json").read_text())) | {
        "launch_id": "loop1", "questions": 2, "ledger": str(tmp_path / "ledger.json"),
        "authorisation": str(tmp_path / "auth.json")}  # fmt: skip
    spec["arm_settings"]["paper_clm_shell"]["version"] = "paper-clm-shell/2"
    L.plan(spec, tmp_path / "L")
    assert L.run(tmp_path / "L") == L.EXIT_OK
    rows = L.status(tmp_path / "L")["sessions"]
    assert [r["outcome"] for r in rows] == ["complete"] * 3
    for run_dir in sorted((tmp_path / "L/runs").glob("*/r1")):
        assert [a["outcome"] for a in finished(run_dir)] == ["ok"] * 4, run_dir.parent.name
    assert len(server.agent_requests()) == 12
    manifest = L.export_launch(tmp_path / "L", tmp_path / "export")
    assert all(not r["verify_problems"] for r in manifest["runs"]) and manifest["problems"] == []
