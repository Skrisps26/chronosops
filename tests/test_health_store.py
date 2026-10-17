"""Stored probe identities must be attributable, immutable and bound to the incident."""
from datetime import datetime, timedelta, timezone

import pytest
from falkordb import FalkorDB

from agents.settings import graph_names
from integrations.health_store import HealthEvidenceConflict, persist_health_samples
from integrations.monitoring import HealthSample
from seed.load import get_config


@pytest.fixture
def observation_binding():
    config = get_config()
    graph = FalkorDB(host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
                     username=config["FALKORDB_ADMIN_USERNAME"],
                     password=config["FALKORDB_ADMIN_PASSWORD"]).select_graph(graph_names().topology)
    binding = {"deployment_id": "test-observation-deployment", "endpoint": "http://203.0.113.7/healthz",
               "service_id": "payments-primary", "tenant_id": "acme", "active": True,
               "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    graph.query("CREATE (d:Deployment) SET d += $binding", {"binding": binding})
    graph.query("CREATE (:Incident {incident_id:'test-observed-outage', tenant_id:'acme', deployment_id:$deployment})",
                {"deployment": binding["deployment_id"]})
    return binding, graph


def _sample(binding, identity="test-scrape"):
    return HealthSample(id=identity, deployment_id=binding["deployment_id"], endpoint=binding["endpoint"],
                        observed_at=datetime.now(timezone.utc).timestamp(), probe_success=False,
                        http_status=0, duration_seconds=0.1, exporter_up=True)


def test_scrape_replay_is_idempotent_and_links_actual_evidence(observation_binding):
    binding, graph = observation_binding
    sample = _sample(binding)
    persist_health_samples(binding, [sample], incident_id="test-observed-outage")
    persist_health_samples(binding, [sample], incident_id="test-observed-outage")
    rows = graph.ro_query("MATCH (:Deployment {deployment_id:$deployment})-[:HAS_OBSERVATION]->(h:HealthObservation) "
                          "<-[:HEALTH_EVIDENCE]-(:Incident {incident_id:'test-observed-outage'}) "
                          "RETURN h.id, h.probe_success, h.observed_at", {"deployment": binding["deployment_id"]}).result_set
    assert [(row[0], row[1]) for row in rows] == [(sample.id, False)]
    assert rows[0][2] == pytest.approx(sample.observed_at, rel=0, abs=0.00001)


def test_changed_scrape_cannot_overwrite_failed_probe_evidence(observation_binding):
    binding, graph = observation_binding
    sample = _sample(binding)
    persist_health_samples(binding, [sample])
    changed = sample.model_copy(update={"probe_success": True, "http_status": 200})
    with pytest.raises(HealthEvidenceConflict):
        persist_health_samples(binding, [changed])
    assert graph.ro_query("MATCH (h:HealthObservation {id:$id}) RETURN h.probe_success", {"id": sample.id}).result_set == [[False]]


def test_foreign_inactive_or_unattributed_samples_are_not_accepted(observation_binding):
    binding, graph = observation_binding
    sample = _sample(binding)
    with pytest.raises(HealthEvidenceConflict):
        persist_health_samples(binding, [sample.model_copy(update={"deployment_id": "foreign"})])
    with pytest.raises(HealthEvidenceConflict):
        persist_health_samples(binding, [sample], incident_id="foreign-incident")
    graph.query("MATCH (d:Deployment {deployment_id:$id}) SET d.active=false", {"id": binding["deployment_id"]})
    with pytest.raises(HealthEvidenceConflict):
        persist_health_samples(binding, [sample])
