"""Deploy, manage, and inspect the ChronosOps disposable EC2 demo stack."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_FILE = ROOT / "infra" / "demo.yaml"
ARTIFACTS_DIR = ROOT / "artifacts"
STACK_ARTIFACT = ARTIFACTS_DIR / "demo-stack.json"
SECRETS_FILE = ROOT / ".secrets" / "db.env"


def get_config() -> dict[str, str]:
    config = {k: v for k, v in dotenv_values(SECRETS_FILE).items() if isinstance(v, str) and v}
    config.update({k: v for k, v in os.environ.items() if v})
    config.setdefault("AWS_REGION", "us-east-1")
    config.setdefault("DEMO_STACK_NAME", "chronosops-demo")
    return config


def get_public_ip() -> str:
    """Discover current external IPv4 for restricting security group ingress."""
    urls = ["https://checkip.amazonaws.com", "https://api.ipify.org", "https://icanhazip.com"]
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                ip = resp.read().decode("utf-8").strip()
                if ip and len(ip.split(".")) == 4:
                    return f"{ip}/32"
        except Exception:
            continue
    raise RuntimeError("Could not determine public IPv4. Please supply --operator-ip manually.")


def get_cfn_client(config: dict[str, str]) -> Any:
    kwargs: dict[str, Any] = {"region_name": config["AWS_REGION"]}
    if config.get("AWS_PROFILE"):
        kwargs["profile_name"] = config["AWS_PROFILE"]
    session = boto3.Session(**kwargs)
    return session.client("cloudformation")


def run_preflight(args: argparse.Namespace) -> int:
    config = get_config()
    print("=" * 60)
    print("CHRONOSOPS DEMO WORKLOAD PREFLIGHT ESTIMATE")
    print("=" * 60)
    ip = args.operator_ip or get_public_ip()
    print(f"Region:                {config['AWS_REGION']}")
    print(f"Stack Name:            {config['DEMO_STACK_NAME']}")
    print(f"Monitoring Host IP:    {ip}")
    print("Workload:              1x t4g.micro (ARM64 AL2023, 1GB RAM, 8GB gp3 EBS)")
    print("\nEstimated 6-hour rehearsal & demo cost breakdown:")
    print("  - EC2 t4g.micro:     6 hrs @ ~$0.0084/hr = $0.050")
    print("  - Public IPv4:       6 hrs @ ~$0.0050/hr = $0.030")
    print("  - 8GB gp3 EBS:       6 hrs @ ~$0.0001/hr = $0.001")
    print("  ---------------------------------------------")
    print("  TOTAL WORKLOAD:      ~$0.081 for 6 hours")
    print("  (Bedrock inference and embedding metered separately on AWS)")
    print("=" * 60)
    return 0


def run_deploy(args: argparse.Namespace) -> int:
    config = get_config()
    cfn = get_cfn_client(config)
    stack_name = config["DEMO_STACK_NAME"]
    template_body = TEMPLATE_FILE.read_text(encoding="utf-8")
    operator_ip = args.operator_ip or get_public_ip()

    print(f"Deploying stack '{stack_name}' with OperatorIp={operator_ip}...")
    params = [
        {"ParameterKey": "OperatorIp", "ParameterValue": operator_ip},
        {"ParameterKey": "InstanceType", "ParameterValue": "t4g.micro"},
    ]

    try:
        cfn.describe_stacks(StackName=stack_name)
        exists = True
    except Exception:
        exists = False

    if exists:
        print(f"Stack {stack_name} already exists. Updating...")
        try:
            cfn.update_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=params,
                Capabilities=["CAPABILITY_IAM"],
            )
            waiter = cfn.get_waiter("stack_update_complete")
            waiter.wait(StackName=stack_name)
        except Exception as e:
            if "No updates are to be performed" in str(e):
                print("Stack is up to date.")
            else:
                raise
    else:
        print(f"Creating stack {stack_name}...")
        cfn.create_stack(
            StackName=stack_name,
            TemplateBody=template_body,
            Parameters=params,
            Capabilities=["CAPABILITY_IAM"],
        )
        waiter = cfn.get_waiter("stack_create_complete")
        waiter.wait(StackName=stack_name)

    # Capture stack outputs
    stack_desc = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack_desc.get("Outputs", [])}

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    artifact_payload = {
        "stack_name": stack_name,
        "stack_id": stack_desc["StackId"],
        "region": config["AWS_REGION"],
        "outputs": outputs,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
    }
    STACK_ARTIFACT.write_text(json.dumps(artifact_payload, indent=2) + "\n", encoding="utf-8")
    print(f"Deployment complete. Artifact saved to {STACK_ARTIFACT}")
    print(json.dumps(outputs, indent=2))
    return 0


def run_status(args: argparse.Namespace) -> int:
    config = get_config()
    cfn = get_cfn_client(config)
    stack_name = config["DEMO_STACK_NAME"]
    try:
        desc = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
        print(f"Stack {stack_name}: {desc['StackStatus']}")
        for o in desc.get("Outputs", []):
            print(f"  {o['OutputKey']}: {o['OutputValue']}")
        return 0
    except Exception as e:
        print(f"Error querying stack {stack_name}: {e}")
        return 1


def run_destroy(args: argparse.Namespace) -> int:
    config = get_config()
    cfn = get_cfn_client(config)
    stack_name = config["DEMO_STACK_NAME"]

    if not args.force:
        confirm = input(f"Are you sure you want to delete stack '{stack_name}'? Type '{stack_name}' to confirm: ")
        if confirm != stack_name:
            print("Aborted.")
            return 1

    print(f"Deleting stack {stack_name}...")
    cfn.delete_stack(StackName=stack_name)
    waiter = cfn.get_waiter("stack_delete_complete")
    waiter.wait(StackName=stack_name)
    if STACK_ARTIFACT.is_file():
        STACK_ARTIFACT.unlink()
    print("Stack deleted successfully.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_pre = subparsers.add_parser("preflight", help="Show cost estimate and requirements")
    p_pre.add_argument("--operator-ip", help="Override auto-discovered public IPv4 /32")

    p_dep = subparsers.add_parser("deploy", help="Deploy or update CloudFormation demo stack")
    p_dep.add_argument("--operator-ip", help="Override auto-discovered public IPv4 /32")

    subparsers.add_parser("status", help="Query stack and instance status")

    p_des = subparsers.add_parser("destroy", help="Tear down demo stack")
    p_des.add_argument("--force", action="store_true", help="Skip confirmation prompt")

    args = parser.parse_args()

    if args.command == "preflight":
        return run_preflight(args)
    elif args.command == "deploy":
        return run_deploy(args)
    elif args.command == "status":
        return run_status(args)
    elif args.command == "destroy":
        return run_destroy(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
