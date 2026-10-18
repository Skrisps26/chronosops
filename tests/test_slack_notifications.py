"""Graph-backed notification outbox behavior with controlled Slack adapters."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from falkordb import FalkorDB
from slack_sdk.errors import SlackApiError

from agents.settings import graph_names
from integrations.slack_notifications import (
    NotificationError,
    enqueue_incident_outcome,
    enqueue_incident_root,
    process_next_notification,
    reconcile_notification_receipt,
)
from seed.load import get_config


class ControlledSlack:
    def __init__(self, *, post_error: Exception | None = None, permalink_error: Exception | None = None) -> None:
        self.post_error = post_error
        self.permalink_error = permalink_error
        self.posts: list[dict[str, Any]] = []
        self.permalink_calls = 0
        self.history_messages: list[dict[str, Any]] = []

    async def auth_test(self) -> dict[str, str]:
        return {"team_id": "TACME", "user_id": "UBOT"}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        if self.post_error:
            raise self.post_error
        index = len(self.posts)
        ts = f"1700000000.{index:06d}"
        message: dict[str, Any] = {"ts": ts}
        if "thread_ts" in kwargs:
            message["thread_ts"] = kwargs["thread_ts"]
        return {"channel": kwargs["channel"], "ts": ts, "message": message}

    async def chat_getPermalink(self, *, channel: str, message_ts: str) -> dict[str, str]:
        self.permalink_calls += 1
        if self.permalink_error:
            raise self.permalink_error
        return {"permalink": f"https://slack.example/{channel}/{message_ts}"}

    async def conversations_history(self, **_: Any) -> dict[str, Any]:
        return {"messages": self.history_messages, "response_metadata": {"next_cursor": ""}}

    async def conversations_replies(self, **_: Any) -> dict[str, Any]:
        return {"messages": self.history_messages, "response_metadata": {"next_cursor": ""}}


def _config() -> dict[str, str]:
    config = dict(get_config())
    config.update({"SLACK_TEAM_ID": "TACME", "SLACK_CHANNEL_ID": "CINCIDENTS", "SLACK_SERVICE_ID": "payments-primary"})
    return config


def _graph(config: dict[str, str]) -> Any:
    return FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_WRITER_ACME_USERNAME"],
        password=config["FALKORDB_WRITER_ACME_PASSWORD"],
    ).select_graph(graph_names().topology)


def _incident(config: dict[str, str], incident_id: str = "inc-notify") -> None:
    _graph(config).query(
        "CREATE (:Incident {incident_id: $incident_id, tenant_id: 'acme', service_id: 'payments-primary', "
        "execution_mode: 'real', status: 'observed', starts_at: '2026-01-02T03:04:05+00:00', "
        "recorded_at: '2026-01-02T03:04:06+00:00', fingerprint: 'fp-notify', deployment_id: 'dep-1'})",
        {"incident_id": incident_id},
        timeout=3000,
    )


def _notification(graph: Any, incident_id: str, kind: str) -> list[Any]:
    return graph.ro_query(
        "MATCH (n:SlackNotification {incident_id: $incident_id, notification_type: $kind}) "
        "RETURN n.state, n.receipt_channel, n.receipt_ts, n.receipt_thread_ts, n.permalink, n.not_before "
        "ORDER BY n.created_at",
        {"incident_id": incident_id, "kind": kind},
        timeout=3000,
    ).result_set


@pytest.mark.asyncio
async def test_root_is_immutable_and_competing_consumers_submit_once() -> None:
    config = _config()
    _incident(config)
    queued = await asyncio.gather(*(enqueue_incident_root("inc-notify", config=config) for _ in range(20)))
    first = queued[0]
    assert {item["notification_id"] for item in queued} == {first["notification_id"]}
    graph = _graph(config)
    graph.query(
        "MATCH (i:Incident {incident_id: 'inc-notify', tenant_id: 'acme'}) "
        "SET i.status = 'recovered', i.updated_at = '2026-01-02T04:00:00+00:00', "
        "i.recorded_at = '2026-01-02T04:00:00+00:00'",
        timeout=3000,
    )
    again = await enqueue_incident_root("inc-notify", config=config)
    assert len(_notification(graph, "inc-notify", "root")) == 1
    assert again["notification_id"] == first["notification_id"]

    client = ControlledSlack()
    await asyncio.gather(*(process_next_notification(config, client=client) for _ in range(20)))
    assert len(client.posts) == 1
    rows = _notification(graph, "inc-notify", "root")
    assert len(rows) == 1
    assert rows[0][0:5] == ["sent", "CINCIDENTS", "1700000000.000001", "1700000000.000001", "https://slack.example/CINCIDENTS/1700000000.000001"]


@pytest.mark.asyncio
async def test_outcome_uses_sent_root_thread_and_leaves_recorded_outcome_untouched() -> None:
    config = _config()
    _incident(config, "inc-thread")
    client = ControlledSlack()
    await enqueue_incident_root("inc-thread", config=config)
    await process_next_notification(config, client=client)
    graph = _graph(config)
    graph.query(
        "MATCH (i:Incident {incident_id: 'inc-thread', tenant_id: 'acme'}) SET i.status = 'recovered', "
        "i.updated_at = '2026-01-02T04:00:00+00:00'",
        timeout=3000,
    )
    await enqueue_incident_outcome("inc-thread", config=config)
    await process_next_notification(config, client=client)
    assert client.posts[1]["thread_ts"] == "1700000000.000001"
    assert _notification(graph, "inc-thread", "outcome")[0][0] == "sent"
    assert graph.ro_query(
        "MATCH (i:Incident {incident_id: 'inc-thread', tenant_id: 'acme'}) RETURN i.status",
        timeout=3000,
    ).result_set == [["recovered"]]


@pytest.mark.asyncio
async def test_permalink_failure_retries_only_the_known_receipt_read() -> None:
    config = _config()
    _incident(config, "inc-permalink")
    client = ControlledSlack(permalink_error=RuntimeError("controlled read failure"))
    await enqueue_incident_root("inc-permalink", config=config)
    assert await process_next_notification(config, client=client) == {"status": "receipt_pending"}
    assert len(client.posts) == 1
    client.permalink_error = None
    assert (await process_next_notification(config, client=client))["status"] == "sent"
    assert len(client.posts) == 1
    assert _notification(_graph(config), "inc-permalink", "root")[0][0] == "sent"


@pytest.mark.asyncio
async def test_ambiguous_send_and_rate_limit_never_immediately_resubmit() -> None:
    config = _config()
    _incident(config, "inc-unknown")
    unknown_client = ControlledSlack(post_error=TimeoutError("controlled timeout"))
    await enqueue_incident_root("inc-unknown", config=config)
    assert await process_next_notification(config, client=unknown_client) == {"status": "unknown"}
    assert await process_next_notification(config, client=unknown_client) is None
    assert len(unknown_client.posts) == 1

    _incident(config, "inc-429")
    rate_error = SlackApiError("rate limited", response=SimpleNamespace(status_code=429, headers={"Retry-After": "30"}))
    limited_client = ControlledSlack(post_error=rate_error)
    await enqueue_incident_root("inc-429", config=config)
    assert await process_next_notification(config, client=limited_client) == {"status": "rate_limited"}
    row = _notification(_graph(config), "inc-429", "root")[0]
    assert row[0] == "pending"
    assert row[5] > "2026-01-01T00:00:00+00:00"
    assert await process_next_notification(config, client=limited_client) is None
    assert len(limited_client.posts) == 1


@pytest.mark.asyncio
async def test_reconciliation_fetches_one_exact_external_receipt_without_posting() -> None:
    config = _config()
    _incident(config, "inc-reconcile")
    client = ControlledSlack(post_error=TimeoutError("controlled timeout"))
    queued = await enqueue_incident_root("inc-reconcile", config=config)
    await process_next_notification(config, client=client)
    client.post_error = None
    attempted_text = client.posts[0]["text"]
    client.history_messages = [{"user": "UBOT", "text": attempted_text.replace(queued["version"], "obsolete-version"),
                                "ts": "1700000010.000001"}]
    assert (await reconcile_notification_receipt(queued["notification_id"], config=config, client=client))["status"] == "unverifiable"
    assert _notification(_graph(config), "inc-reconcile", "root")[0][0] == "notification_unknown"
    client.history_messages = [{"user": "UBOT", "text": attempted_text, "ts": "1700000010.000001"}]
    result = await reconcile_notification_receipt(queued["notification_id"], config=config, client=client)
    assert result["status"] == "reconciled"
    assert len(client.posts) == 1
    row = _notification(_graph(config), "inc-reconcile", "root")[0]
    assert row[0:5] == ["sent", "CINCIDENTS", "1700000010.000001", "1700000010.000001", "https://slack.example/CINCIDENTS/1700000010.000001"]


@pytest.mark.asyncio
async def test_restart_marks_abandoned_send_unknown_without_resubmitting() -> None:
    config = _config()
    _incident(config, "inc-restart")
    queued = await enqueue_incident_root("inc-restart", config=config)
    graph = _graph(config)
    graph.query(
        "MATCH (n:SlackNotification {notification_id: $notification_id}) "
        "SET n.state = 'sending', n.claimed_at = '2026-01-02T03:05:00+00:00'",
        {"notification_id": queued["notification_id"]},
        timeout=3000,
    )
    client = ControlledSlack()
    assert await process_next_notification(config, client=client, initialize=True) is None
    assert client.posts == []
    assert _notification(graph, "inc-restart", "root")[0][0] == "notification_unknown"


@pytest.mark.asyncio
async def test_enqueue_refuses_foreign_tenant_or_unbound_service() -> None:
    config = _config()
    _incident(config, "inc-foreign")
    with pytest.raises(ValueError, match="Unknown tenant"):
        await enqueue_incident_root("inc-foreign", tenant_id="foreign", config=config)
    with pytest.raises(NotificationError, match="notification_tenant_not_owned"):
        await enqueue_incident_root("inc-foreign", tenant_id="globex", config=config)
    _graph(config).query(
        "CREATE (:Incident {incident_id: 'inc-wrong-service', tenant_id: 'acme', service_id: 'catalog', "
        "execution_mode: 'real', status: 'observed', starts_at: '2026-01-02T03:04:05+00:00'})",
        timeout=3000,
    )
    with pytest.raises(NotificationError, match="notification_service_not_bound"):
        await enqueue_incident_root("inc-wrong-service", config=config)


@pytest.mark.asyncio
async def test_waiting_outcome_does_not_starve_another_incidents_root(monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta
    from integrations import slack_notifications as notifications

    now = datetime(2026, 1, 2, tzinfo=UTC)
    monkeypatch.setattr(notifications, "_now", lambda: now)
    config = _config()
    _incident(config, "inc-waiting")
    graph = _graph(config)
    graph.query("MATCH (i:Incident {incident_id:'inc-waiting'}) SET i.status='failed'", timeout=3000)
    await enqueue_incident_outcome("inc-waiting", config=config)
    client = ControlledSlack()
    assert (await process_next_notification(config, client=client))["status"] == "waiting_for_root"
    now += timedelta(milliseconds=100)
    _incident(config, "inc-new-root")
    await enqueue_incident_root("inc-new-root", config=config)
    assert (await process_next_notification(config, client=client))["status"] == "sent"
    assert _notification(graph, "inc-new-root", "root")[0][0] == "sent"
    assert _notification(graph, "inc-waiting", "outcome")[0][0] == "pending"
