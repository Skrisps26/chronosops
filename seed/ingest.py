"""Ingest Company Brain sources through the supported GraphRAG SDK lifecycle."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
import hnswlib  # Required by the SDK resolver; fail before extraction if its native runtime is missing.
from falkordb import FalkorDB
from graphrag_sdk import (
    ConnectionConfig,
    GraphRAG,
    LiteLLM,
    LiteLLMEmbedder,
)
from graphrag_sdk.ingestion.extraction_strategies.entity_extractors import LLMExtractor
from graphrag_sdk.ingestion.extraction_strategies.graph_extraction import GraphExtraction
from graphrag_sdk.core.models import IngestionResult
from graphrag_sdk.ingestion.structured_pipeline import StructuredIngestionResult

from agents.settings import graph_names
from seed.generate import DATA_DIR, DOCS_DIR, MANIFEST_FILE
from seed.ontology import get_company_ontology
from seed.sources import evidence_state_guard

ROOT = Path(__file__).resolve().parents[1]
SECRETS_FILE = ROOT / ".secrets" / "db.env"
_CANONICAL_TABLES = (
    ("services.csv", "service_id", "service_name"),
    ("people.csv", "person_id", "name"),
    ("teams.csv", "team_id", "name"),
)


def get_config() -> dict[str, str]:
    config = {k: v for k, v in dotenv_values(SECRETS_FILE).items() if isinstance(v, str) and v}
    config.update({k: v for k, v in os.environ.items() if v})
    config.setdefault("FALKORDB_HOST", "127.0.0.1")
    config.setdefault("FALKORDB_PORT", "6379")
    config.setdefault("FALKORDB_ADMIN_USERNAME", "admin")
    config.setdefault("AWS_REGION", "us-east-1")
    config.setdefault("BEDROCK_LLM_MODEL_ID", "us.amazon.nova-lite-v1:0")
    config.setdefault("BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
    return config


def get_providers(config: dict[str, str]) -> tuple[LiteLLM, LiteLLMEmbedder]:
    os.environ["AWS_REGION_NAME"] = config["AWS_REGION"]
    if config.get("AWS_PROFILE"):
        os.environ["AWS_PROFILE"] = config["AWS_PROFILE"]

    llm = LiteLLM(
        model=f"bedrock/{config['BEDROCK_LLM_MODEL_ID']}",
        temperature=0,
        max_tokens=2048,
        aws_region_name=config["AWS_REGION"],
        aws_profile_name=config.get("AWS_PROFILE"),
    )
    embedder = LiteLLMEmbedder(
        model=f"bedrock/{config['BEDROCK_EMBED_MODEL_ID']}",
        dimensions=256,
        normalize=True,
        aws_region_name=config["AWS_REGION"],
        aws_profile_name=config.get("AWS_PROFILE"),
    )
    return llm, embedder


def _company_graph(config: dict[str, str]) -> Any:
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    return db.select_graph(graph_names().company)


def _connection(config: dict[str, str]) -> ConnectionConfig:
    return ConnectionConfig(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
        graph_name=graph_names().company,
        query_timeout_ms=3_000,
    )


def _extractor(llm: LiteLLM) -> GraphExtraction:
    return GraphExtraction(llm=llm, entity_extractor=LLMExtractor(llm=llm))


def _read_table(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _validate_canonical_tables() -> set[str]:
    """Refuse ambiguous canonical identities before SDK writes any table."""
    service_ids: set[str] = set()
    for filename, canonical_column, reconciliation_column in _CANONICAL_TABLES:
        seen_canonical: set[str] = set()
        seen_reconciliation: set[str] = set()
        for row in _read_table(DATA_DIR / filename):
            canonical_id = (row.get(canonical_column) or "").strip()
            reconciliation_key = (row.get(reconciliation_column) or "").strip()
            if not canonical_id or not reconciliation_key:
                raise ValueError(
                    f"{filename} requires non-empty {canonical_column} and {reconciliation_column}"
                )
            if canonical_id in seen_canonical:
                raise ValueError(f"{filename} duplicates canonical {canonical_column} {canonical_id!r}")
            if reconciliation_key in seen_reconciliation:
                raise ValueError(
                    f"{filename} duplicates SDK reconciliation key "
                    f"{reconciliation_column} {reconciliation_key!r}"
                )
            seen_canonical.add(canonical_id)
            seen_reconciliation.add(reconciliation_key)
        if filename == "services.csv":
            service_ids = seen_canonical
    return service_ids


def load_alias_mappings(
    *,
    alias_path: Path | None = None,
    known_service_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    """Read stable explicit aliases without selecting an arbitrary target."""
    known_service_ids = known_service_ids if known_service_ids is not None else _validate_canonical_tables()
    aliases: list[dict[str, str]] = []
    canonical_names = {
        value.casefold(): row["service_id"]
        for row in _read_table(DATA_DIR / "services.csv")
        for value in (row["service_id"], row["service_name"])
    }
    seen: set[str] = set()
    for row in _read_table(alias_path or DATA_DIR / "aliases.csv"):
        name = (row.get("alias") or "").strip()
        service_id = (row.get("service_id") or "").strip()
        normalized = name.casefold()
        if not name or not service_id:
            raise ValueError("aliases.csv requires non-empty alias and service_id")
        if normalized in seen:
            raise ValueError(f"aliases.csv duplicates alias {name!r}")
        if service_id not in known_service_ids:
            raise ValueError(f"aliases.csv maps {name!r} to unknown service_id {service_id!r}")
        if normalized in canonical_names and canonical_names[normalized] != service_id:
            raise ValueError(f"aliases.csv conflicts with canonical identity {name!r}")
        seen.add(normalized)
        aliases.append({"id": f"alias:{normalized}", "name": name, "service_id": service_id})
    return aliases


def _publish_aliases(graph: Any, aliases: list[dict[str, str]]) -> None:
    """Attach aliases only to the exact SDK-created canonical Service identity."""
    ontology = get_company_ontology()
    for filename, canonical_column, _ in _CANONICAL_TABLES:
        mapping = next(table for table in ontology.tables if Path(table.source).name == filename)
        property_name = mapping.signed_name(canonical_column)
        rows = graph.ro_query(
            f"MATCH (n:{mapping.label}) WHERE n.{property_name} IS NOT NULL "
            f"RETURN n.{property_name}, count(n) LIMIT 100", timeout=3000,
        ).result_set
        expected = {row[canonical_column]: 1 for row in _read_table(DATA_DIR / filename)}
        if dict(rows) != expected:
            raise ValueError(f"SDK canonical identities changed or became ambiguous for {filename}")
    bindings: list[dict[str, str]] = []
    for alias in aliases:
        rows = graph.ro_query(
            "MATCH (s:Service) WHERE s.services__service_id = $service_id RETURN s.id",
            {"service_id": alias["service_id"]},
        ).result_set
        if len(rows) != 1:
            raise ValueError(
                f"alias {alias['name']!r} needs exactly one SDK Service with "
                f"service_id {alias['service_id']!r}; found {len(rows)}"
            )
        bindings.append({**alias, "service_node_id": str(rows[0][0])})

    graph.query(
        "MATCH (a:Alias) WHERE a.id IS NULL OR NOT a.id IN $ids DETACH DELETE a",
        {"ids": [binding["id"] for binding in bindings]},
    )
    for binding in bindings:
        graph.query(
            "MERGE (a:Alias {id: $alias_id}) "
            "SET a.name = $alias_name "
            "WITH a OPTIONAL MATCH (a)-[old:POINTS_TO]->() DELETE old "
            "WITH DISTINCT a MATCH (s:Service {id: $service_node_id}) "
            "MERGE (a)-[:POINTS_TO]->(s)",
            {
                "alias_id": binding["id"],
                "alias_name": binding["name"],
                "service_node_id": binding["service_node_id"],
            },
        )


@evidence_state_guard()
def _set_schema_state(graph: Any, *, schema_ready: bool, error: str | None = None) -> None:
    graph.query(
        "MERGE (m:Meta {id: 'company_brain_meta'}) "
        "SET m.ready = false, m.schema_ready = $schema_ready, m.schema_error = $error",
        {"schema_ready": schema_ready, "error": error},
    )


def _require_complete(result: IngestionResult | StructuredIngestionResult) -> None:
    """The SDK reports partial extraction/index writes without raising."""
    incomplete = (
        result.incomplete_writes
        if isinstance(result, StructuredIngestionResult)
        else result.metadata.get("incomplete_writes", [])
    )
    if incomplete:
        raise RuntimeError("SDK ingestion incomplete: " + "; ".join(incomplete))


async def apply_sdk_source(request: dict[str, Any]) -> None:
    """Apply one already-validated source request through the supported SDK."""
    document_id = request.get("document_id")
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("source request requires a non-empty document_id")

    config = get_config()
    llm, embedder = get_providers(config)
    async with GraphRAG(
        connection=_connection(config),
        llm=llm,
        embedder=embedder,
        ontology=get_company_ontology(),
        embedding_dimension=256,
        enable_cypher=False,
    ) as rag:
        if request.get("source_kind") == "canonical_table":
            if document_id not in {filename for filename, _, _ in _CANONICAL_TABLES} or request.get("operation") == "delete":
                raise ValueError("Canonical tables require structured operator ingestion")
            source_path = DATA_DIR / document_id
            if hashlib.sha256(source_path.read_bytes()).hexdigest() != request["content_hash"]:
                raise ValueError("Canonical source changed; resubmit structured ingestion")
            _require_complete(await rag.ingest(source=str(source_path), strict_mapping=True))
        elif request.get("operation") == "delete":
            await rag.delete_document(document_id, if_missing="ignore")
        else:
            text = request.get("text")
            if not isinstance(text, str):
                raise ValueError("upsert source request requires text")
            _require_complete(await rag.update(
                text=text,
                document_id=document_id,
                extractor=_extractor(llm),
                if_missing="ingest" if request.get("allow_create") else "error",
            ))
        await rag.finalize(resolve=True, judge=True, judge_vote=True)
        _publish_aliases(_company_graph(config), load_alias_mappings())


async def _initialize_company_schema() -> None:
    """Ingest the initial bounded corpus in one SDK session under the worker lock."""
    from seed.sources import enqueue_source, publish_source_request, _fail_request

    config = get_config()
    graph = _company_graph(config)
    _set_schema_state(graph, schema_ready=False)
    from seed.load import ensure_application_constraints
    await asyncio.to_thread(ensure_application_constraints, graph, company=True)
    requests: list[dict[str, Any]] = []
    try:
        known_service_ids = _validate_canonical_tables()
        aliases = load_alias_mappings(known_service_ids=known_service_ids)
        llm, embedder = get_providers(config)
        async with GraphRAG(
            connection=_connection(config),
            llm=llm,
            embedder=embedder,
            ontology=get_company_ontology(),
            embedding_dimension=256,
            enable_cypher=False,
        ) as rag:
            for filename, _, _ in _CANONICAL_TABLES:
                source_path = DATA_DIR / filename
                request = enqueue_source(
                    filename, source_path.read_bytes().decode("utf-8"), allow_create=True,
                    source_kind="canonical_table", path=source_path.relative_to(ROOT).as_posix(),
                )
                if request.get("status") not in {"published", "ignored"}:
                    requests.append(request)
                _require_complete(await rag.ingest(source=str(DATA_DIR / filename), strict_mapping=True))
            for doc_path in sorted(DOCS_DIR.glob("*.md")):
                request = enqueue_source(
                    doc_path.stem, doc_path.read_bytes().decode("utf-8"),
                    allow_create=True, origin="synthetic",
                    path=doc_path.relative_to(ROOT).as_posix(),
                )
                if request.get("status") in {"published", "ignored"}:
                    continue
                requests.append(request)
                _require_complete(await rag.update(
                    text=request["text"], document_id=request["document_id"],
                    extractor=_extractor(llm), if_missing="ingest",
                ))
            await rag.finalize(resolve=True, judge=True, judge_vote=True)
            _publish_aliases(graph, aliases)
        _set_schema_state(graph, schema_ready=True)
        for request in requests:
            publish_source_request(request["request_id"])
    except Exception as exc:
        for request in requests:
            _fail_request(request)
        _set_schema_state(graph, schema_ready=False, error=str(exc))
        raise


async def ingest_company_brain(*, wipe: bool = False) -> dict[str, Any]:
    """Build Company Brain by initializing and draining the one source worker."""
    from seed.sources import run_source_worker

    config = get_config()
    graph = _company_graph(config)
    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    async def initialize() -> None:
        if wipe:
            with evidence_state_guard():
                await asyncio.to_thread(graph.delete)
        await _initialize_company_schema()

    await run_source_worker(
        processor=apply_sdk_source,
        once=True,
        initialize=initialize,
    )

    doc_count = graph.ro_query("MATCH (d:Document) RETURN count(d)").result_set[0][0]
    chunk_count = graph.ro_query("MATCH (c:Chunk) RETURN count(c)").result_set[0][0]
    ready_rows = graph.ro_query(
        "MATCH (m:Meta {id: 'company_brain_meta'}) RETURN m.ready"
    ).result_set
    return {
        "ready": bool(ready_rows and ready_rows[0][0] is True),
        "graph": graph_names().company,
        "documents": doc_count,
        "chunks": chunk_count,
        "manifest_hash": manifest["manifest_hash"],
    }


def is_company_brain_ready() -> bool:
    config = get_config()
    try:
        res = _company_graph(config).ro_query(
            "MATCH (m:Meta {id: 'company_brain_meta'}) RETURN m.ready"
        ).result_set
        return bool(res and res[0][0] is True)
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wipe", action="store_true", help="Explicitly wipe the configured company graph")
    parser.add_argument("--worker", action="store_true", help="Run the serialized source worker")
    args = parser.parse_args()
    if args.worker and args.wipe:
        parser.error("--wipe only applies to bulk ingestion")

    if args.worker:
        from seed.sources import run_source_worker

        asyncio.run(run_source_worker(processor=apply_sdk_source))
        return 0

    result = asyncio.run(ingest_company_brain(wipe=args.wipe))
    print(f"Company Brain ingestion result: {result}")
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
