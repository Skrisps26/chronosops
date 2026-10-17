"""File discovery must follow the authoritative binding and discard stale targets."""
import json

import pytest

from scripts.configure_monitoring import MonitoringNotReadyError, configure_monitoring
from tests.test_health_store import observation_binding


def test_unbound_monitoring_clears_previously_generated_target(tmp_path):
    target = tmp_path / "targets" / "demo.json"
    target.parent.mkdir()
    target.write_text(json.dumps([{"targets": ["http://stale.example/healthz"]}]))
    with pytest.raises(MonitoringNotReadyError):
        configure_monitoring(target_file=target)
    assert json.loads(target.read_text()) == []


def test_binding_changes_replace_the_observed_target_not_the_application(observation_binding, tmp_path):
    binding, graph = observation_binding
    target = tmp_path / "targets" / "demo.json"
    configure_monitoring(target_file=target)
    first = json.loads(target.read_text())
    assert first[0]["targets"] == [binding["endpoint"]]
    assert first[0]["labels"]["deployment_id"] == binding["deployment_id"]
    graph.query("MATCH (d:Deployment {deployment_id:$id}) SET d.endpoint=$endpoint",
                {"id": binding["deployment_id"], "endpoint": "http://203.0.113.8/healthz"})
    configure_monitoring(target_file=target)
    assert json.loads(target.read_text())[0]["targets"] == ["http://203.0.113.8/healthz"]
    graph.query("MATCH (d:Deployment {deployment_id:$id}) SET d.active=false", {"id": binding["deployment_id"]})
    with pytest.raises(MonitoringNotReadyError):
        configure_monitoring(target_file=target)
    assert json.loads(target.read_text()) == []
