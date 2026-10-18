"""Durable, graph-backed Slack incident notification outbox.

All notification content, destinations, versions, and thread links are derived from
recorded topology facts.  This module deliberately has no dependency on the Slack
lifecycle module: it is started by that module only after its source-writer lock is
held.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from falkordb import FalkorDB
from redis.exceptions import ResponseError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from agents.settings import graph_names, require_tenant
from seed.load import get_config


_TERMINAL_STATUSES = frozenset(
    {"recovered", "failed", "recovery_failed", "verification_unknown", "execution_unknown",
     "recovered_externally", "applied", "rejected"}
)
_MAX_RATE_LIMIT_RETRIES = 3
_MAX_RECONCILIATION_PAGES = 4
_RECONCILIATION_PAGE_SIZE = 15
_TS_RE = re.compile(r"^\d{1,16}\.\d{1,9}$")


class NotificationError(ValueError):
    """A requested notification cannot safely be created or dispatched."""


class NotificationConflict(NotificationError):
    """A notification key is already bound to a different immutable payload."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()

def _runtime_config(config: Mapping[str, str] | None) -> Mapping[str, str]:
    return get_config() if config is None else config


def _require_config(config: Mapping[str, str], *keys: str) -> None:
    missing = [key for key in keys if not isinstance(config.get(key), str) or not config[key].strip()]
    if missing:
        raise NotificationError("missing_notification_configuration")


def _notification_config(config: Mapping[str, str]) -> tuple[str, str, str]:
    _require_config(config, "SLACK_TEAM_ID", "SLACK_CHANNEL_ID", "SLACK_SERVICE_ID")
    return config["SLACK_TEAM_ID"], config["SLACK_CHANNEL_ID"], config["SLACK_SERVICE_ID"]


def _graph(config: Mapping[str, str]) -> Any:
    _require_config(
        config,
        "FALKORDB_HOST",
        "FALKORDB_PORT",
        "FALKORDB_WRITER_ACME_USERNAME",
        "FALKORDB_WRITER_ACME_PASSWORD",
    )
    graph = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_WRITER_ACME_USERNAME"],
        password=config["FALKORDB_WRITER_ACME_PASSWORD"],
    ).select_graph(graph_names().topology)
    constraints = [item for item in graph.list_constraints()
                   if item["type"] == "UNIQUE" and item["label"] == "SlackNotification"
                   and item["properties"] == ["outbox_key"] and item["status"] == "OPERATIONAL"]
    if len(constraints) != 1:
        raise NotificationError("notification_identity_constraint_unavailable")
    return graph


def _rows(result: Any) -> list[Any]:
    return list(getattr(result, "result_set", result))


