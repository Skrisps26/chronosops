"""A deployment must be proved, not inferred from an artifact or environment variable."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from integrations import deployment
from integrations.aws_actions import AwsRecoveryClient, RecoveryExecutionError
from scripts import demo_stack


@pytest.fixture
def aws_evidence(monkeypatch):
    account = "123456789012"
    stack_id = f"arn:aws:cloudformation:us-east-1:{account}:stack/chronosops-demo/fixture"
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    tags = {"Project": "ChronosOps", "Environment": "demo", "Operator": "fixture", "ExpiresAt": expiry}
    tag_rows = lambda values: [{"Key": key, "Value": value} for key, value in values.items()]
    outputs = {"InstanceId": "i-0123456789abcdef0", "PublicIp": "203.0.113.7", "StartDocumentName": "chronosops-demo-StartNginx",
               "StopDocumentName": "chronosops-demo-StopNginx", "RecoveryRoleArn": f"arn:aws:iam::{account}:role/demo-recovery"}
    stack = {"StackId": stack_id, "StackStatus": "CREATE_COMPLETE", "Tags": tag_rows(tags),
             "Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in outputs.items()]}
    instance = {"InstanceId": outputs["InstanceId"], "State": {"Name": "running"},
                "Tags": tag_rows({**tags, "Service": "payments-primary", "aws:cloudformation:stack-id": stack_id}),
                "PublicIpAddress": outputs["PublicIp"], "MetadataOptions": {"HttpTokens": "required"},
                "RootDeviceName": "/dev/xvda", "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": "vol-fixture"}}]}
    clients = {name: MagicMock() for name in ("sts", "cloudformation", "ec2", "ssm")}
    clients["sts"].get_caller_identity.return_value = {"Account": account}
    clients["cloudformation"].describe_stacks.return_value = {"Stacks": [stack]}
    clients["ec2"].describe_instances.return_value = {"Reservations": [{"Instances": [instance]}]}
    clients["ec2"].describe_volumes.return_value = {"Volumes": [{"Encrypted": True, "VolumeType": "gp3"}]}
    clients["ec2"].describe_instance_credit_specifications.return_value = {"InstanceCreditSpecifications": [{"CpuCredits": "standard"}]}
    clients["ssm"].describe_instance_information.return_value = {"InstanceInformationList": [{"PingStatus": "Online"}]}
    content = {"schemaVersion": "2.2", "mainSteps": [{"name": "startNginx", "action": "aws:runShellScript", "inputs": {"runCommand": deployment.START_COMMANDS}}]}
    clients["ssm"].get_document.return_value = {"DocumentVersion": "3", "Content": json.dumps(content)}
    session = MagicMock()
    session.client.side_effect = lambda name, **kwargs: clients[name]
    monkeypatch.setattr(deployment, "operator_session", lambda config: session)
    return SimpleNamespace(config={"AWS_REGION": "us-east-1", "DEMO_STACK_NAME": "chronosops-demo"},
                           clients=clients, stack=stack, instance=instance, document=content)


def test_verified_binding_retains_actual_endpoint_and_pinned_document(aws_evidence):
    result = deployment.verify_deployment(aws_evidence.config)
    assert result["endpoint"] == "http://203.0.113.7/healthz"
    assert result["document_version"] == "3"
    assert result["account_id"] == "123456789012"
    assert result["instance_id"] == aws_evidence.instance["InstanceId"]
    import hashlib
    assert result["document_hash"] == hashlib.sha256(json.dumps(aws_evidence.document).encode()).hexdigest()


@pytest.mark.parametrize("change", ["foreign-stack", "expired", "offline", "arbitrary-script", "expectation", "unlimited-credits"])
def test_unproved_or_drifted_deployment_refuses_publication(aws_evidence, monkeypatch, change):
    evidence = aws_evidence
    if change == "foreign-stack":
        evidence.instance["Tags"] = []
    elif change == "expired":
        next(row for row in evidence.stack["Tags"] if row["Key"] == "ExpiresAt")["Value"] = "2000-01-01T00:00:00Z"
    elif change == "offline":
        evidence.clients["ssm"].describe_instance_information.return_value = {"InstanceInformationList": []}
    elif change == "arbitrary-script":
        changed = deepcopy(evidence.document)
        changed["mainSteps"][0]["inputs"]["runCommand"] = ["shutdown -h now"]
        evidence.clients["ssm"].get_document.return_value["Content"] = json.dumps(changed)
    elif change == "expectation":
        evidence.config["DEMO_INSTANCE_ID"] = "i-foreign"
    elif change == "unlimited-credits":
        evidence.clients["ec2"].describe_instance_credit_specifications.return_value = {"InstanceCreditSpecifications": [{"CpuCredits": "unlimited"}]}
    writer = MagicMock(side_effect=AssertionError("Unsafe binding was published"))
    monkeypatch.setattr(deployment, "FalkorDB", writer)
    with pytest.raises(deployment.DeploymentValidationError):
        deployment.verify_deployment(evidence.config, publish=True)
    writer.assert_not_called()


def test_describe_access_denial_is_not_permission_to_create():
    cfn = MagicMock()
    cfn.describe_stacks.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "SCP denies read"}}, "DescribeStacks")
    with pytest.raises(ClientError):
        demo_stack.stack_exists(cfn, "chronosops-demo")
    cfn.create_stack.assert_not_called()
    cfn.describe_stacks.side_effect = ClientError({"Error": {"Code": "ValidationError", "Message": "Stack with id chronosops-demo does not exist"}}, "DescribeStacks")
    assert demo_stack.stack_exists(cfn, "chronosops-demo") is False


@pytest.mark.parametrize("config", [{"AWS_REGION": "us-east-1"},
                                    {"AWS_REGION": "us-east-1", "AWS_PROFILE": "operator", "AWS_RECOVERY_PROFILE": "operator"}])
def test_missing_or_operator_recovery_profile_fails_before_client_creation(monkeypatch, config):
    monkeypatch.setattr("integrations.aws_actions.get_config", lambda: config)
    session = MagicMock(side_effect=AssertionError("Operator credentials reached SDK"))
    monkeypatch.setattr("integrations.aws_actions.boto3.Session", session)
    with pytest.raises(RecoveryExecutionError):
        AwsRecoveryClient()
    session.assert_not_called()


def test_pricing_and_ami_root_size_drive_the_estimate(monkeypatch):
    session = MagicMock()
    ec2, ssm, sts, quotas = (MagicMock() for _ in range(4))
    clients = {"ec2": ec2, "ssm": ssm, "sts": sts, "service-quotas": quotas, "pricing": MagicMock()}
    session.client.side_effect = lambda name, **kwargs: clients[name]
    monkeypatch.setattr(demo_stack, "operator_session", lambda config: session)
    ec2.describe_instance_type_offerings.return_value = {"InstanceTypeOfferings": [{}]}
    ec2.describe_instance_types.return_value = {"InstanceTypes": [{"VCpuInfo": {"DefaultVCpus": 2}}]}
    quotas.get_service_quota.return_value = {"Quota": {"Value": 4}}
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-current"}}
    ec2.describe_images.return_value = {"Images": [{"Architecture": "arm64", "State": "available", "RootDeviceName": "/dev/xvda",
        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 30}}]}]}
    def price(client, service, filters, **kwargs):
        if kwargs.get("usage_suffix") == "EBS:VolumeUsage.gp3":
            return {"usd": "0.08", "unit": "GB-Mo"}
        return {"usd": "0.005" if service == "AmazonVPC" else "0.0084", "unit": "Hrs"}
    monkeypatch.setattr(demo_stack, "_unit_price", price)
    result = demo_stack.preflight({"AWS_REGION": "us-east-1"}, operator_ip="203.0.113.7/32")
    assert result["root_volume_gib"] == 30
    assert result["estimated_usd"] == "0.100126"
    quotas.get_service_quota.return_value = {"Quota": {"Value": 1}}
    with pytest.raises(ValueError, match="quota"):
        demo_stack.preflight({"AWS_REGION": "us-east-1"}, operator_ip="203.0.113.7/32")


def test_publication_replaces_active_binding_without_erasing_history(aws_evidence, monkeypatch, tmp_path):
    from seed.load import get_config
    from falkordb import FalkorDB
    from agents.settings import graph_names
    config = {**get_config(), **aws_evidence.config}
    monkeypatch.setattr(deployment, "STACK_ARTIFACT", tmp_path / "demo-stack.json")
    graph = FalkorDB(host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
                     username=config["FALKORDB_ADMIN_USERNAME"],
                     password=config["FALKORDB_ADMIN_PASSWORD"]).select_graph(graph_names().topology)
    graph.query("CREATE (:Deployment {deployment_id:'old-binding', service_id:'payments-primary', "
                "tenant_id:'acme', active:true})")
    current = deployment.verify_deployment(config, publish=True)
    assert deployment.bound_deployment(config)["deployment_id"] == current["deployment_id"]
    historical = graph.ro_query("MATCH (d:Deployment {deployment_id:'old-binding'}) RETURN d.active").result_set
    assert historical == [[False]]
    # Republishing the same verified workload must not introduce ambiguous active targets.
    deployment.verify_deployment(config, publish=True)
    assert deployment.bound_deployment(config)["instance_id"] == aws_evidence.instance["InstanceId"]
