"""Operator-verified binding of the one disposable workload; artifacts are not authority."""
from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from falkordb import FalkorDB

from agents.settings import graph_names

ROOT = Path(__file__).resolve().parents[1]
STACK_ARTIFACT = ROOT / "artifacts" / "demo-stack.json"
START_COMMANDS = [
    "set -eu",
    "/usr/bin/systemctl start nginx.service",
    "/usr/bin/systemctl is-active --quiet nginx.service",
    "/usr/bin/systemctl stop chronosops-watchdog.timer || true",
]


class DeploymentValidationError(ValueError):
    """The workload cannot currently be proved to match the controlled demo."""


def operator_session(config: dict[str, str]) -> Any:
    return boto3.Session(region_name=config["AWS_REGION"],
                         profile_name=config.get("AWS_PROFILE") or None)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DeploymentValidationError(message)


def _tags(rows: list[dict[str, str]]) -> dict[str, str]:
    return {row["Key"]: row["Value"] for row in rows}


def verify_deployment(config: dict[str, str], *, publish: bool = False) -> dict[str, Any]:
    """Refresh all AWS evidence before publishing or operator use. No AWS mutations."""
    session = operator_session(config)
    read_config = Config(connect_timeout=5, read_timeout=10,
                         retries={"total_max_attempts": 2, "mode": "standard"})
    sts = session.client("sts", config=read_config)
    cfn = session.client("cloudformation", config=read_config)
    ec2 = session.client("ec2", config=read_config)
    ssm = session.client("ssm", config=read_config)
    account = sts.get_caller_identity()["Account"]
    stacks = cfn.describe_stacks(StackName=config["DEMO_STACK_NAME"])["Stacks"]
    _require(len(stacks) == 1, "Expected exactly one demo stack")
    stack = stacks[0]
    stack_id = stack["StackId"]
    _require(stack_id.split(":")[3:5] == [config["AWS_REGION"], account],
             "Stack account/region mismatch")
    _require(stack["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"},
             "Demo stack is not complete")
    tags = _tags(stack.get("Tags", []))
    _require(tags.get("Project") == "ChronosOps" and tags.get("Environment") == "demo",
             "Missing dedicated demo stack tags")
    _require(bool(tags.get("Operator")), "Missing operator tag")
    try:
        expires = datetime.fromisoformat(tags["ExpiresAt"].replace("Z", "+00:00"))
        _require(expires.tzinfo is not None and expires > datetime.now(timezone.utc),
                 "Deployment expired")
    except (KeyError, ValueError) as error:
        raise DeploymentValidationError("Missing or invalid deployment expiry") from error
    outputs = {row["OutputKey"]: row["OutputValue"] for row in stack.get("Outputs", [])}
    required = {"InstanceId", "PublicIp", "StartDocumentName", "StopDocumentName", "RecoveryRoleArn"}
    _require(required <= outputs.keys(), "Incomplete deployment outputs")
    instance_id = outputs["InstanceId"]
    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    instances = [instance for reservation in reservations for instance in reservation["Instances"]]
    _require(len(instances) == 1, "Expected exactly one bound instance")
    instance = instances[0]
    instance_tags = _tags(instance.get("Tags", []))
    expected_tags = {"Project": "ChronosOps", "Environment": "demo", "Service": "payments-primary",
                     "aws:cloudformation:stack-id": stack_id, "Operator": tags["Operator"],
                     "ExpiresAt": tags["ExpiresAt"]}
    _require(all(instance_tags.get(key) == value for key, value in expected_tags.items()),
             "Instance ownership/stack tags mismatch")
    _require(instance["State"]["Name"] == "running", "Demo instance is not running")
    _require(instance.get("MetadataOptions", {}).get("HttpTokens") == "required",
             "Instance does not require IMDSv2")
    public_ip = str(ipaddress.IPv4Address(instance.get("PublicIpAddress", "")))
    _require(public_ip == outputs["PublicIp"], "Endpoint no longer matches stack output")
    roots = [mapping["Ebs"]["VolumeId"] for mapping in instance.get("BlockDeviceMappings", [])
             if mapping["DeviceName"] == instance.get("RootDeviceName") and "Ebs" in mapping]
    _require(len(roots) == 1, "Missing bound root volume")
    volumes = ec2.describe_volumes(VolumeIds=roots)["Volumes"]
    _require(len(volumes) == 1 and volumes[0].get("Encrypted") is True
             and volumes[0].get("VolumeType") == "gp3", "Root volume is not encrypted gp3")
    credit = ec2.describe_instance_credit_specifications(InstanceIds=[instance_id])["InstanceCreditSpecifications"]
    _require(len(credit) == 1 and credit[0].get("CpuCredits") == "standard", "CPU credits are not standard")
    managed = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])["InstanceInformationList"]
    _require(len(managed) == 1 and managed[0].get("PingStatus") == "Online", "Bound SSM agent is not online")
    document = ssm.get_document(Name=outputs["StartDocumentName"], DocumentVersion="$DEFAULT", DocumentFormat="JSON")
    version = document["DocumentVersion"]
    _require(version.isdigit() and int(version) > 0, "Recovery document version is not numeric")
    content = document["Content"]
    parsed = json.loads(content)
    steps = parsed.get("mainSteps", [])
    _require(parsed.get("schemaVersion") == "2.2" and not parsed.get("parameters")
             and len(steps) == 1 and steps[0].get("name") == "startNginx"
             and steps[0].get("action") == "aws:runShellScript"
             and steps[0].get("inputs", {}).get("runCommand") == START_COMMANDS,
             "Recovery document does not match the parameterless approved service operation")
    digest = hashlib.sha256(content.encode()).hexdigest()
    role_arn = outputs["RecoveryRoleArn"]
    _require(role_arn.startswith(f"arn:aws:iam::{account}:role/"), "Recovery role account mismatch")
    deployment = {
        "deployment_id": "deployment-" + hashlib.sha256(f"{stack_id}:{instance_id}".encode()).hexdigest()[:24],
        "service_id": "payments-primary", "tenant_id": "acme", "account_id": account,
        "region": config["AWS_REGION"], "instance_id": instance_id,
        "endpoint": f"http://{public_ip}/healthz", "stack_id": stack_id,
        "project": "ChronosOps", "environment": "demo", "operator": tags["Operator"],
        "expires_at": tags["ExpiresAt"], "document_name": outputs["StartDocumentName"],
        "stop_document_name": outputs["StopDocumentName"], "document_version": version,
        "document_hash": digest, "recovery_role_arn": role_arn,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    for setting, field in {"DEMO_INSTANCE_ID": "instance_id", "DEMO_AWS_ACCOUNT_ID": "account_id",
                           "DEMO_RECOVERY_DOCUMENT": "document_name",
                           "DEMO_RECOVERY_DOCUMENT_VERSION": "document_version",
                           "DEMO_RECOVERY_DOCUMENT_HASH": "document_hash"}.items():
        _require(not config.get(setting) or config[setting] == deployment[field], f"{setting} expectation mismatch")
    if publish:
        graph = FalkorDB(host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
                         username=config["FALKORDB_ADMIN_USERNAME"],
                         password=config["FALKORDB_ADMIN_PASSWORD"]).select_graph(graph_names().topology)
        graph.query("OPTIONAL MATCH (old:Deployment {service_id:'payments-primary', tenant_id:'acme'}) "
                    "SET old.active = false WITH count(old) AS previous "
                    "MERGE (d:Deployment {deployment_id: $id}) SET d += $binding, d.active = true",
                    {"id": deployment["deployment_id"], "binding": deployment}, timeout=3000)
        STACK_ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        STACK_ARTIFACT.write_text(json.dumps({"deployment": deployment, "outputs": outputs,
                                            "stack_id": stack_id, "region": config["AWS_REGION"]}, indent=2) + "\n")
    return deployment


def bound_deployment(config: dict[str, str]) -> dict[str, Any]:
    """Read the single unexpired published binding. Never resolve a browser-selected target."""
    graph = FalkorDB(host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
                     username=config["FALKORDB_ADMIN_USERNAME"],
                     password=config["FALKORDB_ADMIN_PASSWORD"]).select_graph(graph_names().topology)
    rows = graph.ro_query("MATCH (d:Deployment {service_id:'payments-primary', tenant_id:'acme', active:true}) "
                          "RETURN properties(d) ORDER BY d.verified_at DESC LIMIT 2", timeout=3000).result_set
    _require(len(rows) == 1, "Expected exactly one authoritative workload binding")
    binding = dict(rows[0][0])
    expiry = datetime.fromisoformat(binding["expires_at"].replace("Z", "+00:00"))
    _require(expiry.tzinfo is not None and expiry > datetime.now(timezone.utc), "Deployment expired")
    return binding
