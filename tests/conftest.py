"""Shared isolated FalkorDB fixtures for ChronosOps tests."""
from __future__ import annotations

import csv
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from falkordb import FalkorDB
from redis.exceptions import ResponseError

# conftest loads before application test modules. Never let a developer's default
# production namespace leak into an ad-hoc pytest invocation.
_PREFIX = os.environ.get("CHRONOSOPS_GRAPH_PREFIX", "")
if not _PREFIX.startswith("test_"):
    os.environ["CHRONOSOPS_GRAPH_PREFIX"] = "test_suite_"
os.environ["REAL_RECOVERY_ENABLED"] = "false"
# boto3 treats an empty AWS_PROFILE as an explicit nonexistent profile.
# Isolated tests use dummy environment credentials, not a host profile.
if not os.environ.get("AWS_PROFILE"):
    os.environ.pop("AWS_PROFILE", None)

from agents.settings import GraphNames, graph_names
from seed.load import get_config, load_topology

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "seed" / "data"


def _admin_db() -> FalkorDB:
    config = get_config()
    return FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )


def _delete_graph(db: FalkorDB, name: str) -> None:
    try:
        db.select_graph(name).delete()
    except ResponseError as error:
        if "Invalid graph operation on empty key" not in str(error):
            raise


def _read_csv(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / name).open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _seed_company_fixture(graph_name: str) -> None:
    """Seed deterministic native and SDK-shaped data without invoking Bedrock."""
    graph = _admin_db().select_graph(graph_name)
    graph.query(
        "MERGE (m:Meta {id: 'company_brain_meta'}) "
        "SET m.ready = true, m.hash = 'test-company-fixture-v1'"
    )
    graph.query(
        "UNWIND $services AS service "
        "MERGE (s:Service {service_id: service.service_id}) "
        "SET s.name = service.service_name, s.environment = service.environment, "
        "s.owner_team = service.owner_team, s.tier = service.tier",
        {"services": _read_csv("services.csv")},
    )
    graph.query(
        "UNWIND $teams AS team "
        "MERGE (t:Team {team_id: team.team_id}) SET t.name = team.name",
        {"teams": _read_csv("teams.csv")},
    )
    graph.query(
        "UNWIND $people AS person "
        "MERGE (p:Person {person_id: person.person_id}) "
        "SET p.name = person.name, p.email = person.email, p.team = person.team",
        {"people": _read_csv("people.csv")},
    )
    graph.query(
        "MATCH (p:Person), (t:Team) WHERE p.team = t.name "
        "MERGE (p)-[:MEMBER_OF]->(t)"
    )
    graph.query(
        "MATCH (t:Team), (s:Service) WHERE t.name = s.owner_team "
        "MERGE (t)-[:OWNS]->(s)"
    )
    graph.query(
        "UNWIND $aliases AS alias "
        "MERGE (a:Alias {name: alias.alias}) "
        "WITH a, alias MATCH (s:Service {service_id: alias.service_id}) "
        "MERGE (a)-[:POINTS_TO]->(s)",
        {"aliases": _read_csv("aliases.csv")},
    )

    # This is deliberately shaped like the pinned SDK output. The native
    # authorship edges remain deterministic fixture data until step 5 owns the
    # supported ingestion lifecycle.
    documents = [
        {
            "id": "rfc-001-payments-failover",
            "chunk_id": "chunk-rfc-001-0",
            "service_id": "payments-primary",
            "text": "Queue failover is approved because rollback speed stays within the cost cap.",
            "author_id": "user-alice",
        },
        {
            "id": "slack-001-failover-budget",
            "chunk_id": "chunk-slack-001-0",
            "service_id": "payments-primary",
            "text": "The Payments team confirmed the queue failover budget decision.",
            "author_id": "user-alice",
        },
        {
            "id": "rfc-002-checkout-resilience",
            "chunk_id": "chunk-rfc-002-0",
            "service_id": "checkout",
            "text": "Checkout preserves resilience while upstream payments recovers.",
            "author_id": "user-carol",
        },
    ]
    graph.query(
        "UNWIND $documents AS document "
        "MERGE (d:Document {id: document.id}) "
        "MERGE (c:Chunk {id: document.chunk_id}) "
        "SET c.text = document.text "
        "MERGE (d)-[:PART_OF]->(c) "
        "WITH d, c, document "
        "MATCH (s:Service {service_id: document.service_id}) "
        "MATCH (p:Person {person_id: document.author_id}) "
        "MERGE (d)-[:ABOUT]->(s) "
        "MERGE (s)-[:MENTIONED_IN]->(c) "
        "MERGE (c)-[:RELATES {rel_type: 'MENTIONS'}]->(s) "
        "MERGE (p)-[:AUTHORED]->(d)",
        {"documents": documents},
    )
    graph.query(
        "MATCH (p:Person {person_id: 'user-alice'}), "
        "(d:Document {id: 'rfc-001-payments-failover'}) "
        "MERGE (p)-[:RESOLVED]->(d)"
    )


@pytest.fixture(autouse=True)
def isolated_graphs() -> Iterator[GraphNames]:
    """Give every test a fresh server-owned graph namespace and no memory history."""
    names = graph_names()
    graph_names_to_clean = (
        names.topology,
        names.company,
        names.memory_for("acme"),
        names.memory_for("globex"),
    )
    if not all(name.startswith("test_") for name in graph_names_to_clean):
        raise RuntimeError("pytest may only clean graphs in a test_ namespace")

    db = _admin_db()
    try:
        for name in graph_names_to_clean:
            _delete_graph(db, name)

        load_topology(graph_name=names.topology, wipe=False)
        _seed_company_fixture(names.company)
    except Exception:
        for name in graph_names_to_clean:
            _delete_graph(db, name)
        raise

    try:
        yield names
    finally:
        for name in graph_names_to_clean:
            _delete_graph(db, name)
