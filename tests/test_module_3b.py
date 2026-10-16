"""Module 3B verification suite: Slack Socket Mode normalization, loop prevention, and notifications."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
import pytest
import yaml

from integrations.slack import (
    is_allowed_channel,
    is_bot_message,
    is_event_processed,
    normalize_slack_document_id,
    post_incident_outcome,
    post_incident_root,
    record_processed_event,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_FILE = ROOT / "slack" / "manifest.yaml"


def test_slack_manifest_scopes_and_socket_mode():
    """Verify that slack/manifest.yaml requests exact least-privilege scopes and Socket Mode."""
    assert MANIFEST_FILE.is_file()
    manifest = yaml.safe_load(MANIFEST_FILE.read_text(encoding="utf-8"))

    assert manifest["settings"]["socket_mode_enabled"] is True
    assert "message.channels" in manifest["settings"]["event_subscriptions"]["bot_events"]

    scopes = manifest["oauth_config"]["scopes"]["bot"]
    assert "channels:history" in scopes
    assert "chat:write" in scopes
    assert "chat:write.public" not in scopes
    assert "channels:join" not in scopes


def test_slack_document_id_canonical_derivation():
    """Verify that document IDs preserve exact string timestamp without float precision loss."""
    ts = "1729000000.123456"
    doc_id = normalize_slack_document_id("T-CORP", "C-DEMO", ts)
    assert doc_id == "slack:T-CORP:C-DEMO:1729000000.123456"
    assert "123456" in doc_id


def test_bot_loop_prevention():
    """Verify that bot messages, integrations, and self-authored messages are ignored."""
    bot_id = "U-BOT-123"

    # Self-authored message
    assert is_bot_message({"user": "U-BOT-123", "text": "hello"}, bot_user_id=bot_id) is True

    # Bot message with bot_id
    assert is_bot_message({"user": "U-HUMAN", "bot_id": "B-APP-456", "text": "alert"}) is True

    # Bot subtype
    assert is_bot_message({"user": "U-OTHER", "subtype": "bot_message", "text": "automated"}) is True

    # Normal human message
    assert is_bot_message({"user": "U-ALICE", "text": "Decision confirmed"}, bot_user_id=bot_id) is False


def test_channel_allowlist_enforcement():
    """Verify that only the configured demo channel events are accepted."""
    demo_channel = "C-CHRONOS-DEMO"

    assert is_allowed_channel({"channel": "C-CHRONOS-DEMO"}, demo_channel) is True
    assert is_allowed_channel({"channel": "C-RANDOM"}, demo_channel) is False
    assert is_allowed_channel({"item": {"channel": "C-CHRONOS-DEMO"}}, demo_channel) is True
    assert is_allowed_channel({}, demo_channel) is False


def test_slack_inbox_idempotency():
    """Verify that Slack event_id is recorded and prevents duplicate processing."""
    mock_graph = MagicMock()
    mock_graph.ro_query.return_value.result_set = []

    # First check: not processed
    assert is_event_processed(mock_graph, "evt-001") is False

    # Record event
    record_processed_event(mock_graph, "evt-001", "slack:T:C:123", "message")
    mock_graph.query.assert_called_once()
    assert "SlackInbox" in mock_graph.query.call_args[0][0]

    # Second check: simulated as already in graph
    mock_graph.ro_query.return_value.result_set = [["evt-001"]]
    assert is_event_processed(mock_graph, "evt-001") is True


@pytest.mark.asyncio
async def test_post_incident_notifications():
    """Verify that incident root post and outcome replies dispatch to the correct channel and thread."""
    mock_client = AsyncMock()
    mock_client.chat_postMessage.return_value = {
        "channel": "C-DEMO",
        "ts": "1729000000.999999",
    }

    # 1. Post root notice
    res_root = await post_incident_root(
        client=mock_client,
        channel="C-DEMO",
        service_id="payments-primary",
        summary="Service probe failed for 15s",
        approval_url="http://localhost:8080/incidents/inc-1",
    )
    assert res_root["channel"] == "C-DEMO"
    assert res_root["thread_ts"] == "1729000000.999999"

    # 2. Post reply in thread
    res_reply = await post_incident_outcome(
        client=mock_client,
        channel="C-DEMO",
        thread_ts="1729000000.999999",
        status="recovered",
        details="3 consecutive successful health probes verified",
    )
    assert res_reply["status"] == "posted"

    # Verify thread_ts was passed to chat_postMessage
    call_args = mock_client.chat_postMessage.call_args_list[1]
    assert call_args.kwargs["thread_ts"] == "1729000000.999999"
    assert "RECOVERED" in call_args.kwargs["text"]
