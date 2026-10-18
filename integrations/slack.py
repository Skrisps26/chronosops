"""Durable Slack Socket Mode lifecycle ingestion.

Only this module owns Slack message ingestion.  Slack notifications live in
:mod:`integrations.slack_notifications` and are started only after the source
writer has obtained its process-wide lock.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from falkordb import FalkorDB
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from agents.settings import graph_names, require_tenant
from seed.load import get_config
from seed.sources import SourceNotFound, SourceUnavailable, enqueue_source, run_source_worker, wait_for_publication

logger = logging.getLogger(__name__)

_CONNECTOR_ID = "slack"
_HEARTBEAT_STALE_SECONDS = 90
_RECONCILE_PAGES = 4
_RECONCILE_PAGE_SIZE = 15


@dataclass(frozen=True)
class SlackEvent:
    """Normalized, bounded fields retained for one external Slack event."""

    event_id: str
    team_id: str
    channel_id: str
    document_id: str
    event_type: str
    event_timestamp: str
    message_ts: str
    thread_ts: str
    source_version: str
    source_order: int
    text: str
    author_id: str
    delete: bool


def normalize_slack_document_id(team_id: str, channel_id: str, message_ts: str) -> str:
    """Return the canonical document identity without lossy timestamp conversion."""
    return f"slack:{team_id}:{channel_id}:{message_ts}"


def is_bot_message(event: Mapping[str, Any], bot_user_id: str | None = None) -> bool:
    """Identify messages which must never enter the durable inbox."""
    if bot_user_id and event.get("user") == bot_user_id:
        return True
    if event.get("bot_id") or event.get("app_id"):
        return True
    return event.get("subtype") in {"bot_message", "channel_join", "channel_leave"}


def is_allowed_channel(event: Mapping[str, Any], allowed_channel_id: str) -> bool:
    """Return whether an event is explicitly bound to the configured channel."""
    channel = event.get("channel")
    if not channel and isinstance(event.get("item"), Mapping):
        channel = event["item"].get("channel")
    return isinstance(channel, str) and channel == allowed_channel_id


def _require_config(config: Mapping[str, str], *, socket_mode: bool) -> dict[str, str]:
    required = ["SLACK_TEAM_ID", "SLACK_CHANNEL_ID", "SLACK_BOT_TOKEN", "SLACK_SERVICE_ID"]
    if socket_mode:
        required.append("SLACK_APP_TOKEN")
    missing = [key for key in required if not isinstance(config.get(key), str) or not config[key].strip()]
    if missing:
        raise ValueError("Missing required Slack connector configuration")
    return {key: str(value) for key, value in config.items()}


def _timestamp_order(value: Any) -> int:
    """Convert a Slack decimal timestamp to exact integer microseconds."""
    if not isinstance(value, str) or not value:
        raise ValueError("Slack timestamp is missing")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Slack timestamp is invalid") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("Slack timestamp is invalid")
    microseconds = parsed * Decimal(1_000_000)
    if microseconds != microseconds.to_integral_value():
        raise ValueError("Slack timestamp exceeds microsecond precision")
    return int(microseconds)


def _writer_graph(config: Mapping[str, str]) -> Any:
    """Use the current trusted source-ingestion writer convention."""
    return FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config["FALKORDB_ADMIN_PASSWORD"],
    ).select_graph(graph_names().company)


def _reader_graph(config: Mapping[str, str], tenant_id: str) -> Any:
    tenant = require_tenant(tenant_id)
    upper = tenant.upper()
    password = config.get(f"FALKORDB_READER_{upper}_PASSWORD")
    if not password:
        raise ValueError("Tenant reader credentials are unavailable")
    return FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config.get(f"FALKORDB_READER_{upper}_USERNAME", f"reader_{tenant}"),
        password=password,
    ).select_graph(graph_names().company)


def _normalize_event(body: Mapping[str, Any], event: Mapping[str, Any], config: Mapping[str, str], bot_user_id: str | None) -> SlackEvent | None:
    """Accept only complete human post/edit/delete envelopes from this connector."""
    team_id = body.get("team_id") or event.get("team")
    event_id = body.get("event_id")
    if not isinstance(team_id, str) or team_id != config["SLACK_TEAM_ID"]:
        return None
    if not isinstance(event_id, str) or not event_id:
        return None
    channel_id = event.get("channel")
    if not isinstance(channel_id, str) or channel_id != config["SLACK_CHANNEL_ID"]:
        return None

    subtype = event.get("subtype")
    candidate: Mapping[str, Any] = event
    delete = False
    event_type = "post"
    event_timestamp: Any = event.get("event_ts") or event.get("ts")
    message_ts: Any = event.get("ts")
    version: Any = event.get("ts")
    text: Any = event.get("text")
    author: Any = event.get("user")
    thread_ts: Any = event.get("thread_ts") or message_ts

    if subtype == "message_changed":
        candidate = event.get("message") if isinstance(event.get("message"), Mapping) else {}
        event_type = "edit"
        message_ts = candidate.get("ts")
        edited = candidate.get("edited")
        # Wrapper delivery time can describe metadata-only changes, not content.
        version = edited.get("ts") if isinstance(edited, Mapping) else message_ts
        text = candidate.get("text")
        author = candidate.get("user")
        thread_ts = candidate.get("thread_ts") or message_ts
    elif subtype == "message_deleted":
        candidate = event.get("previous_message") if isinstance(event.get("previous_message"), Mapping) else {}
        event_type = "delete"
        delete = True
        message_ts = event.get("deleted_ts") or candidate.get("ts")
        version = event.get("event_ts") or event.get("ts")
        text = ""
        author = candidate.get("user", event.get("user", ""))
        thread_ts = candidate.get("thread_ts") or message_ts
    elif subtype is not None:
        return None

    if is_bot_message(candidate, bot_user_id) or is_bot_message(event, bot_user_id):
        return None
    if not isinstance(event_timestamp, str) or not isinstance(message_ts, str) or not isinstance(version, str) or not isinstance(thread_ts, str):
        return None
    if not delete and not isinstance(text, str):
        return None
    try:
        source_order = _timestamp_order(version)
        _timestamp_order(event_timestamp)
        _timestamp_order(message_ts)
        _timestamp_order(thread_ts)
    except ValueError:
        return None
    if not isinstance(author, str):
        author = ""
    return SlackEvent(
        event_id=event_id,
        team_id=team_id,
        channel_id=channel_id,
        document_id=normalize_slack_document_id(team_id, channel_id, message_ts),
        event_type=event_type,
        event_timestamp=event_timestamp,
        message_ts=message_ts,
        thread_ts=thread_ts,
        source_version=version,
        source_order=source_order,
        text=text if isinstance(text, str) else "",
        author_id=f"{team_id}:{author}" if author else "",
        delete=delete,
    )


def _persist_event(event: SlackEvent, config: Mapping[str, str]) -> dict[str, Any]:
    rows = _writer_graph(config).query(
        "OPTIONAL MATCH (s:SourceRecord {document_id:$document_id}) "
        "WITH coalesce(s.status='deleted' AND s.source_order >= $source_order,false) AS tombstoned "
        "MERGE (i:SlackInbox {event_id:$event_id}) "
        "ON CREATE SET i.document_id=$document_id, i.team_id=$team_id, i.channel_id=$channel_id, "
        "i.event_type=$event_type, i.event_timestamp=$event_timestamp, i.message_ts=$message_ts, i.thread_ts=$thread_ts, "
        "i.source_version=$source_version, i.source_order=$source_order, "
        "i.text=CASE WHEN tombstoned THEN '' ELSE $text END, "
        "i.author_id=$author_id, i.delete=$delete, i.status='pending', i.created_at=$now "
        "RETURN properties(i) LIMIT 1",
        {"event_id": event.event_id, "document_id": event.document_id, "team_id": event.team_id,
         "channel_id": event.channel_id, "event_type": event.event_type, "event_timestamp": event.event_timestamp,
         "message_ts": event.message_ts, "thread_ts": event.thread_ts, "source_version": event.source_version,
         "source_order": event.source_order, "text": event.text, "author_id": event.author_id,
         "delete": event.delete, "now": time.time()},
        timeout=3000,
    ).result_set
    return dict(rows[0][0]) if rows else {}


def _claim_pending(event_id: str, config: Mapping[str, str]) -> dict[str, Any] | None:
    rows = _writer_graph(config).query(
        "MATCH (i:SlackInbox {event_id:$event_id}) WHERE i.status='pending' AND coalesce(i.not_before,0)<=$now "
        "SET i.status='processing', i.processing_at=$now RETURN properties(i) LIMIT 1",
        {"event_id": event_id, "now": time.time()}, timeout=3000,
    ).result_set
    return dict(rows[0][0]) if rows else None


def _set_inbox_state(event_id: str, state: str, config: Mapping[str, str], *, error_code: str = "",
                     not_before: float = 0) -> None:
    assignments = "i.status=$state, i.updated_at=$now, i.error_code=$error_code, i.not_before=$not_before"
    if state == "processed":
        assignments += ", i.processed_at=$now"
    _writer_graph(config).query(
        f"MATCH (i:SlackInbox {{event_id:$event_id}}) SET {assignments}",
        {"event_id": event_id, "state": state, "error_code": error_code, "now": time.time(),
         "not_before": not_before}, timeout=3000,
    )


def _clear_deleted_text(document_id: str, config: Mapping[str, str]) -> None:
    _writer_graph(config).query(
        "MATCH (i:SlackInbox {document_id:$document_id}) SET i.text=''",
        {"document_id": document_id}, timeout=3000,
    )


def _published_source_matches(inbox: Mapping[str, Any], config: Mapping[str, str]) -> bool:
    """Treat an equal-order metadata envelope as processed only with exact evidence."""
    rows = _writer_graph(config).ro_query(
        "MATCH (s:SourceRecord {document_id:$document_id}) "
        "RETURN s.status, s.source_version, s.content_hash LIMIT 1",
        {"document_id": inbox["document_id"]}, timeout=3000,
    ).result_set
    if not rows or bool(inbox.get("delete", False)):
        return False
    status, version, content_hash = rows[0]
    expected_hash = hashlib.sha256(str(inbox.get("text", "")).encode("utf-8")).hexdigest()
    return status == "published" and str(version) == str(inbox["source_version"]) and content_hash == expected_hash


def _reset_interrupted_inbox(config: Mapping[str, str]) -> None:
    _writer_graph(config).query(
        "MATCH (i:SlackInbox {status:'processing'}) SET i.status='pending', i.updated_at=$now",
        {"now": time.time()}, timeout=3000,
    )

def _set_permalink(event_id: str, permalink: str, config: Mapping[str, str]) -> None:
    _writer_graph(config).query(
        "MATCH (i:SlackInbox {event_id:$event_id}) SET i.permalink=$permalink, i.updated_at=$now",
        {"event_id": event_id, "permalink": permalink, "now": time.time()}, timeout=3000,
    )


def _set_source_request(event_id: str, request_id: str, config: Mapping[str, str]) -> None:
    _writer_graph(config).query(
        "MATCH (i:SlackInbox {event_id:$event_id}) SET i.request_id=$request_id, i.updated_at=$now",
        {"event_id": event_id, "request_id": request_id, "now": time.time()}, timeout=3000,
    )


def _pending_inbox_ids(config: Mapping[str, str]) -> list[str]:
    rows = _writer_graph(config).ro_query(
        "MATCH (i:SlackInbox {status:'pending'}) WHERE coalesce(i.not_before,0)<=$now "
        "RETURN i.event_id ORDER BY i.created_at LIMIT 1000",
        {"now": time.time()}, timeout=3000,
    ).result_set
    return [str(row[0]) for row in rows if row and isinstance(row[0], str)]


async def _actual_permalink(inbox: Mapping[str, Any], client: AsyncWebClient) -> str | None:
    """Read Slack's actual permalink; never derive a Slack URL locally."""
    response = await client.chat_getPermalink(channel=str(inbox["channel_id"]), message_ts=str(inbox["message_ts"]))
    permalink = response.get("permalink")
    return permalink if isinstance(permalink, str) and permalink else None


