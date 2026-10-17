"""Preflight and manage only the dedicated, explicitly tagged disposable demo stack."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
import urllib.request

from botocore.exceptions import ClientError
from dotenv import dotenv_values

from integrations.deployment import operator_session, verify_deployment
from seed.load import get_config as database_config

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_FILE = ROOT / "infra" / "demo.yaml"
ARTIFACTS_DIR = ROOT / "artifacts"
STACK_ARTIFACT = ARTIFACTS_DIR / "demo-stack.json"
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"


def get_config() -> dict[str, str]:
    config = database_config()
    config.update({key: value for key, value in dotenv_values(ROOT / ".env").items() if value})
    config.update({key: value for key, value in os.environ.items() if value})
    config.setdefault("AWS_REGION", "us-east-1")
    config.setdefault("DEMO_STACK_NAME", "chronosops-demo")
    return config


def get_public_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5) as response:
        return str(ipaddress.IPv4Network(response.read().decode().strip() + "/32"))


def _operator_ip(value: str | None) -> str:
    network = ipaddress.IPv4Network(value or get_public_ip())
    if network.prefixlen != 32 or not network.network_address.is_global:
        raise ValueError("Monitoring host must be a globally routable IPv4 /32")
    return str(network)


def get_cfn_client(config: dict[str, str]) -> Any:
    return operator_session(config).client("cloudformation")


def stack_exists(cfn: Any, stack_name: str) -> bool:
    try:
        cfn.describe_stacks(StackName=stack_name)
        return True
    except ClientError as error:
        details = error.response["Error"]
        if details["Code"] == "ValidationError" and details["Message"] == f"Stack with id {stack_name} does not exist":
            return False
        raise


def _unit_price(pricing: Any, service: str, filters: dict[str, str], *, usage_suffix: str | None = None) -> dict[str, str]:
    """Resolve one unambiguous USD on-demand unit price, retaining catalog provenance."""
    prices: list[dict[str, str]] = []
    paginator = pricing.get_paginator("get_products")
    for page in paginator.paginate(ServiceCode=service, FormatVersion="aws_v1",
                                    Filters=[{"Type": "TERM_MATCH", "Field": key, "Value": value}
                                             for key, value in filters.items()]):
        for encoded in page["PriceList"]:
            product = json.loads(encoded)
            attributes = product["product"]["attributes"]
            if usage_suffix and not attributes.get("usagetype", "").endswith(usage_suffix):
                continue
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for dimension in term["priceDimensions"].values():
                    price = Decimal(dimension["pricePerUnit"]["USD"])
                    if dimension.get("beginRange") == "0" and dimension.get("endRange") == "Inf" and price > 0:
                        prices.append({"usd": str(price), "unit": dimension["unit"],
                                       "sku": product["product"]["sku"], "effective_at": term["effectiveDate"],
                                       "source": "https://pricing.us-east-1.amazonaws.com/"})
    if len(prices) != 1:
        raise ValueError(f"Expected one current on-demand price for {service}; received {len(prices)}")
    return prices[0]


def preflight(config: dict[str, str], *, operator_ip: str, instance_type: str = "t4g.micro") -> dict[str, Any]:
    session = operator_session(config)
    ec2, ssm = session.client("ec2"), session.client("ssm")
    account = session.client("sts").get_caller_identity()["Account"]
    offering = ec2.describe_instance_type_offerings(LocationType="region", Filters=[
        {"Name": "location", "Values": [config["AWS_REGION"]]},
        {"Name": "instance-type", "Values": [instance_type]}])["InstanceTypeOfferings"]
    if not offering:
        raise ValueError("Selected ARM instance type is unavailable in this region")
    cpu = ec2.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][0]["VCpuInfo"]["DefaultVCpus"]
    quota = session.client("service-quotas").get_service_quota(ServiceCode="ec2", QuotaCode="L-1216C47A")["Quota"]["Value"]
    if quota < cpu:
        raise ValueError("Standard on-demand vCPU quota cannot fit the selected instance")
    ami_id = ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
    images = ec2.describe_images(ImageIds=[ami_id], Owners=["amazon"])["Images"]
    if len(images) != 1 or images[0]["Architecture"] != "arm64" or images[0]["State"] != "available":
        raise ValueError("The current Amazon ARM64 AMI is not available")
    image = images[0]
    if image["RootDeviceName"] != "/dev/xvda":
        raise ValueError("Selected AMI root mapping does not match the demo template")
    root = next(row["Ebs"] for row in image["BlockDeviceMappings"] if row["DeviceName"] == image["RootDeviceName"])
    root_size = max(8, root["VolumeSize"])
    pricing = session.client("pricing", region_name="us-east-1")
    region_filter = {"regionCode": config["AWS_REGION"]}
    compute = _unit_price(pricing, "AmazonEC2", {**region_filter, "productFamily": "Compute Instance",
        "instanceType": instance_type, "operatingSystem": "Linux", "tenancy": "Shared",
        "preInstalledSw": "NA", "capacitystatus": "Used"})
    storage = _unit_price(pricing, "AmazonEC2", {**region_filter, "productFamily": "Storage"}, usage_suffix="EBS:VolumeUsage.gp3")
    address = _unit_price(pricing, "AmazonVPC", region_filter, usage_suffix="PublicIPv4:InUseAddress")
    if compute["unit"] != "Hrs" or address["unit"] != "Hrs" or storage["unit"] != "GB-Mo":
        raise ValueError("Pricing catalog units changed; refusing an unverified estimate")
    hours = Decimal(6)
    total = hours * (Decimal(compute["usd"]) + Decimal(address["usd"]) + Decimal(storage["usd"]) * root_size / Decimal(730))
    return {"account_id": account, "region": config["AWS_REGION"], "operator_ip": operator_ip,
            "instance_type": instance_type, "ami_id": ami_id, "root_volume_gib": root_size,
            "standard_vcpu_quota": quota, "required_vcpus": cpu,
            "quota_note": "Quota limit, not available headroom; AWS still checks current usage/capacity at launch.",
            "prices": {"compute": compute, "gp3": storage, "ipv4": address},
            "hours": 6, "estimated_usd": str(total.quantize(Decimal("0.000001"))),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "exclusions": "Data transfer, taxes and Bedrock; EBS continues billing while stopped. 730h/month storage conversion."}


def run_preflight(args: argparse.Namespace) -> int:
    report = preflight(get_config(), operator_ip=_operator_ip(args.operator_ip), instance_type=args.instance_type)
    print(json.dumps(report, indent=2))
    return 0


def run_deploy(args: argparse.Namespace) -> int:
    config = get_config()
    expires = datetime.fromisoformat(args.expires_at.replace("Z", "+00:00"))
    if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
        raise ValueError("Deployment expiry must be a future timezone-aware timestamp")
    expiry = expires.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = preflight(config, operator_ip=_operator_ip(args.operator_ip), instance_type=args.instance_type)
    cfn = get_cfn_client(config)
    params = {"OperatorIp": report["operator_ip"], "InstanceType": report["instance_type"],
              "AmiId": report["ami_id"], "RootVolumeSize": str(report["root_volume_gib"]),
              "RecoveryAssumerArn": args.recovery_assumer_arn, "Operator": args.operator,
              "ExpiresAt": expiry}
    request = {"StackName": config["DEMO_STACK_NAME"], "TemplateBody": TEMPLATE_FILE.read_text(),
               "Parameters": [{"ParameterKey": key, "ParameterValue": value} for key, value in params.items()],
               "Capabilities": ["CAPABILITY_IAM"],
               "Tags": [{"Key": key, "Value": value} for key, value in {
                   "Project": "ChronosOps", "Environment": "demo", "Operator": args.operator,
                   "ExpiresAt": expiry}.items()]}
    cfn.validate_template(TemplateBody=request["TemplateBody"])
    if stack_exists(cfn, config["DEMO_STACK_NAME"]):
        # A similarly named pre-existing stack is never permission to modify unrelated resources.
        existing = cfn.describe_stacks(StackName=config["DEMO_STACK_NAME"])["Stacks"][0]
        tags = {row["Key"]: row["Value"] for row in existing.get("Tags", [])}
        if tags.get("Project") != "ChronosOps" or tags.get("Environment") != "demo":
            raise ValueError("Existing stack is not the dedicated ChronosOps demo")
        try:
            cfn.update_stack(**request)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ValidationError" or error.response["Error"]["Message"] != "No updates are to be performed.":
                raise
        else:
            cfn.get_waiter("stack_update_complete").wait(StackName=config["DEMO_STACK_NAME"])
    else:
        cfn.create_stack(**request)
        cfn.get_waiter("stack_create_complete").wait(StackName=config["DEMO_STACK_NAME"])
    print(json.dumps(verify_deployment(config, publish=True), indent=2))
    return 0


def run_status(args: argparse.Namespace) -> int:
    print(json.dumps(verify_deployment(get_config(), publish=args.publish), indent=2))
    return 0


def run_stop(args: argparse.Namespace) -> int:
    config = get_config()
    binding = verify_deployment(config)
    if args.confirm_instance != binding["instance_id"]:
        raise ValueError("Exact instance confirmation does not match verified deployment")
    print("Stopping only the bound demo instance; EBS storage continues billing until explicit teardown.")
    ec2 = operator_session(config).client("ec2")
    ec2.stop_instances(InstanceIds=[binding["instance_id"]])
    ec2.get_waiter("instance_stopped").wait(InstanceIds=[binding["instance_id"]])
    return 0


def run_destroy(args: argparse.Namespace) -> int:
    config = get_config()
    binding = verify_deployment(config)
    if args.confirm_stack != binding["stack_id"]:
        raise ValueError("Full verified stack ARN confirmation is required for teardown")
    cfn = get_cfn_client(config)
    cfn.delete_stack(StackName=binding["stack_id"])
    cfn.get_waiter("stack_delete_complete").wait(StackName=binding["stack_id"])
    STACK_ARTIFACT.unlink(missing_ok=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "deploy"):
        command = commands.add_parser(name)
        command.add_argument("--operator-ip")
        command.add_argument("--instance-type", choices=["t4g.nano", "t4g.micro"], default="t4g.micro")
        if name == "deploy":
            command.add_argument("--recovery-assumer-arn", required=True)
            command.add_argument("--operator", required=True)
            command.add_argument("--expires-at", required=True)
    commands.add_parser("status").add_argument("--publish", action="store_true")
    commands.add_parser("stop").add_argument("--confirm-instance", required=True)
    commands.add_parser("destroy").add_argument("--confirm-stack", required=True)
    args = parser.parse_args()
    return {"preflight": run_preflight, "deploy": run_deploy, "status": run_status,
            "stop": run_stop, "destroy": run_destroy}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
