# ChronosOps: Graph-Grounded Incident Decisions

ChronosOps is a local-first experiment in using FalkorDB as context for an
incident workflow. Its code separates three concerns:

- **Company Brain:** ownership and decision-provenance queries require current,
  published source citations.
- **Agents That Act:** bounded graph traversal can identify affected services
  and reject failover routes that exceed a supplied cost cap. A proposed action
  remains pending until a human decides in the authenticated operator app.
- **Agent Memory:** the repository includes tenant-scoped episodic, semantic,
  and procedural-memory code paths.

The application is configured to use Amazon Bedrock model identifiers for
generation and embeddings; it does not use a substitute model provider.

## What can currently be demonstrated

The simulation flow can be observed only against a currently seeded,
authoritative graph. The runner asks the authenticated app for Company Brain
answers, checks that the answers include published citations, observes a
graph-verified route within the cap and at least one cap refusal, and creates a
pending simulation proposal. A human must approve that proposal in the
operator app. Only then can the runner verify the corresponding persisted
`TrafficState`.

When proxy credentials are not available, the runner labels its output
**headless** and uses the existing trusted graph-domain entry points. That path
does not stand in for browser or authenticated-app proof. Every invocation
rewrites `artifacts/track-demo.json` from the observations made during that
invocation; a previous manifest cannot make a chapter pass.

The current browser recording demonstrates graph-based simulation proposals,
explicit approval and rejection, persisted action state, a public WebSocket
upgrade, and tenant isolation. Its labels distinguish synthetic topology and
simulated routing from an observed AWS outage. The recording is not evidence of
all three live chapters.

## Gates that remain blocked

Real recovery is globally disarmed by default. A real run must remain blocked
unless all of the following can be freshly verified: the deliberate arming
state, a dedicated AWS deployment and exact runbook binding, an active
fault-watchdog window, current failed-health evidence, and current Slack source
and channel evidence. Workload discovery is currently denied by the organization
service control policy, live Slack configuration is absent, and the local
Bedrock login has expired. No live EC2-recovery or Slack-outcome claim is made
here. The runner never converts this condition into a simulation result and
never approves a real action.

The Agent Memory chapter is also incomplete. A passing claim requires
authoritative proof of a persistent episode and its later retrieval; the demo
does not manufacture outcome data or procedural-learning success to obtain
that proof.

## Safety boundaries

The demo runner does not seed graphs, modify source documents, approve actions,
or consolidate memory. Missing, ambiguous, stale, or unavailable evidence is
reported as blocked. Simulation and live recovery are distinct modes, and a
browser recording is required to establish browser behavior rather than a CLI
request being treated as browser evidence.
