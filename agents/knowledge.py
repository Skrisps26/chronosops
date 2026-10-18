"""Published-source-grounded Company Brain read tools."""
from __future__ import annotations

from typing import Any

from agents.mcp_client import FalkorDBMcpClient
from agents.schemas import ToolBounds, ToolResult, ToolTrace
from agents.settings import graph_names

_CANONICAL_SERVICE_ID = "services__service_id"
_CANONICAL_PERSON_ID = "people__person_id"
_CITATION_LIMIT = 20
_CONTRIBUTOR_LIMIT = 100


def _company_graph() -> str:
    return graph_names().company


def _trace(
    tool: str,
    cypher: str,
    params: dict[str, Any],
    duration_ms: float = 0.0,
) -> ToolTrace:
    return ToolTrace(
        tool=tool,
        graph=_company_graph(),
        cypher=cypher.strip(),
        params=params,
        duration_ms=duration_ms,
    )


_READINESS_CYPHER = """
MATCH (m:Meta {id: 'company_brain_meta'})
RETURN m.ready AS ready
"""


async def _readiness(client: FalkorDBMcpClient) -> tuple[bool, float]:
    try:
        rows, duration_ms = await client.query_readonly(_company_graph(), _READINESS_CYPHER)
    except Exception:
        return False, 0.0
    return bool(rows and rows[0].get("ready") is True), duration_ms




_SERVICE_RESOLUTION_CYPHER = f"""
MATCH (s:Service)
OPTIONAL MATCH (a:Alias)-[:POINTS_TO]->(s)
WITH s,a WHERE s.{_CANONICAL_SERVICE_ID} IS NOT NULL
  AND (s.{_CANONICAL_SERVICE_ID} = $query OR s.name = $query OR a.name = $query)
RETURN s.{_CANONICAL_SERVICE_ID} AS service_id, count(DISTINCT s) AS identity_count
LIMIT 2
"""


async def _resolve_service(
    client: FalkorDBMcpClient,
    name_or_alias: str,
) -> tuple[str | None, str | None, float]:
    """Resolve only exact, explicitly mapped service identifiers and aliases."""
    query = name_or_alias.strip()
    if not query:
        return None, "not_found", 0.0
    try:
        rows, duration_ms = await client.query_readonly(
            _company_graph(),
            _SERVICE_RESOLUTION_CYPHER,
            {"query": query},
        )
    except Exception:
        return None, "unavailable", 0.0

    if any(row.get("identity_count") != 1 for row in rows):
        return None, "ambiguous", duration_ms
    service_ids = {
        str(row["service_id"])
        for row in rows
        if row.get("service_id") not in (None, "")
    }
    if len(service_ids) == 1:
        return next(iter(service_ids)), None, duration_ms
    if len(service_ids) > 1:
        return None, "ambiguous", duration_ms
    return None, "not_found", duration_ms


async def resolve_service_id(name_or_alias: str, tenant: str = "acme") -> str | None:
    """Resolve an exact external service_id or explicit alias; ambiguity never guesses."""
    try:
        client = FalkorDBMcpClient(tenant=tenant)
    except Exception:
        return None
    service_id, _, _ = await _resolve_service(client, name_or_alias)
    return service_id


def _unavailable_result(
    tool: str,
    service_id: str,
    cypher: str,
    duration_ms: float,
    message: str,
) -> ToolResult:
    return ToolResult(
        status="NOT_READY",
        data=message,
        trace=_trace(tool, cypher, {"service_id": service_id}, duration_ms),
    )


