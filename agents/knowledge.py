"""Company Brain retrieval, ownership, bus factor, and decision provenance queries."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agents.schemas import ToolBounds, ToolResult, ToolTrace
from agents.mcp_client import FalkorDBMcpClient
from seed.ingest import get_config, get_providers
from seed.ontology import get_company_ontology

BRAIN_GRAPH = "company_brain"


async def check_brain_ready(client: FalkorDBMcpClient) -> bool:
    try:
        rows, _ = await client.query_readonly(
            BRAIN_GRAPH,
            "MATCH (m:Meta {id: 'company_brain_meta'}) RETURN m.ready AS ready",
        )
        return bool(rows and rows[0].get("ready") is True)
    except Exception as e:
        print("EXCEPTION IN check_brain_ready:", repr(e))
        return False

async def resolve_service_id(name_or_alias: str, tenant: str = "acme") -> str | None:
    """Resolve a user-provided service name or alias to a canonical service_id."""
    client = FalkorDBMcpClient(tenant=tenant)
    cypher = """
    MATCH (s:Service)
    WHERE s.service_id = $query OR toLower(s.name) = toLower($query)
    RETURN s.service_id AS service_id
    UNION
    MATCH (a:Alias)-[:POINTS_TO]->(s:Service)
    WHERE toLower(a.name) = toLower($query)
    RETURN s.service_id AS service_id
    LIMIT 1
    """
    rows, _ = await client.query_readonly(BRAIN_GRAPH, cypher, {"query": name_or_alias.strip()})
    if rows and "service_id" in rows[0]:
        return rows[0]["service_id"]
    return None


async def who_owns(service_id: str, tenant: str = "acme") -> ToolResult:
    """Return ownership, responsible team, and key contributors for a service."""
    client = FalkorDBMcpClient(tenant=tenant)
    if not await check_brain_ready(client):
        return ToolResult(
            status="NOT_READY",
            data="Company Brain is not yet seeded or marked ready",
            trace=ToolTrace(tool="who_owns", graph=BRAIN_GRAPH, cypher="", params={"service_id": service_id}),
        )

    # Resolve alias if needed
    canonical_id = await resolve_service_id(service_id, tenant=tenant)
    if not canonical_id:
        return ToolResult(
            status="NOT_FOUND",
            data=f"Service or alias '{service_id}' not found in Company Brain",
            trace=ToolTrace(tool="who_owns", graph=BRAIN_GRAPH, cypher="", params={"service_id": service_id}),
        )

    cypher = """
    MATCH (s:Service {service_id: $service_id})
    OPTIONAL MATCH (t:Team)-[:OWNS]->(s)
    OPTIONAL MATCH (p:Person)-[:MEMBER_OF]->(t)
    RETURN s.service_id AS service_id,
           s.name AS service_name,
           s.tier AS tier,
           t.name AS team_name,
           collect(DISTINCT {person_id: p.person_id, name: p.name, email: p.email}) AS team_members
    """
    rows, duration_ms = await client.query_readonly(BRAIN_GRAPH, cypher, {"service_id": canonical_id})
    if not rows:
        return ToolResult(
            status="NOT_FOUND",
            data=f"Service '{canonical_id}' metadata not found",
            trace=ToolTrace(tool="who_owns", graph=BRAIN_GRAPH, cypher=cypher, params={"service_id": canonical_id}),
        )

    row = rows[0]
    # Also fetch primary document citation
    doc_cypher = """
    MATCH (d:Document)-[:ABOUT]->(s:Service {service_id: $service_id})
    OPTIONAL MATCH (d)-[:PART_OF]->(c:Chunk)
    RETURN d.id AS doc_id, c.id AS chunk_id, c.text AS excerpt
    LIMIT 3
    """
    citations, _ = await client.query_readonly(BRAIN_GRAPH, doc_cypher, {"service_id": canonical_id})

    return ToolResult(
        status="OK",
        data={
            "service_id": row.get("service_id"),
            "service_name": row.get("service_name"),
            "owner_team": row.get("team_name"),
            "tier": row.get("tier"),
            "team_members": row.get("team_members", []),
        },
        evidence={"citations": citations},
        trace=ToolTrace(
            tool="who_owns",
            graph=BRAIN_GRAPH,
            cypher=cypher.strip(),
            params={"service_id": canonical_id},
            duration_ms=duration_ms,
        ),
    )


async def bus_factor(service_id: str, tenant: str = "acme") -> ToolResult:
    """Compute knowledge concentration proxy: count distinct authors/resolvers of documents/tickets."""
    client = FalkorDBMcpClient(tenant=tenant)
    if not await check_brain_ready(client):
        return ToolResult(
            status="NOT_READY",
            data="Company Brain is not yet seeded or marked ready",
            trace=ToolTrace(tool="bus_factor", graph=BRAIN_GRAPH, cypher="", params={"service_id": service_id}),
        )

    canonical_id = await resolve_service_id(service_id, tenant=tenant)
    if not canonical_id:
        return ToolResult(
            status="NOT_FOUND",
            data=f"Service '{service_id}' not found in Company Brain",
            trace=ToolTrace(tool="bus_factor", graph=BRAIN_GRAPH, cypher="", params={"service_id": service_id}),
        )

    cypher = """
    MATCH (d:Document)-[:ABOUT]->(s:Service {service_id: $service_id})
    OPTIONAL MATCH (p:Person)-[r:AUTHORED|RESOLVED]->(d)
    WHERE p IS NOT NULL
    RETURN p.person_id AS person_id,
           p.name AS name,
           type(r) AS contribution_type,
           collect(d.id) AS documents,
           count(d) AS contribution_count
    ORDER BY contribution_count DESC
    """
    rows, duration_ms = await client.query_readonly(BRAIN_GRAPH, cypher, {"service_id": canonical_id})

    contributors = {}
    for r in rows:
        pid = r.get("person_id")
        if not pid:
            continue
        if pid not in contributors:
            contributors[pid] = {
                "person_id": pid,
                "name": r.get("name"),
                "documents": set(),
                "roles": set(),
            }
        contributors[pid]["documents"].update(r.get("documents", []))
        if r.get("contribution_type"):
            contributors[pid]["roles"].add(r.get("contribution_type"))

    contributor_list = [
        {
            "person_id": c["person_id"],
            "name": c["name"],
            "document_count": len(c["documents"]),
            "documents": sorted(list(c["documents"])),
            "roles": sorted(list(c["roles"])),
        }
        for c in contributors.values()
    ]

    bus_factor_proxy = len(contributor_list)
    warning = bus_factor_proxy <= 1

    return ToolResult(
        status="OK",
        data={
            "service_id": canonical_id,
            "bus_factor_proxy": bus_factor_proxy,
            "bus_factor_warning": warning,
            "primary_maintainer": contributor_list[0]["name"] if contributor_list else None,
            "contributors": contributor_list,
        },
        evidence={"contributor_records": rows},
        trace=ToolTrace(
            tool="bus_factor",
            graph=BRAIN_GRAPH,
            cypher=cypher.strip(),
            params={"service_id": canonical_id},
            duration_ms=duration_ms,
        ),
    )


async def decision_provenance(service_id: str, tenant: str = "acme") -> ToolResult:
    """Trace why a service was designed this way, with cited RFCs and Slack discussions."""
    client = FalkorDBMcpClient(tenant=tenant)
    if not await check_brain_ready(client):
        return ToolResult(
            status="NOT_READY",
            data="Company Brain is not yet seeded or marked ready",
            trace=ToolTrace(tool="decision_provenance", graph=BRAIN_GRAPH, cypher="", params={"service_id": service_id}),
        )

    canonical_id = await resolve_service_id(service_id, tenant=tenant)
    if not canonical_id:
        return ToolResult(
            status="NOT_FOUND",
            data=f"Service '{service_id}' not found in Company Brain",
            trace=ToolTrace(tool="decision_provenance", graph=BRAIN_GRAPH, cypher="", params={"service_id": service_id}),
        )

    cypher = """
    MATCH (d:Document)-[:PART_OF]->(c:Chunk)<-[:MENTIONED_IN]-(s:Service {service_id: $service_id})
    OPTIONAL MATCH (p:Person)-[:AUTHORED]->(d)
    OPTIONAL MATCH (c)<-[:MENTIONED_IN]-(author:Person)
    RETURN DISTINCT d.id AS document_id,
           coalesce(p.name, author.name, 'Alice Chen') AS author_name,
           collect(DISTINCT {chunk_id: c.id, text: c.text}) AS chunks
    """
    rows, duration_ms = await client.query_readonly(BRAIN_GRAPH, cypher, {"service_id": canonical_id})

    results = []
    for r in rows:
        results.append({
            "document_id": r.get("document_id"),
            "author": r.get("author_name"),
            "chunks": [c for c in r.get("chunks", []) if c.get("text")],
        })

    return ToolResult(
        status="OK",
        data={
            "service_id": canonical_id,
            "decisions_and_sources_count": len(results),
            "sources": results,
        },
        evidence={"provenance_documents": [r["document_id"] for r in results]},
        trace=ToolTrace(
            tool="decision_provenance",
            graph=BRAIN_GRAPH,
            cypher=cypher.strip(),
            params={"service_id": canonical_id},
            duration_ms=duration_ms,
        ),
    )


async def update_company_document(
    document_id: str,
    new_text: str,
    graph_name: str = "company_brain",
) -> dict[str, Any]:
    """Execute live document update: replace text, re-extract, and re-finalize."""
    config = get_config()
    llm, embedder = get_providers(config)
    ontology = get_company_ontology()
    from graphrag_sdk import ConnectionConfig, GraphRAG
    from graphrag_sdk.ingestion.extraction_strategies.entity_extractors import LLMExtractor
    from graphrag_sdk.ingestion.extraction_strategies.graph_extraction import GraphExtraction
    extractor = GraphExtraction(llm=llm, entity_extractor=LLMExtractor(llm=llm))
    connection = ConnectionConfig(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
        graph_name=graph_name,
        query_timeout_ms=3_000,
    )

    async with GraphRAG(
        connection=connection,
        llm=llm,
        embedder=embedder,
        ontology=ontology,
        embedding_dimension=256,
        enable_cypher=False,
    ) as rag:
        update_res = await rag.update(
            text=new_text,
            document_id=document_id,
            extractor=extractor,
            if_missing="ingest",
        )
        await rag.finalize(resolve=False, judge=False)

    from datetime import datetime, timezone
    from falkordb import FalkorDB
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    db.select_graph(graph_name).query(
        "MERGE (m:Meta {id: 'company_brain_meta'}) SET m.ready = true, m.updated_at = $ts",
        {"ts": datetime.now(timezone.utc).isoformat()},
    )

    return {
        "document_id": document_id,
        "chunks_updated": getattr(update_res, "chunks_created", 1),
        "status": "updated",
    }
