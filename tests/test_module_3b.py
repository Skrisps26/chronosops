"""Focused Slack identity and envelope normalization boundaries."""
from __future__ import annotations

from integrations.slack import _normalize_event, _timestamp_order, is_allowed_channel, is_bot_message, normalize_slack_document_id


CONFIG = {
    "SLACK_TEAM_ID": "T-ACME",
    "SLACK_CHANNEL_ID": "C-OPERATIONS",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_SERVICE_ID": "payments-primary",
}


def test_slack_document_id_preserves_exact_timestamp() -> None:
    ts = "1729000000.123456"
    assert normalize_slack_document_id("T-ACME", "C-OPERATIONS", ts) == "slack:T-ACME:C-OPERATIONS:1729000000.123456"
    assert _timestamp_order(ts) == 1_729_000_000_123_456


def test_slack_timestamp_never_uses_float_or_rounds() -> None:
    assert _timestamp_order("9.000001") == 9_000_001
    try:
        _timestamp_order("9.0000001")
    except ValueError:
        pass
    else:  # pragma: no cover - the assertion describes the contract boundary.
        raise AssertionError("sub-microsecond Slack timestamps must abstain")


def test_bot_and_channel_boundaries_reject_nested_automation() -> None:
    assert is_allowed_channel({"channel": "C-OPERATIONS"}, "C-OPERATIONS")
    assert not is_allowed_channel({"channel": "C-OTHER"}, "C-OPERATIONS")
    assert is_bot_message({"user": "U-BOT"}, "U-BOT")
    assert is_bot_message({"bot_id": "B-1"})
    changed = {
        "channel": "C-OPERATIONS", "subtype": "message_changed", "event_ts": "1729000001.000001",
        "message": {"ts": "1729000000.000001", "user": "U-HUMAN", "bot_id": "B-1", "text": "automated"},
    }
    assert _normalize_event({"team_id": "T-ACME", "event_id": "Ev-bot"}, changed, CONFIG, "U-BOT") is None


def test_edit_and_delete_use_external_revision_not_original_timestamp() -> None:
    edit = _normalize_event(
        {"team_id": "T-ACME", "event_id": "Ev-edit"},
        {"channel": "C-OPERATIONS", "subtype": "message_changed", "event_ts": "1729000002.000001",
         "message": {"ts": "1729000000.000001", "thread_ts": "1729000000.000001", "user": "U-ALICE",
                     "text": "revised", "edited": {"ts": "1729000002.000001"}}},
        CONFIG, "U-BOT",
    )
    delete = _normalize_event(
        {"team_id": "T-ACME", "event_id": "Ev-delete"},
        {"channel": "C-OPERATIONS", "subtype": "message_deleted", "deleted_ts": "1729000000.000001",
         "event_ts": "1729000003.000001", "previous_message": {"ts": "1729000000.000001", "user": "U-ALICE"}},
        CONFIG, "U-BOT",
    )
    assert edit is not None and delete is not None
    assert edit.source_version == "1729000002.000001"
    assert delete.source_version == "1729000003.000001"
    assert delete.source_order > edit.source_order
    assert delete.delete is True


def test_metadata_only_change_keeps_nested_message_content_version() -> None:
    event = _normalize_event(
        {"team_id": "T-ACME", "event_id": "Ev-metadata"},
        {"channel": "C-OPERATIONS", "subtype": "message_changed", "event_ts": "1729000004.000001",
         "message": {"ts": "1729000000.000001", "thread_ts": "1729000000.000001",
                     "user": "U-ALICE", "text": "same content"}},
        CONFIG, "U-BOT",
    )
    assert event is not None
    assert event.event_timestamp == "1729000004.000001"
    assert event.source_version == "1729000000.000001"
    assert event.source_order == 1_729_000_000_000_001
