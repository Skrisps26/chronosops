# ChronosOps

> **Incident intelligence platform powered by FalkorDB as the context layer for AI agents, integrating Amazon Bedrock and the official FalkorDB MCP Server.**

ChronosOps targets all three **Graph Hacks 2026** tracks. Correctness repair is in progress; the live incident story and clean-clone gates are not yet verified. Real recovery is disabled by default.

- **Track 01 (Agents That Act on Connected Data):** Autonomous blast radius computation, `algo.SPpaths` failover routing under cost caps, human-in-the-loop approval, and bounded AWS Systems Manager recovery.
- **Track 02 (Agent Memory and Coordination):** Episodic recall across sessions without chat history, semantic blocked-route facts, zero-LLM procedural workflows (`NEXT_STEP`), and strict multi-tenant isolation.
- **Track 03 (Company Brain):** Knowledge graph connecting teams, services, RFCs, tickets, and live Slack discussions; single-maintainer bus factor warning; and live atomic document updates that refresh citations in real time.

---

## Verification status

The previous graph/vector accuracy comparison was unsupported and has been withdrawn. The old evaluator ran graph-only checks and automatically credited several questions; its output is not a generated-answer benchmark. The previous concurrency timings measured non-overlapping synchronous writes, not concurrent writers.

No comparative accuracy or category-latency numbers are published until paired raw graph/vector measurements and actual overlapping-write proof exist. See the finding-numbered repair ledger below.
- **Full Architecture Spec:** See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Side Prize Blog Post:** See [docs/blog/chronosops.md](docs/blog/chronosops.md).
- **Module Implementation Progress:** See [docs/PROGRESS.md](docs/PROGRESS.md).

---

## 🚀 Clean-Clone Quick Start

### 1. Prerequisites
- Linux with Docker Compose or rootless Podman (our bootstrap auto-detects and uses pinned `podman-compose` via `uvx`).
- `uv` Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- Node.js 18+ and npm.
- Configured AWS profile with Amazon Bedrock access in `us-east-1` (Nova Lite and Titan Embeddings V2).

### 2. Boot Local FalkorDB
```bash
./scripts/bootstrap-local.sh up -d db
```
This boots FalkorDB v4.22.1 with AOF persistence, 3000ms query timeout, and cryptographically generated ACLs stored in `.secrets/db.env`.

### 3. Install Dependencies & Build Frontend
```bash
uv sync --frozen
npm ci --prefix mcp --ignore-scripts
npm ci --prefix ui --ignore-scripts
npm run build --prefix ui
```

### 4. Seed Topology & Knowledge Graphs
```bash
./scripts/seed.sh
```
This generates and loads the 45-service topology into `ops_topology`, and ingests 16 architecture documents into `company_brain` via GraphRAG-SDK with Amazon Bedrock.

---

## 🎬 Running the Three-Track Demonstration

Execute the self-contained three-chapter demonstration in simulation mode:

```bash
AWS_PROFILE=sai AWS_REGION=us-east-1 ./scripts/demo.sh --mode simulation
```

### What You Will Observe:

1. **Chapter 1: Company Brain (Track 03)**
   - Queries `payments-primary` ownership: returns Team Payments and key engineers (Alice Chen, Bob Martinez).
   - Computes Bus Factor: flags **1 (Single Maintainer Risk)** because Alice Chen authored all key RFCs and resolved all tickets.
   - Live Update: atomically updates RFC-001 failover rationale in FalkorDB; subsequent queries immediately cite the revised text!

2. **Chapter 2: Agents That Act on Connected Data (Track 01)**
   - Calculates Downstream Blast Radius: failure of `payments-primary` reaches exactly 4 services (`checkout`, `gateway`, `orders`, `worker`).
   - Runs `algo.SPpaths` with Cost Cap 12:
     - **REJECTS** the 20-cost Premium Link route (`checkout -> premium -> backup`).
     - **SELECTS** the 8-cost Buffered Queue route (`checkout -> queue -> backup`, 5ms latency).
   - Generates server-side `ApprovalRecord` with 5-minute expiry.
   - Operator approves action: merges `:TrafficState` in FalkorDB.

3. **Chapter 3: Agent Memory and Coordination (Track 02)**
   - Consolidates episode into `agent_memory_acme`.
   - Starts a fresh session with **zero chat history**: recalls past episode and active semantic blocked route rule (`checkout -/> premium`).
   - Executes learned procedural playbook (`NEXT_STEP`) with **0 LLM planning calls**, while enforcing human approval.
   - Verifies Multi-Tenant Isolation: tenant Globex query finds 0 Acme episodes; direct Redis ACL cross-tenant read is denied (`NOPERM`).

Summary manifest is automatically recorded to `artifacts/track-demo.json`.

---

## 🖥️ Running the Operator Console UI

Start the backend API server:

```bash
AWS_PROFILE=sai AWS_REGION=us-east-1 uv run uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Open `http://localhost:8000` in your browser:
- **Left Panel:** Query Company Brain with pre-seeded questions or trigger incident triage.
- **Center Canvas:** Interactive Cytoscape.js topology view with health-coded nodes and route highlights.
- **Right Panel:** Investigation evidence, cited RFC excerpts, and pending approval banner.
- **Bottom Drawer:** Glass-box trace drawer showing exact OpenCypher executed, latency in ms, and algorithm output.

To expose the console via a Cloudflare Quick Tunnel:
```bash
./scripts/tunnel.sh
```

---

## 🧪 Running the Test Suite

```bash
# Run all 53 unit and behavioral tests across all modules
AWS_PROFILE=sai AWS_REGION=us-east-1 uv run --frozen pytest -v tests/

# Run the 24-question benchmark evaluation suite
AWS_PROFILE=sai AWS_REGION=us-east-1 uv run python -m tests.eval_suite --mode graph-only
```
