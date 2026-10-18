"""Module 5 verification suite: episodic/semantic memory, procedural workflows, tenant isolation, and concurrency."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from agents.memory import (
    consolidate_incident,
    execute_learned_procedural_workflow,
    get_tenant_db,
    is_workflow_learned,
    retrieve_tenant_memory,
)
from agents.settings import graph_names
from agents.tools import failover_routes
from api import main
from api.contracts import get_tenant_id


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_DIR = ROOT / "artifacts"


def _seed_authoritative_incident(
    incident_id: str,
    *,
    tenant_id: str = "acme",
    service_id: str = "payments-primary",
    execution_mode: str = "simulation",
    action_kind: str = "simulate_failover",
    status: str = "applied",
    proposed_action: dict | None = None,
    topology_version: str = "v1.0",
    max_cost: int = 12,
    rejected_routes: list | None = None,
    command_id: str | None = None,
    ssm_status: str | None = None,
    recovery_sample_ids: list[str] | None = None,
    deployment_binding: dict | None = None,
) -> None:
    db = get_tenant_db(tenant_id, admin=True)
    graph = db.select_graph(graph_names().topology)
    now = datetime.now(timezone.utc).isoformat()
    approval_id = f"appr-{incident_id}"

    proposed = proposed_action or (
        {"route": ["checkout", "queue", "payments-backup"], "cost": 8, "action_kind": action_kind}
        if execution_mode == "simulation"
        else {"action_kind": action_kind, "service_id": service_id}
    )

    graph.query(
        "MERGE (i:Incident {incident_id: $incident_id, tenant_id: $tenant_id}) "
        "SET i.service_id = $service_id, "
        "    i.execution_mode = $execution_mode, "
        "    i.status = $status, "
        "    i.topology_version = $topology_version, "
        "    i.approval_id = $approval_id, "
        "    i.rejected_routes_json = $rejected_json, "
        "    i.created_at = $now, "
        "    i.memory_pending = true "
        "WITH i "
        "MERGE (a:ApprovalRecord {approval_id: $approval_id, tenant_id: $tenant_id, incident_id: $incident_id}) "
        "SET a.status = $status, "
        "    a.service_id = $service_id, "
        "    a.execution_mode = $execution_mode, "
        "    a.action_kind = $action_kind, "
        "    a.proposed_action = $proposed_json, "
        "    a.topology_version = $topology_version, "
        "    a.max_cost = $max_cost, "
        "    a.command_id = $command_id, "
        "    a.ssm_status = $ssm_status, "
        "    a.recovery_sample_ids_json = $sample_ids_json, "
        "    a.deployment_binding_json = $binding_json, "
        "    a.rejected_routes_json = $rejected_json, "
        "    a.memory_pending = true, "
        "    a.applied_at = $now, "
        "    a.recovered_at = $now, "
        "    a.outcome_at = $now "
        "MERGE (i)-[:HAS_APPROVAL]->(a)",
        {
            "incident_id": incident_id,
            "tenant_id": tenant_id,
            "service_id": service_id,
            "execution_mode": execution_mode,
            "status": status,
            "topology_version": topology_version,
            "max_cost": max_cost,
            "approval_id": approval_id,
            "action_kind": action_kind,
            "proposed_json": json.dumps(proposed),
            "command_id": command_id or ("cmd-" + incident_id if execution_mode == "real" and status == "recovered" else ""),
            "ssm_status": ssm_status or ("Success" if execution_mode == "real" and status == "recovered" else ""),
            "sample_ids_json": json.dumps(recovery_sample_ids or (["s1", "s2", "s3"] if execution_mode == "real" and status == "recovered" else [])),
            "binding_json": json.dumps(deployment_binding or ({"document_name": "restart", "document_version": "1", "document_hash": "hash1"} if execution_mode == "real" else {})),
            "rejected_json": json.dumps(rejected_routes or []),
            "now": now,
        },
    )


@pytest.mark.asyncio
async def test_incident_consolidation_and_new_session_recall():
    """Verify episodic memory persists and is retrieved in a fresh session without chat history."""
    inc_id = f"inc-mem-{uuid.uuid4().hex[:8]}"
    rejected = [
        {
            "route": ["checkout", "premium", "payments-backup"],
            "cost": 20,
            "reason": "Cost 20 exceeds safety cap 12",
        }
    ]
    _seed_authoritative_incident(
        inc_id,
        tenant_id="acme",
        service_id="payments-primary",
        execution_mode="simulation",
        action_kind="simulate_failover",
        status="applied",
        rejected_routes=rejected,
    )

    # 1. Consolidate into acme memory
    res = await consolidate_incident(incident_id=inc_id, tenant_id="acme")
    assert res["status"] == "consolidated"
    assert res["episode_id"] == f"ep-{inc_id}"

    # 2. Fresh session retrieval (zero conversation messages)
    memory = await retrieve_tenant_memory(service_id="payments-primary", tenant_id="acme")
    assert memory["tenant_id"] == "acme"
    assert memory["episodes_count"] >= 1

    ep_ids = [e["incident_id"] for e in memory["episodes"]]
    assert inc_id in ep_ids

    # 3. Verify semantic fact was derived from rejected route
    facts = memory["active_facts"]
    assert any(f["subject"] == "checkout" and f["object"] == "premium" for f in facts)

    # 4. Verify memory_pending was cleared on source action in ops_topology
    ops_db = get_tenant_db("acme", admin=True)
    ops_graph = ops_db.select_graph(graph_names().topology)
    rows = ops_graph.ro_query(
        "MATCH (i:Incident {incident_id: $id}) RETURN i.memory_pending",
        {"id": inc_id},
    ).result_set
    assert rows == [[False]]


@pytest.mark.asyncio
async def test_tenant_isolation_boundary():
    """Verify tenant Globex cannot observe Acme episodes, and restricted DB user is denied access."""
    inc_id = f"inc-iso-{uuid.uuid4().hex[:8]}"
    _seed_authoritative_incident(
        inc_id,
        tenant_id="acme",
        service_id="payments-primary",
        execution_mode="simulation",
        action_kind="simulate_failover",
        status="applied",
    )
    await consolidate_incident(incident_id=inc_id, tenant_id="acme")

    # 1. Globex memory retrieval returns zero Acme episodes
    globex_mem = await retrieve_tenant_memory(service_id="payments-primary", tenant_id="globex")
    assert inc_id not in [e["incident_id"] for e in globex_mem["episodes"]]

    # 2. Direct restricted reader DB access denial
    globex_db = get_tenant_db("globex", admin=False)
    with pytest.raises(Exception) as exc_info:
        globex_db.select_graph(graph_names().memory_for("acme")).ro_query("RETURN 1")
    err = str(exc_info.value).lower()
    assert "noperm" in err or "permission" in err

    # 3. HTTP GET /api/memory/episodes/{episode_id} isolation check
    episode_id = f"ep-{inc_id}"
    try:
        main.app.dependency_overrides[get_tenant_id] = lambda: "globex"
        with TestClient(main.app) as client:
            res_foreign = client.get(f"/api/memory/episodes/{episode_id}")
            assert res_foreign.status_code == 404

        main.app.dependency_overrides[get_tenant_id] = lambda: "acme"
        with TestClient(main.app) as client:
            res_owner = client.get(f"/api/memory/episodes/{episode_id}")
            assert res_owner.status_code == 200
            assert res_owner.json()["incident_id"] == inc_id
            assert res_owner.json()["tenant_id"] == "acme"
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_procedural_workflow_formation_after_three_successes():
    """Verify that 3 successful incidents create a procedural WorkflowSubgraph with NEXT_STEP edges."""
    service_id = f"svc-wf-{uuid.uuid4().hex[:6]}"

    # Verify workflow is not learned initially
    assert is_workflow_learned(service_id=service_id, execution_mode="simulation", tenant_id="acme") is False

    # Episode 1
    inc_1 = f"inc-wf-{service_id}-0"
    _seed_authoritative_incident(inc_1, service_id=service_id, status="applied")
    res_1 = await consolidate_incident(inc_1, tenant_id="acme")
    assert res_1["success_count"] == 1
    assert res_1["workflow_created"] is False
    assert is_workflow_learned(service_id=service_id, execution_mode="simulation", tenant_id="acme") is False

    # Episode 2
    inc_2 = f"inc-wf-{service_id}-1"
    _seed_authoritative_incident(inc_2, service_id=service_id, status="applied")
    res_2 = await consolidate_incident(inc_2, tenant_id="acme")
    assert res_2["success_count"] == 2
    assert res_2["workflow_created"] is False
    assert is_workflow_learned(service_id=service_id, execution_mode="simulation", tenant_id="acme") is False

    # A failed episode does not count toward success
    inc_failed = f"inc-wf-{service_id}-failed"
    _seed_authoritative_incident(inc_failed, service_id=service_id, status="failed")
    res_fail = await consolidate_incident(inc_failed, tenant_id="acme")
    assert res_fail["success_count"] == 2
    assert res_fail["workflow_created"] is False
    assert is_workflow_learned(service_id=service_id, execution_mode="simulation", tenant_id="acme") is False

    # An episode for another service does not count toward this service
    inc_other = f"inc-wf-other-{uuid.uuid4().hex[:6]}"
    _seed_authoritative_incident(inc_other, service_id="other-service", status="applied")
    res_other = await consolidate_incident(inc_other, tenant_id="acme")
    assert is_workflow_learned(service_id=service_id, execution_mode="simulation", tenant_id="acme") is False

    # Episode 3: Third compatible successDerives procedural workflow
    inc_3 = f"inc-wf-{service_id}-2"
    _seed_authoritative_incident(inc_3, service_id=service_id, status="applied")
    res_3 = await consolidate_incident(inc_3, tenant_id="acme")
    assert res_3["success_count"] == 3
    assert res_3["workflow_created"] is True
    assert is_workflow_learned(service_id=service_id, execution_mode="simulation", tenant_id="acme") is True

    # Duplicate consolidation does not duplicate workflow or increment success count
    res_dup = await consolidate_incident(inc_3, tenant_id="acme")
    assert res_dup["success_count"] == 3
    assert res_dup["workflow_created"] is False

    # Inspect NEXT_STEP graph structure
    admin_db = get_tenant_db("acme", admin=True)
    graph = admin_db.select_graph(graph_names().memory_for("acme"))
    edges = graph.ro_query(
        "MATCH (p:WorkflowStep)-[r:NEXT_STEP]->(c:WorkflowStep) "
        "WHERE p.workflow_id = $wf_id "
        "RETURN p.name, c.name ORDER BY p.order",
        {"wf_id": f"wf-simulation-{service_id}"},
    ).result_set
    assert len(edges) == 6  # 7 steps have 6 sequential edges

    # Execute learned procedural workflow: zero planning calls
    playbook_res = await execute_learned_procedural_workflow(
        incident_id=f"inc-playbook-{service_id}",
        service_id=service_id,
        tenant_id="acme",
        execution_mode="simulation",
        max_cost=12,
    )
    assert playbook_res["procedural_execution"] is True
    assert playbook_res["llm_calls_made"] == 0
    # For a service not in the topology graph, blast_radius fails closed
    assert playbook_res["status"] == "failed"
    assert playbook_res["approval_id"] is None


@pytest.mark.asyncio
async def test_invalid_or_cyclic_workflow_fails():
    """Verify that a corrupted or cyclic workflow in memory fails execution immediately."""
    service_id = f"svc-corrupt-{uuid.uuid4().hex[:6]}"
    admin_db = get_tenant_db("acme", admin=True)
    graph = admin_db.select_graph(graph_names().memory_for("acme"))
    wf_id = f"wf-simulation-{service_id}"

    # Create a corrupted workflow with mismatched step count
    graph.query(
        "MERGE (wf:WorkflowSubgraph {id: $id, execution_mode: 'simulation', root_service: $svc}) "
        "MERGE (s1:WorkflowStep {id: $s1, name: 'retrieve_memory', order: 1, workflow_id: $id}) "
        "MERGE (wf)-[:CONTAINS_STEP]->(s1)",
        {"id": wf_id, "svc": service_id, "s1": f"{wf_id}-s1"},
    )

    with pytest.raises(ValueError, match="mismatch"):
        await execute_learned_procedural_workflow(
            incident_id=f"inc-corrupt-{service_id}",
            service_id=service_id,
            tenant_id="acme",
            execution_mode="simulation",
            max_cost=12,
        )


@pytest.mark.asyncio
async def test_duplicate_consolidation_idempotency():
    """Verify that consolidating the same incident twice does not duplicate episode nodes."""
    inc_id = f"inc-dup-{uuid.uuid4().hex[:8]}"
    _seed_authoritative_incident(
        inc_id,
        tenant_id="acme",
        service_id="payments-primary",
        execution_mode="simulation",
        action_kind="simulate_failover",
        status="applied",
    )

    await consolidate_incident(incident_id=inc_id, tenant_id="acme")
    await consolidate_incident(incident_id=inc_id, tenant_id="acme")

    admin_db = get_tenant_db("acme", admin=True)
    graph = admin_db.select_graph(graph_names().memory_for("acme"))
    count = graph.ro_query(
        "MATCH (ep:IncidentEpisode {incident_id: $id}) RETURN count(ep)",
        {"id": inc_id},
    ).result_set[0][0]
    assert count == 1


@pytest.mark.asyncio
async def test_prior_facts_alter_recommendation():
    """Verify that an active SemanticFact derived from prior incidents alters future route selection."""
    inc_id = f"inc-fact-{uuid.uuid4().hex[:8]}"
    rejected = [
        {
            "route": ["checkout", "queue", "payments-backup"],
            "cost": 8,
            "reason": "Queue connection dropped packets under load",
        }
    ]
    _seed_authoritative_incident(
        inc_id,
        tenant_id="acme",
        service_id="payments-primary",
        execution_mode="simulation",
        action_kind="simulate_failover",
        status="applied",
        max_cost=25,
        rejected_routes=rejected,
    )
    await consolidate_incident(incident_id=inc_id, tenant_id="acme")

    # Now run failover_routes with max_cost=25 (which would normally prefer queue cost 8 over premium cost 20)
    result = await failover_routes("checkout", "payments-backup", max_cost=25, tenant="acme")
    assert result.status == "OK"
    recommended = result.data["recommended_route"]
    assert "queue" not in recommended
    assert "premium" in recommended
    assert recommended == ["checkout", "premium", "payments-backup"]


@pytest.mark.asyncio
async def test_concurrent_consolidations_with_barrier(tmp_path):
    """Prove twenty genuinely overlapping client writers preserve distinct records and no cross-tenant leakage."""
    num_workers = 20
    barrier = threading.Barrier(num_workers)
    results: list[dict] = []
    latencies: list[float] = []
    errors: list[Exception] = []

    # Pre-seed incidents in ops_topology so consolidate_incident tests pure consolidation concurrency
    worker_specs = [
        (i, "acme" if i % 2 == 0 else "globex", f"inc-conc-{i}-{uuid.uuid4().hex[:6]}")
        for i in range(num_workers)
    ]
    for _, tenant, inc_id in worker_specs:
        _seed_authoritative_incident(inc_id, tenant_id=tenant, status="applied")

    def worker_thread(worker_id: int, tenant: str, inc_id: str):
        try:
            # Wait at barrier so all 20 threads hit consolidate_incident simultaneously
            barrier.wait(timeout=10)
            t0 = time.perf_counter()
            res = asyncio.run(consolidate_incident(incident_id=inc_id, tenant_id=tenant))
            elapsed = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed)
            results.append((worker_id, tenant, inc_id, res))
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(worker_thread, wid, tnt, iid)
            for wid, tnt, iid in worker_specs
        ]
        for f in futures:
            f.result(timeout=30)

    assert len(errors) == 0
    assert len(results) == num_workers

    # Verify Acme has exactly 10 distinct episodes and Globex has exactly 10 distinct episodes
    acme_db = get_tenant_db("acme", admin=True)
    globex_db = get_tenant_db("globex", admin=True)
    acme_graph = acme_db.select_graph(graph_names().memory_for("acme"))
    globex_graph = globex_db.select_graph(graph_names().memory_for("globex"))

    acme_episodes = acme_graph.ro_query(
        "MATCH (ep:IncidentEpisode) WHERE ep.incident_id STARTS WITH 'inc-conc-' RETURN ep.incident_id"
    ).result_set
    globex_episodes = globex_graph.ro_query(
        "MATCH (ep:IncidentEpisode) WHERE ep.incident_id STARTS WITH 'inc-conc-' RETURN ep.incident_id"
    ).result_set

    acme_ids = [r[0] for r in acme_episodes]
    globex_ids = [r[0] for r in globex_episodes]

    expected_acme = [iid for _, tnt, iid in worker_specs if tnt == "acme"]
    expected_globex = [iid for _, tnt, iid in worker_specs if tnt == "globex"]

    assert len(acme_ids) == len(expected_acme) == 10
    assert len(globex_ids) == len(expected_globex) == 10
    assert set(acme_ids) == set(expected_acme)
    assert set(globex_ids) == set(expected_globex)
    # Zero cross-tenant overlap
    assert set(acme_ids).isdisjoint(set(globex_ids))

    # Record measured latency artifact
    artifact_path = tmp_path / "memory-concurrency.json"
    artifact_path.write_text(
        json.dumps(
            {
                "concurrency": num_workers,
                "barrier_synchronized": True,
                "measured_latencies_ms": sorted(latencies),
                "acme_count": len(acme_ids),
                "globex_count": len(globex_ids),
            },
            indent=2,
        )
    )
