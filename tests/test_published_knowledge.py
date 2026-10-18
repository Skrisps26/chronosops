"""Published-source grounding checks for Company Brain read tools."""
from __future__ import annotations

import pytest
from falkordb import FalkorDB

from agents.knowledge import bus_factor, decision_provenance, resolve_service_id, who_owns
from agents.settings import graph_names
from seed.load import get_config


def _graph():
    config = get_config()
    return FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    ).select_graph(graph_names().company)


def _prepare_canonical_sources(*, include_contribution: bool = True) -> None:
    """Add only application publication records around deterministic SDK-shaped chunks."""
    graph = _graph()
    graph.query("MATCH (c:Contribution) DETACH DELETE c")
    graph.query("MATCH (s:SourceRecord) DETACH DELETE s")
    graph.query(
        "MATCH (s:Service {services__service_id:'payments-primary'}), (t:Team {name:'Payments'}) "
        "MERGE (s)-[r:RELATES {rel_type:'OWNED_BY'}]->(t) "
        "SET r.source_chunk_ids=['chunk-rfc-001-0']"
    )
    graph.query(
        "MATCH (p:Person {people__person_id:'user-alice'}), (t:Team {name:'Payments'}) "
        "MERGE (p)-[:RELATES {rel_type:'MEMBER_OF'}]->(t)"
    )
    graph.query(
        "MATCH (p:Person {people__person_id:'user-carol'}), (t:Team {name:'Checkout'}) "
        "MERGE (p)-[:RELATES {rel_type:'MEMBER_OF'}]->(t)"
    )
    graph.query(
        "MATCH (d:Document {id:'rfc-001-payments-failover'})-[:PART_OF]->(c:Chunk) SET c.index=0 "
        "MERGE (s:SourceRecord {document_id:'rfc-001-payments-failover'}) "
        "SET s.origin='synthetic', s.path='seed/data/docs/rfc-001-payments-failover.md', "
        "s.permalink='', s.content_hash='payments-v1-hash', s.source_version='1.0', "
        "s.status='published', s.author_id='user-alice', s.service_id='payments-primary', "
        "s.revision=1, s.published_revision=1, s.title='Payments failover' "
        "MERGE (s)-[:PUBLISHED_CHUNK]->(c)"
    )
    graph.query(
        "MATCH (d:Document {id:'rfc-002-checkout-resilience'})-[:PART_OF]->(c:Chunk) SET c.index=0 "
        "MERGE (s:SourceRecord {document_id:'rfc-002-checkout-resilience'}) "
        "SET s.origin='synthetic', s.path='seed/data/docs/rfc-002-checkout-resilience.md', "
        "s.permalink='', s.content_hash='checkout-v1-hash', s.source_version='1.0', "
        "s.status='published', s.author_id='user-carol', s.service_id='checkout', "
        "s.revision=1, s.published_revision=1, s.title='Checkout resilience' "
        "MERGE (s)-[:PUBLISHED_CHUNK]->(c)"
    )
    if include_contribution:
        graph.query(
            "MATCH (s:SourceRecord {document_id:'rfc-001-payments-failover'}) "
            "MERGE (c:Contribution {id:'rfc-001-payments-failover:1:user-alice'}) "
            "SET c.document_id=s.document_id, c.revision=1, c.author_id='user-alice', "
            "c.service_id='payments-primary', c.active=true "
            "MERGE (c)-[:CONTRIBUTION_FOR]->(s)"
        )
        graph.query(
            "MATCH (s:SourceRecord {document_id:'rfc-002-checkout-resilience'}) "
            "MERGE (c:Contribution {id:'rfc-002-checkout-resilience:1:user-carol'}) "
            "SET c.document_id=s.document_id, c.revision=1, c.author_id='user-carol', "
            "c.service_id='checkout', c.active=true "
            "MERGE (c)-[:CONTRIBUTION_FOR]->(s)"
        )


