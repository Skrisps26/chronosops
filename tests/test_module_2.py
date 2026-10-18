"""Module 2 verification suite: typed graph tools, route rejection, and safety boundaries."""
from __future__ import annotations
import traceback
import pytest

from agents.tools import blast_radius, choke_points, failover_routes, who_owns
from agents.mcp_client import FalkorDBMcpClient
from agents.settings import graph_names



@pytest.mark.asyncio
async def test_blast_radius_exact_affected_services():
    """Verify that failure of payments-primary impacts exactly gateway, checkout, orders, worker."""
    result = await blast_radius("payments-primary")
    assert result.status == "OK"
    data = result.data
    assert data["root_service"] == "payments-primary"
    assert data["affected_count"] == 4
    assert data["affected_services"] == ["checkout", "gateway", "orders", "worker"]
    assert len(result.evidence["witness_paths"]) >= 4
    assert result.bounds.max_depth == 5
    assert result.bounds.depth_frontier_hit is False
    assert result.trace.tool == "blast_radius"
    assert result.trace.duration_ms > 0


@pytest.mark.asyncio
async def test_blast_radius_unknown_service_returns_not_found():
    """Verify that an unknown service returns NOT_FOUND rather than empty safety."""
    result = await blast_radius("nonexistent-service")
    assert result.status == "NOT_FOUND"
    assert "not found" in result.data.lower()


@pytest.mark.asyncio
async def test_failover_routes_rejects_expensive_route_and_selects_safe():
    """Verify that the 20-cost premium route is rejected under cap 12 and the 8-cost queue route is chosen."""
    result = await failover_routes(source="checkout", target="payments-backup", max_cost=12)
    assert result.status == "OK"
    data = result.data
    assert data["recommended_route"] == ["checkout", "queue", "payments-backup"]
    assert data["latency_ms"] == 5
    assert data["cost"] == 8

    evidence = result.evidence
    rejected = evidence["rejected_routes"]
    assert len(rejected) >= 1
    premium_rejected = [r for r in rejected if r["route"] == ["checkout", "premium", "payments-backup"]]
    assert len(premium_rejected) == 1
    assert premium_rejected[0]["cost"] == 20
    assert "exceeds" in premium_rejected[0]["reason"].lower()


@pytest.mark.asyncio
async def test_failover_routes_no_safe_route_when_budget_too_low():
    """Verify that when max_cost is below all candidates (cap 5 < 8), NO_SAFE_ROUTE is returned."""
    result = await failover_routes(source="checkout", target="payments-backup", max_cost=5)
    assert result.status == "NO_SAFE_ROUTE"
    assert result.data["max_cost_cap"] == 5
    assert len(result.evidence["rejected_routes"]) >= 2


@pytest.mark.asyncio
async def test_choke_points_betweenness_ranking():
    """Verify that choke points return top betweenness services."""
    result = await choke_points()
    assert result.status == "OK"
    assert len(result.data["top_choke_points"]) > 0
    assert "betweenness" in result.data["algorithm"]


@pytest.mark.asyncio
async def test_unpublished_company_graph_abstains():
    """Withdrawn graph readiness must prevent an ownership answer."""
    from seed.sources import _graph
    _graph().query("MATCH (m:Meta {id:'company_brain_meta'}) SET m.ready=false")
    result = await who_owns("payments-primary")
    assert result.status == "NOT_READY"
    assert result.evidence == {}

@pytest.mark.asyncio
async def test_injection_syntax_rejected_at_validation():
    """Verify that malicious injection characters are rejected before query execution."""
    result = await blast_radius("payments-primary'; DROP ALL; //")
    assert result.status == "ERROR"


@pytest.mark.asyncio
async def test_mcp_client_reader_cannot_write():
    """Verify that reader credentials reject write operations even if requested."""
    client = FalkorDBMcpClient(tenant="acme")
    with pytest.raises(Exception) as exc_info:
        # Attempt a write query through read-only client
        await client.query_readonly(graph_names().topology, "CREATE (:HackedNode {id: 1})")
    err_str = "".join(traceback.format_exception(exc_info.value)).lower()
    assert "write" in err_str or "not allowed" in err_str or "noperm" in err_str or "permission" in err_str


@pytest.mark.asyncio
async def test_tenant_cross_graph_access_denied():
    """Verify that tenant acme reader credentials cannot query globex private memory."""
    client = FalkorDBMcpClient(tenant="acme")
    with pytest.raises(Exception) as exc_info:
        await client.query_readonly(graph_names().memory_for("globex"), "RETURN 1")
    err_str = "".join(traceback.format_exception(exc_info.value)).lower()
    assert "permission" in err_str or "noperm" in err_str
