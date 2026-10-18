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
5. Canonical ingestion and citation publication — gate passed: 151 isolated default tests, fresh live SDK edit/delete lifecycle, actual seed CLI/MCP provenance and native resolver index verified. Stacked branch remains unmerged behind the deployment prerequisite.
6. Slack lifecycle and notifications — local gate passed (167 default tests, 22 focused cases, actual SDK/HTTP smoke); live installed-app human post/edit/delete gate blocked by absent Slack configuration.
7. Evidence-bound proposals and routes — local gate passed: 194 isolated tests, actual LangGraph/MCP proposal smoke and official-SDK controlled refresh. Atomic execution is the next gate; real recovery stays disabled.
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


### Step 5 commands and observations

- Replaced manual canonical entity/authorship writes with SDK strict table mappings, signed canonical properties and explicit validated aliases. SourceRecord/Contribution/SourceRequest state is application-owned; SDK metadata remains SDK-owned.
- Source revisions are withdrawn before work and published only after the matching SDK revision finalizes. Unknown/deleted source HTTP responses are 404; pending/failed revisions are 503. Contributions follow current published authorship; duplicate display names cannot inflate the external-identity count. Canonical table row chunks are published and cited as their actual sources.
- An isolated Uvicorn/FalkorDB smoke exercised real HTTP transitions **200 → 503 → 200 with changed hash/author/excerpt → 404**. Extraction was an explicitly synthetic adapter in this HTTP smoke; it is not live-model proof.
- An intermediate cumulative isolated run passed **145 tests, 2 live tests deselected, 1 upstream warning in 391.44s**. Its earlier failure was an obsolete mock of a removed readiness helper; the replacement withdraws actual graph readiness and checks abstention.
- Explicit live command: `AWS_PROFILE=sai ./scripts/bootstrap-local.sh -p chronosops-test -f docker-compose.yml -f docker-compose.test.yml -f docker-compose.bedrock-test.yml run --rm tests pytest -s -q -m bedrock_live -o log_cli=true --log-cli-level=INFO tests/test_sdk_ingestion.py --tb=short` — **1 passed, 1 deselected, 1 upstream warning in 400.84s**, using current source mounts during development. This exercised three SDK tables, 16 documents, Bedrock extraction/resolution/embeddings, canonical ownership and RFC provenance through MCP, changed source hash/excerpt, idempotent publication, unrelated-source preservation and SDK deletion.
- Initial live attempts timed out on an internal test network and then a read-only login refresh cache; neither passed. A bounded single-call probe identified the refresh-cache error. The explicit overlay leaves host AWS paths read-only and refreshes only a private tmpfs copy. An expired session was renewed with explicit user permission and verified through STS; the deployment SCP was not retried or bypassed.
- The separate operator smoke caught a resolver failure that the SDK logs and falls through: `import hnswlib` failed because the image lacked `libgomp.so.1`. Its provenance assertion failed; this smoke is **not passed**. Added `libgomp1` to the image and a direct required resolver import so this dependency cannot silently disable resolution. Native-index, operator and final regression checks are being rerun after the fix.
- The rebuilt image's native-index probe then failed on a host-cached wheel requiring `GLIBC_2.43`; the new direct dependency import also stopped default test collection before any SDK work (**1 collection error, 1 deselected in 6.44s**, not a passing gate). Added a matching Python dependency build stage, isolated its uv cache from host-native wheels and enabled upstream portable compilation. Rebuilding before rerunning the index, operator and regression checks.
- The fully isolated dependency-cache build timed out at 1800s while repeatedly downloading the already host-cached 187.2MiB CPU Torch wheel; hnswlib itself built successfully. Changed to two frozen-lock installation passes: reuse other locked dependencies, then install only missing hnswlib through the container-specific cache. No host-built native hnswlib wheel is reused.
- Selective-cache rebuild command: `podman build --security-opt label=disable --volume /home/skrisps/.cache/uv:/root/.cache/uv:rw -t localhost/chronosops-test_tests:latest .` — succeeded in **133.62s**, image `ee41b781ea06275ff891774c2b73f135416235815f113a293666b7c7a20931d3`. The frozen passes installed 125 dependencies and then only hnswlib.
- Offline native smoke: `podman run --rm --network none localhost/chronosops-test_tests:latest python -c "import hnswlib, numpy as np, shutil; assert shutil.which('g++') is None; vectors=np.zeros((3,256),dtype=np.float32); vectors[0,0]=1; vectors[1,1]=1; vectors[2,0]=-1; index=hnswlib.Index(space='ip',dim=256); index.init_index(max_elements=3,ef_construction=200,M=16); index.add_items(vectors,[10,20,30]); labels,distances=index.knn_query(vectors[:1],k=1); assert labels[0,0]==10 and abs(float(distances[0,0]))<1e-6; print('NATIVE_RESOLVER_INDEX_OK dimensions=256 nearest_id=10 distance='+str(float(distances[0,0]))+' runtime_compiler=absent')"` — returned **`NATIVE_RESOLVER_INDEX_OK dimensions=256 nearest_id=10 distance=0.0 runtime_compiler=absent`** in 0.67s.
- Actual operator path: inside the explicit Bedrock test runner, `python -m seed.ingest` followed by plain Python read-only MCP ownership/provenance and raw-source-hash assertions passed in **109.39s**. CLI reported **ready=true, 19 documents, 77 chunks**. MCP returned owner **Payments**, supporting ownership source **services.csv**, and provenance including **rfc-001-payments-failover**. The RFC remained revision1, SHA-256 `3e93b7d833f62bf24fb5c7975c6c60f91b003e25d426736e5792d9a4245a3df3`, chunk `chunk-fa346fa25220283d387ae8b1d3b450fd`.
- That operator run retained SDK diagnostics for the cross-label name Checkout and incomplete judge vote sets; no forced cross-label merge or fabricated missing vote was used. The previously failed strict RFC-provenance assertion now passed. The current-image full isolated suite and fresh live edit/delete lifecycle are running.
- After the verification processes were interrupted and their final results were unavailable, restored isolated databases and reran the gates rather than infer their outcomes. Current-image live lifecycle command: `AWS_PROFILE=sai ./scripts/bootstrap-local.sh -p chronosops-test -f docker-compose.yml -f docker-compose.test.yml -f docker-compose.bedrock-test.yml run --rm --no-deps -e FALKORDB_HOST=source-smoke-db tests pytest -s -q -m bedrock_live -o log_cli=true --log-cli-level=INFO tests/test_sdk_ingestion.py --tb=short` — **1 passed, 1 deselected, 1 upstream warning in 386.52s**. This fresh graph run exercised canonical tables, all documents, ownership/membership, strict RFC provenance, real Bedrock update/changed hash/excerpt, unchanged-revision idempotency, unrelated-source preservation and actual SDK deletion. The native resolver performed actual cross-document/table merges; ontology pruning and incomplete judge votes remained visible.
- Final current-image default command inside the isolated runner: `pytest -q`, image `localhost/chronosops-test_tests:latest`, internal network `chronosops-test_default`, test-only credentials/environment from `docker-compose.test.yml` — **151 passed, 2 deselected, 1 upstream warning in 506.39s**. No live AWS/Slack credentials or egress were supplied to that runner. The separate disposable live-gate database was stopped and removed after its successful lifecycle proof. Step 5 gate is passed; deployment and subsequent module gates are not implied.

