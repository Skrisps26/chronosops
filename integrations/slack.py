"""Slack Socket Mode event processor and incident notification dispatcher."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from falkordb import FalkorDB
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from agents.knowledge import update_company_document
from seed.load import get_config

logger = logging.getLogger(__name__)


def normalize_slack_document_id(team_id: str, channel_id: str, message_ts: str) -> str:
    """Derive canonical, collision-free Slack document ID preserving exact timestamp string."""
    return f"slack:{team_id}:{channel_id}:{message_ts}"


def is_bot_message(event: dict[str, Any], bot_user_id: str | None = None) -> bool:
    """Prevent bot feedback loops by identifying bot, app, and integration messages."""
    if bot_user_id and event.get("user") == bot_user_id:
        return True
    if event.get("bot_id"):
        return True
    subtype = event.get("subtype")
    if subtype in ("bot_message", "message_deleted", "channel_join", "channel_leave"):
        # Note: message_deleted is handled specifically by delete processor
        return subtype != "message_deleted"
    return False


def is_allowed_channel(event: dict[str, Any], allowed_channel_id: str) -> bool:
    """Enforce strict channel allowlist so the bot only ingests from the dedicated demo channel."""
    # Direct channel field or nested event.item.channel
    ch = event.get("channel") or event.get("item", {}).get("channel")
    return bool(ch and ch == allowed_channel_id)


def is_event_processed(graph: Any, event_id: str) -> bool:
    """Check if event_id was already processed (durable idempotency check)."""
    try:
        res = graph.ro_query(
            "MATCH (i:SlackInbox {event_id: $event_id}) RETURN i.event_id",
            {"event_id": event_id},
        ).result_set
        return bool(res)
    except Exception:
        return False


def record_processed_event(graph: Any, event_id: str, doc_id: str, event_type: str) -> None:
    """Atomically record event_id in SlackInbox table."""
    graph.query(
        "MERGE (i:SlackInbox {event_id: $event_id}) "
        "SET i.doc_id = $doc_id, i.type = $type, i.processed_at = timestamp()",
        {"event_id": event_id, "doc_id": doc_id, "type": event_type},
    )


async def post_incident_root(
    client: AsyncWebClient,
    channel: str,
    service_id: str,
    summary: str,
    approval_url: str,
) -> dict[str, str]:
    """Post root incident notice to the dedicated Slack channel, returning channel and thread_ts."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":warning: *INCIDENT DETECTED* for `{service_id}`\n{summary}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Review graph evidence and approve bounded recovery:\n<{approval_url}|Open Operator Console>",
            },
        },
    ]
    resp = await client.chat_postMessage(
        channel=channel,
        text=f"Incident detected for {service_id}: {summary}",
        blocks=blocks,
    )
    return {
        "channel": str(resp["channel"]),
        "thread_ts": str(resp["ts"]),
        "permalink": "",
    }


async def post_incident_outcome(
    client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    status: str,
    details: str,
) -> dict[str, Any]:
    """Post thread reply with confirmed recovery outcome and health verification."""
    emoji = ":white_check_mark:" if status == "recovered" else ":x:"
    text = f"{emoji} *RECOVERY {status.upper()}*\n{details}"
    resp = await client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=text,
    )
    return {"ts": resp["ts"], "status": "posted"}