@pytest.mark.asyncio
async def test_published_reads_return_current_sdk_chunks_and_source_metadata():
    _prepare_canonical_sources()

    ownership = await who_owns("payments-primary")
    assert ownership.status == "OK"
    assert ownership.data["owner_team"] == "Payments"
    citation = ownership.evidence["citations"][0]
    assert citation["document_id"] == "rfc-001-payments-failover"
    assert citation["content_hash"] == "payments-v1-hash"
    assert citation["source_version"] == "1.0"
    assert citation["revision"] == 1
    assert citation["path"].endswith("rfc-001-payments-failover.md")
    assert citation["chunk_id"] == "chunk-rfc-001-0"
    assert citation["chunk_index"] == 0
    assert citation["excerpt"] in citation["text"]

    provenance = await decision_provenance("payments-primary")
    assert provenance.status == "OK"
    source = provenance.data["sources"][0]
    assert source["document_id"] == "rfc-001-payments-failover"
    assert source["chunks"][0]["text"]


@pytest.mark.asyncio
async def test_bus_factor_uses_current_contributions_not_other_service_or_unknown_authorship():
    _prepare_canonical_sources()
    graph = _graph()
    graph.query(
        "MATCH (s:SourceRecord {document_id:'rfc-001-payments-failover'}) "
        "MERGE (c:Contribution {id:'rfc-001-payments-failover:1:unknown'}) "
        "SET c.document_id=s.document_id, c.revision=1, c.author_id='', "
        "c.service_id='payments-primary', c.active=true "
        "MERGE (c)-[:CONTRIBUTION_FOR]->(s)"
    )

    result = await bus_factor("payments-primary")
    assert result.status == "OK"
    assert result.data["bus_factor_proxy"] == 1
    assert [contributor["person_id"] for contributor in result.data["contributors"]] == ["user-alice"]
    assert result.data["contributors"][0]["documents"] == ["rfc-001-payments-failover"]


@pytest.mark.asyncio
async def test_bus_factor_reports_unknown_attribution_as_unrecorded_not_healthy_distribution():
    _prepare_canonical_sources(include_contribution=False)
    _graph().query(
        "MATCH (s:SourceRecord {document_id:'rfc-001-payments-failover'}) SET s.author_id=''"
    )
    result = await bus_factor("payments-primary")
    assert result.status == "OK"
    assert result.data["bus_factor_proxy"] == 0
    assert result.data["knowledge_concentration"] == "unknown"
    assert result.data["contributor_summary"] == "no recorded contributors"
    assert result.data["primary_maintainer"] is None
    assert result.data["primary_maintainer_status"] == "unknown"
    provenance = await decision_provenance("payments-primary")
    assert provenance.status == "OK"
    assert provenance.data["sources"][0]["author_id"] is None


@pytest.mark.asyncio
async def test_alias_collision_abstains_instead_of_selecting_a_service():
    _prepare_canonical_sources()
    graph = _graph()
    graph.query(
        "MATCH (payments:Service {services__service_id:'payments-primary'}), "
        "(checkout:Service {services__service_id:'checkout'}) "
        "MERGE (alias:Alias {name:'shared-alias'}) "
        "MERGE (alias)-[:POINTS_TO]->(payments) "
        "MERGE (alias)-[:POINTS_TO]->(checkout)"
    )

    assert await resolve_service_id("shared-alias") is None
    result = await who_owns("shared-alias")
    assert result.status == "EVIDENCE_INSUFFICIENT"
    assert "ambiguous" in str(result.data).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "error", "deleted"])
async def test_unavailable_source_revisions_are_not_served_as_current_publication(status: str):
    _prepare_canonical_sources(include_contribution=False)
    graph = _graph()
    graph.query(
        "MATCH (s:SourceRecord {document_id:'rfc-001-payments-failover'}) "
        "OPTIONAL MATCH (s)-[published:PUBLISHED_CHUNK]->() DELETE published "
        "WITH s SET s.status=$status, s.revision=2",
        {"status": status},
    )

    result = await who_owns("payments-primary")
    assert result.status == "NOT_READY"
    assert "unavailable" in str(result.data).lower()


@pytest.mark.asyncio
async def test_model_edges_cannot_override_canonical_owner_or_team_membership():
    _prepare_canonical_sources()
    graph = _graph()
    graph.query(
        "MATCH (s:Service {services__service_id:'payments-primary'}), "
        "(payments:Team {teams__team_id:'team-payments'}), "
        "(checkout:Team {teams__team_id:'team-checkout'}), "
        "(carol:Person {people__person_id:'user-carol'}) "
        "MERGE (s)-[:RELATES {rel_type:'OWNED_BY'}]->(checkout) "
        "MERGE (carol)-[:RELATES {rel_type:'MEMBER_OF'}]->(payments)"
    )
    result = await who_owns("payments-primary")
    assert result.status == "OK"
    assert result.data["owner_team"] == "Payments"
    assert {person["person_id"] for person in result.data["team_members"]} == {
        "user-alice", "user-bob", "user-ivan"
    }


