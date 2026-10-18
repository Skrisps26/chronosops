from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock

import boto3
import pytest
from botocore.stub import Stubber

from integrations import aws_actions
from integrations.aws_actions import (
    AwsRecoveryClient,
    RecoveryExecutionError,
    RecoverySubmissionUnknown,
)

COMMAND_ID = "12345678-1234-1234-1234-123456789012"
BINDING = {
    "instance_id": "i-0123456789abcdef0",
    "document_name": "chronosops-start-nginx",
    "document_version": "7",
    "document_hash": "a" * 64,
}
COMMENT = "ChronosOps recovery incident-0001 approval-0001"


def _ssm_client() -> object:
    return boto3.session.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1"
    ).client("ssm")


def _runtime(*, mutation: object | None = None, reader: object | None = None) -> AwsRecoveryClient:
    runtime = object.__new__(AwsRecoveryClient)
    runtime.ssm_mutation = mutation or _ssm_client()
    runtime.ssm_read = reader or _ssm_client()
    return runtime


def _invocation(**changes: object) -> dict[str, object]:
    response: dict[str, object] = {
        "CommandId": COMMAND_ID,
        "InstanceId": BINDING["instance_id"],
        "DocumentName": BINDING["document_name"],
        "DocumentVersion": BINDING["document_version"],
        "PluginName": "startNginx",
        "ResponseCode": 0,
        "Status": "Success",
        "StatusDetails": "Success",
        "ExecutionEndDateTime": "2026-10-18T12:00:00Z",
    }
    response.update(changes)
    return response


def test_submit_restart_is_single_pinned_parameterless_sdk_request() -> None:
    ssm = _ssm_client()
    expected = {
        "InstanceIds": [BINDING["instance_id"]],
        "DocumentName": BINDING["document_name"],
        "DocumentVersion": BINDING["document_version"],
        "DocumentHash": BINDING["document_hash"],
        "DocumentHashType": "Sha256",
        "Comment": COMMENT,
    }
    with Stubber(ssm) as stubber:
        stubber.add_response("send_command", {"Command": {"CommandId": COMMAND_ID}}, expected)
        command_id = asyncio.run(_runtime(mutation=ssm).submit_restart(BINDING, COMMENT))
    assert command_id == COMMAND_ID


def test_submit_restart_keeps_ambiguous_response_unknown_without_resend() -> None:
    mutation = Mock()
    mutation.send_command.return_value = {"Command": {}}

    with pytest.raises(RecoverySubmissionUnknown):
        asyncio.run(_runtime(mutation=mutation).submit_restart(BINDING, COMMENT))

    assert mutation.send_command.call_count == 1


def test_submit_restart_rejects_non_numeric_version_before_calling_sdk() -> None:
    mutation = Mock()
    binding = {**BINDING, "document_version": "$DEFAULT"}

    with pytest.raises(RecoveryExecutionError):
        asyncio.run(_runtime(mutation=mutation).submit_restart(binding, COMMENT))

    mutation.send_command.assert_not_called()


def test_poll_restart_propagates_then_returns_actual_success_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssm = _ssm_client()
    expected = {
        "CommandId": COMMAND_ID,
        "InstanceId": BINDING["instance_id"],
        "PluginName": "startNginx",
    }

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(aws_actions.asyncio, "sleep", no_sleep)
    with Stubber(ssm) as stubber:
        stubber.add_client_error(
            "get_command_invocation", service_error_code="InvocationDoesNotExist", expected_params=expected
        )
        stubber.add_response("get_command_invocation", _invocation(), expected)
        outcome = asyncio.run(_runtime(reader=ssm).poll_restart(COMMAND_ID, BINDING))

    assert outcome.status == "success"
    assert outcome.completed_at == datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc).timestamp()
    assert outcome.error_code is None


def test_poll_restart_never_reports_success_without_completion_time() -> None:
    ssm = _ssm_client()
    expected = {
        "CommandId": COMMAND_ID,
        "InstanceId": BINDING["instance_id"],
        "PluginName": "startNginx",
    }
    response = _invocation()
    response.pop("ExecutionEndDateTime")
    with Stubber(ssm) as stubber:
        stubber.add_response("get_command_invocation", response, expected)
        outcome = asyncio.run(_runtime(reader=ssm).poll_restart(COMMAND_ID, BINDING))

    assert outcome.status == "execution_unknown"
    assert outcome.completed_at is None


def test_poll_restart_returns_known_terminal_failure_without_inventing_completion() -> None:
    ssm = _ssm_client()
    expected = {
        "CommandId": COMMAND_ID,
        "InstanceId": BINDING["instance_id"],
        "PluginName": "startNginx",
    }
    response = _invocation(
        Status="TimedOut",
        StatusDetails="Execution Timed Out",
        ResponseCode=-1,
    )
    response.pop("ExecutionEndDateTime")
    with Stubber(ssm) as stubber:
        stubber.add_response("get_command_invocation", response, expected)
        outcome = asyncio.run(_runtime(reader=ssm).poll_restart(COMMAND_ID, BINDING))

    assert outcome.status == "failed"
    assert outcome.ssm_status == "Execution Timed Out"
    assert outcome.error_code == "-1"
    assert outcome.completed_at is None


def test_poll_restart_marks_mismatched_binding_unknown() -> None:
    ssm = _ssm_client()
    expected = {
        "CommandId": COMMAND_ID,
        "InstanceId": BINDING["instance_id"],
        "PluginName": "startNginx",
    }
    with Stubber(ssm) as stubber:
        stubber.add_response(
            "get_command_invocation", _invocation(DocumentVersion="8"), expected
        )
        outcome = asyncio.run(_runtime(reader=ssm).poll_restart(COMMAND_ID, BINDING))

    assert outcome.status == "execution_unknown"
    assert outcome.completed_at is None


def test_poll_restart_marks_read_failure_unknown() -> None:
    ssm = _ssm_client()
    expected = {
        "CommandId": COMMAND_ID,
        "InstanceId": BINDING["instance_id"],
        "PluginName": "startNginx",
    }
    with Stubber(ssm) as stubber:
        stubber.add_client_error(
            "get_command_invocation",
            service_error_code="InternalServerError",
            expected_params=expected,
        )
        outcome = asyncio.run(_runtime(reader=ssm).poll_restart(COMMAND_ID, BINDING))

    assert outcome.status == "execution_unknown"
    assert outcome.completed_at is None
