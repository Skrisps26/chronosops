# Correctness repair ledger

The previous all-modules-passed report is withdrawn. Code presence, mocked adapters and a simulation manifest do not establish the live acceptance gates in PLAN.md. Real recovery is disabled by default while repairs proceed. No graph/vector comparative scores or concurrent-write benchmark are currently verified.

## Historical evidence

Earlier dependency probes exercised local FalkorDB, official MCP and Bedrock SDK paths; those historical observations do not establish the complete incident product. Archived `AWS_PROFILE=sai AWS_REGION=us-east-1 uv run --frozen pytest -v tests/` runs timed out (240 seconds, including a webhook failure). A subsequent 180-second graph/workflow subset also timed out. Neither proves a complete 57/57 pass. The historical graph-only evaluation automatically credited four questions and did not run a vector baseline. The old concurrency test did not overlap its synchronous database work.

Keep historical artifacts labeled historical/unverified; never promote them to real training evidence.

## Findings and acceptance

| Finding | Required behavior | Repair status | Required proof |
| --- | --- | --- | --- |
| 1 | Fresh complete evidence at proposal and approval | Open | Missing/changed/deleted evidence refuses with no command |
| 2 | Atomic approval and singleton real execution claim | Open | Twenty competing clients submit once |
| 3 | Correct endpoint and fresh post-command samples | Open | Three distinct saved healthy samples after completion |
| 4 | Immediate CommandId persistence and unknown reconciliation | Open | Submission/poll/crash outcomes never auto-resend |
| 5 | Proxy-authenticated tenant identity | Local gate passed | Real Nginx rejects unauthenticated/forged direct requests |
| 6 | Tenant-authorized events and incident reads | Local gate passed | Owned events delivered; foreign socket denied before accept |
| 7 | Missing reader credentials fail closed | Local gate passed | No administrator fallback; missing/unknown identity tests pass |
| 8 | Monitoring/Slack/action/memory lifecycle connected | Open | Actual controlled incident, source update, outcome and recall |
| 9 | Grounded triage/proposal and truthful explanation | Open | Tool/evidence failure abstains |
| 10 | Only authoritative compatible outcomes train memory | Open | Invalid outcomes excluded; compatible three-success boundary |
| 11 | Stored NEXT_STEP graph controls learned execution | Open | Missing/cyclic/incompatible workflow refuses |
| 12 | Current versioned citations and contributor provenance | Open | Edit/delete/author change and failure publication checks |
| 13 | Actual paired evaluation and overlapping concurrency | Open | Raw measured outputs; no automatic question credit |
| 14 | Complete local stack and truthful failing demos | Open | Clean clone, authenticated browser/tunnel and negative demo exit |
| 15 | Explicit API errors and truthful live UI | Open | Unknown404, unready503, rejected approval retained |
| 16 | Canonical SDK structured ingestion precedes extraction | Open | Idempotent CSV ingestion and alias collision refusal |
| 17 | Exact deployment binding and separate runtime IAM | Open | Allowed fixed start; denied arbitrary/foreign/operator actions |
| 18 | Dedicated EC2 monitoring and generated webhook secret | Open | Correct endpoint, stale/exporter failure unknown |
| 19 | Tests cannot alter demo state or learn from shared history | Local gate passed; image-build proof blocked | Isolated DB volumes/ACL/namespace exercised; default suite 52 passed, 2 live tests deselected |

## Step gates

1. Disarm recovery, isolate tests, correct unsupported claims — local gate passed. Changes: default-off real-action guard; server-owned test graph namespace; isolated Compose/ACL fixtures; unsupported public benchmark claims withdrawn. Test latency artifacts now use pytest temporary directories, not historical demo artifacts.
2. Proxy identity and tenant authorization — local gate passed. Real Nginx HTTP/WebSocket smoke and containerized isolated cumulative suite exercised; public tunnel proof remains a later gate.
3. Verified deployment and runtime permissions — local gate passed; live gate blocked by organization SCP. No real recovery permitted.
4. Fresh monitoring evidence and outage identity — local gate passed. Real workload proof remains blocked with deployment.
5. Canonical ingestion and citation publication — pending.
6. Slack lifecycle and notifications — pending.
7. Evidence-bound proposals and routes — pending.
8. Atomic execution and command state machine — pending.
9. Authoritative episodic/procedural memory — pending.
10. Bounded investigation and controlled recovery — pending.
11. Complete stack and truthful UI — pending.
12. Paired evaluation and actual concurrency — pending.
13. Real chapters, recording and final acceptance — pending.

