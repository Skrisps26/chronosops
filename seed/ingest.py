"""Ingest company knowledge documents and structured tables into FalkorDB company_brain."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from falkordb import FalkorDB
from graphrag_sdk import (
    ConnectionConfig,
    GraphRAG,
    LiteLLM,
    LiteLLMEmbedder,
)
from graphrag_sdk.ingestion.extraction_strategies.entity_extractors import LLMExtractor
from graphrag_sdk.ingestion.extraction_strategies.graph_extraction import GraphExtraction

from seed.generate import DATA_DIR, DOCS_DIR, MANIFEST_FILE
from seed.ontology import get_company_ontology

ROOT = Path(__file__).resolve().parents[1]
SECRETS_FILE = ROOT / ".secrets" / "db.env"


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


async def ingest_company_brain(
    graph_name: str = "company_brain",
    wipe: bool = False,
) -> dict[str, Any]:
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )

    if wipe:
        try:
            db.select_graph(graph_name).delete()
        except Exception:
            pass

    graph = db.select_graph(graph_name)

    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    start_ts = datetime.now(timezone.utc).isoformat()
    graph.query(
        "MERGE (m:Meta {id: 'company_brain_meta'}) "
        "SET m.ready = false, m.hash = $hash, m.started_at = $ts",
        {"hash": manifest["manifest_hash"], "ts": start_ts},
    )

    llm, embedder = get_providers(config)
    ontology = get_company_ontology()
    extractor = GraphExtraction(llm=llm, entity_extractor=LLMExtractor(llm=llm))

    connection = ConnectionConfig(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
        graph_name=graph_name,
        query_timeout_ms=3_000,
    )
    doc_files = sorted(DOCS_DIR.glob("*.md"))

    async with GraphRAG(
        connection=connection,
        llm=llm,
        embedder=embedder,
        ontology=ontology,
        embedding_dimension=256,
        enable_cypher=False,
    ) as rag:
        # 1. Ingest markdown sources
        for doc_path in doc_files:
            doc_id = doc_path.stem
            await rag.ingest(
                source=str(doc_path),
                document_id=doc_id,
                extractor=extractor,
            )

        # 2. Finalize initial graph
        await rag.finalize(resolve=False, judge=False)

    # 3. Ingest and link canonical structured entities & aliases directly
    # Structured services
    with (DATA_DIR / "services.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            graph.query(
                "MERGE (s:Service {service_id: $service_id}) "
                "SET s.name = $name, s.owner_team = $owner_team, s.environment = $environment, s.tier = $tier",
                {
                    "service_id": row["service_id"],
                    "name": row["service_name"],
                    "owner_team": row["owner_team"],
                    "environment": row["environment"],
                    "tier": row["tier"],
                },
            )

    # Structured people
    with (DATA_DIR / "people.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            graph.query(
                "MERGE (p:Person {person_id: $person_id}) "
                "SET p.name = $name, p.email = $email, p.team = $team",
                {
                    "person_id": row["person_id"],
                    "name": row["name"],
                    "email": row["email"],
                    "team": row["team"],
                },
            )

    # Structured teams and relationships
    with (DATA_DIR / "teams.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            graph.query(
                "MERGE (t:Team {team_id: $team_id}) SET t.name = $name",
                {"team_id": row["team_id"], "name": row["name"]},
            )

    # Link people to teams and teams to services
    graph.query(
        "MATCH (p:Person), (t:Team) WHERE p.team = t.name "
        "MERGE (p)-[:MEMBER_OF]->(t)"
    )
    graph.query(
        "MATCH (s:Service), (t:Team) WHERE s.owner_team = t.name "
        "MERGE (t)-[:OWNS]->(s)"
    )

    # Structured aliases
    with (DATA_DIR / "aliases.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            graph.query(
                "MERGE (a:Alias {name: $alias}) "
                "WITH a MATCH (s:Service {service_id: $service_id}) "
                "MERGE (a)-[:POINTS_TO]->(s)",
                {"alias": row["alias"], "service_id": row["service_id"]},
            )

    # 4. Link canonical authored and about relationships for scenario documents
    # Alice authored RFC-001, PR-101, Ticket-101, Ticket-102, Slack-001
    graph.query(
        "MATCH (p:Person {person_id: 'user-alice'}), (s:Service {service_id: 'payments-primary'}) "
        "MATCH (d:Document) WHERE d.id IN ['rfc-001-payments-failover', 'ticket-PAY-101', 'ticket-PAY-102', 'pr-101-payments-queue-routing', 'slack-001-failover-budget'] "
        "MERGE (p)-[:AUTHORED]->(d) "
        "MERGE (d)-[:ABOUT]->(s)"
    )
    graph.query(
        "MATCH (p:Person {person_id: 'user-alice'}), (s:Service {service_id: 'payments-primary'}) "
        "MATCH (d:Document) WHERE d.id IN ['ticket-PAY-101', 'ticket-PAY-102'] "
        "MERGE (p)-[:RESOLVED]->(d)"
    )

    # Carol authored RFC-002, PR-102, Ticket-201
    graph.query(
        "MATCH (p:Person {person_id: 'user-carol'}), (s:Service {service_id: 'checkout'}) "
        "MATCH (d:Document) WHERE d.id IN ['rfc-002-checkout-resilience', 'ticket-CHK-201', 'pr-102-checkout-upstream-circuit-breaker'] "
        "MERGE (p)-[:AUTHORED]->(d) "
        "MERGE (d)-[:ABOUT]->(s)"
    )

    # 5. Mark Meta as ready=true
    end_ts = datetime.now(timezone.utc).isoformat()
    doc_count = graph.query("MATCH (d:Document) RETURN count(d)").result_set[0][0]
    chunk_count = graph.query("MATCH (c:Chunk) RETURN count(c)").result_set[0][0]

    graph.query(
        "MATCH (m:Meta {id: 'company_brain_meta'}) "
        "SET m.ready = true, "
        "    m.hash = $hash, "
        "    m.completed_at = $ts, "
        "    m.documents_count = $doc_count, "
        "    m.chunks_count = $chunk_count",
        {
            "hash": manifest["manifest_hash"],
            "ts": end_ts,
            "doc_count": doc_count,
            "chunk_count": chunk_count,
        },
    )

    return {
        "ready": True,
        "graph": graph_name,
        "documents": doc_count,
        "chunks": chunk_count,
    }


def is_company_brain_ready(graph_name: str = "company_brain") -> bool:
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    try:
        res = db.select_graph(graph_name).ro_query("MATCH (m:Meta {id: 'company_brain_meta'}) RETURN m.ready").result_set
        return bool(res and res[0][0] is True)
    except Exception:
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", default="company_brain", help="Graph name")
    parser.add_argument("--wipe", action="store_true", help="Wipe graph before load")
    args = parser.parse_args()

    result = asyncio.run(ingest_company_brain(graph_name=args.graph, wipe=args.wipe))
    print(f"Company Brain ingestion result: {result}")
    sys.exit(0 if result.get("ready") else 1)
