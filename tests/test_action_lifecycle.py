"""Actual-FalkorDB regressions for atomic approval lifecycle transitions."""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from falkordb import FalkorDB


from agents import actions, safety
from agents.actions import ActionConflictError, ActionUnavailableError, approve_action, reject_action
from agents.governance import governance_node
from agents.remediation import remediation_node
from agents.settings import graph_names
from agents.triage import triage_node
from agents.workflow import IncidentState, create_incident_workflow
from seed.load import get_config


def _graph():
    config = get_config()
    return FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    ).select_graph(graph_names().topology)


async def _approval() -> tuple[str, str]:
    topology = _graph()
    topology_rows = topology.ro_query(
        "MATCH (m:Meta {id:'ops_topology_meta'}) RETURN m.hash",
    ).result_set
    assert topology_rows and isinstance(topology_rows[0][0], str)
    incident_id = f"inc-cas-{uuid.uuid4().hex[:8]}"
    state = IncidentState(
        incident_id=incident_id,
        tenant_id="acme",
        service_id="payments-primary",
        execution_mode="simulation",
        incident_type="bounded_failover",
        topology_version=topology_rows[0][0],
        max_cost=12,
        proposed_action={
            "action_kind": "simulate_failover",
            "route": ["checkout", "queue", "payments-backup"],
            "cost": 8,
        },
    )
    result = await governance_node(state)
    assert result["status"] == "awaiting_approval"
    return incident_id, result["approval_id"]


@pytest.mark.asyncio
async def test_twenty_overlapping_approvals_have_one_atomic_transition() -> None:
    """Competing real graph clients all observe the one durable applied action."""
    incident_id, approval_id = await _approval()
    gate = asyncio.Event()

    async def approve() -> dict:
        await gate.wait()
        return await approve_action(incident_id, approval_id, "acme")

    tasks = [asyncio.create_task(approve()) for _ in range(20)]
    gate.set()
    results = await asyncio.gather(*tasks)
    assert {result["status"] for result in results} == {"applied"}
    assert sum(not result["already_completed"] for result in results) == 1
    assert _graph().ro_query(
        "MATCH (a:ApprovalRecord {approval_id:$approval_id}) "
        "MATCH (t:TrafficState {tenant_id:'acme',incident_id:$incident_id}) "
        "RETURN count(a),count(t)",
        {"approval_id": approval_id, "incident_id": incident_id},
    ).result_set == [[1, 1]]


