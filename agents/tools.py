"""Typed domain tool functions executing graph operations via the official FalkorDB MCP."""
from __future__ import annotations

import asyncio
from numbers import Real
from typing import Any

from agents import queries
from agents.schemas import (
    BlastRadiusInput,
    FailoverRoutesInput,
    ServiceQueryInput,
    ToolBounds,
    ToolResult,
    ToolTrace,
)
from agents.mcp_client import FalkorDBMcpClient
from agents.settings import graph_names

OPS_GRAPH = graph_names().topology
BRAIN_GRAPH = graph_names().company


async def check_topology_ready(client: FalkorDBMcpClient) -> bool:
    try:
        rows, _ = await client.query_readonly(OPS_GRAPH, queries.CHECK_READY_QUERY)
        return bool(rows and rows[0].get("ready") is True)
    except Exception:
        return False


async def blast_radius(service_id: str, tenant: str = "acme") -> ToolResult:
    """Compute downstream impact of a service failure using reverse dependency traversal."""
    try:
        validated = BlastRadiusInput(service_id=service_id)
    except Exception as e:
        return ToolResult(
            status="ERROR",
            data=str(e),
            trace=ToolTrace(tool="blast_radius", graph=OPS_GRAPH, cypher="", params={"service_id": service_id}),
        )

    client = FalkorDBMcpClient(tenant=tenant)
    if not await check_topology_ready(client):
        return ToolResult(
            status="NOT_READY",
            data="Operational topology is not seeded or marked ready",
            trace=ToolTrace(tool="blast_radius", graph=OPS_GRAPH, cypher=queries.CHECK_READY_QUERY),
        )

    # Verify service exists
    params = {"service_id": validated.service_id}
    svc_rows, d1 = await client.query_readonly(OPS_GRAPH, queries.CHECK_SERVICE_EXISTS, params)
    if not svc_rows:
        return ToolResult(
            status="NOT_FOUND",
            data=f"Service '{validated.service_id}' not found in topology",
            trace=ToolTrace(tool="blast_radius", graph=OPS_GRAPH, cypher=queries.CHECK_SERVICE_EXISTS, params=params, duration_ms=d1),
        )

    # 1. Fetch distinct dependent service IDs
    dep_rows, d2 = await client.query_readonly(OPS_GRAPH, queries.BLAST_RADIUS_DEPENDENTS, params)
    affected_ids = sorted([row["service_id"] for row in dep_rows if "service_id" in row])

    # 2. Fetch witness paths
    path_rows, d3 = await client.query_readonly(OPS_GRAPH, queries.BLAST_RADIUS_PATHS, params)

    # 3. Check depth frontier (depth 6)
    frontier_rows, d4 = await client.query_readonly(OPS_GRAPH, queries.BLAST_RADIUS_DEPTH_FRONTIER, params)
    depth_frontier_hit = bool(frontier_rows and frontier_rows[0].get("count", 0) > 0)

    total_duration = d1 + d2 + d3 + d4
    truncated = len(affected_ids) >= 100

    return ToolResult(
        status="OK",
        data={
            "root_service": validated.service_id,
            "affected_count": len(affected_ids),
            "affected_services": affected_ids,
        },
        evidence={
            "witness_paths": path_rows,
            "root_health": svc_rows[0].get("health", "unknown"),
        },
        trace=ToolTrace(
            tool="blast_radius",
            graph=OPS_GRAPH,
            cypher=queries.BLAST_RADIUS_DEPENDENTS.strip(),
            params=params,
            duration_ms=total_duration,
            algorithm_result={"dependent_count": len(affected_ids)},
        ),
        bounds=ToolBounds(
            max_depth=5,
            limit=100,
            truncated=truncated,
            depth_frontier_hit=depth_frontier_hit,
        ),
    )