async def process_pending_slack_event(event_id: str, *, config: Mapping[str, str] | None = None,
                                      client: AsyncWebClient | None = None,
                                      publication_timeout_seconds: float = 120) -> str:
    """Queue one persisted inbox event and mark it processed only after publication."""
    active_config = _require_config(config or get_config(), socket_mode=False)
    inbox = await asyncio.to_thread(_claim_pending, event_id, active_config)
    if inbox is None:
        return "unavailable"
    permalink = str(inbox.get("permalink", ""))
    if not bool(inbox.get("delete", False)) and not permalink:
        try:
            permalink = await _actual_permalink(inbox, client or AsyncWebClient(token=active_config["SLACK_BOT_TOKEN"])) or ""
        except Exception as error:
            delay = 1
            if _slack_error_code(error) == "rate_limited":
                headers = getattr(getattr(error, "response", None), "headers", {}) or {}
                try:
                    delay = max(1, int(headers.get("Retry-After") or headers.get("retry-after")))
                except (TypeError, ValueError):
                    await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config,
                                            error_code="unverifiable_rate_limit")
                    return "error"
            await asyncio.to_thread(_set_inbox_state, event_id, "pending", active_config,
                                    error_code="permalink_unavailable", not_before=time.time() + delay)
            return "pending"
        if not permalink:
            await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="permalink_missing")
            return "error"
        await asyncio.to_thread(_set_permalink, event_id, permalink, active_config)
        inbox = {**inbox, "permalink": permalink}
    request_id = inbox.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        try:
            request = await asyncio.to_thread(
                enqueue_source,
                str(inbox["document_id"]), str(inbox.get("text", "")),
                allow_create=True, origin="slack", path=f"slack://{inbox['team_id']}/{inbox['channel_id']}/{inbox['message_ts']}",
                permalink=permalink, author_id=str(inbox.get("author_id", "")),
                service_id=active_config["SLACK_SERVICE_ID"], source_version=str(inbox["source_version"]),
                source_order=int(inbox["source_order"]), delete=bool(inbox.get("delete", False)),
            )
        except (ValueError, SourceNotFound):
            await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="source_rejected")
            return "error"
        except Exception:
            logger.exception("Slack source queue failure", extra={"event_id": event_id})
            await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="source_queue_failed")
            return "error"
        if not isinstance(request, Mapping):
            await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="source_superseded")
            return "error"
        if request.get("status") == "ignored":
            if await asyncio.to_thread(_published_source_matches, inbox, active_config):
                await asyncio.to_thread(_set_inbox_state, event_id, "processed", active_config)
                return "processed"
            await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="source_superseded")
            return "error"
        if not request.get("request_id"):
            await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="source_superseded")
            return "error"
        request_id = str(request["request_id"])
        await asyncio.to_thread(_set_source_request, event_id, request_id, active_config)
    try:
        source = await wait_for_publication(request_id, timeout_seconds=publication_timeout_seconds)
    except SourceUnavailable as error:
        if "Timed out" in str(error):
            await asyncio.to_thread(_set_inbox_state, event_id, "pending", active_config, error_code="awaiting_publication")
            return "pending"
        await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="source_unavailable")
        return "error"
    except Exception:
        await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="source_unavailable")
        return "error"

    if source.get("document_id") != inbox["document_id"]:
        await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="publication_mismatch")
        return "error"
    if bool(inbox.get("delete", False)):
        if source.get("status") != "deleted":
            await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="publication_mismatch")
            return "error"
        await asyncio.to_thread(_clear_deleted_text, str(inbox["document_id"]), active_config)
    elif source.get("status") != "published" or str(source.get("source_version", "")) != str(inbox["source_version"]):
        await asyncio.to_thread(_set_inbox_state, event_id, "error", active_config, error_code="publication_mismatch")
        return "error"
    await asyncio.to_thread(_set_inbox_state, event_id, "processed", active_config)
    return "processed"




