"""Event-loop and client lifecycle of a p015 session (`p015-session-loop/1`).

**What was wrong:** a session ran each user turn with `asyncio.run`, creating and closing a new event loop per turn.
The session's model keeps one `AsyncAnthropic` client for all turns, and its HTTP connection pool keeps connections
bound to the loop that opened them. From the second turn on, the first request reused a connection whose loop had
closed, failed ("Event loop is closed"), and only the bounded retry rescued it. The failed request may already have
been sent, so it was charged at its full reservation.

**What happens now:** a session owns one `asyncio.Runner`, created at the first turn and kept for all its turns.
Every model request of the session, including the summary arm's summariser requests (which share the session's
model), runs on that one loop, so pooled connections stay valid between turns.

**Shutdown:** `close()` closes the session's model clients on that loop, then closes the loop, so no client outlives
its loop. A closed session refuses further turns, rather than reusing a pool on a closed loop. Each launcher session
builds its own model, so nothing pooled is shared between sessions.

Connection pooling and the retry policy are unchanged, and nothing here changes what any arm sends.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Coroutine
from typing import Any, TypeVar

LOOP_VERSION = "p015-session-loop/1"
T = TypeVar("T")


def model_clients(model: Any) -> list[Any]:
    """The distinct API clients reachable from a model and the models it wraps (none for scripted models)."""
    out: list[Any] = []
    seen: set[int] = set()
    current, depth = model, 0
    while current is not None and depth < 8:
        client = getattr(current, "client", None)
        if (
            client is not None
            and id(client) not in seen
            and callable(getattr(client, "close", None))
        ):
            seen.add(id(client))
            out.append(client)
        current, depth = getattr(current, "wrapped", None), depth + 1
    return out


async def _close_clients(clients: list[Any]) -> None:
    for client in clients:
        result = client.close()
        if inspect.isawaitable(result):
            await result


class SessionLoop:
    """One event loop for all of a session's turns, and the closing of its clients on that loop."""

    def __init__(self) -> None:
        self._runner: asyncio.Runner | None = None
        self.closed = False
        self.turns_run = 0

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        if self.closed:
            coro.close()
            raise RuntimeError("the session is closed; its event loop and clients are gone")
        if self._runner is None:
            self._runner = asyncio.Runner()
        self.turns_run += 1
        return self._runner.run(coro)

    def close(self, *models: Any) -> None:
        """Close the models' clients on the session's loop, then the loop. Safe to call more than once."""
        if self.closed:
            return
        self.closed = True
        if self._runner is None:
            return  # no turn ran, so no client opened a connection on a loop
        try:
            clients: list[Any] = []
            for model in models:  # the summariser shares the session's client: close it once
                clients += [c for c in model_clients(model) if all(c is not d for d in clients)]
            if clients:
                self._runner.run(_close_clients(clients))
        finally:
            self._runner.close()
