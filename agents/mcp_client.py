"""Official FalkorDB MCP client integration with strict read-only and tenant isolation."""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from mcp import ClientSession, StdioServerParameters, stdio_client
from agents.settings import require_tenant

ROOT = Path(__file__).resolve().parents[1]
SECRETS_FILE = ROOT / ".secrets" / "db.env"
MCP_BIN = ROOT / "mcp" / "node_modules" / ".bin" / "falkordb-mcp"
_SESSION_TEARDOWN_TIMEOUT_SECONDS = 8.0

_SessionKey = tuple[str, str, int, str]
_SessionId = tuple[_SessionKey, asyncio.AbstractEventLoop]


@dataclass
class _SessionEntry:
    loop: asyncio.AbstractEventLoop
    operation_lock: asyncio.Lock
    ready: asyncio.Future[ClientSession]
    close_requested: asyncio.Event
    owner_task: asyncio.Task[None] | None = None
    session: ClientSession | None = None
    closing: bool = False


class _McpToolError(RuntimeError):
    """A completed MCP tool call rejected by the server."""


_sessions: dict[_SessionId, _SessionEntry] = {}
_session_locks: dict[_SessionId, asyncio.Lock] = {}


def _session_key(client: FalkorDBMcpClient) -> _SessionKey:
    return (client.tenant, client.host, client.port, client.username)


def _consume_owner_exception(task: asyncio.Task[None]) -> None:
    """Consume owner failures after they have been published to its borrowers."""
    with suppress(asyncio.CancelledError, Exception):
        task.exception()


async def _own_client_session(entry: _SessionEntry, server_params: StdioServerParameters) -> None:
    """Own every AnyIO context for one cached connection from entry to exit."""
    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=30.0,
            ) as session:
                entry.session = session
                await session.initialize()
                if not entry.ready.done():
                    entry.ready.set_result(session)

                await entry.close_requested.wait()
                # Do not tear down the underlying streams while a borrowed call is
                # still using the session. New borrowers are rejected once closing.
                async with entry.operation_lock:
                    pass
    except BaseException as error:
        if not entry.ready.done():
            entry.ready.set_exception(error)
        raise


def _request_entry_close(entry: _SessionEntry) -> None:
    """Mark an entry unavailable and let its owner task unwind its contexts."""
    entry.closing = True
    entry.close_requested.set()


async def _wait_for_owner(entry: _SessionEntry, timeout_seconds: float) -> bool:
    """Await an owner without allowing a broken subprocess to stall teardown."""
    task = entry.owner_task
    if task is None:
        return True
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.shield(task)
    except TimeoutError:
        return False
    except asyncio.CancelledError:
        if task.cancelled():
            return True
        raise
    except Exception:
        return True
    return True


async def _close_entry_on_owner_loop(entry: _SessionEntry) -> None:
    """Request close and await the task that owns the MCP contexts."""
    _request_entry_close(entry)
    if await _wait_for_owner(entry, _SESSION_TEARDOWN_TIMEOUT_SECONDS):
        return

    task = entry.owner_task
    if task is None:
        return
    task.cancel()
    await _wait_for_owner(entry, _SESSION_TEARDOWN_TIMEOUT_SECONDS)


async def _close_entry(entry: _SessionEntry) -> None:
    """Close an entry through its owning event loop, never another task's context."""
    current_loop = asyncio.get_running_loop()
    if entry.loop is current_loop:
        await _close_entry_on_owner_loop(entry)
        return

    if entry.loop.is_closed():
        entry.closing = True
        return

    try:
        future = asyncio.run_coroutine_threadsafe(_close_entry_on_owner_loop(entry), entry.loop)
    except RuntimeError:
        entry.closing = True
        return
    try:
        async with asyncio.timeout(_SESSION_TEARDOWN_TIMEOUT_SECONDS * 2):
            await asyncio.shield(asyncio.wrap_future(future))
    except TimeoutError:
        future.cancel()


async def reset_mcp_sessions() -> None:
    """Close cached MCP owners without leaving AnyIO contexts on another task."""
    entries = tuple(_sessions.values())
    _sessions.clear()
    _session_locks.clear()
    if entries:
        await asyncio.gather(*(_close_entry(entry) for entry in entries), return_exceptions=True)


def _discard_closed_loop_sessions(key: _SessionKey, current_loop: asyncio.AbstractEventLoop) -> None:
    """Forget resources from a closed loop; they can never be reused here."""
    for session_id, entry in tuple(_sessions.items()):
        _key, loop = session_id
        if _key == key and loop is not current_loop and loop.is_closed():
            _sessions.pop(session_id, None)
            _session_locks.pop(session_id, None)
            entry.closing = True


