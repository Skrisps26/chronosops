"""Tenant-isolated persistent episodic, semantic, and procedural graph memory."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from falkordb import FalkorDB

from seed.load import get_config
from agents.settings import graph_names, require_tenant


def get_tenant_db(tenant_id: str, admin: bool = False) -> Any:
    config = get_config()
    tenant = require_tenant(tenant_id)
    upper = tenant.upper()

    if admin:
        username = config.get("FALKORDB_ADMIN_USERNAME", "admin")
        password = config.get("FALKORDB_ADMIN_PASSWORD", "")
    else:
        username = config.get(f"FALKORDB_WRITER_{upper}_USERNAME", f"writer_{tenant}")
        password = config.get(f"FALKORDB_WRITER_{upper}_PASSWORD", "")

    return FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=username,
        password=password,
    )


def get_memory_graph_name(tenant_id: str) -> str:
    return graph_names().memory_for(tenant_id)


async def consolidate_incident(
    incident_id: str,
    tenant_id: str = "acme",
) -> dict[str, Any]:
    """Consolidate completed incident into tenant's isolated episodic & semantic memory graph."""
    db = get_tenant_db(tenant_id, admin=False)
    ops_graph = db.select_graph(graph_names().topology)
    mem_graph_name = get_memory_graph_name(tenant_id)
    mem_graph = db.select_graph(mem_graph_name)
    now_ts = datetime.now(timezone.utc).isoformat()

    # 1. Fetch authoritative incident & approval outcome from ops_topology
    q_inc = """
    MATCH (i:Incident {incident_id: $incident_id, tenant_id: $tenant})
    OPTIONAL MATCH (a:ApprovalRecord {incident_id: $incident_id, tenant_id: $tenant})
    RETURN properties(i) AS inc, properties(a) AS appr
    LIMIT 1
    """
    rows = ops_graph.ro_query(q_inc, {"incident_id": incident_id, "tenant": tenant_id}).result_set
    if not rows:
        raise RuntimeError(f"Incident '{incident_id}' has no matching incident record in ops_topology")
    inc_props = dict(rows[0][0])
    appr_props = dict(rows[0][1]) if rows[0][1] else {}

    status_val = appr_props.get("status") or inc_props.get("status", "unknown")
    root_service = inc_props.get("service_id") or appr_props.get("service_id", "payments-primary")
    mode = inc_props.get("execution_mode") or appr_props.get("execution_mode", "simulation")
    action_kind = appr_props.get("action_kind") or inc_props.get("incident_type", "unknown")
    topology_version = appr_props.get("topology_version") or inc_props.get("topology_version", "")
    max_cost = int(appr_props.get("max_cost") or inc_props.get("max_cost") or 12)
    command_id = appr_props.get("command_id", "")
    ssm_status = appr_props.get("ssm_status", "")
    completed_at = (
        appr_props.get("recovered_at")
        or appr_props.get("applied_at")
        or appr_props.get("outcome_at")
        or inc_props.get("outcome_at")
        or now_ts
    )

    proposed_raw = appr_props.get("proposed_action")
    proposed_action: dict[str, Any] = {}
    if isinstance(proposed_raw, str):
        try:
            proposed_action = json.loads(proposed_raw)
        except Exception:
            pass
    elif isinstance(proposed_raw, dict):
        proposed_action = proposed_raw

    recovery_sample_ids: list[str] = []
    sample_ids_raw = appr_props.get("recovery_sample_ids") or appr_props.get("recovery_sample_ids_json")
    if isinstance(sample_ids_raw, str):
        try:
            recovery_sample_ids = json.loads(sample_ids_raw)
        except Exception:
            pass
    elif isinstance(sample_ids_raw, list):
        recovery_sample_ids = [str(s) for s in sample_ids_raw]

    binding_raw = appr_props.get("deployment_binding_json") or inc_props.get("deployment_binding_json")
    deployment_binding: dict[str, Any] = {}
    if isinstance(binding_raw, str):
        try:
            deployment_binding = json.loads(binding_raw)
        except Exception:
            pass

    document_name = deployment_binding.get("document_name", "")
    document_version = str(deployment_binding.get("document_version", ""))
    document_hash = deployment_binding.get("document_hash", "")

    # Training eligibility determination
    if mode == "real":
        is_eligible = (
            status_val == "recovered"
            and bool(command_id)
            and ssm_status.lower() in ("success", "successful")
            and bool(document_name)
            and bool(document_version)
            and bool(document_hash)
            and len(set(recovery_sample_ids)) >= 3
        )
        runbook_sig = f"{document_name}@{document_version}:{document_hash}"
    elif mode == "simulation":
        is_eligible = (
            status_val == "applied"
            and action_kind == "simulate_failover"
            and bool(proposed_action.get("route"))
            and bool(topology_version)
        )
        runbook_sig = "simulation_v1"
    else:
        is_eligible = False
        runbook_sig = ""

    episode_id = f"ep-{incident_id}"
    dec_id = f"dec-{incident_id}"

    # 2. Atomic MERGE of Episode in tenant memory with immutable chronology
    mem_graph.query(
        "MERGE (ep:IncidentEpisode {id: $ep_id}) "
        "SET ep.incident_id = $inc_id, "
        "    ep.tenant_id = $tenant, "
        "    ep.execution_mode = $mode, "
        "    ep.root_service = $service, "
        "    ep.action_kind = $action_kind, "
        "    ep.status = $status, "
        "    ep.eligible = $eligible, "
        "    ep.topology_version = $topology_version, "
        "    ep.runbook_sig = $runbook_sig, "
        "    ep.command_id = $cmd_id, "
        "    ep.ssm_status = $ssm_status, "
        "    ep.created_at = coalesce(ep.created_at, $now), "
        "    ep.completed_at = coalesce(ep.completed_at, $completed_at), "
        "    ep.timestamp = coalesce(ep.timestamp, $completed_at) "
        "WITH ep "
        "MERGE (s:ServiceRef {service_id: $service}) "
        "MERGE (ep)-[:AFFECTED_SERVICE]->(s)",
        {
            "ep_id": episode_id,
            "inc_id": incident_id,
            "tenant": tenant_id,
            "mode": mode,
            "service": root_service,
            "action_kind": action_kind,
            "status": status_val,
            "eligible": is_eligible,
            "topology_version": topology_version,
            "runbook_sig": runbook_sig,
            "cmd_id": command_id,
            "ssm_status": ssm_status,
            "now": now_ts,
            "completed_at": completed_at,
        },
    )

    # 3. Record DecisionLog
    mem_graph.query(
        "MERGE (d:DecisionLog {id: $dec_id}) "
        "SET d.incident_id = $inc_id, "
        "    d.decision = $action_kind, "
        "    d.status = $status, "
        "    d.timestamp = coalesce(d.timestamp, $completed_at) "
        "WITH d MATCH (ep:IncidentEpisode {id: $ep_id}) "
        "MERGE (ep)-[:RECORDED_DECISION]->(d)",
        {
            "dec_id": dec_id,
            "inc_id": incident_id,
            "action_kind": action_kind,
            "status": status_val,
            "completed_at": completed_at,
            "ep_id": episode_id,
        },
    )

    # 4. Derive SemanticFact exclusions from actual rejected routes
    rejected_raw = appr_props.get("rejected_routes_json") or inc_props.get("rejected_routes_json")
    rejected_routes: list[dict[str, Any]] = []
    if isinstance(rejected_raw, str):
        try:
            rejected_routes = json.loads(rejected_raw)
        except Exception:
            pass
    elif isinstance(rejected_raw, list):
        rejected_routes = rejected_raw

    for item in rejected_routes:
        route = item.get("route")
        reason = item.get("reason", "Excluded by investigation bounds")
        if isinstance(route, list) and len(route) >= 2:
            for src, dst in zip(route, route[1:]):
                fact_id = f"fact-{tenant_id}-{topology_version}-{max_cost}-{src}-{dst}"
                # Supersede older facts with incompatible version or cap
                mem_graph.query(
                    "MATCH (old:SemanticFact {subject: $src, predicate: 'ROUTES_TO', object: $dst, active: true}) "
                    "WHERE old.topology_version <> $topology_version OR old.max_cost <> $max_cost "
                    "SET old.active = false",
                    {"src": src, "dst": dst, "topology_version": topology_version, "max_cost": max_cost},
                )
                mem_graph.query(
                    "MERGE (f:SemanticFact {id: $fact_id}) "
                    "SET f.subject = $src, "
                    "    f.predicate = 'ROUTES_TO', "
                    "    f.object = $dst, "
                    "    f.reason = $reason, "
                    "    f.topology_version = $topology_version, "
                    "    f.source_version = $topology_version, "
                    "    f.max_cost = $max_cost, "
                    "    f.active = true, "
                    "    f.created_at = coalesce(f.created_at, $now), "
                    "    f.updated_at = $now "
                    "WITH f MATCH (d:DecisionLog {id: $dec_id}) "
                    "MERGE (d)-[:BLOCKED_BY]->(f)",
                    {
                        "fact_id": fact_id,
                        "src": src,
                        "dst": dst,
                        "reason": reason,
                        "topology_version": topology_version,
                        "max_cost": max_cost,
                        "now": now_ts,
                        "dec_id": dec_id,
                    },
                )

    # 5. Count distinct eligible episodes matching signature
    q_count = """
    MATCH (ep:IncidentEpisode {
        tenant_id: $tenant,
        root_service: $service,
        execution_mode: $mode,
        action_kind: $action_kind,
        topology_version: $topology_version,
        runbook_sig: $runbook_sig,
        eligible: true
    })
    WHERE ep.status IN ['applied', 'recovered']
    RETURN count(DISTINCT ep.id) AS success_count
    """
    count_rows = mem_graph.query(
        q_count,
        {
            "tenant": tenant_id,
            "service": root_service,
            "mode": mode,
            "action_kind": action_kind,
            "topology_version": topology_version,
            "runbook_sig": runbook_sig,
        },
    ).result_set
    success_count = count_rows[0][0] if count_rows else 0

    workflow_created = False
    if success_count >= 3:
        wf_id = f"wf-{mode}-{root_service}"
        wf_exists = bool(
            mem_graph.query(
                "MATCH (wf:WorkflowSubgraph {id: $id}) RETURN wf.id",
                {"id": wf_id},
            ).result_set
        )

        if not wf_exists:
            if mode == "simulation":
                steps = [
                    ("s1", "retrieve_memory", 1),
                    ("s2", "blast_radius", 2),
                    ("s3", "failover_routes", 3),
                    ("s4", "validate_governance", 4),
                    ("s5", "await_approval", 5),
                    ("s6", "simulate_failover", 6),
                    ("s7", "consolidate", 7),
                ]
            else:
                steps = [
                    ("s1", "retrieve_memory", 1),
                    ("s2", "inspect_health", 2),
                    ("s3", "blast_radius", 3),
                    ("s4", "retrieve_provenance", 4),
                    ("s5", "validate_governance", 5),
                    ("s6", "await_approval", 6),
                    ("s7", "restart_service", 7),
                    ("s8", "verify_health", 8),
                    ("s9", "consolidate", 9),
                ]

            mem_graph.query(
                "MERGE (wf:WorkflowSubgraph {id: $wf_id}) "
                "SET wf.execution_mode = $mode, "
                "    wf.root_service = $service, "
                "    wf.step_count = $step_count, "
                "    wf.topology_version = $topology_version, "
                "    wf.runbook_sig = $runbook_sig, "
                "    wf.created_at = $ts",
                {
                    "wf_id": wf_id,
                    "mode": mode,
                    "service": root_service,
                    "step_count": len(steps),
                    "topology_version": topology_version,
                    "runbook_sig": runbook_sig,
                    "ts": now_ts,
                },
            )

            for i, (sid, name, order) in enumerate(steps):
                step_node_id = f"{wf_id}-{sid}"
                mem_graph.query(
                    "MERGE (s:WorkflowStep {id: $id}) "
                    "SET s.name = $name, s.order = $order, s.workflow_id = $wf_id "
                    "WITH s MATCH (wf:WorkflowSubgraph {id: $wf_id}) "
                    "MERGE (wf)-[:CONTAINS_STEP]->(s)",
                    {"id": step_node_id, "name": name, "order": order, "wf_id": wf_id},
                )
                if i > 0:
                    prev_step_id = f"{wf_id}-{steps[i-1][0]}"
                    mem_graph.query(
                        "MATCH (p:WorkflowStep {id: $prev}), (c:WorkflowStep {id: $curr}) "
                        "MERGE (p)-[:NEXT_STEP]->(c)",
                        {"prev": prev_step_id, "curr": step_node_id},
                    )
            workflow_created = True

    # 6. Idempotently clear memory_pending on source action in ops_topology
    ops_graph.query(
        "MATCH (i:Incident {incident_id: $incident_id, tenant_id: $tenant}) "
        "OPTIONAL MATCH (a:ApprovalRecord {incident_id: $incident_id, tenant_id: $tenant}) "
        "SET i.memory_pending = false, a.memory_pending = false",
        {"incident_id": incident_id, "tenant": tenant_id},
    )

    return {
        "episode_id": episode_id,
        "tenant_id": tenant_id,
        "execution_mode": mode,
        "status": "consolidated",
        "success_count": success_count,
        "workflow_created": workflow_created,
    }