async def failover_routes(source: str, target: str, max_cost: int, tenant: str = "acme") -> ToolResult:
    """Return a bounded, graph-verified healthy failover route or abstain."""
    try:
        validated = FailoverRoutesInput(source=source, target=target, max_cost=max_cost)
    except Exception as e:
        return ToolResult(
            status="ERROR",
            data=str(e),
            trace=ToolTrace(tool="failover_routes", graph=OPS_GRAPH, cypher="", params={"source": source, "target": target, "max_cost": max_cost}),
        )

    params = {
        "source": validated.source,
        "target": validated.target,
        "max_cost": validated.max_cost,
    }
    try:
        client = FalkorDBMcpClient(tenant=tenant)
    except Exception as exc:
        return ToolResult(
            status="ERROR",
            data=f"Route reader is unavailable: {exc}",
            trace=ToolTrace(tool="failover_routes", graph=OPS_GRAPH, cypher="", params=params),
            bounds=ToolBounds(max_depth=5, limit=5),
        )

    async def read_route_evidence(graph: str, cypher: str, query_params: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, float, str | None]:
        try:
            rows, duration = await client.query_readonly(
                graph,
                cypher,
                query_params,
                timeout_seconds=3.0,
                retries=0,
            )
            return rows, duration, None
        except Exception as exc:
            message = str(exc).lower()
            status = "QUERY_TIMEOUT" if isinstance(exc, TimeoutError) or "timeout" in message or "timed out" in message else "ERROR"
            return None, 0.0, status

    def unavailable(status: str, message: str, cypher: str, duration_ms: float = 0.0) -> ToolResult:
        return ToolResult(
            status=status,
            data=message,
            trace=ToolTrace(
                tool="failover_routes",
                graph=OPS_GRAPH,
                cypher=cypher.strip(),
                params=params,
                duration_ms=duration_ms,
            ),
            bounds=ToolBounds(max_depth=5, limit=5),
        )

    context_rows, context_duration, context_error = await read_route_evidence(
        OPS_GRAPH,
        queries.FAILOVER_ROUTE_CONTEXT,
        {"source": validated.source, "target": validated.target},
    )
    if context_error:
        return unavailable(context_error, "Route topology evidence is unavailable", queries.FAILOVER_ROUTE_CONTEXT)
    if not context_rows:
        return unavailable("NOT_READY", "Operational topology metadata is unavailable", queries.FAILOVER_ROUTE_CONTEXT, context_duration)
    if len(context_rows) != 1:
        return unavailable("EVIDENCE_INSUFFICIENT", "Route endpoint evidence is ambiguous", queries.FAILOVER_ROUTE_CONTEXT, context_duration)

    context = context_rows[0]
    topology_version = context.get("topology_version")
    if context.get("topology_ready") is not True or not isinstance(topology_version, str) or not topology_version:
        return unavailable("NOT_READY", "Operational topology is not seeded with a current version", queries.FAILOVER_ROUTE_CONTEXT, context_duration)
    missing_endpoints = [
        service_id
        for service_id, observed in (
            (validated.source, context.get("source_id")),
            (validated.target, context.get("target_id")),
        )
        if observed != service_id
    ]
    if missing_endpoints:
        return ToolResult(
            status="NOT_FOUND",
            data={
                "source": validated.source,
                "target": validated.target,
                "missing_endpoints": missing_endpoints,
            },
            evidence={"topology_version": topology_version},
            trace=ToolTrace(
                tool="failover_routes",
                graph=OPS_GRAPH,
                cypher=queries.FAILOVER_ROUTE_CONTEXT.strip(),
                params={"source": validated.source, "target": validated.target},
                duration_ms=context_duration,
            ),
            bounds=ToolBounds(max_depth=5, limit=5),
        )

    candidate_rows, candidate_duration, candidate_error = await read_route_evidence(
        OPS_GRAPH,
        queries.FAILOVER_ROUTE_CANDIDATES,
        {"source": validated.source, "target": validated.target},
    )
    if candidate_error:
        return unavailable(candidate_error, "Bounded route evidence is unavailable", queries.FAILOVER_ROUTE_CANDIDATES, context_duration)
    assert candidate_rows is not None
    for row in candidate_rows:
        if row.get("topology_version") != topology_version:
            return unavailable(
                "EVIDENCE_INSUFFICIENT",
                "Topology changed while route evidence was being collected",
                queries.FAILOVER_ROUTE_CANDIDATES,
                context_duration + candidate_duration,
            )

    memory_rows, memory_duration, memory_error = await read_route_evidence(
        graph_names().memory_for(tenant),
        queries.MEMORY_ROUTE_EXCLUSIONS,
        {},
    )
    if memory_error:
        return unavailable(
            memory_error,
            "Tenant-private route exclusion evidence is unavailable",
            queries.MEMORY_ROUTE_EXCLUSIONS,
            context_duration + candidate_duration,
        )
    assert memory_rows is not None
    if len(memory_rows) == 100:
        return unavailable(
            "EVIDENCE_INSUFFICIENT",
            "Tenant-private route exclusion evidence reached its fixed safety bound",
            queries.MEMORY_ROUTE_EXCLUSIONS,
            context_duration + candidate_duration + memory_duration,
        )

    compatible_facts: list[dict[str, Any]] = []
    incompatible_facts: list[dict[str, Any]] = []
    excluded_nodes: dict[str, list[str]] = {}
    excluded_edges: dict[tuple[str, str], list[str]] = {}
    source_versions: dict[str, str] = {}
    for fact in memory_rows:
        fact_id = fact.get("fact_id")
        fact_version = fact.get("topology_version")
        fact_max_cost = fact.get("max_cost")
        source_version = fact.get("source_version")
        fact_evidence = {
            "fact_id": fact_id,
            "subject": fact.get("subject"),
            "predicate": fact.get("predicate"),
            "object": fact.get("object"),
            "reason": fact.get("reason"),
            "topology_version": fact_version,
            "max_cost": fact_max_cost,
            "source_version": source_version,
        }
        if (
            not isinstance(fact_id, str)
            or not fact_id
            or fact_version != topology_version
            or fact_max_cost != validated.max_cost
            or source_version != topology_version
        ):
            incompatible_facts.append({**fact_evidence, "compatibility": "incompatible_version_or_cap"})
            continue

        predicate = fact.get("predicate")
        subject = fact.get("subject")
        object_ = fact.get("object")
        if predicate == "ROUTES_TO" and isinstance(subject, str) and isinstance(object_, str):
            compatible_facts.append({**fact_evidence, "compatibility": "compatible_edge_exclusion"})
            excluded_edges.setdefault((subject, object_), []).append(fact_id)
            source_versions[fact_id] = source_version
        elif predicate == "EXCLUDES_NODE" and isinstance(object_, str):
            compatible_facts.append({**fact_evidence, "compatibility": "compatible_node_exclusion"})
            excluded_nodes.setdefault(object_, []).append(fact_id)
            source_versions[fact_id] = source_version
        else:
            incompatible_facts.append({**fact_evidence, "compatibility": "unsupported_exclusion_shape"})

    exclusions = {
        "topology_version": topology_version,
        "max_cost": validated.max_cost,
        "source_versions": {"topology": topology_version, "facts": source_versions},
        "excluded_nodes": [
            {"service_id": service_id, "fact_ids": fact_ids}
            for service_id, fact_ids in sorted(excluded_nodes.items())
        ],
        "excluded_edges": [
            {"source": source_id, "target": target_id, "fact_ids": fact_ids}
            for (source_id, target_id), fact_ids in sorted(excluded_edges.items())
        ],
        "compatible_facts": compatible_facts,
        "incompatible_facts": incompatible_facts,
    }

    safe_routes: list[dict[str, Any]] = []
    rejected_routes: list[dict[str, Any]] = []
    for row in candidate_rows:
        route = row.get("route")
        node_health_values = row.get("node_health")
        edge_health_values = row.get("edge_health")
        edge_cost_values = row.get("edge_costs")
        edge_latency_values = row.get("edge_latency_ms")
        if not isinstance(route, list) or not all(isinstance(node, str) and node for node in route):
            rejected_routes.append({"route": route, "reason": "Route graph evidence is malformed"})
            continue

        node_health = [
            {"service_id": service_id, "health": health}
            for service_id, health in zip(route, node_health_values if isinstance(node_health_values, list) else [])
        ]
        edge_health = [
            {
                "source": route[index],
                "target": route[index + 1],
                "health": health,
                "cost": cost,
                "latency_ms": latency,
            }
            for index, (health, cost, latency) in enumerate(
                zip(
                    edge_health_values if isinstance(edge_health_values, list) else [],
                    edge_cost_values if isinstance(edge_cost_values, list) else [],
                    edge_latency_values if isinstance(edge_latency_values, list) else [],
                )
            )
        ]
        route_evidence = {
            "route": route,
            "nodes": node_health,
            "edges": edge_health,
            "node_health": len(node_health) == len(route) and all(node["health"] == "healthy" for node in node_health),
            "edge_health": len(edge_health) == len(route) - 1 and all(edge["health"] == "healthy" for edge in edge_health),
            "reported_cost": row.get("cost"),
            "reported_latency_ms": row.get("latency_ms"),
        }
        expected_edge_count = len(route) - 1
        malformed = (
            len(route) < 2
            or route[0] != validated.source
            or route[-1] != validated.target
            or len(node_health) != len(route)
            or len(edge_health) != expected_edge_count
            or any(not isinstance(edge["cost"], int) or isinstance(edge["cost"], bool) for edge in edge_health)
            or any(not isinstance(edge["latency_ms"], Real) or isinstance(edge["latency_ms"], bool) for edge in edge_health)
            or not isinstance(row.get("cost"), int)
            or isinstance(row.get("cost"), bool)
            or not isinstance(row.get("latency_ms"), Real)
            or isinstance(row.get("latency_ms"), bool)
        )
        if malformed:
            rejected_routes.append({**route_evidence, "reason": "Route graph evidence is incomplete"})
            continue

        actual_cost = sum(edge["cost"] for edge in edge_health)
        actual_latency = sum(edge["latency_ms"] for edge in edge_health)
        route_evidence["cost"] = actual_cost
        route_evidence["latency_ms"] = actual_latency
        if actual_cost != row["cost"] or actual_latency != row["latency_ms"]:
            rejected_routes.append({**route_evidence, "reason": "Algorithm totals do not match graph edge evidence"})
            continue
        unhealthy_nodes = [node for node in node_health if node["health"] != "healthy"]
        if unhealthy_nodes:
            rejected_routes.append({**route_evidence, "reason": "Route contains unhealthy or unknown node health", "unhealthy_nodes": unhealthy_nodes})
            continue
        unhealthy_edges = [edge for edge in edge_health if edge["health"] != "healthy"]
        if unhealthy_edges:
            rejected_routes.append({**route_evidence, "reason": "Route contains unhealthy or unknown edge health", "unhealthy_edges": unhealthy_edges})
            continue
        if actual_cost > validated.max_cost:
            rejected_routes.append({**route_evidence, "reason": f"Cost {actual_cost} exceeds safety cap {validated.max_cost}"})
            continue

        matched_nodes = [
            {"service_id": node, "fact_ids": excluded_nodes[node]}
            for node in route
            if node in excluded_nodes
        ]
        matched_edges = [
            {"source": left, "target": right, "fact_ids": excluded_edges[(left, right)]}
            for left, right in zip(route, route[1:])
            if (left, right) in excluded_edges
        ]
        if matched_nodes or matched_edges:
            rejected_routes.append(
                {
                    **route_evidence,
                    "reason": "Route is excluded by compatible tenant-private memory",
                    "exclusion_matches": {"nodes": matched_nodes, "edges": matched_edges},
                }
            )
            continue
        safe_routes.append({**route_evidence, "exclusion_matches": {"nodes": [], "edges": []}})

    total_duration = context_duration + candidate_duration + memory_duration
    evidence = {
        "topology_version": topology_version,
        "endpoint_health": {
            "source": {"service_id": validated.source, "health": context.get("source_health")},
            "target": {"service_id": validated.target, "health": context.get("target_health")},
        },
        "safe_routes": safe_routes,
        "rejected_routes": rejected_routes,
        "exclusions": exclusions,
        "total_candidates_examined": len(candidate_rows),
    }
    bounds = ToolBounds(max_depth=5, limit=5, truncated=len(candidate_rows) == 5)
    if not safe_routes:
        return ToolResult(
            status="NO_SAFE_ROUTE",
            data={
                "source": validated.source,
                "target": validated.target,
                "max_cost_cap": validated.max_cost,
                "topology_version": topology_version,
                "message": "No eligible route found within the first five bounded candidates",
            },
            evidence=evidence,
            trace=ToolTrace(
                tool="failover_routes",
                graph=OPS_GRAPH,
                cypher=queries.FAILOVER_ROUTE_CANDIDATES.strip(),
                params={"source": validated.source, "target": validated.target},
                duration_ms=total_duration,
                algorithm_result={"candidates_examined": len(candidate_rows), "safe_count": 0},
            ),
            bounds=bounds,
        )

    best_safe = min(
        safe_routes,
        key=lambda route: (route["latency_ms"], route["cost"], tuple(route["route"])),
    )
    return ToolResult(
        status="OK",
        data={
            "source": validated.source,
            "target": validated.target,
            "max_cost_cap": validated.max_cost,
            "recommended_route": best_safe["route"],
            "latency_ms": best_safe["latency_ms"],
            "cost": best_safe["cost"],
            "topology_version": topology_version,
            "safe_routes_count": len(safe_routes),
        },
        evidence=evidence,
        trace=ToolTrace(
            tool="failover_routes",
            graph=OPS_GRAPH,
            cypher=queries.FAILOVER_ROUTE_CANDIDATES.strip(),
            params={"source": validated.source, "target": validated.target},
            duration_ms=total_duration,
            algorithm_result={"candidates_examined": len(candidate_rows), "safe_count": len(safe_routes)},
        ),
        bounds=bounds,
    )


