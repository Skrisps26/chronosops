from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from scripts import reconcile_action

BINDING = {
    "instance_id": "i-0123456789abcdef0",
    "document_name": "chronosops-start-nginx",
    "document_version": "7",
}
COMMENT = "ChronosOps recovery incident-0001 approval-0001"
COMMAND_ID = "12345678-1234-1234-1234-123456789012"


class _Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.kwargs: dict[str, Any] | None = None

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.kwargs = kwargs
        return self.pages


class _Ssm:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.paginator = _Paginator(pages)

    def get_paginator(self, operation: str) -> _Paginator:
        assert operation == "list_commands"
        return self.paginator


class _Session:
    def __init__(self, ssm: _Ssm) -> None:
        self.ssm = ssm

    def client(self, service: str, **_: Any) -> _Ssm:
        assert service == "ssm"
        return self.ssm


def _command(requested_at: datetime, **changes: object) -> dict[str, object]:
    command: dict[str, object] = {
        "CommandId": COMMAND_ID,
        "InstanceIds": [BINDING["instance_id"]],
        "DocumentName": BINDING["document_name"],
        "DocumentVersion": BINDING["document_version"],
        "Comment": COMMENT,
        "RequestedDateTime": requested_at,
    }
    command.update(changes)
    return command


def test_reconcile_lists_only_exact_bound_command_and_delegates(
    monkeypatch: object,
) -> None:
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    ssm = _Ssm(
        [{"Commands": [_command(started + timedelta(seconds=1)), _command(started + timedelta(seconds=2), Comment="other")]}]
    )
    delegated: list[str] = []

    async def action(_: str, __: str) -> dict[str, object]:
        return {
            "deployment_binding": BINDING,
            "command_comment": COMMENT,
            "executing_at": started.isoformat(),
            "command_id": None,
        }

    async def reconcile(_: str, __: str, command_id: str) -> dict[str, object]:
        delegated.append(command_id)
        return {"status": "verifying", "command_id": command_id}

    monkeypatch.setattr(reconcile_action, "get_reconcilable_action", action)
    monkeypatch.setattr(reconcile_action, "reconcile_known_command", reconcile)
    monkeypatch.setattr(reconcile_action, "get_config", lambda: {"AWS_REGION": "us-east-1"})
    monkeypatch.setattr(reconcile_action, "operator_session", lambda _: _Session(ssm))

    exit_code, result = asyncio.run(reconcile_action.reconcile("incident-0001", "approval-0001"))

    assert exit_code == 0
    assert result["command_id"] == COMMAND_ID
    assert delegated == [COMMAND_ID]


def test_reconcile_leaves_zero_multiple_or_truncated_lookup_unknown(
    monkeypatch: object,
) -> None:
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    outcomes = [
        [{"Commands": []}],
        [{"Commands": [_command((started + timedelta(seconds=1)).replace(tzinfo=None))]}],
        [{"Commands": [_command(started + timedelta(seconds=1)), _command(started + timedelta(seconds=2), CommandId="22345678-1234-1234-1234-123456789012")]}],
        [
            {"Commands": [_command(started + timedelta(seconds=1))], "NextToken": "second"},
            {"Commands": [], "NextToken": "third"},
            {"Commands": [], "NextToken": "fourth"},
        ],
    ]
    delegated: list[str] = []

    async def action(_: str, __: str) -> dict[str, object]:
        return {
            "deployment_binding": BINDING,
            "command_comment": COMMENT,
            "executing_at": started.isoformat(),
        }

    async def reconcile(_: str, __: str, command_id: str) -> dict[str, object]:
        delegated.append(command_id)
        return {"command_id": command_id}

    monkeypatch.setattr(reconcile_action, "get_reconcilable_action", action)
    monkeypatch.setattr(reconcile_action, "reconcile_known_command", reconcile)
    monkeypatch.setattr(reconcile_action, "get_config", lambda: {"AWS_REGION": "us-east-1"})

    for pages in outcomes:
        monkeypatch.setattr(reconcile_action, "operator_session", lambda _, pages=pages: _Session(_Ssm(pages)))
        exit_code, result = asyncio.run(reconcile_action.reconcile("incident-0001", "approval-0001"))
        assert exit_code == 2
        assert result["status"] == "execution_unknown"

    assert delegated == []
