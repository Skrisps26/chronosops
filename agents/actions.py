"""Execution and approval handlers for real SSM recovery and simulated failover."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from falkordb import FalkorDB

from integrations.aws_actions import AwsRecoveryClient
from integrations.monitoring import MonitoringClient
from seed.load import get_config


class ActionConflictError(Exception):
    """Raised when an approval is expired, stale, or conflicting."""
    pass


class ActionNotFoundError(Exception):
    """Raised when an incident or approval record is missing or belongs to another tenant."""
    pass


async def approve_action(
    incident_id: str,
    approval_id: str,
    tenant_id: str = "acme",
    skip_health_polling: bool = False,
) -> dict[str, Any]:
    """Execute human-approved recovery or simulation action."""
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    graph = db.select_graph("ops_topology")

    # 1. Fetch approval record
    cypher = """
    MATCH (a:ApprovalRecord {approval_id: $approval_id})
    RETURN a.approval_id AS approval_id,
           a.incident_id AS incident_id,
           a.tenant_id AS tenant_id,
           a.execution_mode AS mode,
           a.action_kind AS action_kind,
           a.proposed_action AS proposed_json,
           a.status AS status,
           a.expires_at AS expires_at
    """
    rows = graph.query(cypher, {"approval_id": approval_id}).result_set
    if not rows:
        raise ActionNotFoundError(f"Approval record '{approval_id}' not found")

    rec = rows[0]
    rec_incident_id = rec[1]
    rec_tenant_id = rec[2]
    mode = rec[3]
    action_kind = rec[4]
    proposed = json.loads(rec[5]) if rec[5] else {}
    current_status = rec[6]
    expires_at_str = rec[7]

    # Cross-tenant check: tenant cannot approve another tenant's incident
    if rec_tenant_id != tenant_id:
        raise ActionNotFoundError(f"Approval record '{approval_id}' does not belong to tenant '{tenant_id}'")

    if rec_incident_id != incident_id:
        raise ActionNotFoundError(f"Approval record does not match incident '{incident_id}'")

    # Idempotency: if already completed, return existing state
    if current_status in ("applied", "recovered"):
        return {
            "status": current_status,
            "incident_id": incident_id,
            "approval_id": approval_id,
            "execution_mode": mode,
            "already_completed": True,
        }

    if current_status not in ("pending", "awaiting_approval"):
        raise ActionConflictError(f"Approval record is already in status '{current_status}'")

    # Check expiration
    now = datetime.now(timezone.utc)
    if expires_at_str:
        expires_at = datetime.fromisoformat(expires_at_str)
        if now > expires_at:
            graph.query("MATCH (a:ApprovalRecord {approval_id: $id}) SET a.status = 'expired'", {"id": approval_id})
            raise ActionConflictError("Approval proposal has expired (5-minute window exceeded). Re-triage required.")

    now_ts = now.isoformat()

    # 2. Simulation execution branch
    if mode == "simulation":
        route = proposed.get("route", [])
        graph.query(
            "MATCH (a:ApprovalRecord {approval_id: $approval_id}) "
            "SET a.status = 'applied', a.applied_at = $ts "
            "WITH a "
            "MERGE (ts:TrafficState {tenant_id: $tenant, incident_id: $incident_id}) "
            "SET ts.active_route = $route, "
            "    ts.applied_at = $ts, "
            "    ts.action = 'simulate_failover'",
            {
                "approval_id": approval_id,
                "tenant": tenant_id,
                "incident_id": incident_id,
                "route": route,
                "ts": now_ts,
            },
        )
        return {
            "status": "applied",
            "incident_id": incident_id,
            "approval_id": approval_id,
            "execution_mode": "simulation",
            "active_route": route,
            "applied_at": now_ts,
        }

    # 3. Real execution branch
    instance_id = proposed.get("instance_id")
    doc_name = proposed.get("document_name")

    if not instance_id or not doc_name:
        raise ActionConflictError("Missing target instance_id or document_name in proposal")

    # Atomically mark executing in graph before network call
    graph.query(
        "MATCH (a:ApprovalRecord {approval_id: $approval_id}) SET a.status = 'executing', a.executing_at = $ts",
        {"approval_id": approval_id, "ts": now_ts},
    )

    client = AwsRecoveryClient()
    cmd_outcome = await client.execute_restart(
        instance_id=instance_id,
        document_name=doc_name,
        comment=f"ChronosOps approved recovery for {incident_id}",
    )
    cmd_id = cmd_outcome["command_id"]

    graph.query(
        "MATCH (a:ApprovalRecord {approval_id: $approval_id}) "
        "SET a.status = 'verifying', a.command_id = $cmd_id",
        {"approval_id": approval_id, "cmd_id": cmd_id},
    )

    if not skip_health_polling:
        monitor = MonitoringClient()
        target_url = f"http://{instance_id}:80"
        # Verify post-recovery health
        recovered = monitor.verify_recovery_samples(target_url, required_samples=3)
        if not recovered:
            graph.query("MATCH (a:ApprovalRecord {approval_id: $id}) SET a.status = 'recovery_failed'", {"id": approval_id})
            raise RuntimeError(f"Service did not return to healthy state within verification deadline after command {cmd_id}")

    recovered_ts = datetime.now(timezone.utc).isoformat()
    graph.query(
        "MATCH (a:ApprovalRecord {approval_id: $approval_id}) "
        "SET a.status = 'recovered', a.recovered_at = $ts",
        {"approval_id": approval_id, "ts": recovered_ts},
    )

    return {
        "status": "recovered",
        "incident_id": incident_id,
        "approval_id": approval_id,
        "execution_mode": "real",
        "command_id": cmd_id,
        "recovered_at": recovered_ts,
    }


async def reject_action(
    incident_id: str,
    approval_id: str,
    tenant_id: str = "acme",
) -> dict[str, Any]:
    """Record human rejection of a proposed action."""
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    graph = db.select_graph("ops_topology")
    now_ts = datetime.now(timezone.utc).isoformat()

    graph.query(
        "MATCH (a:ApprovalRecord {approval_id: $approval_id, tenant_id: $tenant}) "
        "SET a.status = 'rejected', a.rejected_at = $ts",
        {"approval_id": approval_id, "tenant": tenant_id, "ts": now_ts},
    )
    return {
        "status": "rejected",
        "incident_id": incident_id,
        "approval_id": approval_id,
        "rejected_at": now_ts,
    }