async def choke_points(tenant: str = "acme") -> ToolResult:
    """Identify architectural choke points using betweenness centrality on the dependency graph."""
    client = FalkorDBMcpClient(tenant=tenant)
    if not await check_topology_ready(client):
        return ToolResult(
            status="NOT_READY",
            data="Operational topology is not seeded or marked ready",
            trace=ToolTrace(tool="choke_points", graph=OPS_GRAPH, cypher=queries.CHECK_READY_QUERY),
        )

    rows, duration_ms = await client.query_readonly(OPS_GRAPH, queries.CHOKE_POINTS_BETWEENNESS)
    return ToolResult(
        status="OK",
        data={"top_choke_points": rows, "algorithm": "betweenness_centrality (sampled, seed=42)"},
        evidence={"ranking": rows},
        trace=ToolTrace(
            tool="choke_points",
            graph=OPS_GRAPH,
            cypher=queries.CHOKE_POINTS_BETWEENNESS.strip(),
            duration_ms=duration_ms,
            algorithm_result=rows,
        ),
        bounds=ToolBounds(limit=10),
    )


async def who_owns(service_id: str, tenant: str = "acme") -> ToolResult:
    """Return ownership context for a service (Module 3 Company Brain integration)."""
    from agents.knowledge import who_owns as kb_who_owns
    return await kb_who_owns(service_id=service_id, tenant=tenant)


async def bus_factor(service_id: str, tenant: str = "acme") -> ToolResult:
    """Compute knowledge concentration proxy (Module 3 Company Brain integration)."""
    from agents.knowledge import bus_factor as kb_bus_factor
    return await kb_bus_factor(service_id=service_id, tenant=tenant)


async def decision_provenance(service_id: str, tenant: str = "acme") -> ToolResult:
    """Return cited architectural decision trail (Module 3 Company Brain integration)."""
    from agents.knowledge import decision_provenance as kb_decision_provenance
    return await kb_decision_provenance(service_id=service_id, tenant=tenant)