async def _require_service(
    tool: str,
    requested_service_id: str,
    tenant: str,
) -> tuple[FalkorDBMcpClient | None, str | None, ToolResult | None]:
    try:
        client = FalkorDBMcpClient(tenant=tenant)
    except Exception:
        return None, None, _unavailable_result(
            tool,
            requested_service_id,
            _READINESS_CYPHER,
            0.0,
            "Company Brain reader is unavailable",
        )

    ready, ready_ms = await _readiness(client)
    if not ready:
        return None, None, _unavailable_result(
            tool,
            requested_service_id,
            _READINESS_CYPHER,
            ready_ms,
            "Company Brain is not yet seeded, published, or available",
        )

    canonical_id, reason, resolve_ms = await _resolve_service(client, requested_service_id)
    if reason == "unavailable":
        return None, None, _unavailable_result(
            tool,
            requested_service_id,
            _SERVICE_RESOLUTION_CYPHER,
            resolve_ms,
            "Company Brain service evidence is unavailable",
        )
    if reason == "ambiguous":
        return None, None, ToolResult(
            status="EVIDENCE_INSUFFICIENT",
            data=f"Service identifier '{requested_service_id}' is ambiguous",
            trace=_trace(
                tool,
                _SERVICE_RESOLUTION_CYPHER,
                {"query": requested_service_id.strip()},
                resolve_ms,
            ),
        )
    if not canonical_id:
        return None, None, ToolResult(
            status="NOT_FOUND",
            data=f"Service identifier '{requested_service_id}' is not a canonical service_id or explicit alias",
            trace=_trace(
                tool,
                _SERVICE_RESOLUTION_CYPHER,
                {"query": requested_service_id.strip()},
                resolve_ms,
            ),
        )
    return client, canonical_id, None


_PUBLISHED_CITATIONS_CYPHER = """
MATCH (sr:SourceRecord {status: 'published'})
WHERE sr.published_revision = sr.revision
MATCH (sr)-[:PUBLISHED_CHUNK]->(c:Chunk)
MATCH (d:Document {id: sr.document_id})-[:PART_OF]->(c)
OPTIONAL MATCH (canonical:Service {services__service_id:$service_id})-[:MENTIONED_IN]->(c)
WITH sr,c,canonical
WHERE (sr.service_id=$service_id OR (sr.source_kind='canonical_table' AND canonical IS NOT NULL))
  AND (size($chunk_ids)=0 OR c.id IN $chunk_ids)
RETURN DISTINCT
       sr.document_id AS document_id,
       sr.origin AS origin,
       sr.source_kind AS source_kind,
       sr.content_hash AS content_hash,
       sr.source_version AS source_version,
       sr.revision AS revision,
       sr.path AS path,
       sr.permalink AS permalink,
       sr.author_id AS author_id,
       c.id AS chunk_id,
       c.index AS chunk_index,
       c.text AS text
ORDER BY document_id, chunk_index
LIMIT 21
"""


_SOURCE_STATUS_CYPHER = """
MATCH (sr:SourceRecord {service_id: $service_id})
RETURN DISTINCT sr.status AS status
LIMIT 20
"""


def _citation(row: dict[str, Any]) -> dict[str, Any]:
    text = row.get("text") or ""
    return {
        "document_id": row.get("document_id"),
        "origin": row.get("origin"),
        "source_kind": row.get("source_kind"),
        "content_hash": row.get("content_hash"),
        "source_version": row.get("source_version"),
        "revision": row.get("revision"),
        "path": row.get("path"),
        "permalink": row.get("permalink"),
        "author_id": row.get("author_id") or None,
        "chunk_id": row.get("chunk_id"),
        "chunk_index": row.get("chunk_index"),
        "excerpt": text[:500],
        "text": text,
    }


def _sources_from_citations(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for citation in citations:
        document_id = citation["document_id"]
        source = sources.setdefault(
            document_id,
            {
                key: citation[key]
                for key in (
                    "document_id",
                    "origin",
                    "source_kind",
                    "content_hash",
                    "source_version",
                    "revision",
                    "path",
                    "permalink",
                    "author_id",
                )
            }
            | {"chunks": []},
        )
        source["chunks"].append(citation)
    return list(sources.values())


async def _published_citations(
    client: FalkorDBMcpClient,
    service_id: str,
    chunk_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], float, bool]:
    rows, duration_ms = await client.query_readonly(
        _company_graph(),
        _PUBLISHED_CITATIONS_CYPHER,
        {"service_id": service_id, "chunk_ids": chunk_ids or []},
    )
    citations = [_citation(row) for row in rows if row.get("chunk_id") and row.get("text")]
    return citations[:_CITATION_LIMIT], duration_ms, len(rows) > _CITATION_LIMIT