### Step 6 discovery

- Step 5 commit `4b0754c` was pushed to `feat/correctness-5`; implementation continues on stacked `feat/correctness-6`, not main.
- Presence-only discovery used `uv run --frozen --no-sync python -c` with `dotenv_values(".env")` plus the process environment. `SLACK_TEAM_ID`, `SLACK_CHANNEL_ID`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `SLACK_SERVICE_ID` were all absent. No secret values were printed and no Slack request was attempted. Real installed-app human post/edit/delete proof needs the dedicated workspace/channel, installed member bot, app-level Socket Mode token and explicit canonical service binding; deterministic adapters are not that proof.
- Integration regression checks first failed on native SDK responses (the plain-dict adapters had hidden `AsyncSlackResponse` not being a `Mapping`) and on root IDs changing when alert delivery `recorded_at` changed. Corrected the application and retained native-response outcome tests; no prose expectation was re-pinned.
- Real competing graph clients then exposed duplicate same-key nodes from unconstrained `MERGE`, causing two Slack sends. A synthetic write-barrier experiment did not fix creation uniqueness and was removed. Native operational `UNIQUE` constraints now protect outbox and source identities; trusted seeding installs them, runtime writers do not acquire admin credentials. After this fix the outbox regression exercised twenty enqueue calls and twenty consumer calls with one stored root and one send.
- Focused current local gate inside the isolated runner: `pytest -q tests/test_module_3b.py tests/test_slack_lifecycle.py tests/test_slack_notifications.py tests/test_slack_status_api.py --tb=short` — **22 passed in 53.95s**. Includes supervised disconnected/connected/disconnected transport state, worker shutdown on transport failure, native-response publication, post/edit/delete/replay privacy, metadata-only edits, durable read Retry-After, exact source refresh, outbox ambiguity/receipt-only retry, tenant isolation and missing-reader-credential refusal.
- Actual CLI `python -m integrations.slack` with no Slack configuration exited1; the shell asserted that expected exit and printed `EXPECTED_UNCONFIGURED_CONNECTOR_EXIT=1` (**14.68s**). No network connection or source publication was attempted.
- Actual official AsyncWebClient against a controlled local HTTP peer plus Uvicorn/FalkorDB passed (**52.52s**): applied request followed by connection drop became unknown and did not resend; known send plus permalink503 retried only the read and became sent; stored incident outcome stayed unchanged. Actual HTTP returned401 without proxy identity and observed connected -> stale plus Globex not_owner despite a forged tenant query. Printed `CONTROLLED_SLACK_HTTP_AND_LOCAL_API`; external Slack was not contacted. The first run's 10s heartbeat aged during application import; the corrected harness published the observed heartbeat after the server became ready rather than weakening freshness.
- Final cumulative isolated command inside the current-source runner: `pytest -q` — **167 passed, 2 deselected, 1 upstream warning in 420.25s**. Separate fresh constrained SDK lifecycle: `AWS_PROFILE=sai ./scripts/bootstrap-local.sh -p chronosops-test -f docker-compose.yml -f docker-compose.test.yml -f docker-compose.bedrock-test.yml run --rm --no-deps -e FALKORDB_HOST=source-smoke-db -v /home/skrisps/falkor/seed:/app/seed:ro -v /home/skrisps/falkor/tests:/app/tests:ro tests pytest -q -m bedrock_live tests/test_sdk_ingestion.py --tb=short` — **1 passed, 1 deselected, 1 upstream warning in 511.34s**. Operational application identity constraints did not break supported SDK update/delete or canonical citations. Local Step 6 gate passed; real Slack proof remains blocked and is not replaced by these controlled adapters.

