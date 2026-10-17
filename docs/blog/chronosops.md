# Why AI Agents Need Graphs, Not Flat Vector Chunks
### Building ChronosOps with FalkorDB and Amazon Bedrock

*An evidence-first technical breakdown of our entry for Graph Hacks 2026 by WeMakeDevs & FalkorDB.*

---

## The Vector Bottleneck in Agent Systems

Most AI agents built today suffer from a fundamental architectural flaw: **they treat context as flat text chunks.**

When an incident hits production at 2:00 AM, an operations agent is asked questions like:
- *"What breaks if the primary payments service fails?"*
- *"Who owns this service and where is the failover policy documented?"*
- *"Can we route traffic over the backup transit link under our $12 risk budget?"*
- *"Did this happen last month, and what did we do?"*

A standard vector database attempts to answer these by embedding text chunks and computing cosine similarity against the question. But:
1. **Multi-hop dependencies don't live in a single chunk.** If Service A depends on Service B, and Service B depends on Service C, a vector similarity search on "what breaks if Service C fails" will frequently miss Service A entirely.
2. **Vectors can't calculate shortest paths under constraints.** You cannot compute `algo.SPpaths` with cost caps using dot products.
3. **Session memory becomes a bloated prompt window.** Passing raw chat logs across sessions wastes context, spikes token costs, and easily causes hallucinations.

For the **Graph Hacks** hackathon, we built **ChronosOps** to prove that **a graph database—specifically FalkorDB—is the missing context layer for autonomous agents.**

---

## One Graph, Three Hackathon Tracks

Instead of building three disconnected prototypes, ChronosOps unites all three hackathon tracks into a single production-grade incident intelligence story:

```
+-------------------------------------------------------------------------+
|                               ChronosOps                                |
|                                                                         |
|  [Track 03: Company Brain]     --> Who owns this service, why was it    |
|                                    designed this way, and what is its   |
|                                    bus-factor risk?                     |
|                                                                         |
|  [Track 01: Agents That Act]   --> Graph-driven blast radius, failover  |
|                                    algorithms via MCP, human approval   |
|                                    gate, and bounded AWS recovery       |
|                                                                         |
|  [Track 02: Agent Memory]      --> Episodic recall across sessions,     |
|                                    semantic exclusion rules, and zero-  |
|                                    LLM procedural workflow playbooks    |
+-------------------------------------------------------------------------+
```

---

## 1. Company Brain (Track 03): Citation Provenance & Live Updates

Using FalkorDB's official `GraphRAG-SDK` with Amazon Bedrock foundation models (`us.amazon.nova-lite-v1:0` and `amazon.titan-embed-text-v2:0`), we ingested architecture RFCs, engineering tickets, pull requests, and live Slack discussions into a dedicated `company_brain` graph.

### The Bus-Factor Proxy
By querying the authorship and resolution trail of decisions, tickets, and PRs, the agent identifies single points of failure in organizational knowledge:

```cypher
MATCH (d:Document)-[:ABOUT]->(s:Service {service_id: $service_id})
OPTIONAL MATCH (p:Person)-[r:AUTHORED|RESOLVED]->(d)
RETURN p.name AS maintainer, count(DISTINCT d) AS contributions
ORDER BY contributions DESC
```

When Alice Chen was identified as the sole primary contributor across RFCs, Slack decisions, and PRs for `payments-primary`, ChronosOps raised an automated **Bus Factor Warning (Proxy: 1)** before any outage occurred.

### Live Source Invalidation
When an engineer edited the failover policy in RFC-001 from *"bounded cost"* to *"rollback speed under the same cost cap"*, ChronosOps executed a single-file atomic update (`rag.update`). Subsequent queries immediately cited the revised version and ceased using stale context—without rebuilding the knowledge graph.

---

## 2. Agents That Act (Track 01): Constrained Graph Algorithms via MCP

When an outage strikes, ChronosOps doesn't ask an LLM to guess what broke. It issues OpenCypher queries through the official `@falkordb/mcpserver` running in strict read-only mode over stdio.

### Exact Blast Radius
Using reverse dependency traversal, the agent discovers every downstream service that depends on the failed primary within 5 hops:

```cypher
MATCH (dependent:Service)-[:DEPENDS_ON*1..5]->(failed:Service {service_id: 'payments-primary'})
RETURN DISTINCT dependent.service_id AS affected_service
```
Result: Exactly 4 services affected (`checkout`, `gateway`, `orders`, `worker`).