async def receive_slack_event(body: Mapping[str, Any], event: Mapping[str, Any], *, config: Mapping[str, str] | None = None,
                              client: AsyncWebClient | None = None, bot_user_id: str | None = None,
                              publication_timeout_seconds: float = 120, process: bool = True) -> str:
    """Durably accept a trusted Slack event, then optionally process its source revision."""
    active_config = _require_config(config or get_config(), socket_mode=False)
    normalized = _normalize_event(body, event, active_config, bot_user_id)
    if normalized is None:
        return "ignored"
    persisted = await asyncio.to_thread(_persist_event, normalized, active_config)
    if persisted.get("status") == "processed":
        return "processed"
    if not process:
        return str(persisted.get("status", "pending"))
    return await process_pending_slack_event(normalized.event_id, config=active_config, client=client,
                                             publication_timeout_seconds=publication_timeout_seconds)


def _record_connector_state(config: Mapping[str, str], *, connected: bool | None = None, event_at: float | None = None,
                            error_code: str = "", reconciliation_truncated: bool | None = None,
                            reconciliation_gap: bool | None = None) -> None:
    now = time.time()
    fields = ["c.heartbeat_at=$now", "c.error_code=$error_code"]
    if connected is True:
        fields.extend(["c.connected=true", "c.connected_at=CASE WHEN coalesce(c.connected,false) THEN coalesce(c.connected_at,$now) ELSE $now END", "c.disconnected_at=0"])
    elif connected is False:
        fields.extend(["c.connected=false", "c.disconnected_at=$now"])
    if event_at is not None:
        fields.append("c.last_event_at=$event_at")
    if reconciliation_truncated is not None:
        fields.append("c.reconciliation_truncated=$reconciliation_truncated")
    if reconciliation_gap is not None:
        fields.append("c.reconciliation_gap=$reconciliation_gap")
    _writer_graph(config).query(
        "MERGE (c:SlackConnector {tenant_id:'acme', connector_id:'slack'}) "
        "SET " + ", ".join(fields),
        {"now": now, "event_at": event_at or 0, "error_code": error_code,
         "reconciliation_truncated": reconciliation_truncated if reconciliation_truncated is not None else False,
         "reconciliation_gap": reconciliation_gap if reconciliation_gap is not None else False}, timeout=3000,
    )


