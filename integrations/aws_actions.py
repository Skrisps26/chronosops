"""Bounded AWS SSM recovery action client with single-attempt mutation and polling."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Mapping

import boto3
from botocore.config import Config
from integrations.deployment import START_COMMANDS, bound_deployment
from scripts.demo_stack import get_config


class RecoveryExecutionError(RuntimeError):
    pass


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

    async def execute_restart(
        self,
        instance_id: str,
        document_name: str,
        comment: str = "ChronosOps approved recovery restart",
        poll_timeout_seconds: float = 90.0,
    ) -> dict[str, Any]:
        """Execute parameterless restart document, poll for terminal status, return outcome."""
        start_time = time.perf_counter()
        binding = await asyncio.to_thread(bound_deployment, self.config)
        if (instance_id, document_name, self.region) != (
            binding["instance_id"], binding["document_name"], binding["region"]
        ):
            raise RecoveryExecutionError("Recovery target does not match the verified deployment")
        identity = await asyncio.to_thread(self.sts.get_caller_identity)
        role_name = binding["recovery_role_arn"].rsplit("/", 1)[-1]
        expected = f"arn:aws:sts::{binding['account_id']}:assumed-role/{role_name}/"
        if identity["Account"] != binding["account_id"] or not identity["Arn"].startswith(expected):
            raise RecoveryExecutionError("Recovery credentials are not the bound restricted role")

        # Execute SendCommand in worker thread
        def _send() -> str:
            resp = self.ssm_mutation.send_command(
                InstanceIds=[instance_id],
                DocumentName=document_name,
                DocumentVersion=binding["document_version"],
                DocumentHash=binding["document_hash"],
                DocumentHashType="Sha256",
                Comment=comment,
            )
            return resp["Command"]["CommandId"]

        try:
            cmd_id = await asyncio.to_thread(_send)
        except Exception as exc:
            raise RecoveryExecutionError(f"Failed to submit SSM SendCommand: {exc}") from exc

        # Poll GetCommandInvocation
        deadline = time.perf_counter() + poll_timeout_seconds
        poll_interval = 2.0
        final_status = "Pending"
        status_details = ""

        while time.perf_counter() < deadline:
            await asyncio.sleep(poll_interval)

            def _get_status() -> dict[str, Any] | None:
                try:
                    return self.ssm_read.get_command_invocation(
                        CommandId=cmd_id,
                        InstanceId=instance_id,
                        PluginName="startNginx",
                    )
                except self.ssm_read.exceptions.InvocationDoesNotExist:
                    return None

            inv = await asyncio.to_thread(_get_status)
            if inv is None:
                continue

            final_status = inv.get("Status", "Pending")
            status_details = inv.get("StatusDetails", "")

            if final_status in ("Success", "Failed", "Cancelled", "TimedOut"):
                break

        duration_ms = (time.perf_counter() - start_time) * 1000.0

        if final_status != "Success":
            raise RecoveryExecutionError(
                f"SSM command {cmd_id} ended with non-success status: {final_status} ({status_details})"
            )
        completed = inv.get("ExecutionEndDateTime") if inv else None
        if not completed:
            raise RecoveryExecutionError("Successful SSM status has no command completion timestamp")
        completed_time = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        if completed_time.tzinfo is None:
            raise RecoveryExecutionError("SSM command completion timestamp has no timezone")

        return {
            "command_id": cmd_id,
            "instance_id": instance_id,
            "document_name": document_name,
            "status": final_status,
            "status_details": status_details,
            "completed_at": completed_time.timestamp(),
            "duration_ms": duration_ms,
        }
