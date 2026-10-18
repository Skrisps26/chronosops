"""Load operational topology into FalkorDB ops_topology graph idempotently."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from falkordb import FalkorDB
from agents.settings import TENANTS, graph_names

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_FILE = ROOT / "seed" / "manifest.json"
SECRETS_FILE = ROOT / ".secrets" / "db.env"
BATCH_SIZE = 300


def get_config() -> dict[str, str]:
    config = {k: v for k, v in dotenv_values(SECRETS_FILE).items() if isinstance(v, str) and v}
    config.update({k: v for k, v in os.environ.items() if v})
    config.setdefault("FALKORDB_HOST", "127.0.0.1")
    config.setdefault("FALKORDB_PORT", "6379")
    config.setdefault("FALKORDB_ADMIN_USERNAME", "admin")
    return config


def ensure_application_constraints(graph: Any, *, company: bool = False) -> None:
    """Install application identity constraints using trusted seeding credentials."""
    identities = (("SlackInbox", ("event_id",)), ("SourceRecord", ("document_id",)),
                  ("SourceRequest", ("request_id",)), ("SlackConnector", ("tenant_id", "connector_id"))) \
        if company else (
            ("SlackNotification", ("outbox_key",)), ("ApprovalRecord", ("approval_id",)),
            ("RecoverySlot", ("id",)), ("TrafficState", ("tenant_id", "incident_id")),
            ("Incident", ("tenant_id", "incident_id")), ("HealthObservation", ("id",)),
        )
    for label, properties in identities:
        existing = [item for item in graph.list_constraints()
                    if item["type"] == "UNIQUE" and item["label"] == label
                    and tuple(item["properties"]) == properties]
        if not existing:
            graph.create_node_unique_constraint(label, *properties)
        deadline = time.monotonic() + 3
        while True:
            matches = [item for item in graph.list_constraints()
                       if item["type"] == "UNIQUE" and item["label"] == label
                       and tuple(item["properties"]) == properties]
            if len(matches) == 1 and matches[0]["status"] == "OPERATIONAL":
                break
            if any(item["status"] == "FAILED" for item in matches) or time.monotonic() >= deadline:
                raise RuntimeError(f"Application identity constraint unavailable: {label}")
            time.sleep(0.02)


def load_topology(
    graph_name: str = "ops_topology",
    simulate_interruption: bool = False,
    wipe: bool = False,
) -> dict[str, Any]:
    if not MANIFEST_FILE.is_file():
        raise FileNotFoundError(f"Manifest not found at {MANIFEST_FILE}. Run seed/generate.py first.")

    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
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

    # 1. Initialize Meta record to ready=false
    start_ts = datetime.now(timezone.utc).isoformat()
    graph.query(
        "MERGE (m:Meta {id: 'ops_topology_meta'}) "
        "SET m.ready = false, m.hash = $hash, m.started_at = $ts",
        {"hash": manifest["manifest_hash"], "ts": start_ts},
    )
    ensure_application_constraints(graph)
    graph.query(
        "MERGE (slot:RecoverySlot {id:'real'}) ON CREATE SET slot.status='free'",
        timeout=3000,
    )

    if simulate_interruption:
        print("Simulating load interruption before loading entities...")
        return {"ready": False, "status": "interrupted"}

    # 2. Batch load services
    services = manifest["services"]
    for i in range(0, len(services), BATCH_SIZE):
        batch = services[i : i + BATCH_SIZE]
        graph.query(
            "UNWIND $batch AS s "
            "MERGE (node:Service {service_id: s.service_id}) "
            "SET node.name = s.name, "
            "    node.owner_team = s.owner_team, "
            "    node.environment = s.environment, "
            "    node.health = s.health, "
            "    node.tier = s.tier, "
            "    node.origin = s.origin",
            {"batch": batch},
        )

    # 3. Batch load dependencies
    dependencies = manifest["dependencies"]
    for i in range(0, len(dependencies), BATCH_SIZE):
        batch = dependencies[i : i + BATCH_SIZE]
        graph.query(
            "UNWIND $batch AS d "
            "MATCH (src:Service {service_id: d.source_id}) "
            "MATCH (dst:Service {service_id: d.target_id}) "
            "MERGE (src)-[r:DEPENDS_ON]->(dst) "
            "SET r.origin = d.origin",
            {"batch": batch},
        )

    # 4. Batch load routes
    routes = manifest["routes"]
    for i in range(0, len(routes), BATCH_SIZE):
        batch = routes[i : i + BATCH_SIZE]
        graph.query(
            "UNWIND $batch AS r "
            "MATCH (src:Service {service_id: r.source_id}) "
            "MATCH (dst:Service {service_id: r.target_id}) "
            "MERGE (src)-[route:ROUTES_TO]->(dst) "
            "SET route.latency_ms = r.latency_ms, "
            "    route.cost = r.cost, "
            "    route.health = r.health, "
            "    route.origin = r.origin",
            {"batch": batch},
        )

    # 5. Count verification
    svc_count = graph.query("MATCH (s:Service) RETURN count(s) AS count").result_set[0][0]
    dep_count = graph.query("MATCH ()-[r:DEPENDS_ON]->() RETURN count(r) AS count").result_set[0][0]
    route_count = graph.query("MATCH ()-[r:ROUTES_TO]->() RETURN count(r) AS count").result_set[0][0]

    expected_services = manifest["counts"]["services"]
    expected_deps = manifest["counts"]["dependencies"]
    expected_routes = manifest["counts"]["routes"]

    if svc_count != expected_services or dep_count != expected_deps or route_count != expected_routes:
        raise RuntimeError(
            f"Counts validation failed! Observed: services={svc_count}, deps={dep_count}, routes={route_count}. "
            f"Expected: services={expected_services}, deps={expected_deps}, routes={expected_routes}."
        )

    # A successfully initialized namespace has known empty tenant-private memory.
    # Reader failures must never be interpreted as an empty exclusion history.
    names = graph_names()
    if graph_name == names.topology:
        for tenant in sorted(TENANTS):
            db.select_graph(names.memory_for(tenant)).query(
                "MERGE (m:MemoryMeta {id:'memory_meta'}) "
                "ON CREATE SET m.schema_version=1, m.ready=true"
            )

    # 6. Mark Meta as ready=true only upon successful verification
    end_ts = datetime.now(timezone.utc).isoformat()
    graph.query(
        "MATCH (m:Meta {id: 'ops_topology_meta'}) "
        "SET m.ready = true, "
        "    m.hash = $hash, "
        "    m.completed_at = $ts, "
        "    m.service_count = $svc_count, "
        "    m.dependency_count = $dep_count, "
        "    m.route_count = $route_count",
        {
            "hash": manifest["manifest_hash"],
            "ts": end_ts,
            "svc_count": svc_count,
            "dep_count": dep_count,
            "route_count": route_count,
        },
    )

    return {
        "ready": True,
        "graph": graph_name,
        "hash": manifest["manifest_hash"],
        "services": svc_count,
        "dependencies": dep_count,
        "routes": route_count,
    }


def is_graph_ready(graph_name: str = "ops_topology") -> bool:
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    try:
        res = db.select_graph(graph_name).ro_query("MATCH (m:Meta {id: 'ops_topology_meta'}) RETURN m.ready").result_set
        return bool(res and res[0][0] is True)
    except Exception:
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", default="ops_topology", help="Graph name to load into")
    parser.add_argument("--wipe", action="store_true", help="Wipe graph before load")
    parser.add_argument("--simulate-interruption", action="store_true", help="Simulate failure before loading data")
    args = parser.parse_args()

    result = load_topology(
        graph_name=args.graph,
        simulate_interruption=args.simulate_interruption,
        wipe=args.wipe,
    )
    print(f"Load result: {result}")
    sys.exit(0 if result.get("ready") else 1)
