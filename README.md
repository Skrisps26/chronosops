# ChronosOps

> **Autonomous Incident Intelligence Platform powered by FalkorDB as the authoritative context layer for AI agents, integrating Amazon Bedrock and the official FalkorDB MCP Server.**

ChronosOps is a graph-native incident management platform. Instead of relying on LLM hallucinations or ungrounded heuristics, **every agent decision—from calculating blast radius and failover routing to safety approvals and episodic recall—is strictly derived from FalkorDB graph traversals and graph algorithms**.

---

## 🎯 At a Glance

| Feature | How FalkorDB Powers It | Track |
| :--- | :--- | :--- |
| **Blast Radius Calculation** | Traverses incoming `[:DEPENDS_ON*1..5]` reverse dependencies in `ops_topology` | **Track 01** |
| **Cost-Capped Failover Routing** | Evaluates candidate paths via `algo.SPpaths` with cost caps and node health checks | **Track 01** |
| **Human-in-the-Loop Safety Gate** | Binds action proposals to immutable graph hashes and 5-minute `ApprovalRecord` expiry | **Track 01** |
| **Episodic Recall Across Sessions** | Retrieves past incident outcomes from `agent_memory_{tenant}` without conversation history | **Track 02** |
| **Semantic Route Exclusions** | Queries active `[:BLOCKED_BY]->(:SemanticFact)` to block historically degraded routes | **Track 02** |
| **Zero-LLM Procedural Workflows** | Derives `WorkflowSubgraph` with `[:NEXT_STEP]` chains after 3 successes; runs with 0 LLM calls | **Track 02** |
| **Canonical Ownership Citations** | Traverses `[:RELATES {rel_type:'OWNED_BY'}]` and cites exact SDK CSV row chunks | **Track 03** |
| **Bus Factor & Knowledge Risk** | Analyzes `[:MEMBER_OF]` and validated `SourceRecord` author distributions | **Track 03** |

---

## 🌐 Live Public Demo & Video

