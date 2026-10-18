"""Operator-only reconciliation for an ambiguous SSM recovery submission."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

from botocore.config import Config

from agents.actions import get_reconcilable_action, reconcile_known_command
from integrations.deployment import operator_session
from scripts.demo_stack import get_config

_MAX_PAGES = 3
_MAX_RESULTS_PER_PAGE = 20
_MAX_RESULTS = _MAX_PAGES * _MAX_RESULTS_PER_PAGE


def _as_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _aws_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _command_matches(
    command: Mapping[str, Any],
    binding: Mapping[str, Any],
    comment: str,
    invoked_after: datetime,
    invoked_before: datetime,
) -> bool:
    requested_at = _as_utc(command.get("RequestedDateTime"))
    if requested_at is None or not invoked_after <= requested_at <= invoked_before:
        return False
    return (
        command.get("InstanceIds") == [binding["instance_id"]]
        and command.get("DocumentName") == binding["document_name"]
        and str(command.get("DocumentVersion", "")) == binding["document_version"]
        and command.get("Comment") == comment
    )


def _find_exact_command(
    ssm: Any,
    binding: Mapping[str, Any],
    comment: str,
    executing_at: datetime,
) -> tuple[str | None, bool]:
    """Return an unambiguous command ID, with truncation represented separately."""
    now = datetime.now(timezone.utc)
    paginator = ssm.get_paginator("list_commands")
    pages = paginator.paginate(
        InstanceId=binding["instance_id"],
        Filters=[
            {"key": "InvokedAfter", "value": _aws_timestamp(executing_at)},
            {"key": "InvokedBefore", "value": _aws_timestamp(now)},
        ],
        PaginationConfig={"PageSize": _MAX_RESULTS_PER_PAGE},
    )
    command_ids: set[str] = set()
    scanned = 0
    for page_number, page in enumerate(pages, start=1):
        if page_number > _MAX_PAGES:
            return None, True
        commands = page.get("Commands", [])
        if not isinstance(commands, list):
            return None, True
        scanned += len(commands)
        if scanned > _MAX_RESULTS:
            return None, True
        for command in commands:
            if not isinstance(command, Mapping):
                return None, True
            if _command_matches(command, binding, comment, executing_at, now):
                command_id = command.get("CommandId")
                if not isinstance(command_id, str) or not command_id:
                    return None, True
                command_ids.add(command_id)
                if len(command_ids) > 1:
                    return None, False
        if page_number == _MAX_PAGES:
            if page.get("NextToken"):
                return None, True
            break
    if len(command_ids) != 1:
        return None, False
    return next(iter(command_ids)), False


async def reconcile(incident_id: str, approval_id: str) -> tuple[int, dict[str, Any]]:
    action = await get_reconcilable_action(incident_id, approval_id)
    known_command_id = action.get("command_id")
    if isinstance(known_command_id, str) and known_command_id:
        result = await reconcile_known_command(incident_id, approval_id, known_command_id)
        return 0, result

    binding = action.get("deployment_binding")
    comment = action.get("command_comment")
    executing_at = _as_utc(action.get("executing_at"))
    required_binding = ("instance_id", "document_name", "document_version")
    if (
        not isinstance(binding, Mapping)
        or any(not isinstance(binding.get(key), str) or not binding[key] for key in required_binding)
        or not binding["document_version"].isdigit()
        or not isinstance(comment, str)
        or not comment
        or executing_at is None
    ):
        return 2, {"status": "execution_unknown", "reason": "reconciliation evidence is incomplete"}

    config = get_config()
    session = operator_session(config)
    ssm = session.client(
        "ssm",
        config=Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 2, "mode": "standard"}),
    )
    try:
        command_id, truncated = await asyncio.to_thread(
            _find_exact_command, ssm, binding, comment, executing_at
        )
    except Exception:
        return 2, {"status": "execution_unknown", "reason": "operator command lookup unavailable"}
    if truncated:
        return 2, {"status": "execution_unknown", "reason": "operator command lookup was truncated"}
    if command_id is None:
        return 2, {"status": "execution_unknown", "reason": "operator command lookup was ambiguous"}

    result = await reconcile_known_command(incident_id, approval_id, command_id)
    return 0, result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--approval-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        exit_code, result = asyncio.run(reconcile(args.incident_id, args.approval_id))
    except Exception:
        print(json.dumps({"status": "execution_unknown", "reason": "reconciliation unavailable"}))
        return 2
    print(json.dumps(result, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