@pytest.mark.asyncio
async def test_overlapping_approval_and_rejection_leave_one_native_winner(monkeypatch) -> None:
    """Concurrent pending-only transitions cannot overwrite their native winner."""
    incident_id, approval_id = await _approval()
    graph_factory = actions._graph
    query_barrier = threading.Barrier(2)

    class BarrierGraph:
        def __init__(self, graph):
            self._graph = graph

        def query(self, query, *args, **kwargs):
            if "SET a.status='applied'" in query or "SET a.status='rejected'" in query:
                query_barrier.wait(timeout=10)
            return self._graph.query(query, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._graph, name)

    monkeypatch.setattr(actions, "_graph", lambda tenant_id: BarrierGraph(graph_factory(tenant_id)))
    start = asyncio.Event()

    async def approve():
        await start.wait()
        try:
            return await approve_action(incident_id, approval_id, "acme")
        except ActionConflictError as error:
            return error

    async def reject():
        await start.wait()
        try:
            return await reject_action(incident_id, approval_id, "acme")
        except ActionConflictError as error:
            return error

    approval_task = asyncio.create_task(approve())
    rejection_task = asyncio.create_task(reject())
    start.set()
    results = await asyncio.gather(approval_task, rejection_task)

    winners = [result for result in results if isinstance(result, dict)]
    conflicts = [result for result in results if isinstance(result, ActionConflictError)]
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert winners[0]["status"] in {"applied", "rejected"}
    status = _graph().ro_query(
        "MATCH (a:ApprovalRecord {approval_id:$approval_id}) RETURN a.status",
        {"approval_id": approval_id},
    ).result_set
    assert status == [[winners[0]["status"]]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "MATCH (:Service {service_id:'checkout'})-[route:ROUTES_TO]->(:Service {service_id:'queue'}) "
        "SET route.health='unhealthy'",
        "MATCH (:Service {service_id:'checkout'})-[route:ROUTES_TO]->(:Service {service_id:'queue'}) "
        "SET route.cost=13",
    ],
)
async def test_simulation_claim_rechecks_route_facts_inside_native_write(monkeypatch, mutation: str) -> None:
    """A route that changes after refresh but before claim cannot be applied."""
    incident_id, approval_id = await _approval()
    native_claim = actions._claim

    def mutate_then_claim(graph, *args, **kwargs):
        graph.query(mutation)
        return native_claim(graph, *args, **kwargs)

    monkeypatch.setattr(actions, "_claim", mutate_then_claim)
    with pytest.raises(ActionConflictError):
        await approve_action(incident_id, approval_id, "acme")

    assert _graph().ro_query(
        "MATCH (a:ApprovalRecord {approval_id:$approval_id}) "
        "OPTIONAL MATCH (t:TrafficState {tenant_id:'acme',incident_id:$incident_id}) "
        "RETURN a.status,count(t)",
        {"approval_id": approval_id, "incident_id": incident_id},
    ).result_set == [["pending", 0]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
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
        "endpoint",
        "health_id",
    ],
)
async def test_real_claim_rechecks_frozen_evidence_before_submission(monkeypatch, field: str) -> None:
    """A deployment or health drift after refresh leaves the action pending and unsent."""
    topology = _graph()
    topology_version = topology.ro_query(
        "MATCH (m:Meta {id:'ops_topology_meta'}) RETURN m.hash",
    ).result_set[0][0]
    approval_id = f"approval-binding-{uuid.uuid4().hex[:8]}"
    incident_id = f"inc-binding-{uuid.uuid4().hex[:8]}"
    deployment_id = f"dep-binding-{uuid.uuid4().hex[:8]}"
    health_id = f"health-binding-{uuid.uuid4().hex[:8]}"
    binding = {
        "deployment_id": deployment_id,
        "tenant_id": "acme",
        "service_id": "payments-primary",
        "account_id": "123456789012",
        "region": "us-east-1",
        "instance_id": "i-binding-check",
        "stack_id": "arn:aws:cloudformation:us-east-1:123456789012:stack/demo/binding-check",
        "document_name": "ChronosOps-StartNginx",
        "document_version": "4",
        "document_hash": "b" * 64,
        "recovery_role_arn": "arn:aws:iam::123456789012:role/Recovery",
        "operator": "isolated-test",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "endpoint": "http://198.51.100.11/healthz",
    }
    action = {
        "action_kind": "restart_service",
        "deployment_id": deployment_id,
        "service_id": binding["service_id"],
        "instance_id": binding["instance_id"],
        "document_name": binding["document_name"],
        "document_version": binding["document_version"],
        "document_hash": binding["document_hash"],
    }
    frozen_binding = {"deployment": binding, "action": action}
    approval = {
        "approval_id": approval_id,
        "tenant_id": "acme",
        "incident_id": incident_id,
        "service_id": binding["service_id"],
        "execution_mode": "real",
        "incident_type": "application_outage",
        "topology_version": topology_version,
        "action_kind": "restart_service",
        "evidence_hash": f"binding-{approval_id}",
        "proposed_action": json.dumps(action, sort_keys=True, separators=(",", ":")),
        "binding_json": json.dumps(frozen_binding, sort_keys=True, separators=(",", ":")),
        "deployment_binding_json": json.dumps(binding, sort_keys=True, separators=(",", ":")),
        "runbook_binding_json": "{}",
        "fault_window_json": "{}",
        "evidence_versions_json": "{}",
        "observation_ids": [health_id],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "status": "pending",
    }
    topology.query(
        "CREATE (a:ApprovalRecord) SET a += $approval "
        "CREATE (i:Incident {tenant_id:'acme',incident_id:$incident_id}) "
        "SET i.service_id=$service_id,i.execution_mode='real',i.deployment_id=$deployment_id,"
        "i.approval_id=$approval_id,i.status='observed' "
        "CREATE (d:Deployment {deployment_id:$deployment_id}) SET d += $deployment,d.active=true "
        "CREATE (h:HealthObservation {id:$health_id}) "
        "CREATE (i)-[:HEALTH_EVIDENCE]->(h)",
        {
            "approval": approval,
            "incident_id": incident_id,
            "approval_id": approval_id,
            "service_id": binding["service_id"],
            "deployment_id": deployment_id,
            "deployment": binding,
            "health_id": health_id,
        },
    )
    topology.query("MATCH (slot:RecoverySlot {id:'real'}) SET slot.status='free'")
    validated = SimpleNamespace(
        binding=frozen_binding,
        topology_version=topology_version,
        proposed_action=action,
    )

    async def revalidate(_record):
        return validated

    submitted = 0

    class SubmissionClient:
        def __init__(self, region=None, **_kwargs):
            pass

        async def submit_restart(self, _binding, _comment):
            nonlocal submitted
            submitted += 1
            return "must-not-submit"

    original_claim = actions._claim

    def drift_then_claim(graph, *args, **kwargs):
        if field == "health_id":
            graph.query(
                "MATCH (:Incident {tenant_id:'acme',incident_id:$incident_id})"
                "-[e:HEALTH_EVIDENCE]->(:HealthObservation {id:$health_id}) DELETE e",
                {"incident_id": incident_id, "health_id": health_id},
            )
        else:
            graph.query(
                f"MATCH (d:Deployment {{deployment_id:$deployment_id}}) SET d.{field}=$value",
                {"deployment_id": deployment_id, "value": f"changed-{field}"},
            )
        return original_claim(graph, *args, **kwargs)

    monkeypatch.setenv("REAL_RECOVERY_ENABLED", "true")
    monkeypatch.setattr(actions, "_revalidate", revalidate)
    monkeypatch.setattr(actions, "AwsRecoveryClient", SubmissionClient)
    monkeypatch.setattr(actions, "_claim", drift_then_claim)
    with pytest.raises(ActionConflictError):
        await approve_action(incident_id, approval_id, "acme")

    assert submitted == 0
    assert topology.ro_query(
        "MATCH (a:ApprovalRecord {approval_id:$approval_id}) RETURN a.status",
        {"approval_id": approval_id},
    ).result_set == [["pending"]]