- **Public Web Console**: [https://mistress-debug-weblog-should.trycloudflare.com](https://mistress-debug-weblog-should.trycloudflare.com)
- **Demo Credentials**:
  - **Tenant 1 (Acme - Primary Demo)**: Username `acme` / Password `dec558d39d90afe5f18bbdc1375631fafd0215b895785283`
  - **Tenant 2 (Globex - Multi-Tenant Isolation)**: Username `globex` / Password `d564d041e9a00a5c68ad3ae80a4c7fb70fa3e6a71b92537e`
- **Demonstration Video**: `artifacts/chronosops-complete-demo.mp4` (11.3s) and `artifacts/chronosops-demo.mp4` (1m 44s, H.264 1440×1000).

---

## 🗺️ The Graph Map: How Agent Decisions Come From FalkorDB

ChronosOps partitions knowledge into three distinct graph namespaces in FalkorDB. **The LLM never invents an action, route, or dependency—all reasoning is anchored in Cypher execution against these graphs.**

```mermaid
graph TD
  subgraph ops_topology ["1. ops_topology (Infrastructure, Dependencies & Actions)"]
    Service[Service Node] -->|DEPENDS_ON| DepService[Dependent Services]
    Service -->|ROUTES_TO {cost, latency, health}| BackupService[Backup Service Node]
    Incident[Incident] -->|HAS_APPROVAL| Approval[ApprovalRecord {evidence_hash, expires_at}]
    Incident -->|HEALTH_EVIDENCE| Health[HealthObservation {probe_success, exporter_up}]
    Approval -->|APPLIES| Traffic[TrafficState {active_route}]
  end

  subgraph company_brain ["2. company_brain (Organizational Ownership & Architecture)"]
    CanonicalService[Service] -->|RELATES {rel_type: 'OWNED_BY'}| Team[Team Node]
    Person[Person Node] -->|RELATES {rel_type: 'MEMBER_OF'}| Team
    Doc[Document Node] -->|PART_OF| Chunk[Chunk Node {text, index}]
    CanonicalService -->|MENTIONED_IN| Chunk
    SourceRecord[SourceRecord {content_hash}] -->|PUBLISHED_CHUNK| Chunk
  end

  subgraph agent_memory ["3. agent_memory_{tenant} (Episodic & Procedural Memory)"]
    Episode[IncidentEpisode {eligible, status}] -->|AFFECTED_SERVICE| ServiceRef[ServiceRef]
    Episode -->|RECORDED_DECISION| Decision[DecisionLog]
    Decision -->|BLOCKED_BY| Fact[SemanticFact {subject, predicate, object, active}]
    Workflow[WorkflowSubgraph] -->|CONTAINS_STEP| S1[WorkflowStep 1: retrieve_memory]
    S1 -->|NEXT_STEP| S2[WorkflowStep 2: blast_radius]
    S2 -->|NEXT_STEP| S3[WorkflowStep 3: failover_routes]
    S3 -->|NEXT_STEP| S4[WorkflowStep 4: validate_governance]
    S4 -->|NEXT_STEP| S5[WorkflowStep 5: await_approval]
    S5 -->|NEXT_STEP| S6[WorkflowStep 6: simulate_failover]
    S6 -->|NEXT_STEP| S7[WorkflowStep 7: consolidate]
  end

  %% Cross-graph decision flows
  Incident -.->|Reads blast radius & routes| ops_topology
  Approval -.->|Consolidates episode| Episode
  Fact -.->|Excludes candidate path| RouteBackup
```

### Exact Cypher Queries Governing Agent Decisions

1. **Blast Radius (Track 01)**:
   ```cypher
   MATCH (s:Service {service_id: $service_id})<-[:DEPENDS_ON*1..5]-(dep:Service)
   RETURN DISTINCT dep.service_id AS affected_service
   ```
   *Guarantees*: Exact bounded reachability (depth ≤ 5). Centrality provides rank, but incoming dependency traversal determines the actual affected blast radius.

2. **Cost-Capped Route Discovery (Track 01)**:
   ```cypher
   MATCH (s:Service {service_id: $source}), (t:Service {service_id: $target})
   CALL algo.SPpaths({
     sourceNode: s, targetNode: t, relTypes: ['ROUTES_TO'], weightProp: 'cost', maxLen: 5, pathCount: 5
   })
   YIELD path, pathCost
   WHERE pathCost <= $max_cost
   RETURN [n IN nodes(path) | n.service_id] AS route, pathCost
   ORDER BY pathCost ASC
   ```
   *Guarantees*: Rejects routes exceeding the cost cap (e.g. rejecting the 20-cost premium route under a cap of 12, selecting the 8-cost queue route).

3. **Active Semantic Fact Exclusions (Track 02)**:
   ```cypher
   MATCH (f:SemanticFact {active: true, topology_version: $version, max_cost: $cap})
   RETURN f.subject AS subject, f.predicate AS predicate, f.object AS object, f.reason AS reason
   ```
   *Guarantees*: If an operational outage previously proved a path degraded, the active fact prunes that candidate path from future recommendations.

4. **Procedural Workflow Execution (Track 02)**:
   ```cypher
   MATCH (wf:WorkflowSubgraph {id: $wf_id})-[:CONTAINS_STEP]->(s:WorkflowStep)
   OPTIONAL MATCH (s)-[:NEXT_STEP]->(next:WorkflowStep)
   RETURN s.name, next.name, s.order
   ORDER BY s.order ASC
   ```
   *Guarantees*: After 3 distinct verified successes, the deterministic sequence is loaded from memory. The execution proceeds with **zero LLM planning calls**, enforcing the human approval step.

5. **Company Brain Grounded Ownership (Track 03)**:
   ```cypher
   MATCH (s:Service {services__service_id: $service_id})-[:RELATES {rel_type: 'OWNED_BY'}]->(t:Team)
   OPTIONAL MATCH (p:Person)-[:RELATES {rel_type: 'MEMBER_OF'}]->(t)
   RETURN s.name AS service, t.name AS team, collect(p.name) AS members
   ```
   *Guarantees*: Answers are grounded directly in signed SDK properties and published source chunks, not fuzzy co-mentions.

---

## 🚀 Quick Start (Clean Clone)

### 1. Prerequisites
- Docker Compose or rootless Podman (`scripts/bootstrap-local.sh` auto-selects).
- `uv` Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- Node.js 18+ and npm.

### 2. Boot FalkorDB & Bootstrap Local ACLs
```bash
./scripts/bootstrap-local.sh up -d db
```
Starts FalkorDB v4.22.1 with AOF persistence, 3000ms query timeout, and cryptographically generated ACL credentials stored in `.secrets/db.env`.

### 3. Install Dependencies & Build Frontend
```bash
uv sync --frozen
npm ci --prefix mcp --ignore-scripts
npm ci --prefix ui --ignore-scripts
npm run build --prefix ui
```

### 4. Seed Canonical Data & Topology
```bash
./scripts/seed.sh
```
Loads the 45-service microservice topology into `ops_topology`, canonical team/engineer mappings into `company_brain`, and sets up initial application constraints.

### 5. Launch the Operator Platform
```bash
./scripts/up.sh
```
Starts the full stack: application API (`:8000`), proxy with basic auth (`:8080`), database, and Prometheus monitoring.

---

## 🎬 How to Run the Demo Scenarios

Open the operator console in your browser at `http://localhost:8080` (or through the public tunnel):

1. **Company Brain Inquiry**:
   - Select `payments-primary` from the dropdown.
   - Click **"Who owns this service?"** → Click **"Query Company Brain"**.
   - *Result*: Returns Team Payments (`Ivan Vanko`, `Bob Martinez`, `Alice Chen`) with exact source citations from `services.csv`.

2. **Prior Incidents & Episodic Memory**:
   - Click **"Prior incidents?"** → Click **"Query Company Brain"**.
   - *Result*: Queries `agent_memory_acme` for historical episodes and active route exclusion facts.

3. **Autonomous Outage Simulation**:
   - Click **"Simulate payments-primary outage"**.
   - *Result*: Downstream blast radius (4 services) is computed; `algo.SPpaths` selects the 8-cost route (`checkout → queue → payments-backup`) and rejects the 20-cost premium route.
   - Proposal banner appears with an immutable `ApprovalRecord` and 5-minute expiry countdown.

4. **Human-in-the-Loop Approval**:
   - Click **"Approve simulation"**.
   - *Result*: FalkorDB atomically merges `:TrafficState` and consolidates the episode into `agent_memory_acme`.

5. **Glass-Box Inspection**:
   - Expand the bottom **"Server-backed trace"** drawer.
   - *Result*: Inspect the actual OpenCypher statements executed, query parameters, and execution latencies in milliseconds.

---

## 🧪 Test Suite & Verified Guarantees

```bash
# Run all isolated default tests across all modules
pytest -q

# Run benchmark evaluation suite (graph-only query contracts)
uv run python -m tests.eval_suite --mode graph-only --output artifacts/eval-graph-only.json
```

- **Default Unit & Behavioral Suite**: 232 passed test cases across 32 test files covering atomic claims, lease concurrency, tenant boundaries, monitoring evidence, and memory consolidation.
- **Safety Invariant**: Real recovery requires explicit human approval on pinned SSM runbooks; zero unverified automated AWS mutations.
- **Multi-Tenant Isolation**: Separate `agent_memory_acme` and `agent_memory_globex` databases enforced by FalkorDB ACLs.
