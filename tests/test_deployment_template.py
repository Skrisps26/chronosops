"""Safety invariants for the disposable ChronosOps CloudFormation stack."""
from __future__ import annotations

from pathlib import Path

import yaml


TEMPLATE_FILE = Path(__file__).resolve().parents[1] / "infra" / "demo.yaml"


class CfnLoader(yaml.SafeLoader):
    """Parse CloudFormation intrinsics as their scalar or collection payload."""


def _intrinsic(loader: CfnLoader, node: yaml.Node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


for _tag in ("!Ref", "!Sub", "!GetAtt", "!Join", "!Select", "!Split", "!Base64"):
    yaml.add_constructor(_tag, _intrinsic, Loader=CfnLoader)


def _template() -> dict:
    return yaml.load(TEMPLATE_FILE.read_text(encoding="utf-8"), Loader=CfnLoader)


def _tag_values(resource: dict) -> dict[str, str]:
    return {
        tag["Key"]: tag["Value"]
        for tag in resource["Properties"].get("Tags", [])
    }


def test_recovery_role_is_bound_to_one_assumer_document_and_instance():
    """The runtime role can restart only the stack's one service through one runbook."""
    template = _template()
    parameters = template["Parameters"]
    for name in ("RecoveryAssumerArn", "Operator", "ExpiresAt", "RootVolumeSize"):
        assert name in parameters
        assert "Default" not in parameters[name]

    role = template["Resources"]["RecoveryRole"]["Properties"]
    trust = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust == [
        {
            "Effect": "Allow",
            "Principal": {"AWS": "RecoveryAssumerArn"},
            "Action": "sts:AssumeRole",
        }
    ]

    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    allowed_actions = {
        action
        for statement in statements
        for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])
    }
    assert allowed_actions == {
        "ssm:SendCommand",
        "ssm:GetDocument",
        "ssm:GetCommandInvocation",
        "ssm:DescribeInstanceInformation",
        "ec2:DescribeInstances",
    }

    send = next(statement for statement in statements if statement["Action"] == "ssm:SendCommand")
    assert set(send["Resource"]) == {
        "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:document/${StartNginxDocument}",
        "arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:instance/${DemoInstance}",
    }
    get_document = next(statement for statement in statements if statement["Action"] == "ssm:GetDocument")
    assert get_document["Resource"] == "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:document/${StartNginxDocument}"
    status = next(statement for statement in statements if statement["Sid"] == "ReadOnlyRecoveryStatus")
    assert status["Resource"] == "*"  # These three read actions have no resource-level scope.


def test_instance_and_documents_preserve_safe_operating_invariants():
    """The AMI-derived volume, standard credits, watchdog, and restart ordering are enforced."""
    resources = _template()["Resources"]
    instance = resources["DemoInstance"]["Properties"]
    ebs = instance["BlockDeviceMappings"][0]["Ebs"]
    assert ebs["VolumeSize"] == "RootVolumeSize"
    assert ebs["Encrypted"] is True
    assert ebs["VolumeType"] == "gp3"
    assert instance["CreditSpecification"]["CPUCredits"] == "standard"
    assert instance["MetadataOptions"]["HttpTokens"] == "required"

    stop_commands = resources["StopNginxDocument"]["Properties"]["Content"]["mainSteps"][0]["inputs"]["runCommand"]
    assert stop_commands == [
        "set -eu",
        "/usr/bin/systemd-run --on-active=10m --unit=chronosops-watchdog /usr/bin/systemctl start nginx.service",
        "/usr/bin/systemctl is-active --quiet chronosops-watchdog.timer",
        "/usr/bin/systemctl stop nginx.service",
    ]

    start_document = resources["StartNginxDocument"]["Properties"]["Content"]
    assert start_document["schemaVersion"] == "2.2"
    assert "parameters" not in start_document
    assert start_document["mainSteps"] == [
        {
            "action": "aws:runShellScript",
            "name": "startNginx",
            "inputs": {
                "runCommand": [
                    "set -eu",
                    "/usr/bin/systemctl start nginx.service",
                    "/usr/bin/systemctl is-active --quiet nginx.service",
                    "/usr/bin/systemctl stop chronosops-watchdog.timer || true",
                ]
            },
        }
    ]


def test_required_operator_expiry_tags_and_recovery_output_are_exposed():
    """Deployment binding data is surfaced without weakening the network boundary."""
    template = _template()
    resources = template["Resources"]
    for resource_name in (
        "VPC",
        "InternetGateway",
        "PublicSubnet",
        "RouteTable",
        "SecurityGroup",
        "InstanceRole",
        "RecoveryRole",
        "DemoInstance",
        "StopNginxDocument",
        "StartNginxDocument",
    ):
        tags = _tag_values(resources[resource_name])
        assert tags["Project"] == "ChronosOps"
        assert tags["Environment"] == "demo"
        assert tags["Operator"] == "Operator"
        assert tags["ExpiresAt"] == "ExpiresAt"

    outputs = template["Outputs"]
    assert outputs["RecoveryRoleArn"]["Value"] == "RecoveryRole.Arn"
    assert outputs["InstanceId"]["Value"] == "DemoInstance"
    assert outputs["PublicIp"]["Value"] == "DemoInstance.PublicIp"
    assert outputs["StartDocumentName"]["Value"] == "StartNginxDocument"
    assert outputs["StopDocumentName"]["Value"] == "StopNginxDocument"

    ingress = resources["SecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert len(ingress) == 1
    assert ingress[0]["IpProtocol"] == "tcp"
    assert ingress[0]["FromPort"] == 80
    assert ingress[0]["ToPort"] == 80
    assert ingress[0]["CidrIp"] == "OperatorIp"
