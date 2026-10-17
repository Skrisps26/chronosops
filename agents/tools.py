"""Typed domain tool functions executing graph operations via the official FalkorDB MCP."""
from __future__ import annotations

import asyncio
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
    """Identify failover candidate routes via algo.SPpaths, rejecting routes exceeding max_cost."""
    try:
        validated = FailoverRoutesInput(source=source, target=target, max_cost=max_cost)
    except Exception as e:
        return ToolResult(
            status="ERROR",
            data=str(e),
            trace=ToolTrace(tool="failover_routes", graph=OPS_GRAPH, cypher="", params={"source": source, "target": target, "max_cost": max_cost}),
        )

    client = FalkorDBMcpClient(tenant=tenant)
    if not await check_topology_ready(client):
        return ToolResult(
            status="NOT_READY",
            data="Operational topology is not seeded or marked ready",
            trace=ToolTrace(tool="failover_routes", graph=OPS_GRAPH, cypher=queries.CHECK_READY_QUERY),
        )

    # 1. Fetch unconstrained routes to establish all candidates
    unconstrained_params = {"source": validated.source, "target": validated.target}
    all_candidates, d1 = await client.query_readonly(OPS_GRAPH, queries.FAILOVER_ROUTES_UNCONSTRAINED, unconstrained_params)

    # 2. Fetch constrained routes within max_cost
    constrained_params = {"source": validated.source, "target": validated.target, "max_cost": validated.max_cost}
    safe_candidates, d2 = await client.query_readonly(OPS_GRAPH, queries.FAILOVER_ROUTES_CONSTRAINED, constrained_params)

    total_duration = d1 + d2

    # Separate eligible routes from rejected routes
    safe_paths = [r["route"] for r in safe_candidates if "route" in r]
    rejected_routes = []
    for cand in all_candidates:
        if cand.get("route") not in safe_paths:
            rejected_routes.append({
                "route": cand.get("route"),
                "latency_ms": cand.get("latency_ms"),
                "cost": cand.get("cost"),
                "reason": f"Cost {cand.get('cost')} exceeds safety cap {validated.max_cost}",
            })

    if not safe_candidates:
        return ToolResult(
            status="NO_SAFE_ROUTE",
            data={
                "source": validated.source,
                "target": validated.target,
                "max_cost_cap": validated.max_cost,
                "message": "No eligible route found within search bounds and safety cost cap",
            },
            evidence={"rejected_routes": rejected_routes, "total_candidates_examined": len(all_candidates)},
            trace=ToolTrace(
                tool="failover_routes",
                graph=OPS_GRAPH,
                cypher=queries.FAILOVER_ROUTES_CONSTRAINED.strip(),
                params=constrained_params,
                duration_ms=total_duration,
                algorithm_result={"safe_count": 0, "rejected_count": len(rejected_routes)},
            ),
            bounds=ToolBounds(max_depth=5, limit=5),
        )

    # Pick the lowest latency safe route
    best_safe = safe_candidates[0]

    return ToolResult(
        status="OK",
        data={
            "source": validated.source,
            "target": validated.target,
            "max_cost_cap": validated.max_cost,
            "recommended_route": best_safe.get("route"),
            "latency_ms": best_safe.get("latency_ms"),
            "cost": best_safe.get("cost"),
            "safe_routes_count": len(safe_candidates),
        },
        evidence={
            "safe_routes": safe_candidates,
            "rejected_routes": rejected_routes,
        },
        trace=ToolTrace(
            tool="failover_routes",
            graph=OPS_GRAPH,
            cypher=queries.FAILOVER_ROUTES_CONSTRAINED.strip(),
            params=constrained_params,
            duration_ms=total_duration,
            algorithm_result={"safe_routes": safe_candidates, "rejected_routes": rejected_routes},
        ),
        bounds=ToolBounds(max_depth=5, limit=5),
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
