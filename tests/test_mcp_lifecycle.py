"""MCP session ownership lifecycle tests."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import agents.mcp_client as mcp_client


class _FakeSession:
    created: list["_FakeSession"] = []

    def __init__(self, _read_stream, _write_stream, *, read_timeout_seconds: float) -> None:
        self.read_timeout_seconds = read_timeout_seconds
        self.enter_task: asyncio.Task[object] | None = None
        self.exit_task: asyncio.Task[object] | None = None
        self.initialized = False
        self.__class__.created.append(self)

    async def __aenter__(self):
        self.enter_task = asyncio.current_task()
        return self

    async def __aexit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.exit_task = asyncio.current_task()

    async def initialize(self) -> None:
        self.initialized = True


@pytest.mark.asyncio
async def test_cached_session_contexts_are_owned_and_closed_by_one_task(monkeypatch):
    """A borrower never enters or exits the AnyIO-backed MCP contexts itself."""
    entered_by: list[asyncio.Task[object] | None] = []
    exited_by: list[asyncio.Task[object] | None] = []

    @asynccontextmanager
    async def fake_stdio_client(_server_params):
        entered_by.append(asyncio.current_task())
        try:
            yield object(), object()
        finally:
            exited_by.append(asyncio.current_task())

    _FakeSession.created.clear()
    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "ClientSession", _FakeSession)
    client = SimpleNamespace(
        tenant="acme",
        host="localhost",
        port=6379,
        username="reader_acme",
        _get_server_params=lambda *, readonly: object(),
    )

    try:
        first = await mcp_client._get_client_session(client)
        second = await mcp_client._get_client_session(client)

        assert first is second
        assert len(_FakeSession.created) == 1
        assert _FakeSession.created[0].initialized
        assert entered_by[0] is _FakeSession.created[0].enter_task
        assert entered_by[0] is not asyncio.current_task()
    finally:
        await mcp_client.reset_mcp_sessions()

    assert exited_by == entered_by
    assert _FakeSession.created[0].exit_task is entered_by[0]
