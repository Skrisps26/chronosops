"""Module 3 verification suite: Company Brain ownership, bus factor, citations, and live update."""
from __future__ import annotations

import pytest

from agents.knowledge import (
    bus_factor,
    decision_provenance,
    resolve_service_id,
    who_owns,
)


@pytest.mark.asyncio
async def test_alias_resolution_canonical_id():
    """Verify that multiple aliases and exact names resolve to the canonical service_id."""
    assert await resolve_service_id("Payments API") == "payments-primary"
    assert await resolve_service_id("pay-api") == "payments-primary"
    assert await resolve_service_id("payments-primary") == "payments-primary"
    assert await resolve_service_id("Checkout Service") == "checkout"
    assert await resolve_service_id("nonexistent-alias-xyz") is None


@pytest.mark.asyncio
async def test_who_owns_service_and_team():
    """Verify that who_owns resolves team ownership and lead members."""
    res = await who_owns("payments-primary")
    assert res.status == "OK"
    data = res.data
    assert data["service_id"] == "payments-primary"
    assert data["owner_team"] == "Payments"
    members = data["team_members"]
    assert any(m.get("person_id") == "user-alice" or "alice" in str(m.get("name", "")).lower() for m in members)
    assert res.trace.tool == "who_owns"
    assert res.trace.duration_ms > 0


@pytest.mark.asyncio
async def test_who_owns_via_alias():
    """Verify that querying ownership via alias returns identical canonical results."""
    res = await who_owns("pay-api")
    assert res.status == "OK"
    assert res.data["service_id"] == "payments-primary"
    assert res.data["owner_team"] == "Payments"


@pytest.mark.asyncio
async def test_bus_factor_proxy_and_warning():
    """Verify bus factor calculation flags single-contributor dependency risk."""
    res = await bus_factor("payments-primary")
    assert res.status == "OK"
    data = res.data
    assert data["service_id"] == "payments-primary"
    assert data["bus_factor_proxy"] == 1
    assert data["bus_factor_warning"] is True
    assert "alice" in data["primary_maintainer"].lower()
    assert len(data["contributors"]) >= 1


@pytest.mark.asyncio
async def test_decision_provenance_citations():
    """Verify decision provenance traces reasons back to cited RFCs and Slack threads."""
    res = await decision_provenance("payments-primary")
    assert res.status == "OK"
    data = res.data
    doc_ids = [s["document_id"] for s in data["sources"]]
    assert "rfc-001-payments-failover" in doc_ids
    assert "slack-001-failover-budget" in doc_ids

    rfc_source = next(s for s in data["sources"] if s["document_id"] == "rfc-001-payments-failover")
    assert len(rfc_source["chunks"]) >= 1
    assert "failover" in rfc_source["chunks"][0]["text"].lower()


@pytest.mark.asyncio
async def test_nonexistent_service_returns_not_found():
    """Verify that unknown service queries return NOT_FOUND."""
    assert (await who_owns("unknown-service-abc")).status == "NOT_FOUND"
    assert (await bus_factor("unknown-service-abc")).status == "NOT_FOUND"
    assert (await decision_provenance("unknown-service-abc")).status == "NOT_FOUND"