async def _get_client_session(client: FalkorDBMcpClient) -> ClientSession:
    key = _session_key(client)
    current_loop = asyncio.get_running_loop()
    _discard_closed_loop_sessions(key, current_loop)
    session_id = (key, current_loop)
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        entry = _sessions.get(session_id)
        if entry is not None:
            if entry.closing or entry.owner_task is None or entry.owner_task.done():
                _sessions.pop(session_id, None)
                await _close_entry(entry)
            else:
                try:
                    session = await entry.ready
                except BaseException:
                    if _sessions.get(session_id) is entry:
                        _sessions.pop(session_id, None)
                    await _close_entry(entry)
                    raise
                if _sessions.get(session_id) is entry and not entry.closing and not entry.owner_task.done():
                    return session
                raise RuntimeError("MCP session was reset during initialization")

        server_params = client._get_server_params(readonly=True)
        entry = _SessionEntry(
            loop=current_loop,
            operation_lock=asyncio.Lock(),
            ready=current_loop.create_future(),
            close_requested=asyncio.Event(),
        )
        _sessions[session_id] = entry
        entry.owner_task = asyncio.create_task(
            _own_client_session(entry, server_params),
            name=f"mcp-session-owner:{client.tenant}",
        )
        entry.owner_task.add_done_callback(_consume_owner_exception)
        try:
            session = await entry.ready
        except BaseException:
            if _sessions.get(session_id) is entry:
                _sessions.pop(session_id, None)
            await _close_entry(entry)
            raise
        if _sessions.get(session_id) is entry and not entry.closing and not entry.owner_task.done():
            return session
        await _close_entry(entry)
        raise RuntimeError("MCP session was reset during initialization")


async def _drop_client_session(client: FalkorDBMcpClient, session: ClientSession) -> None:
    key = _session_key(client)
    current_loop = asyncio.get_running_loop()
    session_id = (key, current_loop)
    lock = _session_locks.get(session_id)
    if lock is None:
        return
    async with lock:
        entry = _sessions.get(session_id)
        if entry is None or entry.session is not session:
            return
        _sessions.pop(session_id, None)
        await _close_entry(entry)




def get_db_secrets() -> dict[str, str]:
    values = {k: v for k, v in dotenv_values(SECRETS_FILE).items() if isinstance(v, str) and v}
    values.update({k: v for k, v in os.environ.items() if v})
    return values


class FalkorDBMcpClient:
    """Provides managed async stdio sessions to the official FalkorDB MCP server."""

    def __init__(self, tenant: str = "acme", host: str | None = None, port: int | None = None) -> None:
        self.tenant = require_tenant(tenant)
        self.secrets = get_db_secrets()
        self.host = host or self.secrets.get("FALKORDB_HOST", "127.0.0.1")
        self.port = port or int(self.secrets.get("FALKORDB_PORT", 6379))

        upper = self.tenant.upper()
        self.username = self.secrets.get(f"FALKORDB_READER_{upper}_USERNAME", f"reader_{self.tenant}")
        self.password = self.secrets.get(f"FALKORDB_READER_{upper}_PASSWORD", "")
        if not self.password:
            raise ValueError(f"Missing reader credentials for tenant {self.tenant}")

    def _get_server_params(self, readonly: bool = True) -> StdioServerParameters:
        if not MCP_BIN.is_file():
            raise FileNotFoundError(f"Official FalkorDB MCP binary not found at {MCP_BIN}")

        env = {
            **{name: os.environ[name] for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if name in os.environ},
            "FALKORDB_HOST": self.host,
            "FALKORDB_PORT": str(self.port),
            "FALKORDB_USERNAME": self.username,
            "FALKORDB_PASSWORD": self.password,
            "FALKORDB_DEFAULT_READONLY": "true" if readonly else "false",
            "FALKORDB_STRICT_READONLY": "true" if readonly else "false",
        }
        return StdioServerParameters(command=str(MCP_BIN), env=env)

    async def query_readonly(
        self,
        graph_name: str,
        query: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
        retries: int = 2,
    ) -> tuple[list[dict[str, Any]], float]:
        """Execute a query via query_graph_readonly tool; returns (rows, duration_ms)."""
        start_time = time.perf_counter()
        last_error = None
        for attempt in range(retries + 1):
            session: ClientSession | None = None
            try:
                session = await _get_client_session(self)
                session_id = (_session_key(self), asyncio.get_running_loop())
                entry = _sessions.get(session_id)
                if entry is None or entry.session is not session:
                    raise RuntimeError("MCP session was replaced before the query could run")

                # A stdio MCP connection has one request/response stream. Serializing
                # calls keeps a failing request from closing the session beneath a
                # second in-flight request.
                async with entry.operation_lock:
                    if _sessions.get(session_id) is not entry or entry.closing:
                        raise RuntimeError("MCP session was reset while the query was waiting")
                    async with asyncio.timeout(timeout_seconds):
                        result = await session.call_tool(
                            "query_graph_readonly",
                            {
                                "graphName": graph_name,
                                "query": query,
                                "params": params or {},
                            },
                        )
                duration_ms = (time.perf_counter() - start_time) * 1000.0

                if result.is_error:
                    error_msg = result.content[0].text if result.content else "Unknown error"
                    raise _McpToolError(f"MCP query_graph_readonly error: {error_msg}")

                text = result.content[0].text if result.content else "[]"
                try:
                    data = json.loads(text)
                except Exception:
                    data = [{"raw": text}]

                if isinstance(data, dict) and "data" in data:
                    rows = data["data"]
                elif isinstance(data, list):
                    rows = data
                else:
                    rows = [data]

                return rows, duration_ms
            except _McpToolError:
                raise
            except Exception as e:
                last_error = e
                if session is not None:
                    await _drop_client_session(self, session)
                if attempt < retries:
                    await asyncio.sleep(0.5)
        raise last_error
