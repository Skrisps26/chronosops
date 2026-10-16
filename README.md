# ChronosOps

Incident intelligence with FalkorDB evidence, AWS Bedrock generation/embeddings, approved recovery and persistent tenant memory.

The approved execution specification is [PLAN.md](PLAN.md); coding-agent rules are [agents.md](agents.md). Implementation is gated module by module. **The incident product and judge demos are not yet complete.** Current verified results and blockers belong in [docs/PROGRESS.md](docs/PROGRESS.md).

## Intended demonstrations

1. **Company Brain:** cited ownership and decisions across documents and live Slack, including an answer changing after a source edit.
2. **Agents That Act:** an actual failed HTTP probe on a disposable EC2 service, graph-grounded proposal, human approval, restricted SSM action and independently observed recovery.
3. **Memory and Coordination:** a fresh session retrieves a prior incident, agents use shared memory, a learned procedural workflow runs after genuine successes, and another tenant is denied access.

ChronosOps and FalkorDB remain local. The EC2 workload is a separate, explicitly provisioned demonstration target—not the database host. Synthetic company data and graph-predicted impact must be distinguished from observed failures. Cost-capped failover is a **simulation**, not real traffic routing. No AWS workload has been deployed yet.

## Module 0 prerequisites

- Linux with Docker Compose or rootless Podman and `uv` (the bootstrap can invoke pinned Podman Compose through uvx).
- Node.js/npm for the official FalkorDB MCP dependency; Python 3.12 managed through uv for local probes.
- A valid existing AWS profile with Bedrock permissions. Default demo region: `us-east-1`; generation `us.amazon.nova-lite-v1:0`; embeddings `amazon.titan-embed-text-v2:0`, normalized, 256 dimensions.
- Existing AWS MCP connection or the official signed library operator probe below.

Do not commit `.env`, `.secrets/`, credentials, local logs or raw `artifacts/`. Bootstrap generates local DB passwords and binds Redis to loopback only. No public database/MCP endpoint is started.

## Local database and real integration probe

```sh
./scripts/bootstrap-local.sh            # starts only persistent local FalkorDB
npm ci --prefix mcp --ignore-scripts
uv sync
AWS_PROFILE=sai AWS_REGION=us-east-1 uv run python -m scripts.probe
```

Use an actual configured profile rather than assuming `sai` exists on another machine. The probe writes `artifacts/probe.json` and exits nonzero on failed or blocked required checks. Mock responses and recorded replay do not satisfy the gate. Probe-owned graphs are separate from demonstration graphs.

Equivalent Docker command when Docker is installed:

```sh
docker compose up -d db
docker compose run --rm app python -m scripts.probe
```

The container probe needs explicitly supplied AWS credential/config mounts before it can pass live model checks. Never mount an administrator profile into a future runtime recovery service; the approved deployment separates operator and scoped runtime credentials.

## AWS MCP operator connection

The existing coding-tool connection can be used directly when loaded. For reproducible verification from this repository, the operator probe uses the official `mcp-proxy-for-aws` signed client. It has an isolated dependency environment because its MCP 1.x dependency is separate from the application's MCP 2.x client.

```sh
uv run --no-project --with 'mcp<2' --with 'mcp-proxy-for-aws==1.7.0' \
  python scripts/aws_mcp_probe.py --profile sai \
  --tool aws___run_script \
  --arguments '{"code":"identity = await call_boto3(service_name=\"sts\", operation_name=\"GetCallerIdentity\", region_name=\"us-east-1\", params={})\nresult = {\"identity\": identity}\nresult"}' \
  --output artifacts/aws-mcp.json
```

This is an **operator-only diagnostic**, not a browser or incident-agent tool. Inspect `structuredContent.api_calls` to confirm APIs actually executed. Public discovery (`--public`) proves connectivity/documentation access only. Optional proxy metadata is omitted because an observed upstream Content-Length failure prevents that path on the pinned version. No custom signing/protocol server is used.

## Tests

```sh
uv run pytest -q tests/test_module_0.py
```

Unit tests do not replace the integration probe. Do not begin Module 1 until all Module 0 required live checks pass. Later API, Slack, EC2 and UI commands are specified in PLAN.md, not advertised here as implemented.

## Security and cost

Use temporary AWS credentials through the standard credential chain. Keep secrets out of source, traces, screenshots and model prompts. Real recovery remains disarmed until its approved module is verified. Database volumes are durable: do not use `compose down -v` against user data.

Local database hosting has no additional subscription, but AWS Bedrock, the future EC2 target, public IPv4 and EBS are separately metered. No free-tier or benchmark improvement is assumed. A judge URL will depend on the local machine/tunnel staying online, with a labeled recorded backup demo.