@pytest.mark.asyncio
async def test_twenty_real_claimants_submit_one_command(monkeypatch, tmp_path) -> None:
    """Only one real graph claimant reaches the controlled external submit adapter."""
    topology = _graph()
    config = get_config()
    company = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    ).select_graph(graph_names().company)
    topology_version = topology.ro_query(
        "MATCH (m:Meta {id:'ops_topology_meta'}) RETURN m.hash",
    ).result_set[0][0]
    deployment = {
        "deployment_id": "dep-action-cas",
        "tenant_id": "acme",
        "service_id": "payments-primary",
        "account_id": "123456789012",
        "region": "us-east-1",
        "instance_id": "i-action-cas",
        "stack_id": "arn:aws:cloudformation:us-east-1:123456789012:stack/demo/action-cas",
        "document_name": "ChronosOps-StartNginx",
        "document_version": "4",
        "document_hash": "a" * 64,
        "recovery_role_arn": "arn:aws:iam::123456789012:role/Recovery",
        "endpoint": "http://198.51.100.10/healthz",
        "operator": "isolated-test",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    incident_id = f"inc-real-{uuid.uuid4().hex[:8]}"
    topology.query(
        "MERGE (d:Deployment {deployment_id:$deployment_id}) SET d += $deployment,d.active=true "
        "MERGE (i:Incident {incident_id:$incident_id,tenant_id:'acme'}) "
        "SET i.service_id='payments-primary',i.execution_mode='real',i.origin='probe',"
        "i.deployment_id=$deployment_id,i.status='observed' "
        "MERGE (h:HealthObservation {id:$health_id}) "
        "SET h.origin='probe',h.deployment_id=$deployment_id,h.probe_success=false,"
        "h.exporter_up=true,h.http_status=503,h.data_hash='action-cas-health' "
        "MERGE (i)-[:HEALTH_EVIDENCE]->(h)",
        {"deployment_id": deployment["deployment_id"], "deployment": deployment,
         "incident_id": incident_id, "health_id": "action-cas-health"},
    )
    source_id = "slack:T-ACME:C-OPERATIONS:action-cas"
    source = {
        "content_hash": "b" * 64,
        "source_version": "1",
        "revision": 1,
        "origin": "slack",
        "service_id": "payments-primary",
    }
    company.query(
        "MERGE (s:SourceRecord {document_id:$id}) SET s += $source,s.status='published'",
        {"id": source_id, "source": source},
    )
    fault = {key: deployment[key] for key in ("deployment_id", "account_id", "region", "instance_id", "stack_id")}
    fault |= {"state": "active", "watchdog_deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    fault_file = tmp_path / "fault-window.json"
    fault_file.write_text(json.dumps(fault), encoding="utf-8")
    monkeypatch.setenv("REAL_RECOVERY_ENABLED", "true")
    monkeypatch.setattr(safety, "FAULT_WINDOW_FILE", fault_file)

    class ValidationMonitoring:
        def check_health(self, _endpoint):
            return SimpleNamespace(
                status="unhealthy",
                samples=[SimpleNamespace(id="action-cas-health", exporter_up=True, probe_success=False, http_status=503)],
            )

    class ValidationAws:
        def __init__(self, region=None, **_kwargs):
            pass
        async def refresh_binding(self, binding):
            return {key: binding[key] for key in (
                "account_id", "region", "instance_id", "document_name",
                "document_version", "document_hash", "recovery_role_arn",
            )}

    async def slack_ok(*_args, **_kwargs):
        return {"valid": True}

    monkeypatch.setattr(safety, "MonitoringClient", lambda **_kwargs: ValidationMonitoring())
    monkeypatch.setattr(safety, "AwsRecoveryClient", ValidationAws)
    monkeypatch.setattr(safety, "validate_slack_source", slack_ok)
    state = IncidentState(
        incident_id=incident_id,
        tenant_id="acme",
        service_id="payments-primary",
        execution_mode="real",
        incident_type="application_outage",
        topology_version=topology_version,
        evidence={"probe": "captured"},
        deployment_binding=deployment,
        runbook_binding={key: deployment[key] for key in ("document_name", "document_version", "document_hash", "recovery_role_arn")},
        fault_window=fault,
        observation_ids=["action-cas-health"],
        evidence_versions={source_id: source},
        proposed_action={"action_kind": "restart_service", "instance_id": deployment["instance_id"], "document_name": deployment["document_name"]},
    )
    approval = await governance_node(state)
    assert approval["status"] == "awaiting_approval", approval
    submissions = 0

    class SubmissionClient:
        def __init__(self, region=None, **_kwargs):
            pass
        async def submit_restart(self, _binding, _comment):
            nonlocal submissions
            submissions += 1
            return "cmd-action-cas"

        async def poll_restart(self, command_id, _binding):
            return SimpleNamespace(
                command_id=command_id,
                status="failed",
                ssm_status="Failed",
                completed_at=None,
                error_code="controlled_failure",
            )

    monkeypatch.setattr(actions, "AwsRecoveryClient", SubmissionClient)
    gate = asyncio.Event()

    async def approve() -> dict:
        await gate.wait()
        try:
            return await approve_action(incident_id, approval["approval_id"], "acme")
        except ActionConflictError:
            return {"status": "conflict"}
        except ActionUnavailableError:
            return {"status": "unavailable"}

    tasks = [asyncio.create_task(approve()) for _ in range(20)]
    gate.set()
    results = await asyncio.gather(*tasks)
    statuses = {result["status"] for result in results}
    assert submissions == 1
    assert statuses <= {"executing", "failed", "conflict", "unavailable"}
    assert "failed" in statuses
