# ChronosOps Architecture

ChronosOps is an incident-intelligence platform built on **FalkorDB** as the primary context layer for AI agents, integrating with **Amazon Bedrock** for foundation models and embeddings, and using the official **FalkorDB MCP server** for agent tool execution.

ChronosOps demonstrates all three WeMakeDevs x FalkorDB Graph Hacks tracks:
- **Track 01:** Agents That Act on Connected Data
- **Track 02:** Agent Memory and Coordination
- **Track 03:** Company Brain

---

## 1. System Overview

```
                               +-----------------------------+
                               |     Local Browser / UI      |
                               | (React + Vite + Cytoscape)  |
                               +--------------+--------------+
                                              |
                                              | HTTPS / WebSocket (:8080)
                                              v
                               +-----------------------------+
                               |         Nginx Proxy         |
                               | (Basic Auth: acme / globex) |
                               +--------------+--------------+
                                              |
                                              | Internal HTTP (:8000)
                                              v
+-----------------------------------------------------------------------------------------+
|                                    ChronosOps App                                       |
|                                                                                         |
|   +--------------------------+   +-----------------------+   +----------------------+   |
|   |       FastAPI API        |   |   LangGraph Engine    |   |  Slack Bolt Handler  |   |
|   | (REST + WebSocket Stream)|   | (Triage -> Plan -> Gov|   |    (Socket Mode)     |   |
|   +-------------+------------+   +-----------+-----------+   +----------+-----------+   |
|                 |                            |                          |               |
|                 +----------------------------+--------------------------+               |
|                                              |                                          |
|                 +----------------------------+--------------------------+               |
|                 |                            |                          |               |
|                 v                            v                          v               |
|   +--------------------------+   +-----------------------+   +----------------------+   |
|   |   FalkorDB MCP Client    |   |   GraphRAG SDK Lite   |   | AWS Recovery Client  |   |
|   |  (@falkordb/mcpserver)   |   |   (Bedrock Providers) |   | (Single-Attempt SSM) |   |
|   +-------------+------------+   +-----------+-----------+   +----------+-----------+   |
+-----------------|----------------------------|--------------------------|---------------+
                  |                            |                          |
       stdio MCP  |                 Redis Wire |                          | HTTPS
                  v                            v                          v
       +--------------------+      +--------------------+      +----------------------+
       | @falkordb/mcpserver| ---> |     FalkorDB       |      |     Amazon Web       |
       |  (Read-Only Stdio) |      | (Docker, Port 6379)|      |       Services       |
       +--------------------+      +--------------------+      +----------------------+
                                     - ops_topology              - Bedrock Nova Lite
                                     - company_brain             - Titan Text Embed V2
                                     - agent_memory_acme         - Systems Manager
                                     - agent_memory_globex       - Disposable EC2 Workload
```

---

## 2. Multigraph Architecture

ChronosOps uses FalkorDB's native multigraph isolation to maintain distinct boundaries within a single database instance:

| Logical Graph | Purpose | Key Labels & Edges |
|---|---|---|
| `ops_topology` | Operational service graph & candidate routes | `:Service`, `:Route`, `[:DEPENDS_ON]`, `[:ROUTES_TO]`, `:TrafficState`, `:ApprovalRecord`, `:Meta` |
| `company_brain` | Track 03: Company knowledge, tickets, RFCs | `:Document`, `:Chunk`, `:Person`, `:Team`, `:Decision`, `[:OWNS]`, `[:MEMBER_OF]`, `[:AUTHORED]`, `[:MENTIONED_IN]` |
| `agent_memory_acme` | Track 02: Acme tenant private memory | `:IncidentEpisode`, `:DecisionLog`, `:ServiceRef`, `:SemanticFact`, `:WorkflowSubgraph`, `:WorkflowStep` |
| `agent_memory_globex`| Track 02: Globex tenant private memory | Completely isolated memory graph. Cross-tenant access is denied at both the application and Redis ACL layers. |

---

## 3. Tool Selection & Model Context Protocol (MCP)

Graph inspection and algorithm tools are executed exclusively through the official `@falkordb/mcpserver` package over stdio transport:

- **Strict Read-Only Enforcement:** Reader sessions run with `FALKORDB_DEFAULT_READONLY=true` and `FALKORDB_STRICT_READONLY=true`. Any write operation attempted via `query_graph` or `query_graph_readonly` is rejected immediately by the server.
- **Database ACL Layer:** Distinct Redis ACL accounts (`reader_acme`, `reader_globex`, `writer_acme`, `writer_globex`, `admin`) restrict graph key patterns. A compromised or misconfigured reader client is prevented from accessing other graphs by Redis ACL rules.
- **Parametric Cypher:** All queries pass inputs through parameters (`$service_id`, `$max_cost`). Parameter injection payloads remain inert literal data.

### Graph Algorithm Execution

1. **Downstream Blast Radius:**
   ```cypher
   MATCH (dependent:Service)-[:DEPENDS_ON*1..5]->(failed:Service {service_id: $service_id})
   RETURN DISTINCT dependent.service_id AS service_id
   LIMIT 100
   ```
2. **Constrained Shortest Path Failover (`algo.SPpaths`):**
   ```cypher
   MATCH (s:Service {service_id: $source}), (t:Service {service_id: $target})
   CALL algo.SPpaths({
     sourceNode: s,
     targetNode: t,
     relTypes: ['ROUTES_TO'],
     weightProp: 'latency_ms',
     costProp: 'cost',
     maxCost: $max_cost,
     maxLen: 5,
     relDirection: 'outgoing',
     pathCount: 5
   })
   YIELD path, pathWeight, pathCost
   RETURN [n IN nodes(path) | n.service_id] AS route, pathWeight AS latency_ms, pathCost AS cost
   ```
3. **Choke Points Analysis (`algo.betweenness`):**
   ```cypher
   CALL algo.betweenness({
     nodeLabels: ['Service'],
     relationshipTypes: ['DEPENDS_ON'],
     samplingSize: 32,
     samplingSeed: 42
   })
   YIELD node, score
   RETURN node.service_id AS service_id, score ORDER BY score DESC LIMIT 10
   ```

---

## 4. Agent Memory Hierarchy

ChronosOps implements a 3-tier memory model:

1. **Episodic Memory:** After each resolution, an `IncidentEpisode` node is merged into `agent_memory_{tenant}` linked to the affected `ServiceRef`, recording the outcome, command ID, timestamp, and duration. A fresh chat session with zero conversation history recalls past incidents directly via graph query.
2. **Semantic Memory:** When a route or option is rejected (e.g. Premium Link exceeding cost cap), a `SemanticFact` node is created with `[:BLOCKED_BY]` relationships. Future agent sessions retrieve active exclusion rules and suppress bad proposals.
3. **Procedural Memory:** After 3 verified successful resolutions of the same incident type, the agent forms a `WorkflowSubgraph` with `[:NEXT_STEP]` edges representing the learned playbook. Subsequent runs execute this deterministic workflow with **zero LLM planning calls**, while maintaining human approval and fresh safety checks.
