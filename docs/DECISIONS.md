# Verified integration decisions

## Module 0

- ChronosOps/FalkorDB run locally. A single disposable EC2 workload is reserved for a later module; no EC2 resource has been created.
- GitHub repository created at https://github.com/Skrisps26/chronosops. The approved plan and agent instructions were committed and pushed to main before starting feat/module-0.
- Docker is not installed. Rootless Podman is installed and reports rootless=true; no Compose provider was initially present. Use the same Compose specification through a pinned Podman Compose provider instead of requiring a privileged Docker installation. This is a local runtime adaptation, not a hosting change.
- Existing AWS MCP configuration was discovered in the user's Codex configuration. The named `sai` AWS profile uses AWS CLI login credentials. Its expired session was renewed with explicit user permission; no credentials were copied into the repository.
- AWS MCP actually executed STS GetCallerIdentity through `aws___run_script`; structured result recorded api_calls status=success. Raw evidence is in ignored artifacts/aws-mcp.json. Public tool discovery alone does not prove authenticated API access.
- Bedrock ListFoundationModels and ListInferenceProfiles were executed through the same AWS MCP server in us-east-1. Nova Lite (`us.amazon.nova-lite-v1:0`) and Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`) were ACTIVE. This proves catalog availability, not invocation permission or model output quality.
- mcp-proxy-for-aws 1.7.0's optional metadata injection caused `LocalProtocolError: Too much data for declared Content-Length` in the direct official client. The official signed library connection without optional metadata succeeded. Specify region explicitly in every run_script API call; do not hand-roll signing, suppress auth errors or patch vendor packages. Existing user-wide MCP configuration was not modified.
- Bootstrap scripts must fail closed when model/API/SDK contracts are unavailable. Modules 1 onward remain gated by Module 0 live verification; recorded data is not a replacement for a successful live probe.
- Published GraphRAG-SDK 1.4.0 does not expose the planned constructor's enable_cypher setting. The official repository commit `35e67a0b27fdcc7f2c28508b09e86169851a8187` does: `enable_cypher: bool = False` in api/main.py, forwarded to MultiPathRetrieval. Pin the official source commit as allowed by Module 0 rather than mixing incompatible legacy examples or fabricating a parameter. Runtime ingestion/update/citation behavior remains subject to the actual probe.
- Local FalkorDB image built successfully through rootless Podman. Actual authenticated PING returned PONG; a probe-owned node was written, read through GRAPH.RO_QUERY with value1 and then deleted. No demonstration graph was touched.

## Sources

- AWS MCP setup: https://docs.aws.amazon.com/agent-toolkit/latest/userguide/getting-started-aws-mcp-server.html
- Official proxy library and programmatic client: https://github.com/aws/mcp-proxy-for-aws
- FalkorDB MCP: https://github.com/FalkorDB/FalkorDB-MCPServer
- GraphRAG-SDK: https://github.com/FalkorDB/GraphRAG-SDK
