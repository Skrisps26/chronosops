"""Operator-only fault injection script for controlled ChronosOps demonstration."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_DIR = ROOT / "artifacts"
FAULT_WINDOW_FILE = ARTIFACTS_DIR / "fault-window.json"
STACK_ARTIFACT = ARTIFACTS_DIR / "demo-stack.json"
SECRETS_FILE = ROOT / ".secrets" / "db.env"


def get_config() -> dict[str, str]:
    config = {k: v for k, v in dotenv_values(SECRETS_FILE).items() if isinstance(v, str) and v}
    config.update({k: v for k, v in os.environ.items() if v})
    config.setdefault("AWS_REGION", "us-east-1")
    config.setdefault("DEMO_STACK_NAME", "chronosops-demo")
    return config


def get_ssm_client(config: dict[str, str]) -> Any:
    kwargs: dict[str, Any] = {"region_name": config["AWS_REGION"]}
    if config.get("AWS_PROFILE"):
        kwargs["profile_name"] = config["AWS_PROFILE"]
    session = boto3.Session(**kwargs)
    return session.client("ssm")


def get_stack_outputs() -> dict[str, str]:
    if not STACK_ARTIFACT.is_file():
        raise FileNotFoundError(f"Stack artifact not found at {STACK_ARTIFACT}. Deploy demo stack first.")
    payload = json.loads(STACK_ARTIFACT.read_text(encoding="utf-8"))
    return payload.get("outputs", {})


def run_stop(args: argparse.Namespace) -> int:
    config = get_config()
    outputs = get_stack_outputs()
    instance_id = outputs.get("InstanceId")
    stop_doc = outputs.get("StopDocumentName")

    if not instance_id or not stop_doc:
        print("Missing InstanceId or StopDocumentName in stack outputs.", file=sys.stderr)
        return 1

    if args.confirm_instance != instance_id:
        print(f"Target confirmation mismatch! Supplied: {args.confirm_instance}, Actual: {instance_id}", file=sys.stderr)
        return 1

    ssm = get_ssm_client(config)
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=10)

    print(f"Injecting fault on {instance_id} using {stop_doc}...")
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=stop_doc,
        Comment=f"ChronosOps controlled fault at {now.isoformat()}",
    )
    cmd_id = resp["Command"]["CommandId"]

    # Save fault window
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    fault_payload = {
        "instance_id": instance_id,
        "command_id": cmd_id,
        "started_at": now.isoformat(),
        "watchdog_deadline": deadline.isoformat(),
        "active": True,
    }
    FAULT_WINDOW_FILE.write_text(json.dumps(fault_payload, indent=2) + "\n", encoding="utf-8")

    print(f"Fault injected successfully. Command ID: {cmd_id}")
    print(f"Safety watchdog deadline: {deadline.isoformat()} (auto-recovery in 10 minutes)")
    return 0


def run_start(args: argparse.Namespace) -> int:
    config = get_config()
    outputs = get_stack_outputs()
    instance_id = outputs.get("InstanceId")
    start_doc = outputs.get("StartDocumentName")

    if not instance_id or not start_doc:
        print("Missing InstanceId or StartDocumentName in stack outputs.", file=sys.stderr)
        return 1

    if args.confirm_instance != instance_id:
        print(f"Target confirmation mismatch! Supplied: {args.confirm_instance}, Actual: {instance_id}", file=sys.stderr)
        return 1

    ssm = get_ssm_client(config)
    print(f"Restoring service on {instance_id} using {start_doc}...")
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=start_doc,
        Comment="ChronosOps operator manual service restart",
    )
    cmd_id = resp["Command"]["CommandId"]

    if FAULT_WINDOW_FILE.is_file():
        FAULT_WINDOW_FILE.unlink()

    print(f"Service restart command sent: {cmd_id}. Watchdog cancelled.")
    return 0


def run_cancel_watchdog(args: argparse.Namespace) -> int:
    config = get_config()
    outputs = get_stack_outputs()
    instance_id = outputs.get("InstanceId")
    start_doc = outputs.get("StartDocumentName")

    if args.confirm_instance != instance_id:
        print(f"Target confirmation mismatch! Supplied: {args.confirm_instance}, Actual: {instance_id}", file=sys.stderr)
        return 1

    ssm = get_ssm_client(config)
    print(f"Cancelling watchdog on {instance_id}...")
    ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=start_doc,
        Comment="ChronosOps watchdog cancellation",
    )
    if FAULT_WINDOW_FILE.is_file():
        FAULT_WINDOW_FILE.unlink()
    print("Watchdog cancelled.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_stop = subparsers.add_parser("stop", help="Stop nginx with 10-minute watchdog")
    p_stop.add_argument("--confirm-instance", required=True, help="Instance ID confirmation")

    p_start = subparsers.add_parser("start", help="Operator manual restore")
    p_start.add_argument("--confirm-instance", required=True, help="Instance ID confirmation")

    p_cancel = subparsers.add_parser("cancel-watchdog", help="Cancel watchdog timer")
    p_cancel.add_argument("--confirm-instance", required=True, help="Instance ID confirmation")

    args = parser.parse_args()
    if args.command == "stop":
        return run_stop(args)
    elif args.command == "start":
        return run_start(args)
    elif args.command == "cancel-watchdog":
        return run_cancel_watchdog(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
