"""Application source revisions around the official SDK's ingestion lifecycle.

One local worker owns SDK writes. Requests and publication state are durable in
FalkorDB; a pending/error revision is never returned as a current citation.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Awaitable, Callable
import uuid

from falkordb import FalkorDB
import yaml

from agents.settings import graph_names, require_tenant
from seed.load import get_config

ROOT = Path(__file__).resolve().parents[1]


class SourceNotFound(LookupError):
    pass


class SourceUnavailable(RuntimeError):
    pass


@contextmanager
def evidence_state_guard(*, blocking: bool = True):
    """Fence brief source-state transitions against final approval version reads.

    This is not the long-lived SDK writer lock or action-claim arbitration.
    All application/source processes must share the configured lock filesystem.
    """
    default = ROOT / ".secrets" / f"source-worker-{graph_names().company}.lock"
    path = Path(os.environ.get("CHRONOSOPS_SOURCE_LOCK_PATH", str(default)) + ".state")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise SourceUnavailable("Evidence publication is changing; retry current evidence") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _graph(*, tenant: str | None = None) -> Any:
    config = get_config()
    if tenant is None:
        username = config["FALKORDB_ADMIN_USERNAME"]
        password = config["FALKORDB_ADMIN_PASSWORD"]
    else:
        tenant = require_tenant(tenant)
        username = config.get(f"FALKORDB_READER_{tenant.upper()}_USERNAME", f"reader_{tenant}")
        password = config.get(f"FALKORDB_READER_{tenant.upper()}_PASSWORD")
        if not password:
            raise SourceUnavailable("Source reader credentials are not configured")
    return FalkorDB(host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
                    username=username, password=password).select_graph(graph_names().company)


def _source(document_id: str) -> dict[str, Any] | None:
    rows = _graph().ro_query("MATCH (s:SourceRecord {document_id:$id}) RETURN properties(s) LIMIT 2",
                             {"id": document_id}, timeout=3000).result_set
    if len(rows) > 1:
        raise SourceUnavailable("Ambiguous source identity")
    return dict(rows[0][0]) if rows else None


def _metadata(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as error:
        raise ValueError("Unterminated source front matter") from error
    metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(metadata, dict):
        raise ValueError("Source front matter must be a mapping")
    return metadata


@evidence_state_guard()
def enqueue_source(document_id: str, text: str, *, allow_create: bool = False,
                   origin: str = "synthetic", path: str = "", permalink: str = "",
                   author_id: str | None = None, service_id: str | None = None,
                   source_version: str | None = None, source_order: int | None = None,
                   delete: bool = False, source_kind: str = "document") -> dict[str, Any]:
    """Queue an explicit source revision, never invoke extraction from a caller."""
    if not isinstance(document_id, str) or not document_id or len(document_id) > 240:
        raise ValueError("Invalid source identity")
    if not isinstance(text, str) or len(text.encode("utf-8")) > 200_000:
        raise ValueError("Source must be bounded UTF-8 text")
    if origin not in {"synthetic", "slack"}:
        raise ValueError("Unrecognized source origin")
    existing = _source(document_id)
    if source_kind not in {"document", "canonical_table"}:
        raise ValueError("Unknown source kind")
    if existing and existing.get("source_kind", "document") != source_kind:
        raise ValueError("Canonical tables require structured operator ingestion")
    if existing is None and not allow_create:
        raise SourceNotFound(document_id)
    if existing and existing.get("status") == "deleted" and not delete:
        if not allow_create or source_order is None:
            raise SourceNotFound(document_id)
    if source_order is not None and (not isinstance(source_order, int) or source_order < 0):
        raise ValueError("Source order must be a nonnegative external revision")
    if existing and source_order is not None and source_order <= existing.get("source_order", -1):
        return {"status": "ignored", "document_id": document_id, "revision": existing["revision"]}
    metadata = {} if delete else _metadata(text)
    if metadata.get("document_id", document_id) != document_id:
        raise ValueError("Front-matter document identity mismatch")
    effective_origin = existing.get("origin", origin) if existing else origin
    service = service_id or metadata.get("service_id") or (existing or {}).get("service_id", "")
    author = author_id if author_id is not None else metadata.get("author", "")
    if not isinstance(service, str) or not isinstance(author, str):
        raise ValueError("Source service and author must be external identity strings")
    graph = _graph()
    if service:
        matches = graph.ro_query("MATCH (s:Service {services__service_id:$id}) RETURN count(s)", {"id": service}, timeout=3000).result_set[0][0]
        if matches != 1:
            raise ValueError("Source service is not one canonical structured identity")
    if effective_origin != "slack" and author:
        matches = graph.ro_query("MATCH (p:Person {people__person_id:$id}) RETURN count(p)", {"id": author}, timeout=3000).result_set[0][0]
        if matches != 1:
            author = ""  # Unknown attribution stays unknown; prose co-mention is not authorship.
    version = source_version if source_version is not None else str(metadata.get("version", ""))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if existing and not delete and existing.get("status") == "published" and existing.get("content_hash") == digest and (
        not version or existing.get("source_version") == version
    ) and existing.get("author_id", "") == author and existing.get("service_id", "") == service and source_order is None:
        return {"status": "published", "document_id": document_id, "revision": existing["revision"],
                "request_id": existing.get("request_id", ""), "content_hash": digest,
                "source_version": existing.get("source_version", "")}
    request_id = "source-request-" + uuid.uuid4().hex
    rows = graph.query(
        "MERGE (s:SourceRecord {document_id:$id}) ON CREATE SET s.revision=0, s.origin=$origin "
        "WITH s WHERE ($allow_create OR s.revision > 0) "
        "AND ($delete OR s.status IS NULL OR s.status <> 'deleted' OR $allow_create) "
        "AND ($order < 0 OR coalesce(s.source_order,-1) < $order) "
        "SET s.revision=coalesce(s.revision,0)+1 "
        "WITH s SET s.status=CASE WHEN $delete THEN 'deleted' ELSE 'pending' END, "
        "s.source_version=CASE WHEN $version='' THEN toString(s.revision) ELSE $version END, "
        "s.content_hash=$hash, s.author_id=CASE WHEN $delete THEN '' ELSE $author END, "
        "s.service_id=$service, s.source_kind=$kind, s.path=$path, s.permalink=$permalink, s.title=$title, "
        "s.source_order=CASE WHEN $order < 0 THEN coalesce(s.source_order,-1) ELSE $order END, s.request_id=$request "
        "CREATE (r:SourceRequest {request_id:$request, document_id:$id, revision:s.revision, "
        "state:'pending', operation:$operation, text:$text, allow_create:$allow_create, "
        "content_hash:$hash, source_version:s.source_version, origin:s.origin, source_kind:s.source_kind, "
        "author_id:s.author_id, service_id:s.service_id, created_at:$now}) "
        "MERGE (m:Meta {id:'company_brain_meta'}) SET m.ready=false "
        "RETURN properties(r) LIMIT 1",
        {"id": document_id, "origin": effective_origin, "allow_create": allow_create, "delete": delete,
         "kind": source_kind,
         "order": source_order if source_order is not None else -1, "version": version, "hash": digest,
         "author": author, "service": service, "path": path or (existing or {}).get("path", ""),
         "permalink": permalink or (existing or {}).get("permalink", ""),
         "title": "" if delete else str(metadata.get("title", document_id)), "request": request_id,
         "operation": "delete" if delete else "upsert", "text": "" if delete else text,
         "now": time.time()}, timeout=3000).result_set
    if not rows:
        current = _source(document_id)
        return {"status": "ignored", "document_id": document_id, "revision": (current or {}).get("revision", 0)}
    if delete:
        graph.query("MATCH (r:SourceRequest {document_id:$id}) SET r.text=''", {"id": document_id}, timeout=3000)
    return dict(rows[0][0])


def _refresh_ready(graph: Any) -> None:
    graph.query(
        "MERGE (m:Meta {id:'company_brain_meta'}) "
        "WITH m OPTIONAL MATCH (s:SourceRecord) "
        "WITH m, count(s) AS sources, sum(CASE WHEN s.status IN ['pending','error'] THEN 1 ELSE 0 END) AS blocked "
        "OPTIONAL MATCH (r:SourceRequest) WHERE r.state IN ['pending','processing'] "
        "WITH m, sources, blocked, count(r) AS pending "
        "SET m.ready=coalesce(m.schema_ready,false) AND sources > 0 AND blocked=0 AND pending=0",
        timeout=3000)


@evidence_state_guard()
def publish_source_request(request_id: str) -> None:
    """Cut over only a finalized SDK revision; never manufacture chunk IDs/text."""
    graph = _graph()
    rows = graph.ro_query("MATCH (r:SourceRequest {request_id:$id}) RETURN properties(r) LIMIT 1",
                          {"id": request_id}, timeout=3000).result_set
    if not rows:
        raise SourceNotFound(request_id)
    request = dict(rows[0][0])
    current = _source(request["document_id"])
    if not current or current["revision"] != request["revision"]:
        graph.query("MATCH (r:SourceRequest {request_id:$id}) SET r.state='superseded', r.text=''",
                    {"id": request_id}, timeout=3000)
        return
    if request["operation"] != "delete":
        chunks = graph.ro_query(
            "MATCH (d:Document {id:$id})-[:PART_OF]->(c:Chunk) "
            "WHERE d.content_hash IS NOT NULL AND d.content_hash <> '' "
            "RETURN c.id, c.index, c.text ORDER BY c.index LIMIT 51",
            {"id": request["document_id"]}, timeout=3000).result_set
        if not chunks or len(chunks) > 50 or any(not row[0] or row[1] is None or not row[2] for row in chunks):
            raise SourceUnavailable("SDK did not certify complete writes with bounded current chunks")
    graph.query(
        "MATCH (s:SourceRecord {document_id:$document}), (r:SourceRequest {request_id:$request}) "
        "WHERE s.revision=r.revision "
        "OPTIONAL MATCH (s)-[old:PUBLISHED_CHUNK]->(:Chunk) DELETE old "
        "WITH DISTINCT s,r OPTIONAL MATCH (con:Contribution)-[:CONTRIBUTION_FOR]->(s) SET con.active=false "
        "WITH DISTINCT s,r SET s.status=CASE WHEN r.operation='delete' THEN 'deleted' ELSE 'published' END, "
        "s.published_revision=r.revision, r.state=CASE WHEN r.operation='delete' THEN 'deleted' ELSE 'published' END, "
        "r.text='', r.completed_at=$now",
        {"document": request["document_id"], "request": request_id, "now": time.time()}, timeout=3000)
    if request["operation"] != "delete":
        graph.query(
            "MATCH (s:SourceRecord {document_id:$document}), (d:Document {id:$document})-[:PART_OF]->(c:Chunk) "
            "WHERE s.revision=$revision AND s.published_revision=$revision AND s.status='published' "
            "MERGE (s)-[:PUBLISHED_CHUNK]->(c)",
            {"document": request["document_id"], "revision": request["revision"]}, timeout=3000)
        if current.get("author_id") and current.get("service_id"):
            contribution_id = f"{request['document_id']}:{request['revision']}:{current['author_id']}"
            graph.query(
                "MATCH (s:SourceRecord {document_id:$document}) WHERE s.revision=$revision AND s.status='published' "
                "MERGE (c:Contribution {id:$id}) SET c.document_id=$document, c.revision=$revision, "
                "c.author_id=s.author_id, c.service_id=s.service_id, c.active=true "
                "MERGE (c)-[:CONTRIBUTION_FOR]->(s)",
                {"document": request["document_id"], "revision": request["revision"], "id": contribution_id}, timeout=3000)
    _refresh_ready(graph)


def get_published_source(document_id: str, tenant: str = "acme") -> dict[str, Any]:
    graph = _graph(tenant=tenant)
    rows = graph.ro_query("MATCH (s:SourceRecord {document_id:$id}) RETURN properties(s) LIMIT 2",
                          {"id": document_id}, timeout=3000).result_set
    if not rows:
        raise SourceNotFound(document_id)
    if len(rows) != 1:
        raise SourceUnavailable("Ambiguous source identity")
    source = dict(rows[0][0])
    if source.get("status") == "deleted":
        raise SourceNotFound(document_id)
    if source.get("status") != "published" or source.get("revision") != source.get("published_revision"):
        raise SourceUnavailable("Source revision is not currently published")
    chunks = graph.ro_query(
        "MATCH (s:SourceRecord {document_id:$id})-[:PUBLISHED_CHUNK]->(c:Chunk) "
        "MATCH (d:Document {id:$id})-[:PART_OF]->(c) "
        "WHERE s.status='published' AND s.revision=$revision AND s.published_revision=$revision AND s.content_hash=$hash "
        "RETURN c.id, c.index, c.text ORDER BY c.index LIMIT 51",
        {"id": document_id, "revision": source["revision"], "hash": source["content_hash"]}, timeout=3000).result_set
    if not chunks or len(chunks) > 50:
        raise SourceUnavailable("Published SDK chunks are unavailable or exceed evidence bounds")
    return {**source, "chunks": [{"chunk_id": row[0], "chunk_index": row[1], "text": row[2], "excerpt": row[2][:1000]}
                                 for row in chunks]}


async def wait_for_publication(request_id: str, *, timeout_seconds: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        def read_request() -> dict[str, Any]:
            rows = _graph().ro_query("MATCH (r:SourceRequest {request_id:$id}) RETURN properties(r) LIMIT 1",
                                     {"id": request_id}, timeout=3000).result_set
            if not rows:
                raise SourceNotFound(request_id)
            return dict(rows[0][0])
        request = await asyncio.to_thread(read_request)
        if request["state"] == "published":
            source = await asyncio.to_thread(get_published_source, request["document_id"])
            if source["revision"] != request["revision"] or source["content_hash"] != request["content_hash"]:
                raise SourceUnavailable("Requested source revision was superseded")
            return source
        if request["state"] == "deleted":
            return {"document_id": request["document_id"], "revision": request["revision"], "status": "deleted"}
        if request["state"] in {"error", "superseded"}:
            raise SourceUnavailable("Source publication failed or was superseded")
        await asyncio.sleep(0.1)
    raise SourceUnavailable("Timed out awaiting source publication; the revision remains unavailable")


@contextmanager
def _writer_guard():
    default = ROOT / ".secrets" / f"source-worker-{graph_names().company}.lock"
    path = Path(os.environ.get("CHRONOSOPS_SOURCE_LOCK_PATH", str(default)))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SourceUnavailable("The single source writer is already running") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _next_request() -> dict[str, Any] | None:
    graph = _graph()
    graph.query("MATCH (r:SourceRequest {state:'pending'}), (s:SourceRecord {document_id:r.document_id}) "
                "WHERE r.revision <> s.revision SET r.state='superseded', r.text=''", timeout=3000)
    rows = graph.query(
        "MATCH (r:SourceRequest {state:'pending'}) WITH r ORDER BY r.created_at, r.request_id LIMIT 1 "
        "SET r.state='processing' RETURN properties(r)", timeout=3000).result_set
    return dict(rows[0][0]) if rows else None


@evidence_state_guard()
def _fail_request(request: dict[str, Any]) -> None:
    graph = _graph()
    graph.query("MATCH (r:SourceRequest {request_id:$id}), (s:SourceRecord {document_id:r.document_id}) "
                "SET r.state=CASE WHEN s.revision=r.revision THEN 'error' ELSE 'superseded' END, r.text='', "
                "s.status=CASE WHEN s.revision=r.revision AND r.operation <> 'delete' THEN 'error' ELSE s.status END",
                {"id": request["request_id"]}, timeout=3000)
    _refresh_ready(graph)


async def run_source_worker(processor: Callable[[dict[str, Any]], Awaitable[None]] | None = None, *,
                            stop_event: asyncio.Event | None = None, once: bool = False,
                            initialize: Callable[[], Awaitable[None]] | None = None) -> None:
    """Serialize this domain's SDK writes and resume only pending interrupted work."""
    if processor is None:
        from seed.ingest import apply_sdk_source
        processor = apply_sdk_source
    with _writer_guard():
        await asyncio.to_thread(lambda: _graph().query(
            "MATCH (r:SourceRequest {state:'processing'}) SET r.state='pending'", timeout=3000))
        if initialize is not None:
            await initialize()
        while stop_event is None or not stop_event.is_set():
            request = await asyncio.to_thread(_next_request)
            if request is None:
                if once:
                    await asyncio.to_thread(_refresh_ready, _graph())
                    return
                if stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=0.25)
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0.25)
                continue
            try:
                await processor(request)
                await asyncio.to_thread(publish_source_request, request["request_id"])
            except Exception:
                await asyncio.to_thread(_fail_request, request)
                if once:
                    raise SourceUnavailable("SDK source publication failed; source is unavailable")
