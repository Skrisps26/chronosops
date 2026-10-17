"""Safety transitions for the operator-only fault command.

All SSM interactions are isolated fakes: these tests never create a boto3
session or make an AWS request.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts import fault


BINDING = {
    "deployment_id": "deployment-123",
    "service_id": "payments-primary",
    "account_id": "123456789012",
    "region": "us-east-1",
    "instance_id": "i-0123456789abcdef0",
    "stack_id": "arn:aws:cloudformation:us-east-1:123456789012:stack/chronosops-demo/example",
    "project": "ChronosOps",
    "environment": "demo",
    "operator": "operator@example.test",
    "expires_at": "2030-01-01T00:00:00+00:00",
    "document_name": "chronosops-demo-StartNginx",
    "document_version": "7",
    "document_hash": "a" * 64,
    "stop_document_name": "chronosops-demo-StopNginx",
}


class StubSsm:
    """A narrowly scoped SSM API fake with observable SendCommand calls."""

    def __init__(self, send: Any, invocations: list[Any], on_poll: Any = None) -> None:
        self.send = send
        self.invocations = iter(invocations)
        self.on_poll = on_poll
        self.send_command = Mock(side_effect=self._send)
        self.get_command_invocation = Mock(side_effect=self._poll)

    def _send(self, **kwargs: Any) -> Any:
        if isinstance(self.send, Exception):
            raise self.send
        return self.send

    def _poll(self, **kwargs: Any) -> Any:
        if self.on_poll is not None:
            self.on_poll(kwargs)
        outcome = next(self.invocations)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def operator_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(fault, "ARTIFACTS_DIR", tmp_path)
    monkeypatch.setattr(fault, "FAULT_WINDOW_FILE", tmp_path / "fault-window.json")
    monkeypatch.setattr(fault, "FAULT_WINDOW_LOCK_FILE", tmp_path / "fault-window.lock")
    monkeypatch.setattr(
        fault,
        "get_config",
        lambda: {"AWS_REGION": "us-east-1", "AWS_PROFILE": "operator", "AWS_RECOVERY_PROFILE": "recovery"},
    )
    monkeypatch.setattr(fault, "_verify_binding", lambda config, confirmation: dict(BINDING))
    monkeypatch.setattr(fault.time, "sleep", lambda _: None)


def _args() -> argparse.Namespace:
    return argparse.Namespace(confirm_instance=BINDING["instance_id"], timeout_seconds=5)


def _window() -> dict[str, Any]:
    return json.loads(fault.FAULT_WINDOW_FILE.read_text(encoding="utf-8"))


def test_stop_waits_for_terminal_success_and_persists_bound_window(
    operator_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_start = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

    def assert_id_was_persisted(kwargs: dict[str, Any]) -> None:
        persisted = _window()
        assert persisted["state"] == "submitted"
        assert persisted["command_id"] == "cmd-stop"
        assert kwargs == {"CommandId": "cmd-stop", "InstanceId": BINDING["instance_id"]}

    ssm = StubSsm(
        {"Command": {"CommandId": "cmd-stop"}},
        [{"Status": "InProgress"}, {"Status": "Success", "ExecutionStartDateTime": actual_start}],
        assert_id_was_persisted,
    )
    monkeypatch.setattr(fault, "get_ssm_client", lambda config: ssm)

    assert fault.run_stop(_args()) == 0

    assert ssm.send_command.call_count == 1
    assert ssm.send_command.call_args.kwargs == {
        "InstanceIds": [BINDING["instance_id"]],
        "DocumentName": BINDING["stop_document_name"],
        "Comment": "ChronosOps operator watchdog-backed stop; deployment=deployment-123",
    }
    assert ssm.get_command_invocation.call_count == 2
    assert _window()["state"] == "active"
    assert _window()["started_at"] == actual_start.isoformat()
    assert _window()["watchdog_deadline"] == "2030-01-01T12:10:00+00:00"


@pytest.mark.parametrize("state", ["active", "unknown"])
def test_stop_refuses_existing_active_or_unknown_window_without_contacting_aws(
    state: str, operator_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fault._write_window({**BINDING, "state": state, "operation": "stop"})
    verify = Mock(side_effect=AssertionError("must not verify a second fault"))
    client = Mock(side_effect=AssertionError("must not create an SSM client"))
    monkeypatch.setattr(fault, "_verify_binding", verify)
    monkeypatch.setattr(fault, "get_ssm_client", client)

    assert fault.run_stop(_args()) == 1
    verify.assert_not_called()
    client.assert_not_called()


def test_stop_submission_error_stays_unknown_and_is_not_retried(
    operator_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssm = StubSsm(RuntimeError("socket closed"), [])
    monkeypatch.setattr(fault, "get_ssm_client", lambda config: ssm)

    assert fault.run_stop(_args()) == 1

    assert ssm.send_command.call_count == 1
    assert ssm.get_command_invocation.call_count == 0
    assert _window()["state"] == "unknown"
    assert _window()["operation"] == "stop"
    assert "command_id" not in _window()


def test_stop_poll_failure_preserves_command_id_as_unknown(
    operator_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssm = StubSsm({"Command": {"CommandId": "cmd-stop"}}, [RuntimeError("read failed")])
    monkeypatch.setattr(fault, "get_ssm_client", lambda config: ssm)

    assert fault.run_stop(_args()) == 1

    assert ssm.send_command.call_count == 1
    assert _window()["state"] == "unknown"
    assert _window()["operation"] == "stop"
    assert _window()["command_id"] == "cmd-stop"


def test_start_only_clears_window_after_pinned_command_succeeds(
    operator_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fault._write_window({**BINDING, "state": "active", "operation": "stop", "command_id": "cmd-stop"})
    ssm = StubSsm({"Command": {"CommandId": "cmd-start"}}, [{"Status": "Success"}])
    monkeypatch.setattr(fault, "get_ssm_client", lambda config: ssm)

    assert fault.run_start(_args()) == 0

    assert ssm.send_command.call_args.kwargs == {
        "InstanceIds": [BINDING["instance_id"]],
        "DocumentName": BINDING["document_name"],
        "DocumentVersion": "7",
        "Comment": "ChronosOps operator safe restore; deployment=deployment-123",
    }
    assert not fault.FAULT_WINDOW_FILE.exists()


def test_start_failure_keeps_an_unknown_window_and_cannot_be_retried(
    operator_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fault._write_window({**BINDING, "state": "active", "operation": "stop", "command_id": "cmd-stop"})
    ssm = StubSsm({"Command": {"CommandId": "cmd-start"}}, [{"Status": "Failed"}])
    monkeypatch.setattr(fault, "get_ssm_client", lambda config: ssm)

    assert fault.run_start(_args()) == 1

    assert fault.FAULT_WINDOW_FILE.exists()
    assert _window()["state"] == "unknown"
    assert _window()["operation"] == "start"
    assert _window()["restore_command_id"] == "cmd-start"
    assert fault.run_start(_args()) == 1
    assert ssm.send_command.call_count == 1


def test_restore_rejects_a_stale_window_for_a_different_deployment(
    operator_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = {**BINDING, "instance_id": "i-00000000000000000", "state": "active", "operation": "stop"}
    fault._write_window(stale)
    client = Mock(side_effect=AssertionError("must not submit against a stale window"))
    monkeypatch.setattr(fault, "get_ssm_client", client)

    assert fault.run_start(_args()) == 1

    client.assert_not_called()


def test_fresh_binding_rejects_malformed_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    import integrations.deployment as deployment

    expected_config = {"AWS_REGION": "us-east-1", "AWS_PROFILE": "operator"}
    live = Mock(return_value={**BINDING, "document_hash": "not-a-sha256"})
    monkeypatch.setattr(deployment, "verify_deployment", live)

    with pytest.raises(fault.FaultSafetyError, match="hash"):
        fault._verify_binding(expected_config, BINDING["instance_id"])

    live.assert_called_once_with(expected_config, publish=False)


def test_fresh_binding_requires_exact_operator_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    import integrations.deployment as deployment

    live = Mock(return_value=dict(BINDING))
    monkeypatch.setattr(deployment, "verify_deployment", live)

    with pytest.raises(fault.FaultSafetyError, match="confirmation mismatch"):
        fault._verify_binding(
            {"AWS_REGION": "us-east-1", "AWS_PROFILE": "operator"},
            "i-00000000000000000",
        )

    live.assert_called_once()


def test_operator_profile_must_not_be_the_recovery_profile() -> None:
    with pytest.raises(fault.FaultSafetyError, match="must not"):
        fault._require_operator_profile({"AWS_PROFILE": "shared", "AWS_RECOVERY_PROFILE": "shared"})


def test_parser_has_no_watchdog_cancellation_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fault.sys,
        "argv",
        ["fault.py", "cancel-watchdog", "--confirm-instance", BINDING["instance_id"]],
    )

    with pytest.raises(SystemExit) as error:
        fault.main()

    assert error.value.code == 2
