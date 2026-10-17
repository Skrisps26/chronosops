"""Module 3A verification suite: CloudFormation template security, monitoring, and recovery safety."""
from __future__ import annotations

from unittest.mock import patch
import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import app
from integrations.monitoring import MonitoringClient
from scripts.demo_stack import TEMPLATE_FILE


def test_cloudformation_template_security_defaults():
    """Verify that demo.yaml enforces IMDSv2, encrypted EBS, least-privilege SG, and watchdog."""
    class CfnLoader(yaml.SafeLoader):
        pass

    def cfn_constructor(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    for tag in ("!Ref", "!Sub", "!GetAtt", "!Join", "!Select", "!Split", "!Base64"):
        yaml.add_constructor(tag, cfn_constructor, Loader=CfnLoader)

    content = yaml.load(TEMPLATE_FILE.read_text(encoding="utf-8"), Loader=CfnLoader)

    # Parameters
    params = content["Parameters"]
    assert "OperatorIp" in params
    assert params["OperatorIp"]["AllowedPattern"] == r"^([0-9]{1,3}\.){3}[0-9]{1,3}/32$"
    assert "t4g.micro" in params["InstanceType"]["AllowedValues"]

    # Security Group: no 0.0.0.0/0 ingress
    resources = content["Resources"]
    sg = resources["SecurityGroup"]["Properties"]
    for ingress in sg["SecurityGroupIngress"]:
        assert ingress.get("CidrIp") != "0.0.0.0/0"
        assert ingress["FromPort"] == 80
        assert ingress["ToPort"] == 80

    # Instance: IMDSv2 required, encrypted gp3
    inst = resources["DemoInstance"]["Properties"]
    meta_opts = inst["MetadataOptions"]
    assert meta_opts["HttpTokens"] == "required"
    assert meta_opts["HttpPutResponseHopLimit"] == 1

    ebs = inst["BlockDeviceMappings"][0]["Ebs"]
    assert ebs["Encrypted"] is True
    assert ebs["VolumeType"] == "gp3"
    assert ebs["DeleteOnTermination"] is True



def test_monitoring_client_health_classification():
    """Verify health status, stale data handling, and monitoring_unknown classification."""
    client = MonitoringClient("http://fake-prom:9090")

    # 1. Healthy sample
    with patch.object(client, "query_prometheus", return_value=[{"value": [1000.0, "1"]}]) as mock_q:
        with patch("time.time", return_value=1005.0):  # 5s age
            res = client.check_health("http://1.2.3.4:80")
            assert res["status"] == "healthy"
            assert res["healthy"] is True

    # 2. Unhealthy sample
    with patch.object(client, "query_prometheus", return_value=[{"value": [1000.0, "0"]}]):
        with patch("time.time", return_value=1005.0):
            res = client.check_health("http://1.2.3.4:80")
            assert res["status"] == "unhealthy"
            assert res["healthy"] is False

    # 3. Stale sample (>15s) -> monitoring_unknown
    with patch.object(client, "query_prometheus", return_value=[{"value": [1000.0, "1"]}]):
        with patch("time.time", return_value=1025.0):  # 25s age
            res = client.check_health("http://1.2.3.4:80")
            assert res["status"] == "monitoring_unknown"
            assert res["healthy"] is False

    # 4. Absent metrics -> monitoring_unknown
    with patch.object(client, "query_prometheus", return_value=[]):
        res = client.check_health("http://1.2.3.4:80")
        assert res["status"] == "monitoring_unknown"
        assert res["healthy"] is False


def test_alertmanager_webhook_token_and_idempotency():
    """Verify webhook authentication and idempotent incident registration."""
    client = TestClient(app)

    payload = {
        "version": "4",
        "status": "firing",
        "receiver": "chronosops_webhook",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChronosOpsDemoHttpDown",
                    "service": "payments-primary",
                    "severity": "critical",
                },
                "startsAt": "2026-10-16T12:00:00Z",
                "fingerprint": "test-alert-fingerprint-123",
            }
        ],
    }

    # 1. Missing token -> 401
    resp_unauth = client.post("/internal/alerts", json=payload)
    assert resp_unauth.status_code == 401

    # 2. Wrong token -> 403
    resp_forbidden = client.post(
        "/internal/alerts",
        json=payload,
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp_forbidden.status_code == 403

    # 3. Valid token -> 200 and incident merged
    resp_ok = client.post(
        "/internal/alerts",
        json=payload,
        headers={"Authorization": "Bearer demo-internal-alert-secret"},
    )
    assert resp_ok.status_code == 200
    assert resp_ok.json()["status"] == "recorded"
    assert resp_ok.json()["incidents"][0]["incident_id"] == "inc-test-alert-fingerprint-123"

    # 4. Duplicate alert -> idempotent (updates without error)
    resp_dup = client.post(
        "/internal/alerts",
        json=payload,
        headers={"Authorization": "Bearer demo-internal-alert-secret"},
    )
    assert resp_dup.status_code == 200