async def retrieve_tenant_memory(
    service_id: str,
    tenant_id: str = "acme",
) -> dict[str, Any]:
    """Retrieve prior episodes, active semantic exclusions, and learned workflows for this tenant."""
    db = get_tenant_db(tenant_id, admin=False)
    mem_graph_name = get_memory_graph_name(tenant_id)
    graph = db.select_graph(mem_graph_name)

    # 1. Fetch prior episodes for service
    q_episodes = """
    MATCH (s:ServiceRef {service_id: $service})<-[:AFFECTED_SERVICE]-(ep:IncidentEpisode)
    RETURN ep.id AS episode_id,
           ep.incident_id AS incident_id,
           ep.execution_mode AS mode,
           ep.action_kind AS action_kind,
           ep.status AS status,
           ep.timestamp AS timestamp
    ORDER BY ep.timestamp DESC
    LIMIT 5
    """
    try:
        ep_rows = graph.ro_query(q_episodes, {"service": service_id}).result_set
        episodes = [
            {
                "episode_id": r[0],
                "incident_id": r[1],
                "execution_mode": r[2],
                "action_kind": r[3],
                "status": r[4],
                "timestamp": r[5],
            }
            for r in ep_rows
        ]
    except Exception:
        episodes = []

    # 2. Fetch active semantic facts (e.g. blocked routes)
    q_facts = """
    MATCH (f:SemanticFact {active: true})
    RETURN f.id AS fact_id,
           f.subject AS subject,
           f.predicate AS predicate,
           f.object AS object,
           f.reason AS reason
    """
    try:
        fact_rows = graph.ro_query(q_facts).result_set
        facts = [
            {
                "fact_id": r[0],
                "subject": r[1],
                "predicate": r[2],
                "object": r[3],
                "reason": r[4],
            }
            for r in fact_rows
        ]
    except Exception:
        facts = []

    # 3. Check learned workflows
    q_wf = """
    MATCH (wf:WorkflowSubgraph {root_service: $service})
    RETURN wf.id AS workflow_id, wf.execution_mode AS mode, wf.step_count AS step_count
    """
    try:
        wf_rows = graph.ro_query(q_wf, {"service": service_id}).result_set
        workflows = [{"workflow_id": r[0], "mode": r[1], "step_count": r[2]} for r in wf_rows]
    except Exception:
        workflows = []

    return {
        "tenant_id": tenant_id,
        "service_id": service_id,
        "episodes_count": len(episodes),
        "episodes": episodes,
        "active_facts": facts,
        "learned_workflows": workflows,
    }


