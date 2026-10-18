"""Bounded AWS SSM recovery action client with single-attempt mutation and polling."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

import boto3
from botocore.config import Config

from integrations.deployment import START_COMMANDS
from scripts.demo_stack import get_config


class RecoveryExecutionError(RuntimeError):
    """The requested recovery action is deterministically unsafe or invalid."""


class RecoverySubmissionUnknown(RuntimeError):
    """SendCommand may have succeeded but did not yield a durable command ID."""


@dataclass(frozen=True)
class CommandOutcome:
    command_id: str
    status: Literal["success", "failed", "execution_unknown"]
    ssm_status: str
    completed_at: float | None
    error_code: str | None


_POLL_TIMEOUT_SECONDS = 90.0
_POLL_INTERVAL_SECONDS = 2.0
_NONTERMINAL_STATUSES = frozenset(
    {"pending", "in progress", "inprogress", "delayed", "cancelling"}
)
_FAILED_TERMINAL_STATUSES = frozenset(
    {
        "failed",
        "cancelled",
        "canceled",
        "timed out",
        "timedout",
        "undeliverable",
        "delivery timed out",
        "execution timed out",
        "terminated",
    }
)


def _restart_values(binding: Mapping[str, Any]) -> tuple[str, str, str, str]:
    instance_id = binding.get("instance_id")
    document_name = binding.get("document_name")
    document_version = binding.get("document_version")
    document_hash = binding.get("document_hash")
    if not all(isinstance(value, str) and value for value in (
        instance_id, document_name, document_version, document_hash
    )):
        raise RecoveryExecutionError("Published recovery binding is incomplete")
    if not document_version.isdigit():
        raise RecoveryExecutionError("Published recovery document version must be numeric")
    return instance_id, document_name, document_version, document_hash


def _status_kind(value: object) -> Literal["success", "failed", "pending"] | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).casefold()
    if normalized == "success":
        return "success"
    if normalized in _FAILED_TERMINAL_STATUSES:
        return "failed"
    if normalized in _NONTERMINAL_STATUSES:
        return "pending"
    return None


def _completion_epoch(value: object) -> float | None:
    if isinstance(value, datetime):
        completed = value
    elif isinstance(value, str):
        try:
            completed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if completed.tzinfo is None:
        return None
    return completed.timestamp()


def _error_code(invocation: Mapping[str, Any]) -> str | None:
    value = invocation.get("ResponseCode")
    if value is None or value == 0 or value == "0":
        return None
    return str(value)


class AwsRecoveryClient:
    def __init__(self, region: str | None = None, profile: str | None = None) -> None:
        self.config = get_config()
        self.region = region or self.config["AWS_REGION"]
        self.profile = self.config.get("AWS_RECOVERY_PROFILE")
        if not self.profile or (profile is not None and profile != self.profile):
            raise RecoveryExecutionError("A separate configured AWS_RECOVERY_PROFILE is required")
        if self.profile == self.config.get("AWS_PROFILE"):
            raise RecoveryExecutionError("Recovery must not use the operator/generation profile")

        kwargs: dict[str, Any] = {"region_name": self.region}
        if self.profile:
            kwargs["profile_name"] = self.profile

        session = boto3.Session(**kwargs)
        # CRITICAL SAFETY: Single attempt mutation to avoid duplicate executions
        self.mutation_config = Config(retries={"total_max_attempts": 1, "mode": "standard"})
        self.ssm_mutation = session.client("ssm", config=self.mutation_config)
        self.read_config = Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"total_max_attempts": 2, "mode": "standard"},
        )
        self.ssm_read = session.client("ssm", config=self.read_config)
        self.ec2_read = session.client("ec2", config=self.read_config)
        self.sts = session.client("sts", config=self.read_config)

    async def refresh_binding(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        """Re-read the exact pinned recovery target using only recovery credentials."""
        required = (
            "deployment_id",
            "tenant_id",
            "service_id",
            "account_id",
            "region",
            "instance_id",
            "stack_id",
            "document_name",
            "document_version",
            "document_hash",
            "recovery_role_arn",
            "operator",
            "expires_at",
        )
        if any(not isinstance(binding.get(field), str) or not binding[field] for field in required):
            raise RecoveryExecutionError("Published deployment binding is incomplete")
        if binding["tenant_id"] != "acme" or binding["region"] != self.region:
            raise RecoveryExecutionError("Published deployment binding has the wrong tenant or region")
        try:
            expiry = datetime.fromisoformat(binding["expires_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise RecoveryExecutionError("Published deployment expiration is invalid") from error
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise RecoveryExecutionError("Published deployment has expired")

        def _refresh() -> dict[str, Any]:
            ec2 = self.ec2_read
            ssm = self.ssm_read
            identity = self.sts.get_caller_identity()
            role_name = binding["recovery_role_arn"].rsplit("/", 1)[-1]
            expected_role = f"arn:aws:sts::{binding['account_id']}:assumed-role/{role_name}/"
            if identity.get("Account") != binding["account_id"] or not str(identity.get("Arn", "")).startswith(expected_role):
                raise RecoveryExecutionError("Recovery credentials are not the bound restricted role")

            reservations = ec2.describe_instances(InstanceIds=[binding["instance_id"]]).get("Reservations", [])
            instances = [instance for reservation in reservations for instance in reservation.get("Instances", [])]
            if len(instances) != 1:
                raise RecoveryExecutionError("Expected exactly one bound EC2 instance")
            instance = instances[0]
            tags = {tag.get("Key"): tag.get("Value") for tag in instance.get("Tags", [])}
            expected_tags = {
                "Project": "ChronosOps",
                "Environment": "demo",
                "Service": binding["service_id"],
                "aws:cloudformation:stack-id": binding["stack_id"],
                "Operator": binding["operator"],
                "ExpiresAt": binding["expires_at"],
            }
            if instance.get("InstanceId") != binding["instance_id"] or instance.get("State", {}).get("Name") != "running" or any(
                tags.get(key) != value for key, value in expected_tags.items()
            ):
                raise RecoveryExecutionError("Bound EC2 instance is not the running controlled target")
            managed = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [binding["instance_id"]]}]
            ).get("InstanceInformationList", [])
            if len(managed) != 1 or managed[0].get("InstanceId") != binding["instance_id"] or managed[0].get("PingStatus") != "Online":
                raise RecoveryExecutionError("Bound SSM agent is not online")
            document = ssm.get_document(
                Name=binding["document_name"],
                DocumentVersion=binding["document_version"],
                DocumentFormat="JSON",
            )
            returned_version = str(document.get("DocumentVersion", ""))
            content = document.get("Content")
            if returned_version != binding["document_version"] or not returned_version.isdigit() or not isinstance(content, str):
                raise RecoveryExecutionError("Pinned SSM document version is invalid")
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != binding["document_hash"]:
                raise RecoveryExecutionError("Pinned SSM document content changed")
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as error:
                raise RecoveryExecutionError("Pinned SSM document JSON is invalid") from error
            steps = parsed.get("mainSteps")
            if (
                parsed.get("schemaVersion") != "2.2"
                or parsed.get("parameters")
                or not isinstance(steps, list)
                or len(steps) != 1
                or steps[0].get("name") != "startNginx"
                or steps[0].get("action") != "aws:runShellScript"
                or steps[0].get("inputs", {}).get("runCommand") != START_COMMANDS
            ):
                raise RecoveryExecutionError("Pinned SSM document is not the approved parameterless restart")
            return {
                "account_id": identity["Account"],
                "region": self.region,
                "instance_id": binding["instance_id"],
                "document_name": binding["document_name"],
                "document_version": returned_version,
                "document_hash": binding["document_hash"],
                "recovery_role_arn": binding["recovery_role_arn"],
            }

        return await asyncio.to_thread(_refresh)

    async def submit_restart(self, binding: Mapping[str, Any], comment: str) -> str:
        """Submit one pinned, parameterless SSM command and return its ID immediately."""
        instance_id, document_name, document_version, document_hash = _restart_values(binding)
        if not isinstance(comment, str) or not comment or len(comment) > 100:
            raise RecoveryExecutionError("Recovery command comment must be between 1 and 100 characters")

        def _send() -> object:
            return self.ssm_mutation.send_command(
                InstanceIds=[instance_id],
                DocumentName=document_name,
                DocumentVersion=document_version,
                DocumentHash=document_hash,
                DocumentHashType="Sha256",
                Comment=comment,
            )

        try:
            response = await asyncio.to_thread(_send)
        except Exception as error:
            raise RecoverySubmissionUnknown("SSM command submission outcome is unknown") from error
        if not isinstance(response, Mapping):
            raise RecoverySubmissionUnknown("SSM command submission did not return a command ID")
        command = response.get("Command")
        command_id = command.get("CommandId") if isinstance(command, Mapping) else None
        if not isinstance(command_id, str) or not command_id:
            raise RecoverySubmissionUnknown("SSM command submission did not return a command ID")
        return command_id

    async def poll_restart(self, command_id: str, binding: Mapping[str, Any]) -> CommandOutcome:
        """Poll only the pinned SSM invocation, without resubmitting on any uncertainty."""
        instance_id, document_name, document_version, _ = _restart_values(binding)
        if not isinstance(command_id, str) or not command_id:
            raise RecoveryExecutionError("Recovery command ID is required")

        deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
        last_status = "Unknown"
        missing_exception = getattr(
            getattr(self.ssm_read, "exceptions", None), "InvocationDoesNotExist", None
        )

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return CommandOutcome(command_id, "execution_unknown", last_status, None, None)
            try:
                invocation = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.ssm_read.get_command_invocation,
                        CommandId=command_id,
                        InstanceId=instance_id,
                        PluginName="startNginx",
                    ),
                    timeout=remaining,
                )
            except Exception as error:
                if missing_exception is None or not isinstance(error, missing_exception):
                    return CommandOutcome(
                        command_id=command_id,
                        status="execution_unknown",
                        ssm_status=last_status,
                        completed_at=None,
                        error_code=None,
                    )
                invocation = None

            if isinstance(invocation, Mapping):
                status = invocation.get("Status")
                status_details = invocation.get("StatusDetails")
                last_status = (
                    status_details if isinstance(status_details, str) and status_details
                    else status if isinstance(status, str) and status
                    else last_status
                )
                returned_command_id = invocation.get("CommandId")
                if returned_command_id != command_id:
                    return CommandOutcome(command_id, "execution_unknown", last_status, None, None)
                if (
                    invocation.get("InstanceId") != instance_id
                    or invocation.get("DocumentName") != document_name
                    or str(invocation.get("DocumentVersion", "")) != document_version
                ):
                    return CommandOutcome(command_id, "execution_unknown", last_status, None, None)

                status_kind = _status_kind(status)
                details_kind = _status_kind(status_details)
                if (
                    status_kind is not None
                    and details_kind is not None
                    and status_kind != details_kind
                ):
                    return CommandOutcome(command_id, "execution_unknown", last_status, None, None)
                terminal_kind = details_kind or status_kind
                if terminal_kind == "success":
                    completed_at = _completion_epoch(invocation.get("ExecutionEndDateTime"))
                    if completed_at is None:
                        return CommandOutcome(command_id, "execution_unknown", last_status, None, None)
                    return CommandOutcome(command_id, "success", last_status, completed_at, None)
                if terminal_kind == "failed":
                    return CommandOutcome(
                        command_id,
                        "failed",
                        last_status,
                        _completion_epoch(invocation.get("ExecutionEndDateTime")),
                        _error_code(invocation),
                    )
                if terminal_kind is None:
                    return CommandOutcome(command_id, "execution_unknown", last_status, None, None)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return CommandOutcome(command_id, "execution_unknown", last_status, None, None)
            await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
