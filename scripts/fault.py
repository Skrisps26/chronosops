"""Operator-only fault injection for the verified ChronosOps demo workload.

The fault window is deliberately fail-closed. A failed or ambiguous request is
recorded as unknown rather than cleared, so a second stop cannot create an
overlapping outage.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_DIR = ROOT / "artifacts"
FAULT_WINDOW_FILE = ARTIFACTS_DIR / "fault-window.json"
FAULT_WINDOW_LOCK_FILE = ARTIFACTS_DIR / "fault-window.lock"
SECRETS_FILE = ROOT / ".secrets" / "db.env"
WATCHDOG_WINDOW = timedelta(minutes=10)
PENDING_COMMAND_STATUSES = frozenset({"Pending", "InProgress", "Delayed", "Cancelling"})
FAILED_COMMAND_STATUSES = frozenset({"Cancelled", "TimedOut", "Failed"})


class FaultSafetyError(ValueError):
    """The requested operator action cannot be proved safe."""


class CommandTerminalFailure(FaultSafetyError):
    """An accepted command reached a non-success terminal state."""


def get_config() -> dict[str, str]:
    config = {k: v for k, v in dotenv_values(SECRETS_FILE).items() if isinstance(v, str) and v}
    config.update({k: v for k, v in os.environ.items() if v})
    config.setdefault("AWS_REGION", "us-east-1")
    config.setdefault("DEMO_STACK_NAME", "chronosops-demo")
    return config


def _require_operator_profile(config: dict[str, str]) -> None:
    """Keep operator fault credentials distinct from app recovery credentials."""
    profile = config.get("AWS_PROFILE")
    if not profile:
        raise FaultSafetyError("AWS_PROFILE is required for operator fault commands")
    if profile == config.get("AWS_RECOVERY_PROFILE"):
        raise FaultSafetyError("AWS_PROFILE must not be the runtime recovery profile")


def get_ssm_client(config: dict[str, str]) -> Any:
    _require_operator_profile(config)
    session = boto3.Session(region_name=config["AWS_REGION"], profile_name=config["AWS_PROFILE"])
    return session.client("ssm")


def _load_window() -> dict[str, Any] | None:
    if not FAULT_WINDOW_FILE.is_file():
        return None
    try:
        payload = json.loads(FAULT_WINDOW_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FaultSafetyError("Existing fault window is unreadable; refusing overlapping action") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
        raise FaultSafetyError("Existing fault window is invalid; refusing overlapping action")
    return payload


def _write_window(payload: dict[str, Any]) -> None:
    """Atomically durably replace a window record before the next AWS call."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    temporary = FAULT_WINDOW_FILE.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        output.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, FAULT_WINDOW_FILE)
    directory = os.open(ARTIFACTS_DIR, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _claim_new_window(payload: dict[str, Any]) -> None:
    """Atomically reserve the singleton fault window before sending a stop."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with FAULT_WINDOW_LOCK_FILE.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            _require_no_open_window()
            _write_window(payload)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

def _claim_restore_window(binding: dict[str, str]) -> dict[str, Any]:
    """Atomically reserve an existing window for one safe restore submission."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with FAULT_WINDOW_LOCK_FILE.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            window = _load_window()
            if window is None:
                raise FaultSafetyError("No active or unknown fault window exists to restore")
            if window.get("state") == "unknown" and window.get("operation") == "start":
                raise FaultSafetyError("Previous restore outcome is unknown; do not submit another restore")
            if window.get("state") not in {"active", "unknown"}:
                raise FaultSafetyError("Fault window is already being changed; do not submit another restore")
            _window_matches_binding(window, binding)
            window.update(
                {
                    "state": "restoring",
                    "operation": "start",
                    "restore_submitted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            _write_window(window)
            return window
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _require_no_open_window() -> None:
    if _load_window() is not None:
        raise FaultSafetyError(
            "An active or unknown fault window already exists; restore or reconcile it before another stop"
        )


def _verify_binding(config: dict[str, str], confirmation: str) -> dict[str, str]:
    """Resolve a fresh deployment binding; never authorize from a local artifact."""
    try:
        from integrations.deployment import verify_deployment

        deployment = verify_deployment(config, publish=False)
    except Exception as error:
        raise FaultSafetyError("Trusted deployment verification failed") from error

    required = (
        "deployment_id",
        "service_id",
        "account_id",
        "region",
        "instance_id",
        "stack_id",
        "project",
        "environment",
        "operator",
        "expires_at",
        "document_name",
        "document_version",
        "document_hash",
        "stop_document_name",
    )
    if not isinstance(deployment, dict) or any(
        not isinstance(deployment.get(key), str) or not deployment[key] for key in required
    ):
        raise FaultSafetyError("Trusted deployment binding is incomplete")

    binding = {key: deployment[key] for key in required}
    if binding["service_id"] != "payments-primary":
        raise FaultSafetyError("Trusted deployment is not payments-primary")
    if binding["project"] != "ChronosOps" or binding["environment"] != "demo":
        raise FaultSafetyError("Trusted deployment does not have the ChronosOps demo tags")
    if binding["region"] != config["AWS_REGION"]:
        raise FaultSafetyError("Trusted deployment region differs from operator configuration")
    if confirmation != binding["instance_id"]:
        raise FaultSafetyError(
            f"Target confirmation mismatch! Supplied: {confirmation}, Actual: {binding['instance_id']}"
        )
    if not binding["document_version"].isdigit():
        raise FaultSafetyError("Trusted recovery document version is not numeric")
    if len(binding["document_hash"]) != 64 or any(
        char not in "0123456789abcdef" for char in binding["document_hash"].lower()
    ):
        raise FaultSafetyError("Trusted recovery document hash is invalid")
    return binding


def _window_matches_binding(window: dict[str, Any], binding: dict[str, str]) -> None:
    for key in ("deployment_id", "account_id", "region", "instance_id", "stack_id"):
        if window.get(key) != binding[key]:
            raise FaultSafetyError("Existing fault window belongs to a different trusted deployment")


def _is_invocation_not_visible(error: Exception) -> bool:
    return isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") == "InvocationDoesNotExist"


def _wait_for_terminal_command(
    ssm: Any,
    *,
    command_id: str,
    instance_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    """Wait only for the submitted command; polling does not retry submission."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except Exception as error:
            if not _is_invocation_not_visible(error) or time.monotonic() >= deadline:
                raise FaultSafetyError("Unable to determine submitted command outcome") from error
            time.sleep(poll_interval_seconds)
            continue

        status = invocation.get("Status")
        if status == "Success":
            return invocation
        if status in FAILED_COMMAND_STATUSES:
            raise CommandTerminalFailure(f"Submitted command ended with {status}")
        if status not in PENDING_COMMAND_STATUSES:
            raise FaultSafetyError("Submitted command returned an unrecognized status")
        if time.monotonic() >= deadline:
            raise FaultSafetyError("Timed out waiting for submitted command completion")
        time.sleep(poll_interval_seconds)


def _command_started_at(invocation: dict[str, Any]) -> datetime:
    actual_start = invocation.get("ExecutionStartDateTime")
    if isinstance(actual_start, datetime):
        return actual_start.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _operator_comment(operation: str, binding: dict[str, str]) -> str:
    return f"ChronosOps operator {operation}; deployment={binding['deployment_id']}"


def _command_timeout(args: argparse.Namespace) -> float:
    timeout = float(getattr(args, "timeout_seconds", 90))
    if timeout <= 0:
        raise FaultSafetyError("timeout-seconds must be positive")
    return timeout


def _mark_unknown(window: dict[str, Any], *, operation: str, error: Exception) -> None:
    window.update(
        {
            "state": "unknown",
            "operation": operation,
            "outcome_error": type(error).__name__,
            "outcome_checked_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_window(window)


def run_stop(args: argparse.Namespace) -> int:
    """Schedule the watchdog-backed stop only after fresh binding verification."""
    try:
        config = get_config()
        _require_operator_profile(config)
        _require_no_open_window()
        binding = _verify_binding(config, args.confirm_instance)
        try:
            ssm = get_ssm_client(config)
        except Exception as error:
            raise FaultSafetyError("Unable to initialize the operator SSM client") from error
        submitted_at = datetime.now(timezone.utc)
        window: dict[str, Any] = {
            **binding,
            "state": "submitting",
            "operation": "stop",
            "submitted_at": submitted_at.isoformat(),
        }
        _claim_new_window(window)
        try:
            response = ssm.send_command(
                InstanceIds=[binding["instance_id"]],
                DocumentName=binding["stop_document_name"],
                Comment=_operator_comment("watchdog-backed stop", binding),
            )
            command_id = response["Command"]["CommandId"]
            if not isinstance(command_id, str) or not command_id:
                raise FaultSafetyError("SSM did not return a command ID")
        except Exception as error:
            _mark_unknown(window, operation="stop", error=error)
            raise FaultSafetyError("Stop submission is unknown; do not retry") from error

        # This durable ID is written before the first read/poll.
        window.update({"state": "submitted", "command_id": command_id})
        _write_window(window)
        try:
            invocation = _wait_for_terminal_command(
                ssm,
                command_id=command_id,
                instance_id=binding["instance_id"],
                timeout_seconds=_command_timeout(args),
                poll_interval_seconds=2,
            )
        except Exception as error:
            _mark_unknown(window, operation="stop", error=error)
            raise FaultSafetyError("Stop outcome is unknown; do not create another fault window") from error

        actual_start = _command_started_at(invocation)
        deadline = actual_start + WATCHDOG_WINDOW
        window.update(
            {
                "state": "active",
                "started_at": actual_start.isoformat(),
                "watchdog_deadline": deadline.isoformat(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _write_window(window)
    except FaultSafetyError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Fault-window storage failed; do not retry the operator action.", file=sys.stderr)
        return 1

    print(f"Fault injected successfully. Command ID: {command_id}")
    print(f"Safety watchdog deadline: {deadline.isoformat()} (auto-recovery in 10 minutes)")
    return 0


def run_start(args: argparse.Namespace) -> int:
    """Restore through the pinned recovery document, retaining protection until success."""
    try:
        config = get_config()
        _require_operator_profile(config)
        window = _load_window()
        if window is None:
            raise FaultSafetyError("No active or unknown fault window exists to restore")
        if window.get("state") == "unknown" and window.get("operation") == "start":
            raise FaultSafetyError("Previous restore outcome is unknown; do not submit another restore")
        if window.get("state") not in {"active", "unknown"}:
            raise FaultSafetyError("Fault window is already being changed; do not submit another restore")
        binding = _verify_binding(config, args.confirm_instance)
        _window_matches_binding(window, binding)
        try:
            ssm = get_ssm_client(config)
        except Exception as error:
            raise FaultSafetyError("Unable to initialize the operator SSM client") from error
        window = _claim_restore_window(binding)
        try:
            response = ssm.send_command(
                InstanceIds=[binding["instance_id"]],
                DocumentName=binding["document_name"],
                DocumentVersion=binding["document_version"],
                Comment=_operator_comment("safe restore", binding),
            )
            command_id = response["Command"]["CommandId"]
            if not isinstance(command_id, str) or not command_id:
                raise FaultSafetyError("SSM did not return a command ID")
        except Exception as error:
            _mark_unknown(window, operation="start", error=error)
            raise FaultSafetyError("Restore submission is unknown; do not retry") from error

        # Do not clear the window merely because SSM accepted the request.
        window["restore_command_id"] = command_id
        _write_window(window)
        try:
            _wait_for_terminal_command(
                ssm,
                command_id=command_id,
                instance_id=binding["instance_id"],
                timeout_seconds=_command_timeout(args),
                poll_interval_seconds=2,
            )
        except Exception as error:
            _mark_unknown(window, operation="start", error=error)
            raise FaultSafetyError("Restore outcome is unknown; watchdog protection remains tracked") from error
    except FaultSafetyError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Fault-window storage failed; do not retry the operator action.", file=sys.stderr)
        return 1

    try:
        FAULT_WINDOW_FILE.unlink()
    except OSError:
        print("Restore completed but the fault window could not be cleared; reconcile it before another fault.", file=sys.stderr)
        return 1
    print(f"Service restart completed: {command_id}. Fault window cleared after successful restore.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_stop = subparsers.add_parser("stop", help="Stop nginx with the watchdog protection")
    p_stop.add_argument("--confirm-instance", required=True, help="Instance ID confirmation")
    p_stop.add_argument("--timeout-seconds", type=float, default=90, help="Terminal command wait timeout")

    p_start = subparsers.add_parser("start", help="Safely restore an existing fault window")
    p_start.add_argument("--confirm-instance", required=True, help="Instance ID confirmation")
    p_start.add_argument("--timeout-seconds", type=float, default=90, help="Terminal command wait timeout")

    args = parser.parse_args()
    if args.command == "stop":
        return run_stop(args)
    if args.command == "start":
        return run_start(args)
    raise AssertionError(f"Unsupported operator command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
