"""Module 4 verification suite: LangGraph workflow, approval gate, and action execution."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
import pytest
from falkordb import FalkorDB

from agents.actions import (
    ActionConflictError,
    ActionNotFoundError,
    approve_action,
    reject_action,
)
from agents.governance import governance_node
from agents.remediation import remediation_node
from agents.triage import triage_node
from agents.workflow import IncidentState, create_incident_workflow
from agents.settings import graph_names

from seed.load import get_config


@pytest.fixture
def workflow_app():
    return create_incident_workflow(triage_node, remediation_node, governance_node)


@pytest.mark.asyncio
async def test_simulation_workflow_end_to_end(workflow_app):
    """Verify simulation workflow: blast radius -> failover SPpaths route selection -> approval -> TrafficState."""
    inc_id = f"inc-sim-{uuid.uuid4().hex[:8]}"
    initial_state = IncidentState(
        incident_id=inc_id,
        tenant_id="acme",
        service_id="payments-primary",
        execution_mode="simulation",
        max_cost=12,
    )

    final_state_dict = await workflow_app.ainvoke(initial_state)
    state = IncidentState(**final_state_dict)

    # 1. Triage verified 4 affected services
    assert state.evidence["blast_radius"]["affected_count"] == 4
    assert state.evidence["blast_radius"]["affected_services"] == ["checkout", "gateway", "orders", "worker"]

    # 2. Remediation rejected cost 20 route and selected cost 8 route
    assert state.proposed_action["action_kind"] == "simulate_failover"
    assert state.proposed_action["route"] == ["checkout", "queue", "payments-backup"]
    assert state.proposed_action["cost"] == 8

    # 3. Governance created approval record
    assert state.status == "awaiting_approval"
    assert state.approval_id is not None
    assert state.approval_id.startswith("appr-")

    # 4. Human approval execution
    appr_res = await approve_action(
        incident_id=inc_id,
        approval_id=state.approval_id,
        tenant_id="acme",
    )
    assert appr_res["status"] == "applied"
    assert appr_res["active_route"] == ["checkout", "queue", "payments-backup"]

    # 5. Verify TrafficState exists in FalkorDB
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    graph = db.select_graph(graph_names().topology)
    rows = graph.ro_query(
        "MATCH (ts:TrafficState {incident_id: $id}) RETURN ts.active_route, ts.action",
        {"id": inc_id},
    ).result_set
    assert len(rows) == 1
    assert rows[0][0] == ["checkout", "queue", "payments-backup"]
    assert rows[0][1] == "simulate_failover"

    # 6. Idempotency: duplicate approval returns already_completed
    dup_res = await approve_action(
        incident_id=inc_id,
        approval_id=state.approval_id,
        tenant_id="acme",
    )
    assert dup_res["status"] == "applied"
    assert dup_res["already_completed"] is True


@pytest.mark.asyncio
async def test_simulation_workflow_budget_rejection(workflow_app):
    """Verify that when cost cap is below all routes (cap 5 < 8), workflow fails closed."""
    inc_id = f"inc-sim-fail-{uuid.uuid4().hex[:8]}"
    initial_state = IncidentState(
        incident_id=inc_id,
        tenant_id="acme",
        service_id="payments-primary",
        execution_mode="simulation",
        max_cost=5,
    )

    final_state_dict = await workflow_app.ainvoke(initial_state)
    state = IncidentState(**final_state_dict)

    assert state.status == "failed"
    assert state.approval_id is None
    assert len(state.rejected_routes) >= 2


@pytest.mark.live_demo
@pytest.mark.asyncio
async def test_real_workflow_proposal_and_governance(workflow_app):
    """Verify real execution mode gathers ownership + Slack notice and proposes restart_service."""
    inc_id = f"inc-real-{uuid.uuid4().hex[:8]}"
    initial_state = IncidentState(
        incident_id=inc_id,
        tenant_id="acme",
        service_id="payments-primary",
        execution_mode="real",
    )

    final_state_dict = await workflow_app.ainvoke(initial_state)
    state = IncidentState(**final_state_dict)

    assert state.status == "awaiting_approval"
    assert state.proposed_action["action_kind"] == "restart_service"
    assert state.evidence["ownership"]["owner_team"] == "Payments"
    assert state.approval_id is not None


@pytest.mark.asyncio
async def test_cross_tenant_approval_denied(workflow_app):
    """Verify that tenant globex cannot approve an acme approval record."""
    inc_id = f"inc-iso-{uuid.uuid4().hex[:8]}"
    initial_state = IncidentState(
        incident_id=inc_id,
        tenant_id="acme",
        service_id="payments-primary",
        execution_mode="simulation",
    )
    res_dict = await workflow_app.ainvoke(initial_state)
    appr_id = res_dict["approval_id"]

    # Globex tries to approve Acme's incident
    with pytest.raises(ActionNotFoundError) as exc_info:
        await approve_action(
            incident_id=inc_id,
            approval_id=appr_id,
            tenant_id="globex",
        )
    assert "globex" in str(exc_info.value)


@pytest.mark.asyncio
async def test_expired_approval_rejection():
    """Verify that an expired approval record (>5m) raises ActionConflictError."""
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    graph = db.select_graph(graph_names().topology)

    appr_id = f"appr-exp-{uuid.uuid4().hex[:8]}"
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    graph.query(
        "MERGE (a:ApprovalRecord {approval_id: $id}) "
        "SET a.incident_id = 'inc-exp', "
        "    a.tenant_id = 'acme', "
        "    a.execution_mode = 'simulation', "
        "    a.status = 'pending', "
        "    a.expires_at = $past_time, "
        "    a.proposed_action = '{\"route\": [\"a\", \"b\"]}'",
        {"id": appr_id, "past_time": past_time},
    )

    with pytest.raises(ActionConflictError) as exc_info:
        await approve_action(
            incident_id="inc-exp",
            approval_id=appr_id,
            tenant_id="acme",
        )
    assert "expired" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_rejection_action():
    """Verify that reject_action marks status as rejected."""
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    graph = db.select_graph(graph_names().topology)

    appr_id = f"appr-rej-{uuid.uuid4().hex[:8]}"
    graph.query(
        "MERGE (a:ApprovalRecord {approval_id: $id}) "
        "SET a.incident_id = 'inc-rej', "
        "    a.tenant_id = 'acme', "
        "    a.status = 'pending'",
        {"id": appr_id},
    )

    rej_res = await reject_action(
        incident_id="inc-rej",
        approval_id=appr_id,
        tenant_id="acme",
    )
    assert rej_res["status"] == "rejected"

    status = graph.ro_query(
        "MATCH (a:ApprovalRecord {approval_id: $id}) RETURN a.status",
        {"id": appr_id},
    ).result_set[0][0]
    assert status == "rejected"

@pytest.mark.asyncio
async def test_real_recovery_disabled_never_invokes_fake_adapter(monkeypatch):
    """A real isolated approval must stop at the server-owned recovery kill switch."""
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    graph = db.select_graph(graph_names().topology)
    approval_id = f"appr-real-disabled-{uuid.uuid4().hex[:8]}"
    graph.query(
        "MERGE (a:ApprovalRecord {approval_id: $approval_id}) "
        "SET a.incident_id = 'inc-real-disabled', "
        "    a.tenant_id = 'acme', "
        "    a.execution_mode = 'real', "
        "    a.action_kind = 'restart_service', "
        "    a.status = 'pending', "
        "    a.proposed_action = '{\"instance_id\":\"i-test\",\"document_name\":\"ChronosOps-StartNginx\"}'",
        {"approval_id": approval_id},
    )

    created = False

    class FakeRecoveryAdapter:
        def __init__(self) -> None:
            nonlocal created
            created = True

    monkeypatch.setenv("REAL_RECOVERY_ENABLED", "false")
    monkeypatch.setattr("agents.actions.AwsRecoveryClient", FakeRecoveryAdapter)

    with pytest.raises(ActionConflictError, match="disabled"):
        await approve_action(
            incident_id="inc-real-disabled",
            approval_id=approval_id,
            tenant_id="acme",
        )
    assert created is False
