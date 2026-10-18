"""Graph-backed safety outcomes for bounded failover selection."""
from __future__ import annotations

import pytest
from falkordb import FalkorDB

from agents.tools import failover_routes
from seed.load import get_config


def _graph(graph_name: str):
    config = get_config()
    return FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    ).select_graph(graph_name)


def _topology_version(graph_name: str) -> str:
    rows = _graph(graph_name).ro_query(
        "MATCH (m:Meta {id: 'ops_topology_meta'}) RETURN m.hash"
    ).result_set
    assert rows and isinstance(rows[0][0], str)
    return rows[0][0]



@pytest.mark.asyncio
@pytest.mark.parametrize("source,target", [("unknown-service", "payments-backup"), ("checkout", "unknown-service")])
async def test_unknown_route_endpoint_is_not_empty_safe(source: str, target: str):
    result = await failover_routes(source, target, max_cost=12)

    assert result.status == "NOT_FOUND"
    assert "unknown-service" in result.data["missing_endpoints"]
    assert "recommended_route" not in result.data


@pytest.mark.asyncio
async def test_unhealthy_queue_node_is_never_recommended_at_cap_twelve(isolated_graphs):
    _graph(isolated_graphs.topology).query(
        "MATCH (s:Service {service_id: 'queue'}) SET s.health = 'unhealthy'"
    )

    result = await failover_routes("checkout", "payments-backup", max_cost=12)

    assert result.status == "NO_SAFE_ROUTE"
    queue_rejections = [
        route for route in result.evidence["rejected_routes"]
        if route.get("route") == ["checkout", "queue", "payments-backup"]
    ]
    assert queue_rejections
    assert queue_rejections[0]["unhealthy_nodes"][0]["service_id"] == "queue"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "MATCH (s:Service {service_id: 'queue'}) REMOVE s.health",
        "MATCH (:Service {service_id: 'checkout'})-[r:ROUTES_TO]->(:Service {service_id: 'queue'}) REMOVE r.health",
    ],
)
async def test_unknown_node_or_edge_health_is_not_eligible(isolated_graphs, mutation: str):
    _graph(isolated_graphs.topology).query(mutation)

    result = await failover_routes("checkout", "payments-backup", max_cost=12)

    assert result.status == "NO_SAFE_ROUTE"
    queue_rejection = next(
        route for route in result.evidence["rejected_routes"]
        if route.get("route") == ["checkout", "queue", "payments-backup"]
    )
    assert not queue_rejection["node_health"] or not queue_rejection["edge_health"]


@pytest.mark.asyncio
async def test_graph_edge_cost_is_recomputed_before_cap_selection(isolated_graphs):
    _graph(isolated_graphs.topology).query(
        "MATCH (:Service {service_id: 'checkout'})-[r:ROUTES_TO]->(:Service {service_id: 'queue'}) SET r.cost = 13"
    )

    result = await failover_routes("checkout", "payments-backup", max_cost=12)

    assert result.status == "NO_SAFE_ROUTE"
    queue_rejection = next(
        route for route in result.evidence["rejected_routes"]
        if route.get("route") == ["checkout", "queue", "payments-backup"]
    )
    assert queue_rejection["cost"] == 17


@pytest.mark.asyncio
async def test_only_current_versioned_private_exclusions_block_routes(isolated_graphs):
    topology_version = _topology_version(isolated_graphs.topology)
    memory = _graph(isolated_graphs.memory_for("acme"))
    memory.query(
        "MERGE (f:SemanticFact {id: 'exclude-current-queue'}) "
        "SET f.active = true, f.subject = 'checkout', f.predicate = 'ROUTES_TO', f.object = 'queue', "
        "f.reason = 'controlled exclusion', f.topology_version = $version, f.max_cost = 12, f.source_version = $version",
        {"version": topology_version},
    )

    excluded = await failover_routes("checkout", "payments-backup", max_cost=12)

    assert excluded.status == "NO_SAFE_ROUTE"
    queue_rejection = next(
        route for route in excluded.evidence["rejected_routes"]
        if route.get("route") == ["checkout", "queue", "payments-backup"]
    )
    assert queue_rejection["exclusion_matches"]["edges"]
    assert excluded.evidence["exclusions"]["topology_version"] == topology_version
    assert excluded.evidence["exclusions"]["excluded_edges"] == [
        {"source": "checkout", "target": "queue", "fact_ids": ["exclude-current-queue"]}
    ]

    memory.query(
        "MATCH (f:SemanticFact {id: 'exclude-current-queue'}) SET f.topology_version = 'stale-topology'"
    )
    current = await failover_routes("checkout", "payments-backup", max_cost=12)

    assert current.status == "OK"
    assert current.data["recommended_route"] == ["checkout", "queue", "payments-backup"]
    assert current.data["topology_version"] == topology_version
    selected = next(
        route for route in current.evidence["safe_routes"]
        if route["route"] == current.data["recommended_route"]
    )
    assert selected["cost"] == current.data["cost"] == 8
    assert selected["node_health"] is True
    assert selected["edge_health"] is True
    assert [edge["cost"] for edge in selected["edges"]] == [4, 4]
    assert current.evidence["exclusions"]["incompatible_facts"][0]["fact_id"] == "exclude-current-queue"


@pytest.mark.asyncio
async def test_missing_current_topology_metadata_abstains(isolated_graphs):
    _graph(isolated_graphs.topology).query(
        "MATCH (m:Meta {id: 'ops_topology_meta'}) SET m.hash = ''"
    )

    result = await failover_routes("checkout", "payments-backup", max_cost=12)

    assert result.status == "NOT_READY"


@pytest.mark.asyncio
async def test_bounded_first_five_exhaustion_does_not_claim_global_impossibility(isolated_graphs):
    topology = _graph(isolated_graphs.topology)
    topology.query(
        "MATCH (:Service {service_id: 'checkout'})-[r:ROUTES_TO]->(:Service {service_id: 'queue'}) SET r.cost = 20"
    )
    for index in range(4):
        service_id = f"unsafe-route-{index}"
        topology.query(
            "MERGE (s:Service {service_id: $service_id}) "
            "SET s.health = 'healthy' "
            "WITH s MATCH (checkout:Service {service_id: 'checkout'}), (backup:Service {service_id: 'payments-backup'}) "
            "MERGE (checkout)-[first:ROUTES_TO]->(s) "
            "SET first.health = 'healthy', first.cost = 10, first.latency_ms = 1 "
            "MERGE (s)-[second:ROUTES_TO]->(backup) "
            "SET second.health = 'healthy', second.cost = 10, second.latency_ms = 1",
            {"service_id": service_id},
        )
    topology.query(
        "MERGE (s:Service {service_id: 'later-healthy-route'}) "
        "SET s.health = 'healthy' "
        "WITH s MATCH (checkout:Service {service_id: 'checkout'}), (backup:Service {service_id: 'payments-backup'}) "
        "MERGE (checkout)-[first:ROUTES_TO]->(s) "
        "SET first.health = 'healthy', first.cost = 1, first.latency_ms = 100 "
        "MERGE (s)-[second:ROUTES_TO]->(backup) "
        "SET second.health = 'healthy', second.cost = 1, second.latency_ms = 100"
    )

    result = await failover_routes("checkout", "payments-backup", max_cost=12)

    assert result.status == "NO_SAFE_ROUTE"
    assert result.bounds.limit == 5
    assert result.bounds.truncated is True
    assert all(route["route"] != ["checkout", "later-healthy-route", "payments-backup"] for route in result.evidence["safe_routes"])
