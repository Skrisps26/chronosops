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


def get_tenant_db(tenant_id: str, admin: bool = False) -> Any:
    config = get_config()
    tenant = tenant_id.lower()
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
    return f"agent_memory_{tenant_id.lower()}"


async def consolidate_incident(
    incident_id: str,
    tenant_id: str = "acme",
    outcome_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Consolidate completed incident into tenant's isolated episodic & semantic memory graph."""
    db_admin = get_tenant_db(tenant_id, admin=True)
    ops_graph = db_admin.select_graph("ops_topology")
    mem_graph_name = get_memory_graph_name(tenant_id)
    mem_graph = db_admin.select_graph(mem_graph_name)

    now_ts = datetime.now(timezone.utc).isoformat()

    # 1. Fetch incident & approval outcome from ops_topology if not passed directly
    if not outcome_data:
        q_inc = """
        MATCH (a:ApprovalRecord {incident_id: $incident_id, tenant_id: $tenant})
        RETURN a.incident_id AS incident_id,
               a.execution_mode AS mode,
               a.service_id AS service_id,
               a.action_kind AS action_kind,
               a.status AS status,
               a.proposed_action AS proposed_json,
               a.command_id AS command_id
        LIMIT 1
        """
        rows = ops_graph.ro_query(q_inc, {"incident_id": incident_id, "tenant": tenant_id}).result_set
        if not rows:
            raise RuntimeError(f"Incident '{incident_id}' has no matching approval record in ops_topology")
        r = rows[0]
        outcome_data = {
            "incident_id": r[0],
            "execution_mode": r[1],
            "service_id": r[2],
            "action_kind": r[3],
            "status": r[4],
            "proposed_action": json.loads(r[5]) if r[5] else {},
            "command_id": r[6],
        }

    episode_id = f"ep-{incident_id}"
    root_service = outcome_data.get("service_id", "payments-primary")
    mode = outcome_data.get("execution_mode", "simulation")
    action_kind = outcome_data.get("action_kind", "unknown")
    status_val = outcome_data.get("status", "completed")

    # 2. Atomic MERGE of Episode in tenant memory
    mem_graph.query(
        "MERGE (ep:IncidentEpisode {id: $ep_id}) "
        "SET ep.incident_id = $inc_id, "
        "    ep.tenant_id = $tenant, "
        "    ep.execution_mode = $mode, "
        "    ep.root_service = $service, "
        "    ep.action_kind = $action_kind, "
        "    ep.status = $status, "
        "    ep.timestamp = $ts, "
        "    ep.command_id = $cmd_id "
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
            "ts": now_ts,
            "cmd_id": outcome_data.get("command_id", ""),
        },
    )

    # 3. Record DecisionLog & SemanticFact if simulation failover rejected route
    dec_id = f"dec-{incident_id}"
    mem_graph.query(
        "MERGE (d:DecisionLog {id: $dec_id}) "
        "SET d.incident_id = $inc_id, "
        "    d.decision = $action_kind, "
        "    d.timestamp = $ts "
        "WITH d MATCH (ep:IncidentEpisode {id: $ep_id}) "
        "MERGE (ep)-[:RECORDED_DECISION]->(d)",
        {
            "dec_id": dec_id,
            "inc_id": incident_id,
            "action_kind": action_kind,
            "ts": now_ts,
            "ep_id": episode_id,
        },
    )

    if mode == "simulation":
        fact_id = "fact-blocked-premium-route"
        mem_graph.query(
            "MERGE (f:SemanticFact {id: $fact_id}) "
            "SET f.subject = 'checkout', "
            "    f.predicate = 'ROUTES_TO', "
            "    f.object = 'premium', "
            "    f.active = true, "
            "    f.reason = 'Cost 20 exceeds safety cap 12', "
            "    f.topology_version = 'v1.0', "
            "    f.updated_at = $ts "
            "WITH f MATCH (d:DecisionLog {id: $dec_id}) "
            "MERGE (d)-[:BLOCKED_BY]->(f)",
            {
                "fact_id": fact_id,
                "ts": now_ts,
                "dec_id": dec_id,
            },
        )

    # 4. Count successful episodes of this mode and derive procedural workflow if threshold >= 3
    q_count = """
    MATCH (ep:IncidentEpisode {tenant_id: $tenant, execution_mode: $mode})
    WHERE ep.status IN ['applied', 'recovered']
    RETURN count(DISTINCT ep.id) AS success_count
    """
    success_count = mem_graph.query(q_count, {"tenant": tenant_id, "mode": mode}).result_set[0][0]

    workflow_created = False
    if success_count >= 3:
        # Check if workflow subgraph already exists
        wf_id = f"wf-{mode}-{root_service}"
        wf_exists = bool(
            mem_graph.query(
                "MATCH (wf:WorkflowSubgraph {id: $id}) RETURN wf.id",
                {"id": wf_id},
            ).result_set
        )

        if not wf_exists:
            # Create procedural memory graph with sequential NEXT_STEP edges
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
                "    wf.created_at = $ts",
                {
                    "wf_id": wf_id,
                    "mode": mode,
                    "service": root_service,
                    "step_count": len(steps),
                    "ts": now_ts,
                },
            )

            # Create step nodes and NEXT_STEP chain
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
    """Execute fixed procedural playbook without LLM planning calls, enforcing current safety and approval."""
    # Step 1: retrieve memory
    memory = await retrieve_tenant_memory(service_id=service_id, tenant_id=tenant_id)

    # Step 2: blast radius
    from agents.tools import blast_radius
    br_res = await blast_radius(service_id, tenant=tenant_id)

    # Step 3: failover routes (simulation) or restart proposal (real)
    if execution_mode == "simulation":
        from agents.tools import failover_routes
        fo_res = await failover_routes(source="checkout", target="payments-backup", max_cost=max_cost, tenant=tenant_id)
        if fo_res.status != "OK":
            return {"status": "failed", "reason": "No safe route found"}
        rec = fo_res.data
        proposed = {
            "action_kind": "simulate_failover",
            "route": rec.get("recommended_route"),
            "latency_ms": rec.get("latency_ms"),
            "cost": rec.get("cost"),
            "rationale": "Learned deterministic workflow executed without LLM planning.",
        }
    else:
        proposed = {
            "action_kind": "restart_service",
            "service_id": service_id,
            "document_name": "chronosops-demo-StartNginx",
            "rationale": "Learned deterministic workflow executed without LLM planning.",
        }

    # Step 4: Governance validator creates approval record
    from agents.governance import governance_node
    from agents.workflow import IncidentState

    state = IncidentState(
        incident_id=incident_id,
        tenant_id=tenant_id,
        service_id=service_id,
        execution_mode=execution_mode,
        max_cost=max_cost,
        evidence={"memory": memory, "blast_radius": br_res.data},
        proposed_action=proposed,
    )

    gov_res = await governance_node(state)
    return {
        "status": gov_res.get("status", "awaiting_approval"),
        "approval_id": gov_res.get("approval_id"),
        "incident_id": incident_id,
        "procedural_execution": True,
        "llm_calls_made": 0,
        "proposed_action": proposed,
    }
