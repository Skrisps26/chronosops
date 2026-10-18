"""Actual-graph behavioral contracts for evidence-bound proposal authority.

The isolated FalkorDB fixture contains the authority graph.  Only genuinely
external AWS, monitoring, Slack, and fault-file adapters are controlled here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from falkordb import FalkorDB

from agents import safety, tools
from agents.settings import graph_names
from agents.workflow import IncidentState
from seed.load import get_config


@dataclass
class ActualFacts:
    topology_version: str
    deployment: dict[str, str]
    source_id: str
    source: dict[str, Any]
    fault_window: dict[str, str]


class ControlledMonitoring:
    def __init__(self, *, unknown: bool = False) -> None:
        self.unknown = unknown

    def check_health(self, _endpoint: str) -> SimpleNamespace:
        if self.unknown:
            return SimpleNamespace(status="monitoring_unknown", samples=[])
        return SimpleNamespace(
            status="unhealthy",
            samples=[SimpleNamespace(id="health-1", exporter_up=True, probe_success=False, http_status=503)],
        )


class ControlledAws:
    def __init__(self, _region: str, *, changed_field: str | None = None) -> None:
        self.changed_field = changed_field

    async def refresh_binding(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        refreshed = {key: binding[key] for key in (
            "account_id", "region", "instance_id", "document_name", "document_version", "document_hash", "recovery_role_arn",
        )}
        if self.changed_field:
            refreshed[self.changed_field] = "changed"
        return refreshed


def _admin_graph(name: str) -> Any:
    config = get_config()
    return FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config["FALKORDB_ADMIN_PASSWORD"],
    ).select_graph(name)

def _fault(deployment: Mapping[str, str], *, seconds: int = 300) -> dict[str, str]:
    return {key: deployment[key] for key in ("deployment_id", "account_id", "region", "instance_id", "stack_id")} | {
        "state": "active",
        "watchdog_deadline": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
    }


def _state(facts: ActualFacts) -> IncidentState:
    deployment = dict(facts.deployment)
    return IncidentState(
        incident_id="inc-1", tenant_id="acme", service_id="payments-primary", execution_mode="real",
        topology_version=facts.topology_version, incident_type="application_outage", evidence={"alert": "probe"},
        deployment_binding=deployment,
        runbook_binding={key: deployment[key] for key in ("document_name", "document_version", "document_hash", "recovery_role_arn")},
        fault_window=dict(facts.fault_window), observation_ids=["health-1"],
        evidence_versions={facts.source_id: dict(facts.source)},
        proposed_action={"action_kind": "restart_service", "instance_id": "i-123", "document_name": "ChronosOps-StartNginx", "rationale": "not authority"},
    )


@pytest.fixture
def actual_facts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ActualFacts:
    topology = _admin_graph(graph_names().topology)
    company = _admin_graph(graph_names().company)
    topology_rows = topology.ro_query(
        "MATCH (m:Meta {id:'ops_topology_meta'}) RETURN m.hash", timeout=3000
    ).result_set
    assert len(topology_rows) == 1 and isinstance(topology_rows[0][0], str)
    deployment = {
        "deployment_id": "dep-1", "tenant_id": "acme", "service_id": "payments-primary",
        "account_id": "123456789012", "region": "us-east-1", "instance_id": "i-123",
        "stack_id": "arn:aws:cloudformation:us-east-1:123456789012:stack/demo/a",
        "document_name": "ChronosOps-StartNginx", "document_version": "4",
        "document_hash": "a" * 64, "recovery_role_arn": "arn:aws:iam::123456789012:role/Recovery",
        "endpoint": "http://198.51.100.10/healthz",
        "operator": "isolated-test", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    topology.query(
        "MERGE (d:Deployment {deployment_id:$deployment_id}) SET d += $deployment, d.active=true "
        "MERGE (i:Incident {incident_id:'inc-1', tenant_id:'acme'}) "
        "SET i.service_id='payments-primary', i.execution_mode='real', i.origin='probe', "
        "i.deployment_id='dep-1', i.status='observed', i.recorded_at='2026-10-18T00:00:00+00:00' "
        "MERGE (h:HealthObservation {id:'health-1'}) "
        "SET h.origin='probe', h.deployment_id='dep-1', h.probe_success=false, h.exporter_up=true, "
        "h.http_status=503, h.data_hash='health-observation-v1' "
        "MERGE (i)-[:HEALTH_EVIDENCE]->(h)",
        {"deployment_id": deployment["deployment_id"], "deployment": deployment}, timeout=3000,
    )
    source_id = "slack:T-ACME:C-OPERATIONS:1729000020.000001"
    source = {"content_hash": "b" * 64, "source_version": "1729000020.000001", "revision": 4,
              "origin": "slack", "service_id": "payments-primary"}
    company.query(
        "MERGE (s:SourceRecord {document_id:$document_id}) SET s += $source, s.status='published'",
        {"document_id": source_id, "source": source}, timeout=3000,
    )
    fault_window = _fault(deployment)
    fault_path = tmp_path / "fault-window.json"
    fault_path.write_text(json.dumps(fault_window), encoding="utf-8")
    monkeypatch.setenv("REAL_RECOVERY_ENABLED", "true")
    monkeypatch.setattr(safety, "FAULT_WINDOW_FILE", fault_path)
    monkeypatch.setattr(safety, "MonitoringClient", lambda deployment_id: ControlledMonitoring())
    monkeypatch.setattr(safety, "AwsRecoveryClient", lambda region: ControlledAws(region))

    async def slack_ok(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"valid": True}

    monkeypatch.setattr(safety, "validate_slack_source", slack_ok)
    return ActualFacts(str(topology_rows[0][0]), deployment, source_id, source, fault_window)


@pytest.mark.asyncio
async def test_valid_actual_graph_evidence_returns_immutable_binding_and_stable_hash(actual_facts: ActualFacts) -> None:
    state = _state(actual_facts)
    proposal = await safety.validate_proposal(state)
    state.proposed_action["rationale"] = "a different model explanation"
    repeated = await safety.validate_proposal(state)
    assert proposal.evidence_hash == repeated.evidence_hash
    assert proposal.binding["deployment"]["instance_id"] == "i-123"
    assert dict(proposal.proposed_action) == {
        "action_kind": "restart_service", "deployment_id": "dep-1",
        "service_id": "payments-primary", "instance_id": "i-123",
        "document_name": "ChronosOps-StartNginx", "document_version": "4", "document_hash": "a" * 64,
    }
    with pytest.raises(TypeError):
        proposal.binding["tenant_id"] = "globex"  # type: ignore[index]


@pytest.mark.asyncio
async def test_meaningful_source_version_change_changes_hash(actual_facts: ActualFacts) -> None:
    original = await safety.validate_proposal(_state(actual_facts))
    company = _admin_graph(graph_names().company)
    company.query(
        "MATCH (s:SourceRecord {document_id:$document_id}) SET s.source_version=$version, s.revision=$revision",
        {"document_id": actual_facts.source_id, "version": "1729000021.000001", "revision": 5}, timeout=3000,
    )
    actual_facts.source.update({"source_version": "1729000021.000001", "revision": 5})
    changed = await safety.validate_proposal(_state(actual_facts))
    assert changed.evidence_hash != original.evidence_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate,reason", [
    (lambda state, _facts: setattr(state, "evidence", {}), "MISSING_EVIDENCE"),
    (lambda state, _facts: setattr(state, "tenant_id", "globex"), "FOREIGN_TENANT"),
    (lambda state, _facts: setattr(state, "topology_version", "old"), "STALE_TOPOLOGY"),
    (lambda state, _facts: state.evidence_versions.clear(), "MISSING_SLACK_EVIDENCE"),
    (lambda state, _facts: state.proposed_action.update({"instance_id": "i-foreign"}), "TARGET_CONFLICT"),
    (lambda state, _facts: setattr(state, "service_id", "missing-service"), "SERVICE_NOT_FOUND"),
])
async def test_missing_forged_stale_or_unknown_actual_graph_evidence_refuses(actual_facts: ActualFacts, mutate: Any, reason: str) -> None:
    state = _state(actual_facts)
    mutate(state, actual_facts)
    with pytest.raises(safety.ProposalConflict) as failure:
        await safety.validate_proposal(state)
    assert failure.value.reason == reason


@pytest.mark.asyncio
async def test_actual_graph_source_must_remain_published_and_current(actual_facts: ActualFacts) -> None:
    company = _admin_graph(graph_names().company)
    company.query(
        "MATCH (s:SourceRecord {document_id:$document_id}) SET s.status='deleted'",
        {"document_id": actual_facts.source_id}, timeout=3000,
    )
    with pytest.raises(safety.ProposalConflict) as failure:
        await safety.validate_proposal(_state(actual_facts))
    assert failure.value.reason == "MISSING_SLACK_EVIDENCE"


@pytest.mark.asyncio
async def test_graph_failure_and_disconnected_monitoring_preserve_uncertainty(actual_facts: ActualFacts, monkeypatch: pytest.MonkeyPatch) -> None:
    configured_port = get_config()["FALKORDB_PORT"]
    monkeypatch.setenv("FALKORDB_PORT", "1")
    with pytest.raises(safety.ProposalUnavailable) as graph_failure:
        await safety.validate_proposal(_state(actual_facts))
    assert graph_failure.value.reason == "GRAPH_UNAVAILABLE"
    monkeypatch.setenv("FALKORDB_PORT", configured_port)
    monkeypatch.setattr(safety, "MonitoringClient", lambda deployment_id: ControlledMonitoring(unknown=True))
    with pytest.raises(safety.ProposalUnavailable) as monitor_failure:
        await safety.validate_proposal(_state(actual_facts))
    assert monitor_failure.value.reason == "MONITORING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_live_slack_and_fault_window_must_remain_current(actual_facts: ActualFacts, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slack_deleted(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"valid": False, "error": "deleted_or_unavailable"}

    monkeypatch.setattr(safety, "validate_slack_source", slack_deleted)
    with pytest.raises(safety.ProposalUnavailable) as slack_failure:
        await safety.validate_proposal(_state(actual_facts))
    assert slack_failure.value.reason == "SLACK_UNAVAILABLE"

    async def slack_ok(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"valid": True}

    monkeypatch.setattr(safety, "validate_slack_source", slack_ok)
    actual_facts.fault_window = _fault(actual_facts.deployment, seconds=119)
    safety.FAULT_WINDOW_FILE.write_text(json.dumps(actual_facts.fault_window), encoding="utf-8")
    with pytest.raises(safety.ProposalConflict) as window_failure:
        await safety.validate_proposal(_state(actual_facts))
    assert window_failure.value.reason == "FAULT_WINDOW_EXPIRING"


@pytest.mark.asyncio
async def test_aws_runbook_or_role_change_cannot_authorize(actual_facts: ActualFacts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety, "AwsRecoveryClient", lambda region: ControlledAws(region, changed_field="recovery_role_arn"))
    with pytest.raises(safety.ProposalConflict) as failure:
        await safety.validate_proposal(_state(actual_facts))
    assert failure.value.reason == "AWS_BINDING_CONFLICT"


@pytest.mark.asyncio
async def test_real_disarm_refuses_before_any_external_authority(actual_facts: ActualFacts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REAL_RECOVERY_ENABLED", "false")
    with pytest.raises(safety.ProposalConflict) as failure:
        await safety.validate_proposal(_state(actual_facts))
    assert failure.value.reason == "REAL_RECOVERY_DISARMED"


@pytest.mark.asyncio
async def test_simulation_rechecks_a_real_seeded_graph_route() -> None:
    topology = _admin_graph(graph_names().topology)
    topology_rows = topology.ro_query(
        "MATCH (m:Meta {id:'ops_topology_meta'}) RETURN m.hash", timeout=3000
    ).result_set
    assert len(topology_rows) == 1 and isinstance(topology_rows[0][0], str)
    route = await tools.failover_routes("checkout", "payments-backup", max_cost=12)
    assert route.status == "OK"
    proposal = await safety.validate_proposal(IncidentState(
        incident_id="simulation-actual-graph", tenant_id="acme", service_id="payments-primary",
        execution_mode="simulation", incident_type="bounded_failover",
        topology_version=topology_rows[0][0], max_cost=12,
        proposed_action={"action_kind": "simulate_failover", "route": route.data["recommended_route"], "cost": route.data["cost"]},
    ))
    assert proposal.binding["route"] == tuple(route.data["recommended_route"])


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status", [0, 200])
async def test_failed_connection_or_content_marker_is_valid_probe_failure(
    actual_facts: ActualFacts, monkeypatch: pytest.MonkeyPatch, http_status: int,
) -> None:
    _admin_graph(graph_names().topology).query(
        "MATCH (h:HealthObservation {id:'health-1'}) SET h.http_status=$status",
        {"status": http_status}, timeout=3000,
    )
    monkeypatch.setattr(safety, "MonitoringClient", lambda deployment_id: SimpleNamespace(
        check_health=lambda endpoint: SimpleNamespace(status="unhealthy", samples=[
            SimpleNamespace(id="health-1", exporter_up=True, probe_success=False, http_status=http_status),
        ]),
    ))
    proposal = await safety.validate_proposal(_state(actual_facts))
    assert proposal.binding["observation_ids"] == ("health-1",)


@pytest.mark.asyncio
async def test_pending_approval_does_not_hide_an_unresolved_command(actual_facts: ActualFacts) -> None:
    graph = _admin_graph(graph_names().topology)
    graph.query(
        "MERGE (a:ApprovalRecord {approval_id:'pending-existing'}) "
        "SET a.tenant_id='acme',a.execution_mode='real',a.status='pending'",
        timeout=3000,
    )
    accepted = await safety.validate_proposal(_state(actual_facts))
    graph.query("MERGE (r:RecoverySlot {id:'real'}) SET r.status='execution_unknown'", timeout=3000)
    with pytest.raises(safety.ProposalConflict) as failure:
        await safety.validate_proposal(_state(actual_facts))
    assert accepted.proposed_action["instance_id"] == "i-123"
    assert failure.value.reason == "RECOVERY_IN_PROGRESS"