def _row_map(row: Any, names: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {name: row[index] for index, name in enumerate(names)}


def _recorded_root_facts(incident: Mapping[str, Any]) -> dict[str, Any]:
    """Immutable incident identity facts; intentionally excludes changing health/status."""
    return {
        "incident_id": incident["incident_id"],
        "tenant_id": incident["tenant_id"],
        "service_id": incident.get("service_id"),
        "execution_mode": incident.get("execution_mode"),
        "starts_at": incident.get("starts_at"),
        "fingerprint": incident.get("fingerprint"),
        "deployment_id": incident.get("deployment_id"),
        "origin": incident.get("origin"),
    }


def _recorded_outcome_facts(incident: Mapping[str, Any], action: Mapping[str, Any] | None) -> dict[str, Any]:
    """Persisted terminal facts only; no caller outcome object or current time participates."""
    facts: dict[str, Any] = {
        "incident_id": incident["incident_id"],
        "tenant_id": incident["tenant_id"],
        "service_id": incident.get("service_id"),
        "execution_mode": incident.get("execution_mode"),
        "incident_status": incident.get("status"),
        "starts_at": incident.get("starts_at"),
        "approval_id": incident.get("approval_id"),
    }
    if action:
        facts["action"] = {
            key: action.get(key)
            for key in (
                "approval_id",
                "status",
                "execution_mode",
                "service_id",
                "action_kind",
                "command_id",
                "recovered_at",
                "applied_at",
                "rejected_at",
                "failed_at",
            )
        }
    return facts


def _version(kind: Literal["root", "outcome"], facts: Mapping[str, Any]) -> str:
    return f"{kind}-{_digest(facts)}"


def _outbox_key(tenant_id: str, incident_id: str, kind: str, version: str) -> str:
    return f"{tenant_id}:{incident_id}:{kind}:{version}"


def _fetch_incident(graph: Any, incident_id: str, tenant_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    rows = _rows(
        graph.ro_query(
            "MATCH (i:Incident {incident_id: $incident_id, tenant_id: $tenant_id}) "
            "OPTIONAL MATCH (a:ApprovalRecord {incident_id: $incident_id, tenant_id: $tenant_id}) "
            "WHERE a.approval_id = i.approval_id "
            "RETURN properties(i) AS incident, properties(a) AS action LIMIT 2",
            {"incident_id": incident_id, "tenant_id": tenant_id},
            timeout=3000,
        )
    )
    if len(rows) != 1:
        raise NotificationError("incident_not_found_or_ambiguous")
    row = _row_map(rows[0], ("incident", "action"))
    incident = row.get("incident")
    if not isinstance(incident, Mapping):
        raise NotificationError("incident_not_found")
    action = row.get("action")
    return dict(incident), dict(action) if isinstance(action, Mapping) else None


def _enqueue_sync(
    config: Mapping[str, str], *, incident_id: str, tenant_id: str, kind: Literal["root", "outcome"]
) -> dict[str, Any]:
    tenant_id = require_tenant(tenant_id)
    if tenant_id != "acme":
        raise NotificationError("notification_tenant_not_owned")
    team_id, channel_id, service_id = _notification_config(config)
    graph = _graph(config)
    incident, action = _fetch_incident(graph, incident_id, tenant_id)
    if incident.get("service_id") != service_id:
        raise NotificationError("notification_service_not_bound")
    if kind == "root":
        facts = _recorded_root_facts(incident)
        text = (
            f"Incident {incident_id} detected for {service_id}. "
            f"Mode: {incident.get('execution_mode', 'unknown')}."
        )
    else:
        if incident.get("status") not in _TERMINAL_STATUSES:
            raise NotificationError("notification_outcome_not_terminal")
        facts = _recorded_outcome_facts(incident, action)
        text = (
            f"Incident {incident_id} outcome: {incident['status']}. "
            f"Service: {service_id}. Mode: {incident.get('execution_mode', 'unknown')}."
        )
    version = _version(kind, facts)
    # The externally observable receipt must identify the immutable version,
    # including when two outcomes have identical human-readable statuses.
    text += f" Notification version: {version}."
    if kind == "root" and config.get("PUBLIC_ORIGIN"):
        text += f" Review: {config['PUBLIC_ORIGIN'].rstrip('/')}."
    key = _outbox_key(tenant_id, incident_id, kind, version)
    destination = {"team_id": team_id, "channel_id": channel_id}
    payload = {"text": text, "facts": facts}
    payload_hash = _digest(payload)
    destination_hash = _digest(destination)
    notification_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
    try:
        rows = _rows(graph.query(
            "MERGE (n:SlackNotification {outbox_key: $outbox_key}) "
            "ON CREATE SET n.notification_id = $notification_id, n.tenant_id = $tenant_id, "
            "n.incident_id = $incident_id, n.notification_type = $notification_type, n.version = $version, "
            "n.payload_json = $payload_json, n.payload_hash = $payload_hash, "
            "n.destination_hash = $destination_hash, n.team_id = $team_id, n.channel_id = $channel_id, "
            "n.state = 'pending', n.attempt_count = 0, "
            "n.not_before = $created_at, n.created_at = $created_at "
            "RETURN properties(n) AS notification",
            {"outbox_key": key, "notification_id": notification_id, "tenant_id": tenant_id,
             "incident_id": incident_id, "notification_type": kind, "version": version,
             "payload_json": _canonical(payload), "payload_hash": payload_hash,
             "destination_hash": destination_hash, "team_id": team_id, "channel_id": channel_id,
             "created_at": _iso()}, timeout=3000))
    except ResponseError:
        # A competing constrained MERGE can lose rather than re-match. Its
        # already-persisted immutable postcondition is the only acceptable result.
        rows = _rows(graph.ro_query(
            "MATCH (n:SlackNotification {outbox_key:$key}) RETURN properties(n) AS notification LIMIT 2",
            {"key": key}, timeout=3000))
        if len(rows) != 1:
            raise
    if not rows:
        raise NotificationError("notification_enqueue_failed")
    notification = _row_map(rows[0], ("notification",)).get("notification")
    if not isinstance(notification, Mapping):
        raise NotificationError("notification_enqueue_failed")
    if notification.get("payload_hash") != payload_hash or notification.get("destination_hash") != destination_hash:
        raise NotificationConflict("notification_immutable_conflict")
    return dict(notification)


async def enqueue_incident_root(
    incident_id: str, tenant_id: str = "acme", *, config: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Enqueue the one immutable root message for a recorded incident."""
    return await asyncio.to_thread(
        _enqueue_sync, _runtime_config(config), incident_id=incident_id, tenant_id=tenant_id, kind="root"
    )


async def enqueue_incident_outcome(
    incident_id: str, tenant_id: str = "acme", *, config: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Enqueue a recorded terminal incident outcome; callers cannot supply outcome text."""
    return await asyncio.to_thread(
        _enqueue_sync, _runtime_config(config), incident_id=incident_id, tenant_id=tenant_id, kind="outcome"
    )


def _mark_abandoned_sends(graph: Any) -> None:
    graph.query(
        "MATCH (n:SlackNotification {tenant_id: 'acme', state: 'sending'}) "
        "SET n.state = 'notification_unknown', n.unknown_at = $now, n.last_error = 'abandoned_sending'",
        {"now": _iso()},
        timeout=3000,
    )
    graph.query(
        "MATCH (n:SlackNotification {tenant_id: 'acme', state: 'reading_receipt'}) "
        "SET n.state = 'receipt_pending', n.not_before = $now, n.last_error = 'receipt_read_interrupted'",
        {"now": _iso()},
        timeout=3000,
    )


def _claim_due(graph: Any) -> dict[str, Any] | None:
    rows = _rows(
        graph.query(
            "MATCH (n:SlackNotification {tenant_id: 'acme'}) "
            "WHERE n.state IN ['pending', 'receipt_pending'] AND n.not_before <= $now "
            "WITH n ORDER BY n.created_at ASC LIMIT 1 "
            "SET n.state = CASE n.state WHEN 'receipt_pending' THEN 'reading_receipt' ELSE 'sending' END, "
            "    n.claimed_at = $now, n.attempt_count = coalesce(n.attempt_count, 0) + 1 "
            "RETURN properties(n) AS notification",
            {"now": _iso()},
            timeout=3000,
        )
    )
    if not rows:
        return None
    notification = _row_map(rows[0], ("notification",)).get("notification")
    return dict(notification) if isinstance(notification, Mapping) else None


def _set_fields(graph: Any, notification_id: str, fields: Mapping[str, Any]) -> None:
    graph.query(
        "MATCH (n:SlackNotification {notification_id: $notification_id, tenant_id: 'acme'}) "
        "SET n += $fields RETURN n.notification_id",
        {"notification_id": notification_id, "fields": dict(fields)},
        timeout=3000,
    )


def _root_thread(graph: Any, notification: Mapping[str, Any]) -> str | None:
    rows = _rows(
        graph.ro_query(
            "MATCH (root:SlackNotification {tenant_id: $tenant_id, incident_id: $incident_id, notification_type: 'root', state: 'sent'}) "
            "WHERE root.receipt_thread_ts IS NOT NULL "
            "RETURN root.receipt_thread_ts AS thread_ts ORDER BY root.sent_at DESC LIMIT 1",
            {"tenant_id": notification["tenant_id"], "incident_id": notification["incident_id"]},
            timeout=3000,
        )
    )
    if not rows:
        return None
    value = _row_map(rows[0], ("thread_ts",)).get("thread_ts")
    return value if isinstance(value, str) and _TS_RE.fullmatch(value) else None


def _parse_payload(notification: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(notification["payload_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise NotificationError("notification_corrupt_payload") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise NotificationError("notification_corrupt_payload")
    return payload


def _retry_after(error: SlackApiError) -> int | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    return seconds if 1 <= seconds <= 3600 else None


def _slack_status(error: SlackApiError) -> int | None:
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _schedule_or_fail_rate_limit(graph: Any, notification: Mapping[str, Any], error: SlackApiError, *, receipt_only: bool) -> None:
    retry_after = _retry_after(error)
    attempts = int(notification.get("rate_limit_count", 0)) + 1
    if retry_after is not None and attempts <= _MAX_RATE_LIMIT_RETRIES:
        _set_fields(
            graph,
            str(notification["notification_id"]),
            {
                "state": "receipt_pending" if receipt_only else "pending",
                "not_before": _iso(_now() + timedelta(seconds=retry_after)),
                "rate_limit_count": attempts,
                "last_error": "slack_rate_limited",
            },
        )
        return
    _set_fields(
        graph,
        str(notification["notification_id"]),
        {"state": "failed", "failed_at": _iso(), "last_error": "slack_rate_limited"},
    )


async def _bot_identity(client: AsyncWebClient, config: Mapping[str, str]) -> str:
    response = await client.auth_test()
    team_id = response.get("team_id")
    user_id = response.get("user_id")
    if team_id != config["SLACK_TEAM_ID"] or not isinstance(user_id, str) or not user_id:
        raise NotificationError("slack_identity_not_bound")
    return user_id


async def _read_permalink(
    graph: Any, notification: Mapping[str, Any], client: AsyncWebClient
) -> dict[str, Any]:
    channel = notification.get("receipt_channel")
    ts = notification.get("receipt_ts")
    if not isinstance(channel, str) or not isinstance(ts, str):
        await asyncio.to_thread(
            _set_fields,
            graph,
            str(notification["notification_id"]),
            {"state": "notification_unknown", "last_error": "missing_known_receipt"},
        )
        return {"status": "unknown"}
    try:
        response = await client.chat_getPermalink(channel=channel, message_ts=ts)
        permalink = response.get("permalink")
        if not isinstance(permalink, str) or not permalink:
            raise NotificationError("slack_permalink_missing")
    except SlackApiError as error:
        if _slack_status(error) == 429:
            await asyncio.to_thread(_schedule_or_fail_rate_limit, graph, notification, error, receipt_only=True)
        else:
            await asyncio.to_thread(
                _set_fields,
                graph,
                str(notification["notification_id"]),
                {"state": "receipt_pending", "not_before": _iso(), "last_error": "permalink_unavailable"},
            )
        return {"status": "receipt_pending"}
    except Exception:
        await asyncio.to_thread(
            _set_fields,
            graph,
            str(notification["notification_id"]),
            {"state": "receipt_pending", "not_before": _iso(), "last_error": "permalink_unavailable"},
        )
        return {"status": "receipt_pending"}
    await asyncio.to_thread(
        _set_fields,
        graph,
        str(notification["notification_id"]),
        {"state": "sent", "sent_at": _iso(), "permalink": permalink, "last_error": None},
    )
    return {"status": "sent", "permalink": permalink}


async def process_next_notification(
    config: Mapping[str, str], *, client: AsyncWebClient | None = None, initialize: bool = False
) -> dict[str, Any] | None:
    """Claim and process one due row.  Kept public for bounded worker embedding/tests."""
    _notification_config(config)
    if client is None:
        _require_config(config, "SLACK_BOT_TOKEN")
        client = AsyncWebClient(token=config["SLACK_BOT_TOKEN"], retry_handlers=[])
    graph = await asyncio.to_thread(_graph, config)
    if initialize:
        await asyncio.to_thread(_mark_abandoned_sends, graph)
    notification = await asyncio.to_thread(_claim_due, graph)
    if notification is None:
        return None
    if notification.get("state") == "reading_receipt" and notification.get("receipt_ts"):
        return await _read_permalink(graph, notification, client)
    try:
        payload = _parse_payload(notification)
        bot_user_id = await _bot_identity(client, config)
        if notification.get("team_id") != config["SLACK_TEAM_ID"] or notification.get("channel_id") != config["SLACK_CHANNEL_ID"]:
            raise NotificationError("notification_destination_not_bound")
        kwargs: dict[str, Any] = {
            "channel": config["SLACK_CHANNEL_ID"],
            "text": payload["text"],
        }
        if notification.get("notification_type") == "outcome":
            thread_ts = await asyncio.to_thread(_root_thread, graph, notification)
            if thread_ts is None:
                await asyncio.to_thread(
                    _set_fields,
                    graph,
                    str(notification["notification_id"]),
                    {"state": "pending", "not_before": _iso(_now() + timedelta(seconds=1)),
                     "last_error": "root_receipt_unavailable"},
                )
                return {"status": "waiting_for_root"}
            kwargs["thread_ts"] = thread_ts
        response = await client.chat_postMessage(**kwargs)
        channel = response.get("channel")
        ts = response.get("ts")
        returned_thread = response.get("message", {}).get("thread_ts") if isinstance(response.get("message"), Mapping) else None
        if not isinstance(channel, str) or not isinstance(ts, str) or not _TS_RE.fullmatch(ts):
            await asyncio.to_thread(
                _set_fields,
                graph,
                str(notification["notification_id"]),
                {"state": "notification_unknown", "last_error": "send_receipt_unverifiable"},
            )
            return {"status": "unknown"}
        valid_returned_thread = returned_thread if isinstance(returned_thread, str) and _TS_RE.fullmatch(returned_thread) else None
        receipt_thread = valid_returned_thread or ts
        fields = {
            "state": "receipt_pending",
            "receipt_channel": channel,
            "receipt_ts": ts,
            "receipt_thread_ts": receipt_thread,
            "bot_user_id": bot_user_id,
            "send_receipt_at": _iso(),
            "last_error": None,
        }
        await asyncio.to_thread(_set_fields, graph, str(notification["notification_id"]), fields)
        if channel != config["SLACK_CHANNEL_ID"]:
            await asyncio.to_thread(
                _set_fields,
                graph,
                str(notification["notification_id"]),
                {"state": "notification_unknown", "last_error": "send_receipt_wrong_destination"},
            )
            return {"status": "unknown"}
        if "thread_ts" in kwargs and valid_returned_thread != kwargs["thread_ts"]:
            await asyncio.to_thread(
                _set_fields,
                graph,
                str(notification["notification_id"]),
                {"state": "notification_unknown", "last_error": "send_receipt_wrong_thread"},
            )
            return {"status": "unknown"}
        notification = {**notification, **fields}
        return await _read_permalink(graph, notification, client)
    except SlackApiError as error:
        if _slack_status(error) == 429:
            await asyncio.to_thread(_schedule_or_fail_rate_limit, graph, notification, error, receipt_only=False)
            return {"status": "rate_limited"}
        if _slack_status(error) is not None and 400 <= _slack_status(error) < 500:
            await asyncio.to_thread(
                _set_fields,
                graph,
                str(notification["notification_id"]),
                {"state": "failed", "failed_at": _iso(), "last_error": "slack_rejected"},
            )
            return {"status": "failed"}
        await asyncio.to_thread(
            _set_fields,
            graph,
            str(notification["notification_id"]),
            {"state": "notification_unknown", "unknown_at": _iso(), "last_error": "slack_ambiguous_send"},
        )
        return {"status": "unknown"}
    except NotificationError as error:
        await asyncio.to_thread(
            _set_fields,
            graph,
            str(notification["notification_id"]),
            {"state": "failed", "failed_at": _iso(), "last_error": str(error)},
        )
        return {"status": "failed"}
    except Exception:
        await asyncio.to_thread(
            _set_fields,
            graph,
            str(notification["notification_id"]),
            {"state": "notification_unknown", "unknown_at": _iso(), "last_error": "slack_ambiguous_send"},
        )
        return {"status": "unknown"}


async def run_notification_worker(config: Mapping[str, str], stop_event: asyncio.Event | None = None) -> None:
    """Run the production outbox consumer until stopped by the lifecycle owner."""
    _require_config(config, "SLACK_BOT_TOKEN")
    client = AsyncWebClient(token=config["SLACK_BOT_TOKEN"], retry_handlers=[])
    initialized = False
    while stop_event is None or not stop_event.is_set():
        result = await process_next_notification(config, client=client, initialize=not initialized)
        initialized = True
        if result is None:
            if stop_event is None:
                await asyncio.sleep(1)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1)
                except TimeoutError:
                    pass


async def _history_candidates(
    client: AsyncWebClient, channel: str, *, thread_ts: str | None
) -> list[Mapping[str, Any]]:
    candidates: list[Mapping[str, Any]] = []
    cursor: str | None = None
    for _ in range(_MAX_RECONCILIATION_PAGES):
        if thread_ts is None:
            response = await client.conversations_history(channel=channel, limit=_RECONCILIATION_PAGE_SIZE, cursor=cursor)
        else:
            response = await client.conversations_replies(channel=channel, ts=thread_ts, limit=_RECONCILIATION_PAGE_SIZE, cursor=cursor)
        messages = response.get("messages")
        if isinstance(messages, list):
            candidates.extend(message for message in messages if isinstance(message, Mapping))
        metadata = response.get("response_metadata")
        cursor = metadata.get("next_cursor") if isinstance(metadata, Mapping) else None
        if not isinstance(cursor, str) or not cursor:
            break
    return candidates


async def reconcile_notification_receipt(
    notification_id: str, *, config: Mapping[str, str] | None = None, client: AsyncWebClient | None = None
) -> dict[str, Any]:
    """Boundedly discover an exact Slack receipt for an unknown send without posting anything."""
    config = _runtime_config(config)
    _notification_config(config)
    graph = await asyncio.to_thread(_graph, config)
    rows = await asyncio.to_thread(
        lambda: _rows(
            graph.ro_query(
                "MATCH (n:SlackNotification {notification_id: $notification_id, tenant_id: 'acme', state: 'notification_unknown'}) "
                "RETURN properties(n) AS notification LIMIT 1",
                {"notification_id": notification_id},
                timeout=3000,
            )
        )
    )
    if not rows:
        return {"status": "not_unknown"}
    notification = _row_map(rows[0], ("notification",)).get("notification")
    if not isinstance(notification, Mapping):
        return {"status": "not_unknown"}
    if client is None:
        _require_config(config, "SLACK_BOT_TOKEN")
        client = AsyncWebClient(token=config["SLACK_BOT_TOKEN"], retry_handlers=[])
    try:
        bot_user_id = await _bot_identity(client, config)
        expected_bot = notification.get("bot_user_id") or bot_user_id
        if expected_bot != bot_user_id:
            return {"status": "unverifiable"}
        payload = _parse_payload(notification)
        channel = notification.get("channel_id")
        if channel != config["SLACK_CHANNEL_ID"]:
            return {"status": "unverifiable"}
        thread_ts = None
        if notification.get("notification_type") == "outcome":
            thread_ts = await asyncio.to_thread(_root_thread, graph, notification)
            if thread_ts is None:
                return {"status": "unverifiable"}
        candidates = await _history_candidates(client, channel, thread_ts=thread_ts)
        matches = [
            item
            for item in candidates
            if item.get("user") == bot_user_id
            and item.get("text") == payload["text"]
            and isinstance(item.get("ts"), str)
            and _TS_RE.fullmatch(item["ts"])
            and (thread_ts is None or item.get("thread_ts") == thread_ts)
        ]
        if len(matches) != 1:
            return {"status": "unverifiable"}
        match = matches[0]
        ts = match["ts"]
        receipt_thread = match.get("thread_ts") if isinstance(match.get("thread_ts"), str) else ts
        permalink_response = await client.chat_getPermalink(channel=channel, message_ts=ts)
        permalink = permalink_response.get("permalink")
        if not isinstance(permalink, str) or not permalink:
            return {"status": "unverifiable"}
        await asyncio.to_thread(
            _set_fields,
            graph,
            notification_id,
            {
                "state": "sent",
                "receipt_channel": channel,
                "receipt_ts": ts,
                "receipt_thread_ts": receipt_thread,
                "bot_user_id": bot_user_id,
                "permalink": permalink,
                "sent_at": _iso(),
                "reconciled_at": _iso(),
                "last_error": None,
            },
        )
        return {"status": "reconciled", "notification_id": notification_id, "permalink": permalink}
    except Exception:
        return {"status": "unverifiable"}
