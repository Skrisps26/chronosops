"""Bounded AWS SSM recovery action client with single-attempt mutation and polling."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import boto3
from botocore.config import Config
from integrations.deployment import bound_deployment
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
        self.ssm_read = session.client("ssm")
        self.sts = session.client("sts")

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

        return {
            "command_id": cmd_id,
            "instance_id": instance_id,
            "document_name": document_name,
            "status": final_status,
            "status_details": status_details,
            "duration_ms": duration_ms,
        }