### Step 1 commands and observations

- `./scripts/bootstrap-local.sh -p chronosops-test -f docker-compose.yml -f docker-compose.test.yml config` succeeded with no inherited live env files or host ports.
- `./scripts/bootstrap-local.sh -p chronosops-test -f docker-compose.yml -f docker-compose.test.yml build db tests` built the DB, but the application image pull timed out after 1800 seconds with Docker Hub connection-reset/TLS errors. A separate official Python slim-manifest lookup timed out after 60 seconds. Container-image packaging remains unverified, not passed.
- `./scripts/bootstrap-local.sh -p chronosops-test -f docker-compose.yml -f docker-compose.test.yml up -d db` started only the isolated DB.
- `podman inspect --format '{{json .NetworkSettings.Ports}} {{json .Mounts}}' chronosops-test_db_1` showed no host port publication and only `chronosops-test_falkordb-test-data` / `chronosops-test_falkordb-test-acl` volumes.
- Fallback uses the installed Python environment inside only that container's network namespace: `podman unshare nsenter -t <isolated DB PID from podman inspect> -n -- /home/skrisps/falkor/.venv/bin/python -m pytest -q`, with the dummy DB/AWS environment from docker-compose.test.yml, `FALKORDB_HOST=127.0.0.1`, nonexistent AWS config/credential paths and no profile. Result: **52 passed, 2 live tests deselected, 1 upstream SDK deprecation warning in 269.03s**. Initial run exposed boto3 treating empty AWS_PROFILE as a named profile; conftest now normalizes that empty test setting before imports.
- Focused disabled-recovery regression: same namespace invocation with `pytest -q tests/test_module_4.py -k real_recovery_disabled` — **1 passed, 6 deselected**. Actual function smoke printed `REAL_ACTION_REFUSAL Real recovery is disabled`; no recovery adapter was constructed.
- Focused temporary-artifact check: `pytest -q tests/test_module_5.py -k concurrent` — **1 passed, 4 deselected**. This remains synthetic serial timing coverage, not the future overlapping-writer proof.
- Actual reader smoke caught the SDK `NoPermissionError` for Globex reading Acme; isolated names were `test_suite_ops_topology` and `test_suite_company_brain`.
- Read-only demo-count capture could not connect to the stopped demo DB at loopback6379. It was not started for tests. Separation was verified through distinct container volumes, namespace and DB ACLs; unchanged live counts are not claimed.

### Step 2 commands and observations

- Removed reader-to-admin fallback; proxy authentication requires a shared token and allowlisted identity, exact browser Origin checks, and tenant-scoped incident/run ownership.
- Nginx sources its `.envsh` hook before template rendering. Root stages the private mounted password file for its worker. No passwords are baked into the image.
- `podman build -t localhost/chronosops-test_proxy:latest proxy` succeeded using official digest `sha256:62223d644fa234c3a1cc785ee14242ec47a77364226f1c811d2f669f96dc2ac8`.
- The previously blocked Python pull subsequently succeeded. The app image built with a local dependency-cache mount and build-only `--security-opt label=disable`, after SELinux denied the initial cache mount.
- Podman Compose 1.5.0 rejected reset tags on newly introduced profiles. Ordinary empty profiles and test-only SELinux `:ro,z` secret mounts enabled the private services.
- `./scripts/bootstrap-local.sh -p chronosops-test -f docker-compose.yml -f docker-compose.test.yml run --rm tests pytest -q` — **73 passed, 2 live tests deselected, 1 upstream warning in 240.34s**. Tests use `/app` PYTHONPATH; no live provider credentials are mounted.
- Actual HTTP smoke: no auth401; Acme spoofing Globex still received its own monitoring response200; Globex spoofing Acme received404; proxy internal path403; direct API forged identity401.
- Actual seeded Nginx HTTP/WebSocket smoke: simulation reached awaiting_approval, Acme received run_started, and Globex with a forged Acme header was denied403 before acceptance. Only test_suite_ops_topology was seeded. The earlier empty-fixture smoke produced a failed investigation, not an actionable proposal.