### Step 7 integration observations

- Shared deterministic proposal authority now refreshes actual topology, target/runbook, persisted probe IDs, current Slack publication and watchdog metadata. Generated explanations do not grant authority. Native constraints protect immutable approval identity; repeated presentation cannot extend expiry.
- Failed blackbox probes can have HTTP status0 (connection refused) or HTTP200 (failed content marker). Eligibility requires failed probe plus healthy exporter, not HTTP status >=400.
- Safety regressions use actual isolated graph rows and official read-only MCP calls; only external services and fault-file input are controlled. These are not real AWS/Slack recovery evidence.
- Initial separate-prefix workflow smoke refused graph access because the isolated reader ACL permits only its configured namespace. The smoke did not create an approval; rerun must use the configured namespace after the focused gate, without broadening ACLs.
- Initial focused command `pytest -q tests/test_proposal_safety.py tests/test_route_safety.py tests/test_module_4.py` returned **1 failed, 29 passed in 412.61s**: the empty-source guard emitted a generic missing-evidence reason instead of the domain's missing-Slack reason. Removed the redundant generic guard; the typed source validator now owns that refusal.
- Actual workflow smoke in the configured isolated namespace passed in **76.42s**: `ACTUAL_LANGGRAPH_MCP_PROPOSAL`, tenant Globex, checkout -> queue -> payments-backup, one immutable approval, unchanged expiry/hash on repeat, unhealthy queue refused, `REAL_RECOVERY_ENABLED=false`. Its temporary topology/private-memory graphs were removed.
- Official boto3/botocore `Stubber` smoke passed in **1.81s**: `CONTROLLED_OFFICIAL_AWS_SDK_REFRESH`, exact pinned document hash/version accepted, foreign operator tag and expired deployment refused, zero network mutations. The initial standalone harness had retained empty `AWS_PROFILE`; removing that empty setting matched the established isolated-test convention. External AWS was not contacted.
- Final cumulative isolated runner command `pytest -q --tb=short`, using the current `agents`, `api`, `integrations`, `seed` and `tests` bind mounts and only `docker-compose.test.yml` fake credentials — **194 passed, 1 deselected, 1 upstream warning in 773.60s**. Includes actual-graph HTTP0/content-marker failures, pending-versus-unresolved commands, source invalidation, unknown service and all earlier local gates. Bedrock lifecycle was not rerun for this proposal-only change.


## Live prerequisite policy

AWS/model access, exact workload ownership, restricted recovery role, Slack installation/channel, public tunnel and publishing access must be discovered and verified during the corresponding steps. No current-access claim is inferred from old artifacts. Missing access blocks its live proof, not independent local implementation. No AWS workload mutation is part of an automated default test. Never mark a blocked live proof passed.