@pytest.mark.asyncio
async def test_citation_result_bound_reports_truncation():
    import hashlib
    _prepare_canonical_sources()
    documents = [
        {"id": f"bounded-{index:03d}", "chunk": f"bounded-chunk-{index}",
         "text": f"Recorded payments decision {index}.",
         "hash": hashlib.sha256(f"Recorded payments decision {index}.".encode()).hexdigest()}
        for index in range(21)
    ]
    _graph().query(
        "UNWIND $documents AS doc "
        "MATCH (service:Service {services__service_id:'payments-primary'}) "
        "CREATE (s:SourceRecord {document_id:doc.id, status:'published', revision:1, "
        "published_revision:1, service_id:'payments-primary', content_hash:doc.hash, source_version:'1'}) "
        "CREATE (d:Document {id:doc.id})-[:PART_OF]->(c:Chunk {id:doc.chunk, index:0, text:doc.text}) "
        "CREATE (s)-[:PUBLISHED_CHUNK]->(c) CREATE (service)-[:MENTIONED_IN]->(c)",
        {"documents": documents},
    )
    result = await decision_provenance("payments-primary")
    assert result.status == "OK"
    assert result.bounds.truncated is True
    assert result.bounds.limit == 20
    assert len(result.evidence["citations"]) == 20
    assert len({citation["document_id"] for citation in result.evidence["citations"]}) == 20


@pytest.mark.asyncio
async def test_duplicate_canonical_nodes_abstain_even_when_external_ids_agree():
    _graph().query(
        "CREATE (:Service {id:'duplicate-sdk-node', services__service_id:'payments-primary', "
        "name:'Conflicting duplicate'})"
    )
    assert await resolve_service_id("payments-primary") is None
    result = await who_owns("payments-primary")
    assert result.status == "EVIDENCE_INSUFFICIENT"


@pytest.mark.asyncio
async def test_contributor_count_deduplicates_external_identity_not_display_names():
    _prepare_canonical_sources()
    _graph().query(
        "CREATE (:Person {people__person_id:'user-alice', name:'Conflicting Alice display name'})"
    )
    result = await bus_factor("payments-primary")
    assert result.status == "OK"
    assert result.data["bus_factor_proxy"] == 1
    assert result.data["contributors"][0]["author_id"] == "user-alice"
    assert result.data["contributors"][0]["name"] is None


@pytest.mark.asyncio
async def test_ownership_cites_its_actual_published_canonical_table_row():
    import hashlib
    raw = "service_id,owner_team\npayments-primary,Payments\ncheckout,Checkout\n"
    graph = _graph()
    graph.query(
        "MATCH (service:Service {services__service_id:'payments-primary'})-"
        "[ownership:RELATES {rel_type:'OWNED_BY'}]->(:Team) "
        "SET ownership.source_chunk_ids=['canonical-service-row'] "
        "MERGE (source:SourceRecord {document_id:'services.csv'}) "
        "SET source.source_kind='canonical_table', source.origin='synthetic', "
        "source.path='seed/data/services.csv', source.status='published', source.revision=1, "
        "source.published_revision=1, source.content_hash=$hash, source.source_version='1' "
        "MERGE (document:Document {id:'services.csv'}) "
        "MERGE (chunk:Chunk {id:'canonical-service-row'}) "
        "SET chunk.index=0, chunk.text='payments-primary,Payments' "
        "MERGE (source)-[:PUBLISHED_CHUNK]->(chunk) "
        "MERGE (document)-[:PART_OF]->(chunk) "
        "MERGE (service)-[:MENTIONED_IN]->(chunk)",
        {"hash": hashlib.sha256(raw.encode()).hexdigest()},
    )
    result = await who_owns("payments-primary")
    assert result.status == "OK"
    assert result.data["owner_team"] == "Payments"
    assert [(citation["document_id"], citation["chunk_id"]) for citation in result.evidence["citations"]] == [
        ("services.csv", "canonical-service-row")
    ]
    assert result.evidence["citations"][0]["source_kind"] == "canonical_table"
    assert result.evidence["citations"][0]["origin"] == "synthetic"