async def _publication_is_unavailable(
    client: FalkorDBMcpClient,
    service_id: str,
) -> tuple[bool, float]:
    rows, duration_ms = await client.query_readonly(
        _company_graph(),
        _SOURCE_STATUS_CYPHER,
        {"service_id": service_id, "citation_limit": _CITATION_LIMIT},
    )
    statuses = {row.get("status") for row in rows}
    return bool(statuses & {"pending", "deleted", "error"}), duration_ms


_OWNERSHIP_CYPHER = f"""
MATCH (s:Service {{{_CANONICAL_SERVICE_ID}: $service_id}})
OPTIONAL MATCH (s)-[owned_by:RELATES]->(t:Team)
WHERE owned_by.rel_type = 'OWNED_BY'
  AND t.teams__team_id IS NOT NULL AND t.name = s.services__owner_team
OPTIONAL MATCH (p:Person)-[membership:RELATES]->(t)
WHERE membership.rel_type = 'MEMBER_OF'
  AND p.people__person_id IS NOT NULL AND p.people__team = t.name
RETURN s.{_CANONICAL_SERVICE_ID} AS service_id,
       s.name AS service_name,
       s.services__tier AS tier,
       collect(DISTINCT t.name) AS owner_teams,
       collect(DISTINCT owned_by.source_chunk_ids) AS ownership_source_chunk_ids,
       collect(DISTINCT {{
           person_id: p.{_CANONICAL_PERSON_ID},
           name: p.name,
           email: p.people__email
       }}) AS team_members
"""


async def who_owns(service_id: str, tenant: str = "acme") -> ToolResult:
    """Return canonical ownership with citations from the current published sources."""
    client, canonical_id, early_result = await _require_service("who_owns", service_id, tenant)
    if early_result:
        return early_result
    assert client and canonical_id

    params = {"service_id": canonical_id, "citation_limit": _CITATION_LIMIT}
    try:
        rows, ownership_ms = await client.query_readonly(
            _company_graph(), _OWNERSHIP_CYPHER, {"service_id": canonical_id}
        )
        ownership_chunk_ids = {
            chunk_id for row in rows for source_chunk_ids in row.get("ownership_source_chunk_ids", [])
            for chunk_id in (source_chunk_ids or []) if chunk_id
        }
        params["chunk_ids"] = sorted(ownership_chunk_ids)
        citations, citation_ms, citations_truncated = await _published_citations(
            client, canonical_id, sorted(ownership_chunk_ids)
        )
    except Exception:
        return _unavailable_result(
            "who_owns",
            canonical_id,
            _OWNERSHIP_CYPHER + "\n" + _PUBLISHED_CITATIONS_CYPHER,
            0.0,
            "Ownership evidence is unavailable",
        )
    if not rows:
        return ToolResult(
            status="NOT_FOUND",
            data=f"Canonical service '{canonical_id}' is absent",
            trace=_trace("who_owns", _OWNERSHIP_CYPHER, {"service_id": canonical_id}, ownership_ms),
        )
    row = rows[0]
    if not citations:
        try:
            unavailable, status_ms = await _publication_is_unavailable(client, canonical_id)
        except Exception:
            unavailable, status_ms = True, 0.0
        status = "NOT_READY" if unavailable else "EVIDENCE_INSUFFICIENT"
        message = (
            "Published source evidence is unavailable"
            if unavailable
            else "No published source citations ground this ownership record"
        )
        return ToolResult(
            status=status,
            data=message,
            trace=_trace(
                "who_owns",
                _OWNERSHIP_CYPHER + "\n" + _PUBLISHED_CITATIONS_CYPHER + "\n" + _SOURCE_STATUS_CYPHER,
                params,
                ownership_ms + citation_ms + status_ms,
            ),
            bounds=ToolBounds(limit=_CITATION_LIMIT, truncated=citations_truncated),
        )

    owner_teams = [team for team in row.get("owner_teams", []) if team]
    if len(owner_teams) != 1:
        return ToolResult(
            status="EVIDENCE_INSUFFICIENT",
            data="Canonical ownership relation is absent or ambiguous",
            evidence={
                "citations": citations,
                "ownership_source_chunk_ids": sorted(ownership_chunk_ids),
            },
            trace=_trace(
                "who_owns",
                _OWNERSHIP_CYPHER + "\n" + _PUBLISHED_CITATIONS_CYPHER,
                params,
                ownership_ms + citation_ms,
            ),
            bounds=ToolBounds(limit=_CITATION_LIMIT, truncated=citations_truncated),
        )
    members = [
        member
        for member in row.get("team_members", [])
        if member and member.get("person_id")
    ]
    return ToolResult(
        status="OK",
        data={
            "service_id": canonical_id,
            "service_name": row.get("service_name"),
            "owner_team": owner_teams[0],
            "tier": row.get("tier"),
            "team_members": members,
        },
        evidence={
            "citations": citations,
            "ownership_source_chunk_ids": sorted(ownership_chunk_ids),
        },
        trace=_trace(
            "who_owns",
            _OWNERSHIP_CYPHER + "\n" + _PUBLISHED_CITATIONS_CYPHER,
            params,
            ownership_ms + citation_ms,
        ),
        bounds=ToolBounds(limit=_CITATION_LIMIT, truncated=citations_truncated),
    )


