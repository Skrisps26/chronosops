"""Persist attributable probe observations without turning missing metrics into evidence."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from falkordb import FalkorDB

from agents.settings import graph_names
from integrations.monitoring import HealthSample
from seed.load import get_config


class HealthEvidenceConflict(ValueError):
    """A scrape identity changed or the target ceased to be the active deployment."""


def persist_health_samples(binding: dict[str, Any], samples: list[HealthSample], *,
                           incident_id: str | None = None) -> list[str]:
    if not samples or len(samples) > 50:
        raise HealthEvidenceConflict("Expected 1..50 bounded probe samples")
    rows = []
    for sample in samples:
        if sample.deployment_id != binding["deployment_id"] or sample.endpoint != binding["endpoint"]:
            raise HealthEvidenceConflict("Observation does not belong to the bound workload")
        row = sample.model_dump(mode="json")
        row["origin"] = "probe"
        row["data_hash"] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        rows.append(row)
    if len({row["id"] for row in rows}) != len(rows):
        raise HealthEvidenceConflict("Duplicate observation identities")
    config = get_config()
    graph = FalkorDB(host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
                     username=config["FALKORDB_ADMIN_USERNAME"],
                     password=config["FALKORDB_ADMIN_PASSWORD"]).select_graph(graph_names().topology)
    accepted = graph.query(
        "MATCH (d:Deployment {deployment_id:$deployment, active:true}) "
        "UNWIND $samples AS sample "
        "MERGE (h:HealthObservation {id:sample.id}) ON CREATE SET h += sample "
        "WITH d, h, sample WHERE h.data_hash = sample.data_hash "
        "MERGE (d)-[:HAS_OBSERVATION]->(h) RETURN h.id LIMIT 50",
        {"deployment": binding["deployment_id"], "samples": rows}, timeout=3000).result_set
    ids = [row[0] for row in accepted]
    if set(ids) != {row["id"] for row in rows}:
        raise HealthEvidenceConflict("Scrape identity conflict or inactive deployment")
    if incident_id:
        linked = graph.query(
            "MATCH (i:Incident {incident_id:$incident, tenant_id:'acme', deployment_id:$deployment}) "
            "MATCH (h:HealthObservation) WHERE h.id IN $ids "
            "MERGE (i)-[:HEALTH_EVIDENCE]->(h) RETURN h.id LIMIT 50",
            {"incident": incident_id, "deployment": binding["deployment_id"], "ids": ids}, timeout=3000).result_set
        if {row[0] for row in linked} != set(ids):
            raise HealthEvidenceConflict("Incident does not belong to the observed deployment")
    return ids
