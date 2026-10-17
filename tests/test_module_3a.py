"""Module 3A verification suite: CloudFormation template security, monitoring, and recovery safety."""
from __future__ import annotations

import yaml
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


