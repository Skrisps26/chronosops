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

Company ingestion uses SDK structured CSV mappings before source extraction. Canonical IDs live in signed SDK properties (`services__service_id`, `people__person_id`); ownership uses `RELATES {rel_type:'OWNED_BY'}` from Service to Team. Source authorship comes from validated metadata, not co-mentions.

After seeding, run exactly one source writer while submitting document edits:

```bash
AWS_PROFILE=sai AWS_REGION=us-east-1 uv run --frozen python -m seed.ingest --worker
```

All writers must share `CHRONOSOPS_SOURCE_LOCK_PATH` on the same local filesystem; the default is `.secrets/source-worker-company_brain.lock`. Never run a second writer in a separate container/filesystem. The Slack connector will own this same worker when enabled. API requests do not perform SDK writes.

Edits withdraw readiness before extraction and publish only the matching finalized revision. Citations include the raw-source SHA-256, source version, revision, actual SDK chunk ID/index, excerpt and path/permalink. `/api/sources/{document_id}` returns current graph-backed chunks; unknown/deleted sources return 404, pending/failed sources return 503. A publication timeout is not success.

Canonical CSV row chunks have their own published source records. Ownership citations point to those actual SDK chunks, not an unrelated RFC. Change canonical tables through structured operator re-ingestion, not the document-edit entry point.

The default test network cannot reach AWS. The explicit live overlay adds egress only to the test runner and mounts the existing AWS directory read-only:

```bash
AWS_PROFILE=sai ./scripts/bootstrap-local.sh -p chronosops-test \
  -f docker-compose.yml -f docker-compose.test.yml -f docker-compose.bedrock-test.yml \
  run --rm tests pytest -q -m bedrock_live tests/test_sdk_ingestion.py
```

For login-session profiles, botocore requires a writable refresh cache. This overlay copies existing login-cache files into a mode-0700 private tmpfs and uses the supported `AWS_LOGIN_CACHE_DIRECTORY` setting; the host cache stays read-only and the temporary cache disappears with the runner. It does not export credentials, change profiles, or enable recovery.


### Disposable workload safety

Real recovery stays disabled with `REAL_RECOVERY_ENABLED=false`. Deployment, fault injection and teardown are operator-only; the app must never mount the administrator operator profile.

- `AWS_PROFILE`: operator/generation profile on the host.
- `AWS_RECOVERY_PROFILE`: a distinct existing SDK assume-role profile for the exact restricted role returned by the demo stack. No fallback to `AWS_PROFILE`.
- Deploy requires an exact trusted assumer role ARN, operator label and future UTC expiry. Preflight resolves the current ARM64 AMI/root size and queries actual on-demand pricing; denied discovery is not permission to create.

```bash
AWS_PROFILE=sai uv run --no-env-file python -m scripts.demo_stack preflight
AWS_PROFILE=sai uv run --no-env-file python -m scripts.demo_stack deploy \
  --recovery-assumer-arn "$RECOVERY_ASSUMER_ARN" --operator "$USER" \
  --expires-at "$DEMO_EXPIRES_AT"
AWS_PROFILE=sai uv run --no-env-file python -m scripts.demo_stack status --publish
```

`RECOVERY_ASSUMER_ARN` must identify the intended runtime principal; `DEMO_EXPIRES_AT` is a future UTC timestamp such as the operator's planned shutdown time. Neither is a credential. Publication verifies current AWS evidence before binding the instance and `/healthz` URL in FalkorDB.

The operator fault CLI accepts only `stop --confirm-instance <verified-id>` and `start --confirm-instance <verified-id>`. Start verifies the service before cancelling its watchdog. An unknown submission/window blocks automatic retry and requires reconciliation. There is no standalone watchdog-cancellation command.

`scripts.demo_stack stop --confirm-instance <verified-id>` stops only the bound instance; **EBS continues billing**. Teardown is explicit with `destroy --confirm-stack <full-verified-stack-arn>`; it never runs automatically.

Current live prerequisite: the organization's SCP denies CloudFormation discovery in `us-east-1`. Live deployment, restricted-role simulation and real recovery have not passed. Local template and isolated behavior checks do not substitute for those gates.

### Probe evidence and repeated outages

After verified deployment publication, generate the one Prometheus file-discovery target:

```bash
uv run --no-env-file python -m scripts.configure_monitoring
```

The generated `monitoring/targets/demo.json` is ignored by Git and image builds. An unbound/expired deployment clears stale targets and exits75; it never falls back to probing the application. `PROMETHEUS_URL` defaults to the internal Prometheus service.

Health evidence uses actual raw scrape timestamps, synchronized HTTP/probe/exporter metrics and the configured marker-checking module. Missing, stale or disconnected evidence is `monitoring_unknown`, not an empty safe result. Recovery requires three distinct healthy scrapes after the command's completion timestamp; the health-polling bypass was removed.

Bootstrap generates `.secrets/alert-token`; Alertmanager reads the mounted token file. Internal notifications must match the bound workload and current Prometheus evidence. Duplicate notifications reuse the same incident; a new `startsAt` creates a new outage. A healthy resolved notification without agent recovery proof is `recovered_externally`, never a learned recovery success.

---

## 🎬 Running the Three-Track Demonstration

The current simulation script requires the seeded database and single source writer above. The live three-chapter acceptance gate remains unverified:

```bash
AWS_PROFILE=sai AWS_REGION=us-east-1 ./scripts/demo.sh --mode simulation
```

### What You Will Observe:

1. **Chapter 1: Company Brain (Track 03)**
   - Queries `payments-primary` ownership: returns Team Payments and key engineers (Alice Chen, Bob Martinez).
   - Computes contributor concentration from current validated source records; no recorded contributors means unknown, not a healthy distribution.
   - Queues RFC-001 edits through the source writer; subsequent queries cite only the published revision and changed hash/excerpt.

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