### Constrained Shortest Paths (`algo.SPpaths`)
Next, the agent evaluates contingency failover routes using FalkorDB's native graph algorithms:

```cypher
MATCH (s:Service {service_id: 'checkout'}), (t:Service {service_id: 'payments-backup'})
CALL algo.SPpaths({
  sourceNode: s,
  targetNode: t,
  relTypes: ['ROUTES_TO'],
  weightProp: 'latency_ms',
  costProp: 'cost',
  maxCost: 12,
  pathCount: 5
})
YIELD path, pathWeight, pathCost
RETURN [n IN nodes(path) | n.service_id] AS route, pathWeight AS latency_ms, pathCost AS cost
```

Two candidate routes were discovered:
1. `["checkout", "premium", "payments-backup"]`: Latency = 2ms, **Cost = 20**
2. `["checkout", "queue", "payments-backup"]`: Latency = 5ms, **Cost = 8**

Under a safety budget of 12, the unconstrained fastest route (cost 20) was **automatically rejected**. The agent selected the safe alternative (cost 8, latency 5ms) and created a server-side `ApprovalRecord` with a 5-minute expiry.

### Human-in-the-Loop & Real AWS Recovery
Once approved by an operator in the console, ChronosOps triggered a parameterless AWS Systems Manager document (`${StackName}-StartNginx`) on a live disposable EC2 workload, polling health until three consecutive successful HTTP probes confirmed recovery.

---

## 3. Agent Memory & Coordination (Track 02): From Conversations to Workflows

ChronosOps maintains per-tenant memory graphs (`agent_memory_acme`, `agent_memory_globex`) isolated at both the application and Redis ACL layers.

### Multi-Tier Memory Model
1. **Episodic Memory:** After each resolution, an `IncidentEpisode` node records the incident ID, action taken, and command execution ID. In a new session with **zero chat history**, the agent queries:
   ```cypher
   MATCH (s:ServiceRef {service_id: $service})<-[:AFFECTED_SERVICE]-(ep:IncidentEpisode)
   RETURN ep ORDER BY ep.timestamp DESC LIMIT 5
   ```
2. **Semantic Memory:** When the Premium Link route was rejected, ChronosOps merged a `SemanticFact` with a `[:BLOCKED_BY]` relationship. Future agent sessions retrieve active exclusion rules and suppress bad proposals automatically.
3. **Procedural Memory (Zero-LLM Playbooks):** After 3 verified successful resolutions, ChronosOps created a `WorkflowSubgraph` with sequential `[:NEXT_STEP]` edges. Subsequent runs executed the entire investigation and approval pipeline with **0 LLM calls**, drastically reducing latency and token costs.

### High-Concurrency Tenant Benchmarks
We tested 20 concurrent cross-tenant consolidations across Acme and Globex. All 20 completed with zero leaks and atomic writes:
- **p50 Latency:** 5.31 ms
- **p95 Latency:** 7.15 ms
- **Direct Cross-Tenant Read Attempt:** Denied by Redis ACL (`NOPERM`)

---

## 4. Benchmark Evaluation: 100.0% Accuracy

To measure the advantage of graph context over flat vector chunk retrieval, we built a 24-question benchmark suite covering 4 core categories:

| Category | Questions | Vector Baseline | ChronosOps (FalkorDB) | Latency p50 |
|---|:---:|:---:|:---:|:---:|
| **Ownership & Contributor** | 6 | 66.7% | **100.0%** (6/6) | 7.2s |
| **Provenance & Updates** | 6 | 50.0% | **100.0%** (6/6) | 9.5s |
| **Impact & Routes (SPpaths)**| 6 | 33.3% | **100.0%** (6/6) | 7.7s |
| **Session Memory & Isolation**| 6 | 0.0% | **100.0%** (6/6) | 7.1s |
| **OVERALL** | **24** | **37.5%** | **100.0% (24/24)** | **7.7s** |

*Measured results recorded in `artifacts/eval.json`.*

---

## Conclusion

Graphs aren't just a database choice—they are the cognitive architecture that transforms reactive chatbots into trustworthy, explainable autonomous agents.

With FalkorDB, our agents gained:
- The ability to navigate multi-hop dependencies in sub-millisecond query times.
- Native path-finding algorithms (`SPpaths`, `betweenness`) exposed as MCP agent tools.
- Multi-tenant isolated memory that turns past incidents into deterministic workflows.

Check out the code, run the reproducible Docker Compose stack, and test the 24-question evaluation suite at **[github.com/Skrisps26/chronosops](https://github.com/Skrisps26/chronosops)**.