_BUS_FACTOR_CYPHER = """
MATCH (sr:SourceRecord {service_id: $service_id, status: 'published'})
WHERE sr.published_revision = sr.revision
MATCH (contribution:Contribution)-[:CONTRIBUTION_FOR]->(sr)
WHERE contribution.active = true
  AND contribution.revision = sr.published_revision
  AND contribution.author_id IS NOT NULL
  AND contribution.author_id <> ''
OPTIONAL MATCH (person:Person {people__person_id: contribution.author_id})
WITH contribution.author_id AS author_id,
     collect(DISTINCT person.name) AS names,
     collect(DISTINCT sr.document_id) AS documents,
     count(DISTINCT contribution.id) AS contribution_count
RETURN author_id, CASE WHEN size(names)=1 THEN names[0] ELSE null END AS name,
       documents, contribution_count
ORDER BY contribution_count DESC, author_id
LIMIT 101
"""


async def bus_factor(service_id: str, tenant: str = "acme") -> ToolResult:
    """Count validated current publication contributors, never inferred co-mentions."""
    client, canonical_id, early_result = await _require_service("bus_factor", service_id, tenant)
    if early_result:
        return early_result
    assert client and canonical_id

    params = {
        "service_id": canonical_id,
        "contributor_limit": _CONTRIBUTOR_LIMIT,
        "citation_limit": _CITATION_LIMIT,
        "chunk_ids": [],
    }
    try:
        rows, contributor_ms = await client.query_readonly(
            _company_graph(),
            _BUS_FACTOR_CYPHER,
            {"service_id": canonical_id, "contributor_limit": _CONTRIBUTOR_LIMIT},
        )
        citations, citation_ms, citations_truncated = await _published_citations(client, canonical_id)
    except Exception:
        return _unavailable_result(
            "bus_factor",
            canonical_id,
            _BUS_FACTOR_CYPHER + "\n" + _PUBLISHED_CITATIONS_CYPHER,
            0.0,
            "Contributor evidence is unavailable",
        )
    if not citations:
        try:
            unavailable, status_ms = await _publication_is_unavailable(client, canonical_id)
        except Exception:
            unavailable, status_ms = True, 0.0
        return ToolResult(
            status="NOT_READY" if unavailable else "EVIDENCE_INSUFFICIENT",
            data=(
                "Published source evidence is unavailable"
                if unavailable
                else "No published source citations ground contributor records"
            ),
            trace=_trace(
                "bus_factor",
                _BUS_FACTOR_CYPHER + "\n" + _PUBLISHED_CITATIONS_CYPHER + "\n" + _SOURCE_STATUS_CYPHER,
                params,
                contributor_ms + citation_ms + status_ms,
            ),
            bounds=ToolBounds(limit=_CONTRIBUTOR_LIMIT, truncated=citations_truncated),
        )

    contributors = [
        {
            "author_id": row.get("author_id"),
            "person_id": row.get("author_id"),
            "name": row.get("name"),
            "document_count": len(row.get("documents", [])),
            "documents": sorted(row.get("documents", [])),
            "contribution_count": row.get("contribution_count", 0),
        }
        for row in rows[:_CONTRIBUTOR_LIMIT]
        if row.get("author_id")
    ]
    count = len(contributors)
    no_recorded_contributors = count == 0
    return ToolResult(
        status="OK",
        data={
            "service_id": canonical_id,
            "bus_factor_proxy": count,
            "bus_factor_warning": count == 1,
            "knowledge_concentration": (
                "unknown"
                if no_recorded_contributors
                else ("single_contributor" if count == 1 else "distributed")
            ),
            "primary_maintainer": (
                None
                if no_recorded_contributors
                else contributors[0]["name"] or contributors[0]["author_id"]
            ),
            "primary_maintainer_status": "unknown" if no_recorded_contributors else "recorded",
            "contributors": contributors,
            "contributor_summary": "no recorded contributors" if no_recorded_contributors else None,
        },
        evidence={"contributor_records": rows, "citations": citations},
        trace=_trace(
            "bus_factor",
            _BUS_FACTOR_CYPHER + "\n" + _PUBLISHED_CITATIONS_CYPHER,
            params,
            contributor_ms + citation_ms,
        ),
        bounds=ToolBounds(limit=_CONTRIBUTOR_LIMIT, truncated=citations_truncated or len(rows) > _CONTRIBUTOR_LIMIT),
    )


