# ChronosOps

> **Autonomous Incident Intelligence Platform powered by FalkorDB as the authoritative context layer for AI agents, integrating Amazon Bedrock and the official FalkorDB MCP Server.**

ChronosOps is a graph-native incident management platform. Instead of relying on LLM hallucinations or ungrounded heuristics, **every agent decision—from calculating blast radius and failover routing to safety approvals and episodic recall—is strictly derived from FalkorDB graph traversals and graph algorithms**.

---

## At a Glance

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

## Demonstration Video

Playable recordings of the running platform are available in the repository:
- `artifacts/chronosops-complete-demo.mp4` (11.3s, H.264 1440x1000): Complete workflow covering ownership inquiry, episodic memory recall, outage simulation, cost-capped failover proposal, human approval, and glass-box Cypher drawer inspection.
- `artifacts/chronosops-demo.mp4` (1m 44s, H.264 1440x1000): Comprehensive multi-scenario demonstration covering simulation approval, proposal rejection, WebSocket event streaming, and multi-tenant isolation.
- `artifacts/chronosops-operator-console.mp4` (42.6s, H.264 1440x1000): Detailed capture of the operator console, Cytoscape graph canvas, and execution traces.

---

## Graph Schema and How Agents Query It

ChronosOps anchors all AI reasoning directly in FalkorDB. The agents execute parameterized OpenCypher queries against graph nodes and relationships to calculate blast radius, find failover routes, enforce memory exclusions, and look up ownership.

```mermaid
flowchart TD
    subgraph Agents ["AI Agents"]
        direction LR
        Triage["Triage Agent"]
        Remediation["Remediation Agent"]
        Memory["Memory Agent"]
        Brain["Company Brain Agent"]
    end

    subgraph FalkorDB ["FalkorDB Graph (Nodes and Relationships)"]
        direction TB
        Service["(:Service)"] -->|DEPENDS_ON| DepService["(:Service)"]
        Service -->|ROUTES_TO| BackupService["(:Service)"]
        Service -->|OWNED_BY| Team["(:Team)"]
        Team -->|MEMBER_OF| Person["(:Person)"]
        Episode["(:IncidentEpisode)"] -->|RECORDED_DECISION| Decision["(:DecisionLog)"]
        Decision -->|BLOCKED_BY| Fact["(:SemanticFact)"]
        Workflow["(:WorkflowSubgraph)"] -->|NEXT_STEP| Step["(:WorkflowStep)"]
    end

    Triage -->|"1. Reverse DEPENDS_ON"| Service
    Remediation -->|"2. algo.SPpaths on ROUTES_TO"| BackupService
    Memory -->|"3. BLOCKED_BY fact exclusions"| Fact
    Memory -->|"4. NEXT_STEP procedural steps"| Workflow
    Brain -->|"5. OWNED_BY canonical lookup"| Team

### Graph Nodes and Relationships

| Node Label | Key Properties | Relationships |
| :--- | :--- | :--- |
| `(:Service)` | `service_id`, `tier`, `health` | `[:DEPENDS_ON]->(:Service)`, `[:ROUTES_TO]->(:Service)`, `[:OWNED_BY]->(:Team)` |
| `(:Team)` | `name`, `team_id` | `[:MEMBER_OF]<-(:Person)` |
| `(:Person)` | `name`, `email`, `person_id` | `[:MEMBER_OF]->(:Team)` |
| `(:IncidentEpisode)` | `incident_id`, `status`, `eligible` | `[:AFFECTED_SERVICE]->(:ServiceRef)`, `[:RECORDED_DECISION]->(:DecisionLog)` |
| `(:DecisionLog)` | `decision`, `status`, `timestamp` | `[:BLOCKED_BY]->(:SemanticFact)` |
| `(:SemanticFact)` | `subject`, `predicate`, `object`, `active` | Prunes candidate routes in `algo.SPpaths` queries |
| `(:WorkflowSubgraph)` | `execution_mode`, `step_count` | `[:CONTAINS_STEP]->(:WorkflowStep)` |
| `(:WorkflowStep)` | `name`, `order`, `workflow_id` | `[:NEXT_STEP]->(:WorkflowStep)` |

### How the Agent Queries the Graph

1. **Blast Radius (Track 01)**:
   ```cypher
   MATCH (s:Service {service_id: $service_id})<-[:DEPENDS_ON*1..5]-(dep:Service)
   RETURN DISTINCT dep.service_id AS affected_service
   ```
   *Agent action*: Computes exact downstream reachability without LLM guesswork.

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
   *Agent action*: Evaluates candidate paths and cost caps directly in the graph engine.

3. **Active Semantic Fact Exclusions (Track 02)**:
   ```cypher
   MATCH (f:SemanticFact {active: true, topology_version: $version, max_cost: $cap})
   RETURN f.subject AS subject, f.predicate AS predicate, f.object AS object, f.reason AS reason
   ```
   *Agent action*: Prunes previously degraded routes from recommendations based on persisted memory.

4. **Procedural Workflow Execution (Track 02)**:
   ```cypher
   MATCH (wf:WorkflowSubgraph {id: $wf_id})-[:CONTAINS_STEP]->(s:WorkflowStep)
   OPTIONAL MATCH (s)-[:NEXT_STEP]->(next:WorkflowStep)
   RETURN s.name, next.name, s.order
   ORDER BY s.order ASC
   ```
   *Agent action*: Executes verified operational playbooks with **zero LLM planning calls**.

5. **Company Brain Grounded Ownership (Track 03)**:
   ```cypher
   MATCH (s:Service {services__service_id: $service_id})-[:RELATES {rel_type: 'OWNED_BY'}]->(t:Team)
   OPTIONAL MATCH (p:Person)-[:RELATES {rel_type: 'MEMBER_OF'}]->(t)
   RETURN s.name AS service, t.name AS team, collect(p.name) AS members
   ```
   *Agent action*: Retrieves canonical service owners and team members grounded in signed CSV row chunks.
---

## Quick Start (Clean Clone)

### 1. Prerequisites
- Docker Compose or rootless Podman (`scripts/bootstrap-local.sh` auto-selects).
- `uv` Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- Node.js 18+ and npm.

### 2. Boot FalkorDB and Bootstrap Local ACLs
```bash
./scripts/bootstrap-local.sh up -d db
```
Starts FalkorDB v4.22.1 with AOF persistence, 3000ms query timeout, and cryptographically generated ACL credentials stored in `.secrets/db.env`.

### 3. Install Dependencies and Build Frontend
```bash
uv sync --frozen
npm ci --prefix mcp --ignore-scripts
npm ci --prefix ui --ignore-scripts
npm run build --prefix ui
```

### 4. Seed Canonical Data and Topology
```bash
./scripts/seed.sh
```
Loads the 45-service microservice topology into `ops_topology`, canonical team/engineer mappings into `company_brain`, and sets up initial application constraints.

### 5. Launch the Operator Platform
```bash
./scripts/up.sh
```
Starts the full stack: application API (`:8000`), reverse proxy with basic authentication (`:8080`), database, and Prometheus monitoring.

---

## Running Demonstration Scenarios

Open the operator console in your browser at `http://localhost:8080`:

