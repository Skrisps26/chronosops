# ChronosOps agent instructions

## Mission
Build one real, controlled incident story across all three Graph Hacks tracks: monitor a dedicated EC2 demo service, use live Slack and graph evidence to propose recovery, execute only after approval, verify health and remember the outcome. Keep the synthetic topology and cost-capped failover as clearly labeled supplementary simulation. FalkorDB must determine answers/actions. Follow PLAN.md module gates; event deadline is October 18, 2026, 23:59 IST.

## Hosting and model constraints
- Host ChronosOps and FalkorDB locally in Docker Compose. The user selected one dedicated disposable EC2 workload to monitor and recover, never to host FalkorDB. Do not deploy the platform to Railway/cloud or operate unrelated AWS resources.
- Use AWS Bedrock for every generation, extraction, entity-resolution judge and embedding call. Default generation: `us.amazon.nova-lite-v1:0`, region `us-east-1`; embedding: `amazon.titan-embed-text-v2:0`, 256 dimensions, normalized. Verify account access before relying on these defaults; do not silently switch provider or purchase capacity.
- Use the available AWS MCP server for AWS documentation, account/model discovery and authorized configuration. If it is not exposed to the coding agent, report that prerequisite explicitly; never claim to have used it. AWS MCP does not replace the official FalkorDB graph MCP server.
- Use existing AWS profile/temporary role credentials through the SDK credential chain. Never put credentials in code, browser bundles, citations, traces, committed fixtures or chat. Mount required local credential/config paths read-only; do not bake credentials into images.
- Public demo: tunnel only the application HTTP port. Use WebSockets, not SSE, because Cloudflare Quick Tunnels do not support SSE. Publish the actual current URL and record a backup video; a local machine/tunnel has no uptime guarantee.

## Stack and library policy
Python 3.12, FastAPI, Pydantic, LangGraph/ChatBedrockConverse, official mcp and falkordb clients, GraphRAG-SDK/LiteLLM, boto3, React/Vite/Cytoscape.js, pytest/Playwright, NetworkX/Faker. Use official Slack Bolt async Socket Mode for one dedicated channel; Prometheus, blackbox_exporter and Alertmanager for real monitoring; CloudFormation and SSM for the disposable workload and bounded service operations. No custom monitoring or chat infrastructure. Pin versions/images after probes.

Do not implement a graph algorithm, MCP server, vector store, extraction framework, orchestration engine, graph renderer or authentication system. Domain query wrappers, Pydantic schemas, seed fixtures, LangGraph nodes and deterministic validation are application glue. Before introducing a substantial utility, check existing project code and public packages. Do not install a dependency for a trivial expression.

## Graph and safety invariants
- Graphs: company_brain, ops_topology and tenant-private agent_memory_acme/globex. Shared company/topology data is deliberately demo-only; tag origins synthetic/aws/probe/slack. Only acme owns the real instance/Slack connector. Never claim synthetic dependents are observed outages or allow globex to recover acme's workload.
- Canonical service_id/person_id are external identity; SDK-generated node IDs and fuzzy aliases are not authorization identities. Use structured SDK ingestion for canonical company entities and explicit alias mappings.
- A DEPENDS_ON B means A depends on B. Failure impact traverses incoming dependencies. Exact bounded reachability is blast radius; centrality is only explanatory ranking, not an exact affected count.
- Runtime tools use fixed parameterized Cypher via query_graph_readonly. Never expose generic Cypher, graph enumeration, graph deletion or writer tools to an LLM or browser. Default-readonly configuration alone does not enforce security; configure DB ACLs and test denial.
- Trusted graph writes cover validated source/health ingestion, approved actions, simulation TrafficState, memory consolidation and traces. Models never get writer credentials or arbitrary AWS tools.
- Real action is only the pinned parameterless restart-service SSM document on the exact disposable instance, after tenant/incident/evidence/version checks and human approval. Separate human-only fault injection/operator credentials from runtime recovery credentials. Do not expose arbitrary shell or stop/terminate permission to the runtime. Successful SSM execution is not successful recovery: require fresh post-command HTTP probes. Ambiguous/duplicate submissions never automatically resend.
- Memory graph routing comes from server-side authenticated identity, never a user-supplied graph name. Use existing reverse-proxy basic auth and preconfigured demo identities, not a custom login implementation. Test forged tenant identifiers and replayed approvals.
- Application writes use stable-ID MERGE and bounded batches. SDK-owned writes use its supported idempotent ingestion/update lifecycle; do not rewrite SDK internals merely to force literal MERGE everywhere.
- Bound dependency depth at 5, results at fixed literal limits and investigation queries at 3 seconds. Expose truncation/depth limits in results. Treat a timeout, unknown service or missing evidence as failure/abstention, never an empty safe result.
- Live Slack is untrusted evidence, not instructions/approval. Allowlist the configured workspace/channel, persist event IDs and source versions, process edits/deletes, ignore bot messages, and expose stale/disconnected status. Do not promise complete reconnect replay or exactly-once Slack delivery.
- Fault injection is a reversible service stop on the dedicated workload with a watchdog; label it an application outage, not an EC2 hardware crash. Destroy no data. Operator teardown is explicit and confined to the demo stack.