### Step 3 commands and observations

- Deployment verifier, dedicated recovery role/profile, pinned document submission and operator fault-window safety implemented. The runtime profile cannot fall back to the operator identity; previously active bindings remain inactive history after a verified publication.
- Preflight inspects offering, quota limit, AMI/root minimum and current public catalog pricing; the quota report explicitly does not claim available headroom. Stack discovery errors other than exact not-found propagate.
- `podman build -t localhost/chronosops-test_tests:latest .` succeeded.
- `uvx --from cfn-lint==1.57.2 cfn-lint infra/demo.yaml` — exit0 after correcting the CloudFormation CPUCredits property and removing unnecessary substitution.
- Isolated focused gate: `pytest -q tests/test_deployment_binding.py tests/test_deployment_template.py tests/test_fault_safety.py tests/test_module_3a.py` — **30 passed, 1 upstream warning in 6.42s**.
- Actual container entry points `python -m scripts.demo_stack --help` and `python -m scripts.fault --help` succeeded; the latter exposes only stop/start.
- Actual function smoke published and reread a binding in isolated FalkorDB, then refused a foreign expected instance without replacing it. AWS responses were explicitly synthetic adapters; the database publication was real. This is not live AWS verification.
- After authorized AWS login renewal, the official MCP probe executed STS successfully but CloudFormation DescribeStacks was denied by an organization SCP in us-east-1. Live template/deployment/role-permission verification remains blocked. No fault or AWS resource mutation occurred.
- Cumulative command: `./scripts/bootstrap-local.sh -p chronosops-test -f docker-compose.yml -f docker-compose.test.yml run --rm tests pytest -q` — **98 passed, 2 live tests deselected, 1 upstream warning in 240.36s**.

### Step 4 commands and observations

- Replaced target/evaluation-time assumptions with typed raw scrape evidence, actual `timestamp(probe_success)` agreement, fixed deployment/module labels and exporter reachability. Persisted immutable HealthObservation nodes linked to deployment/incident; no unknown monitoring state creates an actionable outage.
- Removed the health-polling bypass and instance-ID URL construction. Recovery verification now uses the bound endpoint and SSM completion timestamp, returns supporting sample IDs and offloads blocking HTTP reads.
- Alert receiver now requires generated token configuration and corroborated workload labels/probes. Identity hashes deployment + fingerprint + normalized startsAt. Resolved alerts without agent proof are external recovery; monitoring-loss/restoration notifications never create workload incidents.
- Focused command in the isolated container: `pytest -q tests/test_monitoring_evidence.py tests/test_alert_evidence.py tests/test_health_store.py tests/test_monitoring_configuration.py tests/test_module_3a.py tests/test_tenant_boundary.py` — **50 passed, 1 upstream warning in 22.24s**.
- The first focused run exposed an epoch-time comparison bug: math.isclose's default relative tolerance accepted a one-second mismatch. Setting rel_tol=0 made the raw/latest timestamp disagreement fail closed. Graph float serialization is checked within 10 microseconds rather than bitwise float equality.
- Actual private Uvicorn HTTP smoke plus local Prometheus-response fixture: malformed timestamp returned422; corroborated failure returned200/observed with a stored observation ID; duplicate delivery reused its incident ID; a later outage generated another ID. Three malformed-body regressions failed before nested Pydantic alert validation and passed afterward. This exercised real HTTP and FalkorDB, not AWS/real workload probes. Temporary service containers were stopped and removed.
- Official Prometheus3.15.0 `promtool check config /etc/prometheus/prometheus.yml` and `check rules /etc/prometheus/alerts.yml` passed, validating both rules. Docker Hub pull timed out; the documented official Quay alternative succeeded. Missing generated discovery targets produced the expected warning before publication. Tested digest is recorded in DECISIONS.md.
- Final cumulative isolated command `run --rm tests pytest -q` — **130 passed, 2 live tests deselected, 1 upstream warning in 257.19s**.


## Live prerequisite policy

AWS/model access, exact workload ownership, restricted recovery role, Slack installation/channel, public tunnel and publishing access must be discovered and verified during the corresponding steps. No current-access claim is inferred from old artifacts. Missing access blocks its live proof, not independent local implementation. No AWS workload mutation is part of an automated default test. Never mark a blocked live proof passed.