1. **Company Brain Inquiry**:
   - Select `payments-primary` from the dropdown.
   - Click **"Who owns this service?"** -> Click **"Query Company Brain"**.
   - *Result*: Returns Team Payments (`Ivan Vanko`, `Bob Martinez`, `Alice Chen`) with exact source citations from `services.csv`.

2. **Prior Incidents and Episodic Memory**:
   - Click **"Prior incidents?"** -> Click **"Query Company Brain"**.
   - *Result*: Queries `agent_memory_acme` for historical episodes and active route exclusion facts.

3. **Autonomous Outage Simulation**:
   - Click **"Simulate payments-primary outage"**.
   - *Result*: Downstream blast radius (4 services) is computed; `algo.SPpaths` selects the 8-cost route (`checkout -> queue -> payments-backup`) and rejects the 20-cost premium route.
   - Proposal banner appears with an immutable `ApprovalRecord` and 5-minute expiry countdown.

4. **Human-in-the-Loop Approval**:
   - Click **"Approve simulation"**.
   - *Result*: FalkorDB atomically merges `:TrafficState` and consolidates the episode into `agent_memory_acme`.

5. **Glass-Box Inspection**:
   - Expand the bottom **"Server-backed trace"** drawer.
   - *Result*: Inspect the actual OpenCypher statements executed, query parameters, and execution latencies in milliseconds.

---

## Test Suite and Verification

```bash
# Run all isolated default tests across all modules
pytest -q

# Run benchmark evaluation suite (graph-only query contracts)
uv run python -m tests.eval_suite --mode graph-only --output artifacts/eval-graph-only.json
```

- **Default Unit and Behavioral Suite**: 232 passed test cases across 32 test files covering atomic claims, lease concurrency, tenant boundaries, monitoring evidence, and memory consolidation.
- **Safety Invariant**: Real recovery requires explicit human approval on pinned SSM runbooks; zero unverified automated AWS mutations.
- **Multi-Tenant Isolation**: Separate `agent_memory_acme` and `agent_memory_globex` databases enforced by FalkorDB ACLs.