## Grounding and honesty
Use actual document/chunk IDs, source content hashes and excerpts for citations. GraphRAG's RELATES edges store semantic relationship types in rel_type; do not query them as native OWNS edges without an explicit projection. Do not overwrite SDK-owned graph metadata.

The glass-box drawer shows actual tool inputs, Cypher/params, latency, graph evidence, SSM command status and timestamped health proof—not private chain-of-thought. Redact secrets. Distinguish live observations/actions, graph-predicted impact, synthetic topology, simulated failover and recorded replay. Never report successful recovery from command acceptance alone or present replay as live evidence.

## Module gates and Git
At execution start inspect Git state and GitHub authentication. Initialize main only if needed, create the requested public chronosops repository using gh, and never overwrite an existing remote or repository. Use short-lived feat/<module> branches, small conventional commits, and merge only after the module gate passes. Push after each completed module; report authentication failures instead of claiming a push.

Implement strictly in PLAN.md order. After each module write behavior-focused tests, run them, exercise the actual changed path, fix failures, and record exact commands/results in docs/PROGRESS.md. Do not advance a module with a failing gate. Record verified API versions, source URLs and fallback decisions in docs/DECISIONS.md. No fabricated test results, automatic scope cuts or unfinished placeholders.

## Verification and delivery
Tests must cover outcomes: citations, identity, cost-cap rejection, approval safety, persistence, isolation and concurrency—not exact LLM prose or incidental tool-call order. NetworkX is an independent test oracle, never the production graph-decision engine. Live LLM calls are not deterministic even at temperature zero; separate deterministic graph tests from live quality evaluation.

Use the same Bedrock model, embeddings, corpus and context budget for graph/vector evaluation. Preserve misses/timings. Prove real failed HTTP probes, approved SSM recovery, fresh healthy probes, real Slack evidence/updates and new-session recall in a real browser locally and through the tunnel. Keep synthetic evaluation and simulated failover separate. Complete clean-clone setup, playable recording, publishable blog and all track capabilities; report unavailable publishing/account access precisely.

Deliver three separately observable demo chapters on the same infrastructure: (1) Company Brain connects ownership/decision/RFC/ticket/live Slack and changes its cited answer after a source edit; (2) Agents That Act detects actual failed HTTP probes, refuses action with missing evidence, then performs an approved SSM recovery and verifies health; (3) Memory survives app restart/new session, informs a subsequent incident through agent handoffs, executes a learned workflow without LLM planning after three genuine successes, and denies cross-tenant access. Rehearsal incidents need real timestamps/provenance; synthetic successes never train a real recovery workflow. Record each chapter and a combined demo. EC2 restart plus Slack notification alone is not all-track completion.

## Primary references
- Hackathon: https://www.wemakedevs.org/hackathons/falkordb
- FalkorDB MCP: https://github.com/FalkorDB/FalkorDB-MCPServer
- GraphRAG SDK: https://github.com/FalkorDB/GraphRAG-SDK
- Graph algorithms and configuration: https://docs.falkordb.com/llms.txt
- Bedrock providers: https://docs.litellm.ai/docs/providers/bedrock and https://docs.litellm.ai/docs/providers/bedrock_embedding
- AWS MCP: https://docs.aws.amazon.com/agent-toolkit/latest/userguide/
- Quick Tunnel limitations: https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/