_RELATED_CHUNKS_CYPHER = f"""
MATCH (s:Service {{{_CANONICAL_SERVICE_ID}: $service_id}})-[relation:RELATES]-(related)
WHERE relation.rel_type IS NOT NULL
RETURN DISTINCT relation.rel_type AS rel_type,
       relation.source_chunk_ids AS source_chunk_ids
LIMIT 21
"""


_PROVENANCE_CITATIONS_CYPHER = f"""
MATCH (s:Service {{{_CANONICAL_SERVICE_ID}: $service_id}})
MATCH (sr:SourceRecord {{service_id: $service_id, status: 'published'}})
WHERE sr.published_revision = sr.revision
MATCH (sr)-[:PUBLISHED_CHUNK]->(c:Chunk)
MATCH (d:Document {{id: sr.document_id}})-[:PART_OF]->(c)
OPTIONAL MATCH (s)-[mentioned:MENTIONED_IN]-(c)
WITH sr, c, mentioned
WHERE mentioned IS NOT NULL OR c.id IN $related_chunk_ids
RETURN DISTINCT
       sr.document_id AS document_id,
       sr.origin AS origin,
       sr.source_kind AS source_kind,
       sr.content_hash AS content_hash,
       sr.source_version AS source_version,
       sr.revision AS revision,
       sr.path AS path,
       sr.permalink AS permalink,
       sr.author_id AS author_id,
       c.id AS chunk_id,
       c.index AS chunk_index,
       c.text AS text
ORDER BY document_id, chunk_index
LIMIT 21
"""


