"""Actual-graph tests for durable Slack source lifecycle boundaries.

The controlled source processor below is explicitly synthetic: it supplies the
SDK-shaped document/chunk output required by the application source worker; it
is not evidence of a live Slack installation.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from agents.settings import graph_names
from falkordb import FalkorDB
from integrations import slack
from seed.load import get_config
from seed.sources import run_source_worker


@pytest.fixture
def slack_config() -> dict[str, str]:
    return {**get_config(), "SLACK_TEAM_ID": "T-ACME", "SLACK_CHANNEL_ID": "C-OPERATIONS",
            "SLACK_BOT_TOKEN": "xoxb-controlled", "SLACK_APP_TOKEN": "xapp-controlled",
            "SLACK_SERVICE_ID": "payments-primary"}


def _graph() -> Any:
    config = get_config()
    return FalkorDB(host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
                    username=config["FALKORDB_ADMIN_USERNAME"], password=config["FALKORDB_ADMIN_PASSWORD"]).select_graph(graph_names().company)


async def _synthetic_sdk_processor(request: dict[str, Any]) -> None:
    """Produce the minimum real graph output the source publisher verifies."""
    if request["operation"] == "delete":
        return
    await asyncio.to_thread(
        _graph().query,
        "MERGE (d:Document {id:$document_id}) "
        "MERGE (c:Chunk {id:$chunk_id}) SET d.content_hash=$hash, c.index=0, c.text=$text "
        "MERGE (d)-[:PART_OF]->(c)",
        {"document_id": request["document_id"], "chunk_id": request["document_id"] + ":0",
         "text": request["text"], "hash": request["content_hash"]},
        timeout=3000,
    )

def _response(data: dict[str, Any]) -> AsyncSlackResponse:
    return AsyncSlackResponse(client=None, http_verb="GET", api_url="https://controlled.invalid/api",
                              req_args={}, data={"ok": True, **data}, headers={}, status_code=200)



class _PermalinkAdapter:
    """Controlled external adapter; it is not live Slack evidence."""

    async def chat_getPermalink(self, *, channel: str, message_ts: str) -> AsyncSlackResponse:
        return _response({"permalink": f"https://controlled.invalid/{channel}/{message_ts}"})


async def _deliver(event_id: str, event: dict[str, Any], config: dict[str, str], *, publish: bool = True) -> str:
    task = asyncio.create_task(slack.receive_slack_event(
        {"team_id": "T-ACME", "event_id": event_id}, event, config=config, client=_PermalinkAdapter(),
        bot_user_id="U-BOT", publication_timeout_seconds=2 if publish else 0,
    ))
    if publish:
        for _ in range(100):
            rows = await asyncio.to_thread(
                lambda: _graph().ro_query("MATCH (r:SourceRequest {state:'pending'}) RETURN r.request_id LIMIT 1", timeout=3000).result_set
            )
            if rows:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("event did not create a source request")
        await run_source_worker(_synthetic_sdk_processor, once=True)
    return await task


def _post(ts: str, text: str, *, user: str = "U-ALICE") -> dict[str, str]:
    return {"channel": "C-OPERATIONS", "ts": ts, "thread_ts": ts, "user": user, "text": text}


@pytest.mark.asyncio
async def test_post_edit_delete_are_published_and_tombstone_defeats_old_replay(slack_config: dict[str, str]) -> None:
    ts = "1729000000.000001"
    assert await _deliver("Ev-post", _post(ts, "first"), slack_config) == "processed"
    changed = {"channel": "C-OPERATIONS", "subtype": "message_changed", "event_ts": "1729000001.000001",
               "message": {**_post(ts, "second"), "edited": {"ts": "1729000001.000001"}}}
    assert await _deliver("Ev-edit", changed, slack_config) == "processed"
    deleted = {"channel": "C-OPERATIONS", "subtype": "message_deleted", "deleted_ts": ts,
               "event_ts": "1729000002.000001", "previous_message": {"ts": ts, "user": "U-ALICE"}}
    assert await _deliver("Ev-delete", deleted, slack_config) == "processed"
    replay = _post(ts, "resurrection")
    assert await _deliver("Ev-replay", replay, slack_config, publish=False) == "error"

    rows = _graph().ro_query(
        "MATCH (s:SourceRecord {document_id:$id}) RETURN s.status, s.source_version, s.source_order",
        {"id": slack.normalize_slack_document_id("T-ACME", "C-OPERATIONS", ts)}, timeout=3000,
    ).result_set
    assert rows == [["deleted", "1729000002.000001", 1_729_000_002_000_001]]
    inbox = _graph().ro_query(
        "MATCH (i:SlackInbox {document_id:$id}) RETURN i.event_id, i.status, i.text ORDER BY i.event_id",
        {"id": slack.normalize_slack_document_id("T-ACME", "C-OPERATIONS", ts)}, timeout=3000,
    ).result_set
    assert [row[1] for row in inbox] == ["processed", "processed", "processed", "error"]
    assert all(row[2] == "" for row in inbox)


@pytest.mark.asyncio
async def test_metadata_only_change_records_delivery_without_reordering_content(slack_config: dict[str, str]) -> None:
    ts = "1729000005.000001"
    assert await _deliver("Ev-post-metadata", _post(ts, "unchanged"), slack_config) == "processed"
    metadata_change = {
        "channel": "C-OPERATIONS", "subtype": "message_changed", "event_ts": "1729000006.000001",
        "message": _post(ts, "unchanged"),
    }
    assert await _deliver("Ev-metadata", metadata_change, slack_config, publish=False) == "processed"
    rows = _graph().ro_query(
        "MATCH (s:SourceRecord {document_id:$id}) RETURN s.source_version, s.source_order, s.status",
        {"id": slack.normalize_slack_document_id("T-ACME", "C-OPERATIONS", ts)}, timeout=3000,
    ).result_set
    assert rows == [[ts, 1_729_000_005_000_001, "published"]]
    inbox = _graph().ro_query(
        "MATCH (i:SlackInbox {event_id:'Ev-metadata'}) RETURN i.status, i.event_timestamp, i.source_version",
        timeout=3000,
    ).result_set
    assert inbox == [["processed", "1729000006.000001", ts]]

@pytest.mark.asyncio
async def test_foreign_and_bot_events_persist_nothing(slack_config: dict[str, str]) -> None:
    assert await slack.receive_slack_event({"team_id": "T-FOREIGN", "event_id": "Ev-foreign"}, _post("1.000001", "no"),
                                           config=slack_config, bot_user_id="U-BOT") == "ignored"
    assert await slack.receive_slack_event({"team_id": "T-ACME", "event_id": "Ev-bot"},
                                           {**_post("1.000002", "no", user="U-BOT"), "bot_id": "B-1"},
                                           config=slack_config, bot_user_id="U-BOT") == "ignored"
    assert _graph().ro_query("MATCH (i:SlackInbox) RETURN count(i)", timeout=3000).result_set == [[0]]


@pytest.mark.asyncio
async def test_pending_and_source_error_never_claim_processed(slack_config: dict[str, str]) -> None:
    pending = _post("1729000010.000001", "awaiting worker")
    assert await _deliver("Ev-pending", pending, slack_config, publish=False) == "pending"
    rejected_config = {**slack_config, "SLACK_SERVICE_ID": "not-a-canonical-service"}
    assert await _deliver("Ev-error", _post("1729000011.000001", "bad service"), rejected_config, publish=False) == "error"
    rows = _graph().ro_query("MATCH (i:SlackInbox) RETURN i.event_id, i.status ORDER BY i.event_id", timeout=3000).result_set
    assert rows == [["Ev-error", "error"], ["Ev-pending", "pending"]]


class _ControlledSlack:
    def __init__(self, *, history: dict[str, Any] | None = None, replies: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.history = history or {"messages": []}
        self.replies = replies or {"messages": []}
        self.error = error

    async def auth_test(self) -> AsyncSlackResponse:
        return _response({"team_id": "T-ACME", "user_id": "U-BOT"})

    async def conversations_history(self, **_: Any) -> AsyncSlackResponse:
        if self.error:
            raise self.error
        return _response(self.history)

    async def conversations_replies(self, **_: Any) -> AsyncSlackResponse:
        if self.error:
            raise self.error
        return _response(self.replies)


@pytest.mark.asyncio
async def test_exact_source_validation_fails_closed_for_changed_deleted_rate_limit_and_reply(monkeypatch: pytest.MonkeyPatch,
                                                                                              slack_config: dict[str, str]) -> None:
    document_id = "slack:T-ACME:C-OPERATIONS:1729000020.000001"
    text = "current source"
    expected_hash = hashlib.sha256(text.encode()).hexdigest()
    _graph().query(
        "MERGE (s:SourceRecord {document_id:$id}) "
        "SET s.status='published', s.origin='slack', s.content_hash=$hash, s.source_version=$version",
        {"id": document_id, "hash": expected_hash, "version": "1729000020.000001"}, timeout=3000,
    )
    missing = await slack.validate_slack_source(
        "slack:T-ACME:C-OPERATIONS:1729000999.000001", expected_hash, "1729000999.000001",
        client=_ControlledSlack(), config=slack_config,
    )
    assert missing["valid"] is False and missing["error"] == "missing_or_nonpublished"
    changed = await slack.validate_slack_source(document_id, expected_hash, "1729000020.000001",
                                                client=_ControlledSlack(history={"messages": [{"ts": "1729000020.000001", "text": "changed"}]}),
                                                config=slack_config)
    assert changed["valid"] is False and changed["error"] == "changed"
    deleted = await slack.validate_slack_source(document_id, expected_hash, "1729000020.000001",
                                                client=_ControlledSlack(), config=slack_config)
    assert deleted["valid"] is False and deleted["error"] == "deleted_or_unavailable"

    class RateLimit(Exception):
        response = type("Response", (), {"status_code": 429, "headers": {"Retry-After": "1"}})()

    limited = await slack.validate_slack_source(document_id, expected_hash, "1729000020.000001",
                                                client=_ControlledSlack(error=RateLimit()), config=slack_config)
    assert limited["valid"] is False and limited["error"] == "rate_limited"
    monkeypatch.setattr(slack, "_inbox_thread_context", lambda *_: "1729000000.000001")
    reply = await slack.validate_slack_source(
        document_id, expected_hash, "1729000020.000001",
        client=_ControlledSlack(replies={"messages": [{"ts": "1729000020.000001", "thread_ts": "1729000000.000001", "text": text}]}),
        config=slack_config,
    )
    assert reply["valid"] is True and reply["observed_hash"] == expected_hash


@pytest.mark.asyncio
async def test_transport_state_and_failure_are_supervised(monkeypatch, slack_config) -> None:
    """A running worker is not a connected socket; a dead transport stops its workers."""
    started = asyncio.Event()
    fail = asyncio.Event()
    stop = asyncio.Event()

    class SocketAdapter:
        connected = False
        closed = False

        def __init__(self, *args):
            self.client = self

        async def start_async(self):
            started.set()
            await fail.wait()

        async def is_connected(self):
            return self.connected

        async def close_async(self):
            self.closed = True

    socket = SocketAdapter()
    original_worker = run_source_worker

    async def controlled_extraction(**kwargs):
        await original_worker(_synthetic_sdk_processor, **kwargs)

    monkeypatch.setattr(slack, "AsyncSocketModeHandler", lambda *args: socket)
    monkeypatch.setattr(slack, "AsyncWebClient", lambda **kwargs: _ControlledSlack())
    monkeypatch.setattr(slack, "run_source_worker", controlled_extraction)
    task = asyncio.create_task(slack.run_slack_connector(config=slack_config, stop_event=stop))

    async def observe(expected):
        async with asyncio.timeout(8):
            while True:
                state = await asyncio.to_thread(slack.get_connector_status)
                if state["status"] == expected:
                    return state
                await asyncio.sleep(0.02)

    try:
        startup = asyncio.create_task(started.wait())
        done, _ = await asyncio.wait({startup, task}, timeout=8, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        await asyncio.wait_for(startup, timeout=1)
        assert (await observe("disconnected"))["available"] is False
        socket.connected = True
        assert (await observe("connected"))["available"] is True
        socket.connected = False
        assert (await observe("disconnected"))["available"] is False
        fail.set()
        with pytest.raises(ExceptionGroup):
            await asyncio.wait_for(task, timeout=8)
        assert socket.closed is True
        assert slack.get_connector_status()["available"] is False
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_permalink_rate_limit_persists_not_before_without_repeating_read(slack_config) -> None:
    import time
    from slack_sdk.errors import SlackApiError

    class LimitedPermalink:
        calls = 0

        async def chat_getPermalink(self, **kwargs):
            self.calls += 1
            response = AsyncSlackResponse(client=None, http_verb="GET", api_url="https://controlled.invalid/api",
                                          req_args={}, data={"ok": False, "error": "ratelimited"},
                                          headers={"Retry-After": "60"}, status_code=429)
            raise SlackApiError("controlled rate limit", response)

    client = LimitedPermalink()
    before = time.time()
    result = await slack.receive_slack_event(
        {"team_id": "T-ACME", "event_id": "Ev-permalink-rate-limit"},
        _post("1729000100.000001", "new policy"), config=slack_config, client=client,
    )
    assert result == "pending"
    rows = _graph().ro_query("MATCH (i:SlackInbox {event_id:'Ev-permalink-rate-limit'}) "
                             "RETURN i.status,i.not_before", timeout=3000).result_set
    assert rows[0][0] == "pending" and rows[0][1] >= before + 60
    assert await slack.process_pending_slack_event("Ev-permalink-rate-limit", config=slack_config, client=client) == "unavailable"
    assert client.calls == 1