def get_connector_status(tenant_id: str = "acme") -> dict[str, Any]:
    """Read a non-secret connector status using only the requested tenant reader."""
    tenant = require_tenant(tenant_id)
    if tenant != "acme":
        return {"tenant_id": tenant, "connector": _CONNECTOR_ID, "status": "not_owner", "available": False}
    config = get_config()
    try:
        rows = _reader_graph(config, tenant).ro_query(
            "MATCH (c:SlackConnector {tenant_id:$tenant_id, connector_id:'slack'}) "
            "RETURN properties(c) LIMIT 1", {"tenant_id": tenant}, timeout=3000,
        ).result_set
    except Exception:
        return {"tenant_id": tenant, "connector": _CONNECTOR_ID, "status": "unavailable", "available": False}
    if not rows:
        return {"tenant_id": tenant, "connector": _CONNECTOR_ID, "status": "disconnected", "available": False}
    state = dict(rows[0][0])
    heartbeat = float(state.get("heartbeat_at", 0) or 0)
    stale_after = max(1, min(int(config.get("SLACK_STATUS_STALE_SECONDS", _HEARTBEAT_STALE_SECONDS)), 3600))
    stale = heartbeat <= 0 or time.time() - heartbeat > stale_after
    status = "connected" if state.get("connected") is True and not stale else ("stale" if stale else "disconnected")
    return {
        "tenant_id": tenant, "connector": _CONNECTOR_ID, "status": status, "available": status == "connected",
        "connected_at": state.get("connected_at"), "last_event_at": state.get("last_event_at"),
        "heartbeat_at": state.get("heartbeat_at"), "disconnected_at": state.get("disconnected_at"),
        "reconciliation_truncated": bool(state.get("reconciliation_truncated", False)),
        "reconciliation_gap": bool(state.get("reconciliation_gap", False)),
    }


