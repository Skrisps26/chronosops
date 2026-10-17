"""Module 5 verification suite: episodic/semantic memory, procedural workflows, tenant isolation, and concurrency."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
import pytest
from falkordb import FalkorDB

from agents.memory import (
    consolidate_incident,
    execute_learned_procedural_workflow,
    get_tenant_db,
    is_workflow_learned,
    retrieve_tenant_memory,
)
from agents.settings import graph_names


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_DIR = ROOT / "artifacts"


@pytest.mark.asyncio
async def test_incident_consolidation_and_new_session_recall():
    """Verify episodic memory persists and is retrieved in a fresh session without chat history."""
    inc_id = f"inc-mem-{uuid.uuid4().hex[:8]}"
    outcome = {
        "incident_id": inc_id,
        "execution_mode": "simulation",
        "service_id": "payments-primary",
        "action_kind": "simulate_failover",
        "status": "applied",
        "proposed_action": {"route": ["checkout", "queue", "payments-backup"], "cost": 8},
    }

    # 1. Consolidate into acme memory
    res = await consolidate_incident(incident_id=inc_id, tenant_id="acme", outcome_data=outcome)
    assert res["status"] == "consolidated"
    assert res["episode_id"] == f"ep-{inc_id}"

    # 2. Fresh session retrieval (zero conversation messages)
    memory = await retrieve_tenant_memory(service_id="payments-primary", tenant_id="acme")
    assert memory["tenant_id"] == "acme"
    assert memory["episodes_count"] >= 1

    ep_ids = [e["incident_id"] for e in memory["episodes"]]
    assert inc_id in ep_ids

    # 3. Verify suppressed rejection / semantic fact was retrieved
    facts = memory["active_facts"]
    assert any(f["subject"] == "checkout" and f["object"] == "premium" for f in facts)


@pytest.mark.asyncio
async def test_tenant_isolation_boundary():
    """Verify tenant Globex cannot observe Acme episodes, and restricted DB user is denied access."""
    inc_id = f"inc-iso-{uuid.uuid4().hex[:8]}"
    outcome = {
        "incident_id": inc_id,
        "execution_mode": "simulation",
        "service_id": "payments-primary",
        "action_kind": "simulate_failover",
        "status": "applied",
    }
    await consolidate_incident(incident_id=inc_id, tenant_id="acme", outcome_data=outcome)

    # 1. Globex memory retrieval returns zero Acme episodes
    globex_mem = await retrieve_tenant_memory(service_id="payments-primary", tenant_id="globex")
    assert inc_id not in [e["incident_id"] for e in globex_mem["episodes"]]

    # 2. Direct restricted reader DB access denial
    globex_db = get_tenant_db("globex", admin=False)
    with pytest.raises(Exception) as exc_info:
        globex_db.select_graph(graph_names().memory_for("acme")).ro_query("RETURN 1")
    err = str(exc_info.value).lower()
    assert "noperm" in err or "permission" in err


@pytest.mark.asyncio
async def test_procedural_workflow_formation_after_three_successes():
    """Verify that 3 successful incidents create a procedural WorkflowSubgraph with NEXT_STEP edges."""
    service_id = f"svc-wf-{uuid.uuid4().hex[:6]}"

    # Consolidate 3 distinct successful incidents
    for i in range(3):
        inc_id = f"inc-wf-{service_id}-{i}"
        outcome = {
            "incident_id": inc_id,
            "execution_mode": "simulation",
            "service_id": service_id,
            "action_kind": "simulate_failover",
            "status": "applied",
        }
        res = await consolidate_incident(incident_id=inc_id, tenant_id="acme", outcome_data=outcome)

    # Verify workflow is marked learned
    assert is_workflow_learned(service_id=service_id, execution_mode="simulation", tenant_id="acme") is True

    # Inspect NEXT_STEP graph structure
    admin_db = get_tenant_db("acme", admin=True)
    graph = admin_db.select_graph(graph_names().memory_for("acme"))
    edges = graph.ro_query(
        "MATCH (p:WorkflowStep)-[r:NEXT_STEP]->(c:WorkflowStep) "
        "WHERE p.workflow_id = $wf_id "
        "RETURN p.name, c.name ORDER BY p.order",
        {"wf_id": f"wf-simulation-{service_id}"},
    ).result_set
    assert len(edges) >= 5

    # Execute learned procedural workflow (zero LLM calls)
    playbook_res = await execute_learned_procedural_workflow(
        incident_id=f"inc-playbook-{service_id}",
        service_id=service_id,
        tenant_id="acme",
        execution_mode="simulation",
        max_cost=12,
    )
    assert playbook_res["procedural_execution"] is True
    assert playbook_res["llm_calls_made"] == 0
    assert playbook_res["status"] == "awaiting_approval"
    assert playbook_res["approval_id"] is not None


@pytest.mark.asyncio
async def test_duplicate_consolidation_idempotency():
    """Verify that consolidating the same incident twice does not duplicate episode nodes."""
    inc_id = f"inc-dup-{uuid.uuid4().hex[:8]}"
    outcome = {
        "incident_id": inc_id,
        "execution_mode": "simulation",
        "service_id": "payments-primary",
        "action_kind": "simulate_failover",
        "status": "applied",
    }

    await consolidate_incident(incident_id=inc_id, tenant_id="acme", outcome_data=outcome)
    await consolidate_incident(incident_id=inc_id, tenant_id="acme", outcome_data=outcome)

    admin_db = get_tenant_db("acme", admin=True)
    graph = admin_db.select_graph(graph_names().memory_for("acme"))
    count = graph.ro_query(
        "MATCH (ep:IncidentEpisode {incident_id: $id}) RETURN count(ep)",
        {"id": inc_id},
    ).result_set[0][0]
    assert count == 1


@pytest.mark.asyncio
async def test_concurrent_consolidations_proof(tmp_path):
    """Record synthetic consolidation timings; overlapping-write proof is pending."""
    latencies: list[float] = []

    async def single_worker(worker_id: int, tenant: str):
        t0 = time.perf_counter()
        inc_id = f"inc-conc-{tenant}-{worker_id}-{uuid.uuid4().hex[:6]}"
        outcome = {
            "incident_id": inc_id,
            "execution_mode": "simulation",
            "service_id": "payments-primary",
            "action_kind": "simulate_failover",
            "status": "applied",
        }
        res = await consolidate_incident(incident_id=inc_id, tenant_id=tenant, outcome_data=outcome)
        elapsed = (time.perf_counter() - t0) * 1000.0
        latencies.append(elapsed)
        return res

    tasks = []
    # 10 workers for acme, 10 workers for globex
    for i in range(10):
        tasks.append(single_worker(i, "acme"))
        tasks.append(single_worker(i, "globex"))

    results = await asyncio.gather(*tasks)
    assert len(results) == 20
    assert all(r["status"] == "consolidated" for r in results)

    # Compute p50 and p95 latency
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95_idx = int(len(latencies) * 0.95)
    p95 = latencies[min(p95_idx, len(latencies) - 1)]

    # Record latency artifact
    concurrency_artifact = {
        "total_operations": len(results),
        "tenants": ["acme", "globex"],
        "p50_latency_ms": round(p50, 2),
        "p95_latency_ms": round(p95, 2),
        "min_latency_ms": round(latencies[0], 2),
        "max_latency_ms": round(latencies[-1], 2),
        "measured_at": time.time(),
    }
    (tmp_path / "concurrency-module-5.json").write_text(
        json.dumps(concurrency_artifact, indent=2) + "\n", encoding="utf-8"
    )

    assert p50 > 0
    assert p95 >= p50