async def decision_provenance(service_id: str, tenant: str = "acme") -> ToolResult:
    """Return published SDK relation provenance without inventing document authorship."""
    client, canonical_id, early_result = await _require_service(
        "decision_provenance", service_id, tenant
    )
    if early_result:
        return early_result
    assert client and canonical_id

    params = {
        "service_id": canonical_id,
        "relation_limit": _CITATION_LIMIT,
        "citation_limit": _CITATION_LIMIT,
    }
    try:
        relations, relation_ms = await client.query_readonly(
            _company_graph(),
            _RELATED_CHUNKS_CYPHER,
            {"service_id": canonical_id, "relation_limit": _CITATION_LIMIT},
        )
        relations_truncated = len(relations) > _CITATION_LIMIT
        relations = relations[:_CITATION_LIMIT]
        related_chunk_ids = sorted(
            {
                chunk_id
                for relation in relations
                for chunk_id in (relation.get("source_chunk_ids") or [])
                if chunk_id
            }
        )
        citation_rows, citation_ms = await client.query_readonly(
            _company_graph(),
            _PROVENANCE_CITATIONS_CYPHER,
            {
                "service_id": canonical_id,
                "related_chunk_ids": related_chunk_ids,
                "citation_limit": _CITATION_LIMIT,
            },
        )
    except Exception:
        return _unavailable_result(
            "decision_provenance",
            canonical_id,
            _RELATED_CHUNKS_CYPHER + "\n" + _PROVENANCE_CITATIONS_CYPHER,
            0.0,
            "Published provenance evidence is unavailable",
        )

    citations_truncated = len(citation_rows) > _CITATION_LIMIT
    citations = [_citation(row) for row in citation_rows[:_CITATION_LIMIT] if row.get("chunk_id") and row.get("text")]
    if not citations:
        try:
            unavailable, status_ms = await _publication_is_unavailable(client, canonical_id)
        except Exception:
            unavailable, status_ms = True, 0.0
        return ToolResult(
            status="NOT_READY" if unavailable else "EVIDENCE_INSUFFICIENT",
            data=(
                "Published provenance evidence is unavailable"
                if unavailable
                else "No published chunks support a canonical SDK relation for this service"
            ),
            evidence={"relations": relations},
            trace=_trace(
                "decision_provenance",
                _RELATED_CHUNKS_CYPHER + "\n" + _PROVENANCE_CITATIONS_CYPHER + "\n" + _SOURCE_STATUS_CYPHER,
                params | {"related_chunk_ids": related_chunk_ids},
                relation_ms + citation_ms + status_ms,
            ),
            bounds=ToolBounds(limit=_CITATION_LIMIT, truncated=relations_truncated or citations_truncated),
        )

    sources = _sources_from_citations(citations)
    return ToolResult(
        status="OK",
        data={
            "service_id": canonical_id,
            "decisions_and_sources_count": len(sources),
            "sources": sources,
        },
        evidence={
            "relations": relations,
            "citations": citations,
            "provenance_documents": [source["document_id"] for source in sources],
        },
        trace=_trace(
            "decision_provenance",
            _RELATED_CHUNKS_CYPHER + "\n" + _PROVENANCE_CITATIONS_CYPHER,
            params | {"related_chunk_ids": related_chunk_ids},
            relation_ms + citation_ms,
        ),
        bounds=ToolBounds(limit=_CITATION_LIMIT, truncated=relations_truncated or citations_truncated),
    )


async def update_company_document(
    document_id: str,
    new_text: str,
    *,
    allow_create: bool = False,
    origin: str = "synthetic",
    path: str = "",
    permalink: str = "",
    author_id: str | None = None,
    service_id: str | None = None,
    source_version: str | None = None,
    source_order: int | None = None,
    delete: bool = False,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Submit one source revision and await serialized worker publication."""
    if allow_create and origin != "slack":
        raise ValueError("Only Slack message ingestion may create a new source")
    import asyncio
    from seed.sources import enqueue_source, get_published_source, wait_for_publication

    request = await asyncio.to_thread(
        enqueue_source,
        document_id,
        new_text,
        allow_create=allow_create,
        origin=origin,
        path=path,
        permalink=permalink,
        author_id=author_id,
        service_id=service_id,
        source_version=source_version,
        source_order=source_order,
        delete=delete,
    )
    if request.get("status") == "published":
        return await asyncio.to_thread(get_published_source, document_id)
    if request.get("status") == "ignored":
        from seed.sources import SourceUnavailable
        raise SourceUnavailable("Source revision is older than the current revision")
    return await wait_for_publication(
        request["request_id"],
        timeout_seconds=timeout_seconds,
    )