def _parse_document_id(document_id: str) -> tuple[str, str, str] | None:
    parts = document_id.split(":", 3)
    if len(parts) != 4 or parts[0] != "slack" or not all(parts[1:]):
        return None
    try:
        _timestamp_order(parts[3])
    except ValueError:
        return None
    return parts[1], parts[2], parts[3]


def _published_source_observation(document_id: str, config: Mapping[str, str]) -> dict[str, Any] | None:
    rows = _reader_graph(config, "acme").ro_query(
        "MATCH (s:SourceRecord {document_id:$document_id}) RETURN properties(s) LIMIT 2",
        {"document_id": document_id}, timeout=3000,
    ).result_set
    if len(rows) != 1:
        return None
    return dict(rows[0][0])


def _inbox_thread_context(document_id: str, config: Mapping[str, str]) -> str | None:
    rows = _reader_graph(config, "acme").ro_query(
        "MATCH (i:SlackInbox {document_id:$document_id}) RETURN i.thread_ts ORDER BY i.created_at DESC LIMIT 1",
        {"document_id": document_id}, timeout=3000,
    ).result_set
    return str(rows[0][0]) if rows and isinstance(rows[0][0], str) else None


def _slack_error_code(error: Exception) -> str:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        return "rate_limited"
    return "unverifiable"