def is_workflow_learned(
    service_id: str,
    execution_mode: str,
    tenant_id: str = "acme",
) -> bool:
    """Check if procedural workflow subgraph exists for service and execution mode."""
    db = get_tenant_db(tenant_id, admin=False)
    graph = db.select_graph(get_memory_graph_name(tenant_id))
    wf_id = f"wf-{execution_mode}-{service_id}"
    try:
        res = graph.ro_query("MATCH (wf:WorkflowSubgraph {id: $id}) RETURN wf.id", {"id": wf_id}).result_set
        return bool(res)
    except Exception:
        return False


async def execute_learned_procedural_workflow(
    incident_id: str,
    service_id: str,
    tenant_id: str = "acme",
    execution_mode: str = "simulation",
    max_cost: int = 12,
) -> dict[str, Any]:
    """Run the deterministic proposal nodes with current evidence and approval."""
    wf_id = f"wf-{execution_mode}-{service_id}"
    db = get_tenant_db(tenant_id, admin=False)
    graph = db.select_graph(get_memory_graph_name(tenant_id))

    rows = graph.ro_query(
        "MATCH (wf:WorkflowSubgraph {id: $id}) "
        "MATCH (wf)-[:CONTAINS_STEP]->(s:WorkflowStep) "
        "OPTIONAL MATCH (s)-[:NEXT_STEP]->(nxt:WorkflowStep) "
        "RETURN s.id AS step_id, s.name AS name, s.order AS order, nxt.id AS next_id, nxt.name AS next_name "
        "ORDER BY s.order ASC",
        {"id": wf_id},
    ).result_set
    if not rows:
        raise RuntimeError(f"Workflow '{wf_id}' not found in tenant memory")

    expected_steps = (
        ["retrieve_memory", "blast_radius", "failover_routes", "validate_governance", "await_approval", "simulate_failover", "consolidate"]
        if execution_mode == "simulation"
        else ["retrieve_memory", "inspect_health", "blast_radius", "retrieve_provenance", "validate_governance", "await_approval", "restart_service", "verify_health", "consolidate"]
    )

    step_records = [
        {"id": r[0], "name": r[1], "order": r[2], "next_id": r[3], "next_name": r[4]}
        for r in rows
    ]
    if len(step_records) != len(expected_steps):
        raise ValueError(f"Workflow step count mismatch: expected {len(expected_steps)}, found {len(step_records)}")

    visited: set[str] = set()
    for i, step in enumerate(step_records):
        if step["name"] != expected_steps[i] or step["order"] != (i + 1):
            raise ValueError(f"Invalid workflow step sequence at order {step['order']}: {step['name']} != {expected_steps[i]}")
        if step["id"] in visited:
            raise ValueError(f"Cyclic workflow detected at step {step['id']}")
        visited.add(step["id"])
        if i < len(step_records) - 1:
            if step["next_name"] != expected_steps[i + 1]:
                raise ValueError(f"Invalid NEXT_STEP edge from {step['name']} to {step['next_name']}, expected {expected_steps[i + 1]}")
        else:
            if step["next_name"] is not None:
                raise ValueError(f"Final step {step['name']} must not have outgoing NEXT_STEP edge")

    from agents.governance import governance_node
    from agents.remediation import remediation_node
    from agents.triage import triage_node
    from agents.workflow import IncidentState, create_incident_workflow

    memory = await retrieve_tenant_memory(service_id=service_id, tenant_id=tenant_id)
    state = IncidentState(
        incident_id=incident_id,
        tenant_id=tenant_id,
        service_id=service_id,
        execution_mode=execution_mode,
        max_cost=max_cost,
        evidence={"memory": memory},
    )
    workflow = create_incident_workflow(triage_node, remediation_node, governance_node)
    result = await workflow.ainvoke(state)
    return {
        "status": result["status"],
        "approval_id": result.get("approval_id"),
        "incident_id": incident_id,
        "procedural_execution": True,
        "llm_calls_made": 0,
        "proposed_action": result["proposed_action"],
    }