async def validate_slack_source(document_id: str, expected_hash: str, expected_version: str, *,
                                client: AsyncWebClient | None = None,
                                config: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Verify an exact published Slack message without treating it as current authority."""
    validated_at = datetime.now(timezone.utc).isoformat()
    parsed = _parse_document_id(document_id)
    result: dict[str, Any] = {"document_id": document_id, "valid": False, "observed_hash": None,
                              "observed_version": None, "validated_at": validated_at}
    active_config = _require_config(config or get_config(), socket_mode=False)
    if parsed is None:
        result["error"] = "invalid_document_id"
        return result
    team_id, channel_id, message_ts = parsed
    if team_id != active_config["SLACK_TEAM_ID"] or channel_id != active_config["SLACK_CHANNEL_ID"]:
        result["error"] = "wrong_destination"
        return result
    try:
        local_source = await asyncio.to_thread(_published_source_observation, document_id, active_config)
    except Exception:
        result["error"] = "unverifiable"
        return result
    if local_source is None or local_source.get("status") != "published" or local_source.get("origin") != "slack":
        result["error"] = "missing_or_nonpublished"
        return result
    if local_source.get("content_hash") != expected_hash or str(local_source.get("source_version", "")) != expected_version:
        result["error"] = "changed"
        return result
    api = client or AsyncWebClient(token=active_config["SLACK_BOT_TOKEN"])
    try:
        auth = await api.auth_test()
    except Exception as error:
        result["error"] = _slack_error_code(error)
        return result
    if auth.get("team_id") != team_id:
        result["error"] = "wrong_workspace"
        return result
    try:
        history = await api.conversations_history(channel=channel_id, oldest=message_ts, latest=message_ts,
                                                   inclusive=True, limit=1)
    except Exception as error:
        result["error"] = _slack_error_code(error)
        return result
    messages = history.get("messages")
    message = next((item for item in messages or [] if isinstance(item, Mapping) and item.get("ts") == message_ts), None)
    if message is None:
        try:
            thread_ts = await asyncio.to_thread(_inbox_thread_context, document_id, active_config)
        except Exception:
            result["error"] = "unverifiable"
            return result
        if not thread_ts or thread_ts == message_ts:
            result["error"] = "deleted_or_unavailable"
            return result
        try:
            replies = await api.conversations_replies(channel=channel_id, ts=thread_ts, oldest=message_ts,
                                                       latest=message_ts, inclusive=True, limit=1)
        except Exception as error:
            result["error"] = _slack_error_code(error)
            return result
        reply_messages = replies.get("messages")
        message = next((item for item in reply_messages or [] if isinstance(item, Mapping) and item.get("ts") == message_ts), None)
        if message is None:
            result["error"] = "deleted_or_unavailable"
            return result
    if not isinstance(message.get("text"), str) or is_bot_message(message, auth.get("user_id") if isinstance(auth.get("user_id"), str) else None):
        result["error"] = "unverifiable"
        return result
    edited = message.get("edited")
    observed_version = edited.get("ts") if isinstance(edited, Mapping) else message.get("ts")
    if not isinstance(observed_version, str):
        result["error"] = "unverifiable"
        return result
    observed_hash = hashlib.sha256(message["text"].encode("utf-8")).hexdigest()
    result.update({"observed_hash": observed_hash, "observed_version": observed_version})
    if observed_hash != expected_hash or observed_version != expected_version:
        result["error"] = "changed"
        return result
    try:
        current = await asyncio.to_thread(_published_source_observation, document_id, active_config)
    except Exception:
        current = None
    if current is None or current.get("status") != "published" or current.get("content_hash") != expected_hash \
            or str(current.get("source_version", "")) != expected_version \
            or current.get("revision") != local_source.get("revision"):
        result["error"] = "changed"
        return result
    result["validated_at"] = datetime.now(timezone.utc).isoformat()
    result["valid"] = True
    return result


async def reconcile_recent_messages(client: AsyncWebClient, *, config: Mapping[str, str] | None = None,
                                    bot_user_id: str | None = None) -> dict[str, bool]:
    """Perform the deliberately bounded reconnect replay; it never claims completeness."""
    active_config = _require_config(config or get_config(), socket_mode=False)
    cursor: str | None = None
    truncated = False
    gap = False
    for page in range(_RECONCILE_PAGES):
        try:
            response = await client.conversations_history(channel=active_config["SLACK_CHANNEL_ID"], limit=_RECONCILE_PAGE_SIZE,
                                                          cursor=cursor) if cursor else await client.conversations_history(
                                                              channel=active_config["SLACK_CHANNEL_ID"], limit=_RECONCILE_PAGE_SIZE)
        except Exception as error:
            gap = True
            if _slack_error_code(error) == "rate_limited":
                headers = getattr(getattr(error, "response", None), "headers", {}) or {}
                retry_after = headers.get("Retry-After")
                try:
                    await asyncio.sleep(max(float(retry_after), 0))
                except (TypeError, ValueError):
                    pass
            break
        for message in response.get("messages", []):
            if not isinstance(message, Mapping) or not isinstance(message.get("ts"), str):
                gap = True
                continue
            edited = message.get("edited")
            content_version = edited.get("ts") if isinstance(edited, Mapping) else message["ts"]
            if not isinstance(content_version, str):
                gap = True
                continue
            synthetic_id = f"reconcile:{active_config['SLACK_TEAM_ID']}:{active_config['SLACK_CHANNEL_ID']}:{message['ts']}:{content_version}"
            replay_event: dict[str, Any] = {"channel": active_config["SLACK_CHANNEL_ID"], **dict(message)}
            if isinstance(edited, Mapping):
                replay_event = {"channel": active_config["SLACK_CHANNEL_ID"], "subtype": "message_changed",
                                "event_ts": content_version, "message": dict(message)}
            await receive_slack_event({"team_id": active_config["SLACK_TEAM_ID"], "event_id": synthetic_id},
                                      replay_event, config=active_config, client=client, bot_user_id=bot_user_id, process=False)
        metadata = response.get("response_metadata", {})
        cursor = metadata.get("next_cursor") if isinstance(metadata, Mapping) else None
        if not cursor:
            break
        if page == _RECONCILE_PAGES - 1:
            truncated = True
    await asyncio.to_thread(_record_connector_state, active_config, reconciliation_truncated=truncated,
                            reconciliation_gap=gap)
    return {"truncated": truncated, "gap": gap}


async def run_slack_connector(*, config: Mapping[str, str] | None = None, stop_event: asyncio.Event | None = None) -> None:
    """Supervise the official transport and workers under exclusive writer ownership."""
    active_config = _require_config(config or get_config(), socket_mode=True)
    stop = stop_event or asyncio.Event()
    ready = asyncio.Event()
    owns_writer = False
    client = AsyncWebClient(token=active_config["SLACK_BOT_TOKEN"])
    auth = await client.auth_test()
    if auth.get("team_id") != active_config["SLACK_TEAM_ID"] or not isinstance(auth.get("user_id"), str):
        raise RuntimeError("Slack workspace identity mismatch")
    bot_user_id = auth["user_id"]
    # This worker exposes no Slack HTTP endpoint. Socket Mode is authenticated
    # by the official app-token transport, not an HTTP signing secret.
    app = AsyncApp(token=active_config["SLACK_BOT_TOKEN"], request_verification_enabled=False)

    @app.event("message")
    async def handle_message(body: dict[str, Any], event: dict[str, Any], logger: Any) -> None:
        del logger
        result = await receive_slack_event(body, event, config=active_config, client=client,
                                           bot_user_id=bot_user_id, process=False)
        if result != "ignored":
            await asyncio.to_thread(_record_connector_state, active_config, event_at=time.time())

    socket_handler = AsyncSocketModeHandler(app, active_config["SLACK_APP_TOKEN"])
    tasks: set[asyncio.Task[Any]] = set()

    async def initialize() -> None:
        nonlocal owns_writer
        rows = await asyncio.to_thread(lambda: _writer_graph(active_config).ro_query(
            "MATCH (m:Meta {id:'company_brain_meta'}) WHERE m.schema_ready=true "
            "MATCH (s:Service {services__service_id:$service}) RETURN count(s) LIMIT 1",
            {"service": active_config["SLACK_SERVICE_ID"]}, timeout=3000).result_set)
        if rows != [[1]]:
            raise SourceUnavailable("Seeded canonical service required before Slack startup")
        constraints = await asyncio.to_thread(lambda: _writer_graph(active_config).list_constraints())
        identities = {(item["label"], tuple(item["properties"])) for item in constraints
                      if item["type"] == "UNIQUE" and item["status"] == "OPERATIONAL"}
        required = {("SlackInbox", ("event_id",)), ("SourceRecord", ("document_id",)),
                    ("SourceRequest", ("request_id",)), ("SlackConnector", ("tenant_id", "connector_id"))}
        if not required.issubset(identities):
            raise SourceUnavailable("Seeded application identity constraints required before Slack startup")
        owns_writer = True
        await asyncio.to_thread(_reset_interrupted_inbox, active_config)
        await asyncio.to_thread(_record_connector_state, active_config, connected=False)
        ready.set()

    async def socket_loop() -> None:
        await ready.wait()
        await socket_handler.start_async()
        if not stop.is_set():
            raise RuntimeError("Slack transport stopped unexpectedly")

    async def connection_loop() -> None:
        await ready.wait()
        was_connected = False
        reconciliation: asyncio.Task[Any] | None = None
        while not stop.is_set():
            connected = await socket_handler.client.is_connected()
            await asyncio.to_thread(_record_connector_state, active_config, connected=connected)
            if connected and not was_connected and (reconciliation is None or reconciliation.done()):
                reconciliation = spawn(reconcile_recent_messages(client, config=active_config, bot_user_id=bot_user_id))
            was_connected = connected
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass

    async def inbox_loop() -> None:
        await ready.wait()
        while not stop.is_set():
            pending_ids = await asyncio.to_thread(_pending_inbox_ids, active_config)
            for event_id in pending_ids[:20]:
                if stop.is_set():
                    break
                await process_pending_slack_event(event_id, config=active_config, client=client)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except TimeoutError:
                pass

    async def notification_loop() -> None:
        await ready.wait()
        from integrations.slack_notifications import run_notification_worker
        await run_notification_worker(active_config, stop_event=stop)

    def spawn(coroutine: Any) -> asyncio.Task[Any]:
        task = group.create_task(coroutine)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    try:
        async with asyncio.TaskGroup() as group:
            spawn(run_source_worker(stop_event=stop, initialize=initialize))
            spawn(socket_loop())
            spawn(connection_loop())
            spawn(inbox_loop())
            spawn(notification_loop())
            await stop.wait()
            for task in tuple(tasks):
                task.cancel()
    finally:
        stop.set()
        await socket_handler.close_async()
        if owns_writer:
            await asyncio.to_thread(_record_connector_state, active_config, connected=False)

if __name__ == "__main__":
    asyncio.run(run_slack_connector())
