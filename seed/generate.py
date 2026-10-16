"""Generate deterministic synthetic topology and evidence corpus for ChronosOps."""
from __future__ import annotations

import csv
import hashlib
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from faker import Faker
import networkx as nx

SEED = 42
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "seed" / "data"
DOCS_DIR = DATA_DIR / "docs"
MANIFEST_FILE = ROOT / "seed" / "manifest.json"


@dataclass
class ServiceNode:
    service_id: str
    name: str
    owner_team: str
    environment: str = "production"
    health: str = "healthy"
    tier: str = "tier-2"
    origin: str = "synthetic"


@dataclass
class DependencyEdge:
    source_id: str
    target_id: str
    origin: str = "synthetic"


@dataclass
class RouteEdge:
    source_id: str
    target_id: str
    latency_ms: int
    cost: int
    health: str = "healthy"
    origin: str = "synthetic"


@dataclass
class Person:
    person_id: str
    name: str
    email: str
    team: str


def compute_hash(data: Any) -> str:
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def generate_data() -> dict[str, Any]:
    rng = random.Random(SEED)
    fake = Faker()
    fake.seed_instance(SEED)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Teams & People (12 people across 4 teams)
    teams = [
        {"team_id": "team-payments", "name": "Payments"},
        {"team_id": "team-checkout", "name": "Checkout"},
        {"team_id": "team-platform", "name": "Core-Platform"},
        {"team_id": "team-data", "name": "Data-Engineering"},
    ]

    # Alice is the authoritative lead for Payments
    people: list[Person] = [
        Person(person_id="user-alice", name="Alice Chen", email="alice@chronosops.corp", team="Payments"),
        Person(person_id="user-bob", name="Bob Martinez", email="bob@chronosops.corp", team="Payments"),
        Person(person_id="user-carol", name="Carol Danvers", email="carol@chronosops.corp", team="Checkout"),
        Person(person_id="user-david", name="David Kim", email="david@chronosops.corp", team="Checkout"),
        Person(person_id="user-eve", name="Eve Jenkins", email="eve@chronosops.corp", team="Core-Platform"),
        Person(person_id="user-frank", name="Frank Castle", email="frank@chronosops.corp", team="Core-Platform"),
        Person(person_id="user-grace", name="Grace Hopper", email="grace@chronosops.corp", team="Data-Engineering"),
        Person(person_id="user-heidi", name="Heidi Turner", email="heidi@chronosops.corp", team="Data-Engineering"),
        Person(person_id="user-ivan", name="Ivan Vanko", email="ivan@chronosops.corp", team="Payments"),
        Person(person_id="user-judy", name="Judy Alvarez", email="judy@chronosops.corp", team="Checkout"),
        Person(person_id="user-karl", name="Karl Urban", email="karl@chronosops.corp", team="Core-Platform"),
        Person(person_id="user-laura", name="Laura Kinney", email="laura@chronosops.corp", team="Data-Engineering"),
    ]

    # 2. Canonical Scenario Services (8 services)
    # A DEPENDS_ON B means A depends on B.
    # gateway -> checkout -> payments-primary
    # worker -> orders -> payments-primary
    scenario_services: list[ServiceNode] = [
        ServiceNode(service_id="gateway", name="API Gateway", owner_team="Core-Platform", tier="tier-0"),
        ServiceNode(service_id="checkout", name="Checkout Service", owner_team="Checkout", tier="tier-1"),
        ServiceNode(service_id="orders", name="Orders Ingestion", owner_team="Checkout", tier="tier-1"),
        ServiceNode(service_id="worker", name="Order Processing Worker", owner_team="Checkout", tier="tier-2"),
        ServiceNode(service_id="payments-primary", name="Payments Primary API", owner_team="Payments", tier="tier-0", health="degraded"),
        ServiceNode(service_id="payments-backup", name="Payments Standby Gateway", owner_team="Payments", tier="tier-1"),
        ServiceNode(service_id="premium", name="High-Priority Transit Link", owner_team="Core-Platform", tier="tier-2"),
        ServiceNode(service_id="queue", name="Buffered Failover Queue", owner_team="Core-Platform", tier="tier-2"),
    ]

    # Scenario dependencies (exactly 4 services depend on payments-primary directly or indirectly)
    scenario_dependencies: list[DependencyEdge] = [
        DependencyEdge(source_id="gateway", target_id="checkout"),
        DependencyEdge(source_id="checkout", target_id="payments-primary"),
        DependencyEdge(source_id="orders", target_id="payments-primary"),
        DependencyEdge(source_id="worker", target_id="orders"),
    ]

    # Scenario routing (normal + candidate failovers)
    scenario_routes: list[RouteEdge] = [
        RouteEdge(source_id="checkout", target_id="payments-primary", latency_ms=1, cost=1, health="degraded"),
        # Candidate 1 (Fastest, Rejected): latency 1+1=2, cost 10+10=20 > cap 12
        RouteEdge(source_id="checkout", target_id="premium", latency_ms=1, cost=10),
        RouteEdge(source_id="premium", target_id="payments-backup", latency_ms=1, cost=10),
        # Candidate 2 (Safe Alternative): latency 2+3=5, cost 4+4=8 <= cap 12
        RouteEdge(source_id="checkout", target_id="queue", latency_ms=2, cost=4),
        RouteEdge(source_id="queue", target_id="payments-backup", latency_ms=3, cost=4),
    ]

    # 3. Filler Services (37 services to reach exactly 45 services)
    # INVARIANT: No paths from filler services into payments-primary or scenario graph
    filler_services: list[ServiceNode] = []
    filler_teams = ["Checkout", "Core-Platform", "Data-Engineering"]
    for i in range(1, 38):
        sid = f"svc-{i:02d}"
        sname = f"Service {fake.bs().title()}"
        filler_services.append(
            ServiceNode(
                service_id=sid,
                name=sname,
                owner_team=rng.choice(filler_teams),
                tier=rng.choice(["tier-2", "tier-3"]),
            )
        )

    # Generate sparse tree/DAG between filler services (independent component)
    filler_dag = nx.gnp_random_graph(37, 0.08, seed=SEED, directed=True)
    filler_dependencies: list[DependencyEdge] = []
    for u, v in filler_dag.edges():
        if u != v:
            filler_dependencies.append(
                DependencyEdge(
                    source_id=f"svc-{u+1:02d}",
                    target_id=f"svc-{v+1:02d}",
                )
            )

    all_services = scenario_services + filler_services
    all_dependencies = scenario_dependencies + filler_dependencies
    all_routes = scenario_routes

    # Verify NetworkX invariant: Ancestors of payments-primary must be exactly gateway, checkout, orders, worker
    G_dep = nx.DiGraph()
    for d in all_dependencies:
        G_dep.add_edge(d.source_id, d.target_id)

    # In A -> B (A depends on B), ancestors of B in this directed graph are the nodes that can reach B
    affected_nodes = sorted(nx.ancestors(G_dep, "payments-primary"))
    expected_affected = ["checkout", "gateway", "orders", "worker"]
    assert affected_nodes == expected_affected, f"Affected nodes mismatch: {affected_nodes} vs {expected_affected}"

    # 4. Aliases
    aliases = [
        {"alias": "Payments API", "service_id": "payments-primary"},
        {"alias": "pay-api", "service_id": "payments-primary"},
        {"alias": "Checkout Service", "service_id": "checkout"},
        {"alias": "Standby Gateway", "service_id": "payments-backup"},
    ]

    # 5. Company Brain Documents (RFCs, Slack threads, Tickets, PRs)
    # RFC 001: The core architectural decision authored by Alice
    rfc_001 = """---
document_id: "rfc-001-payments-failover"
title: "RFC-001: Payments Primary Failure and Contingency Routing"
author: "user-alice"
service_id: "payments-primary"
version: "1.0"
status: "APPROVED"
---
# RFC-001: Payments Failover Architecture

## Summary
The Payments Primary API (payments-primary) is the core transaction clearing engine owned by Team Payments.
Lead author: Alice Chen (user-alice).

## Failover Strategy
In the event of an outage on payments-primary, downstream services must reroute traffic to payments-backup.
Two potential transit paths exist:
1. Premium Transit Link (premium): Latency is minimal (2ms), but cost is 20 units (risk of egress congestion).
2. Buffered Failover Queue (queue): Latency is slightly higher (5ms), but cost is strictly bounded at 8 units.

## Architectural Decision
Due to financial risk caps and provider rate limits (budget cap: 12 units), the Queue-based route (checkout -> queue -> payments-backup) is APPROVED as the primary contingency path.
The Premium link route (checkout -> premium -> payments-backup) is explicitly REJECTED under standard operational budgets because its total cost (20) exceeds the maximum safety cap of 12.
"""

    rfc_002 = """---
document_id: "rfc-002-checkout-resilience"
title: "RFC-002: Checkout Service Upstream Dependencies"
author: "user-carol"
service_id: "checkout"
version: "1.0"
status: "APPROVED"
---
# RFC-002: Checkout Service Architecture

Checkout Service relies directly on payments-primary for payment authorizations and API Gateway for ingress traffic.
If payments-primary becomes degraded, checkout transactions block immediately.
Owner: Team Checkout. Contact Carol Danvers.
"""

    rfc_003 = """---
document_id: "rfc-003-data-pipeline"
title: "RFC-003: Analytics Ingestion and ETL Pipeline"
author: "user-grace"
service_id: "svc-01"
version: "1.0"
status: "APPROVED"
---
# RFC-003: Data Pipeline Architecture

Details the data lake extraction and batch analytics processing for business intelligence.
Owned by Team Data-Engineering. Irrelevant to payments incident triage.
"""

    rfc_004 = """---
document_id: "rfc-004-legacy-routing-superseded"
title: "RFC-004: Superseded 2024 Failover Routing (SUPERSEDED)"
author: "user-bob"
service_id: "payments-primary"
version: "0.1-superseded"
status: "DEPRECATED"
---
# RFC-004: Legacy Routing Guideline (DO NOT USE)

NOTE: This document is SUPERSEDED by RFC-001.
Old recommendation suggested always routing via the Premium link regardless of cost.
This caused a $45,000 budget overrun in Q2 2024. RFC-001 overrides this with the queue-based cap.
"""

    slack_001 = """---
document_id: "slack-001-failover-budget"
title: "Slack: #payments-architecture - Failover Budget Review"
author: "user-alice"
service_id: "payments-primary"
timestamp: "2026-09-15T14:30:00Z"
channel: "C-PAYMENTS-ARCH"
---
Alice Chen (user-alice): Team, confirming the failover policy for payments-primary. We tested the premium route vs the queue route. The premium route incurred 20 cost units, which triggers an automated budget freeze. The queue route (checkout -> queue -> payments-backup) takes 5ms and costs 8 units. We will strictly use the queue route.
Bob Martinez (user-bob): Agreed. I've archived the old RFC-004.
"""

    slack_002 = """---
document_id: "slack-002-gateway-rate-limits"
title: "Slack: #core-platform - Gateway Rate Limits"
author: "user-eve"
service_id: "gateway"
timestamp: "2026-09-20T10:15:00Z"
channel: "C-PLATFORM"
---
Eve Jenkins (user-eve): We bumped API Gateway connection limits to 5000 rps for checkout ingress.
"""

    slack_003 = """---
document_id: "slack-003-lunch-planning"
title: "Slack: #general - Friday Team Lunch"
author: "user-david"
service_id: "svc-05"
timestamp: "2026-10-01T12:00:00Z"
channel: "C-RANDOM"
---
David Kim (user-david): Anyone up for tacos this Friday?
Carol Danvers (user-carol): Count me in!
"""

    slack_004 = """---
document_id: "slack-004-backup-maintenance"
title: "Slack: #operations - Live Demo Runbook Notice"
author: "user-alice"
service_id: "payments-primary"
timestamp: "2026-10-15T09:00:00Z"
channel: "C-OPS-DEMO"
---
Alice Chen (user-alice): Operational note for ChronosOps demonstration: payments-backup is unavailable for live traffic shifting in this sandbox; use the reviewed start-service runbook for a stopped primary instance.
"""

    ticket_001 = """---
document_id: "ticket-PAY-101"
title: "PAY-101: Payments Primary Latency Spike and Failover Drill"
author: "user-alice"
resolver: "user-alice"
service_id: "payments-primary"
status: "RESOLVED"
---
Description: Routine drill verifying contingency failover for payments-primary.
Resolution: Confirmed queue failover routes within cost 8. Resolved by Alice Chen.
"""

    ticket_002 = """---
document_id: "ticket-PAY-102"
title: "PAY-102: Configure Buffered Failover Queue Parameters"
author: "user-alice"
resolver: "user-alice"
service_id: "payments-primary"
status: "RESOLVED"
---
Description: Updated queue consumer timeouts to 3ms.
Resolution: Deployed and tested by Alice Chen.
"""

    ticket_003 = """---
document_id: "ticket-CHK-201"
title: "CHK-201: Checkout Basket Validation Timeouts"
author: "user-carol"
resolver: "user-david"
service_id: "checkout"
status: "RESOLVED"
---
Description: Checkout service experienced basket validation delays.
Resolution: Indexed user session cache. Resolved by David Kim.
"""

    ticket_004 = """---
document_id: "ticket-OPS-301"
title: "OPS-301: Routine Kernel Security Patching"
author: "user-frank"
resolver: "user-frank"
service_id: "svc-12"
status: "RESOLVED"
---
Description: Fleet-wide security patching on non-critical worker tier.
Resolution: Completed with zero customer impact.
"""

    pr_001 = """---
document_id: "pr-101-payments-queue-routing"
title: "PR #101: Implement Bounded Queue Routing for Payments Failover"
author: "user-alice"
service_id: "payments-primary"
merged: true
---
Implements the queue route selection in accordance with RFC-001. Authored by Alice Chen.
"""

    pr_002 = """---
document_id: "pr-102-checkout-upstream-circuit-breaker"
title: "PR #102: Upstream Timeout Handling in Checkout"
author: "user-carol"
service_id: "checkout"
merged: true
---
Adds circuit breaking to checkout when payments-primary returns 5xx errors.
"""

    pr_003 = """---
document_id: "pr-103-gateway-burst-limits"
title: "PR #103: Update Gateway Token Bucket Parameters"
author: "user-eve"
service_id: "gateway"
merged: true
---
Configures API gateway burst rates.
"""

    pr_004 = """---
document_id: "pr-104-worker-retry-backoff"
title: "PR #104: Exponential Backoff for Orders Worker"
author: "user-david"
service_id: "worker"
merged: true
---
Prevents worker retry thundering herd on order processing failure.
"""

    documents = [
        ("rfc-001-payments-failover.md", rfc_001),
        ("rfc-002-checkout-resilience.md", rfc_002),
        ("rfc-003-data-pipeline.md", rfc_003),
        ("rfc-004-legacy-routing-superseded.md", rfc_004),
        ("slack-001-failover-budget.md", slack_001),
        ("slack-002-gateway-rate-limits.md", slack_002),
        ("slack-003-lunch-planning.md", slack_003),
        ("slack-004-backup-maintenance.md", slack_004),
        ("ticket-PAY-101.md", ticket_001),
        ("ticket-PAY-102.md", ticket_002),
        ("ticket-CHK-201.md", ticket_003),
        ("ticket-OPS-301.md", ticket_004),
        ("pr-101-payments-queue-routing.md", pr_001),
        ("pr-102-checkout-upstream-circuit-breaker.md", pr_002),
        ("pr-103-gateway-burst-limits.md", pr_003),
        ("pr-104-worker-retry-backoff.md", pr_004),
    ]

    for fname, content in documents:
        (DOCS_DIR / fname).write_text(content.strip() + "\n", encoding="utf-8")

    # Write CSV tables for structured ingestion
    with (DATA_DIR / "services.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["service_id", "service_name", "environment", "owner_team", "tier"])
        for s in all_services:
            writer.writerow([s.service_id, s.name, s.environment, s.owner_team, s.tier])

    with (DATA_DIR / "people.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["person_id", "name", "email", "team"])
        for p in people:
            writer.writerow([p.person_id, p.name, p.email, p.team])

    with (DATA_DIR / "teams.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["team_id", "name"])
        for t in teams:
            writer.writerow([t["team_id"], t["name"]])

    with (DATA_DIR / "aliases.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["alias", "service_id"])
        for a in aliases:
            writer.writerow([a["alias"], a["service_id"]])

    # Build and write manifest
    manifest_data = {
        "scenario": {
            "root_service": "payments-primary",
            "affected_services": expected_affected,
            "routes": {
                "fastest_rejected": {
                    "path": ["checkout", "premium", "payments-backup"],
                    "latency_ms": 2,
                    "cost": 20,
                    "max_cost_cap": 12,
                    "rejected": True,
                },
                "safe_alternative": {
                    "path": ["checkout", "queue", "payments-backup"],
                    "latency_ms": 5,
                    "cost": 8,
                    "max_cost_cap": 12,
                    "rejected": False,
                },
            },
            "ownership": {
                "service_id": "payments-primary",
                "team": "Payments",
                "primary_contributor": "user-alice",
                "bus_factor_proxy": 1,
            },
        },
        "counts": {
            "services": len(all_services),
            "dependencies": len(all_dependencies),
            "routes": len(all_routes),
            "people": len(people),
            "teams": len(teams),
            "aliases": len(aliases),
            "documents": len(documents),
        },
        "services": [asdict(s) for s in all_services],
        "dependencies": [asdict(d) for d in all_dependencies],
        "routes": [asdict(r) for r in all_routes],
        "people": [asdict(p) for p in people],
        "teams": teams,
        "aliases": aliases,
        "document_files": [fname for fname, _ in documents],
    }

    manifest_hash = compute_hash(manifest_data)
    manifest_data["manifest_hash"] = manifest_hash

    MANIFEST_FILE.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
    return manifest_data


if __name__ == "__main__":
    res = generate_data()
    print(f"Generated {res['counts']['services']} services, {res['counts']['dependencies']} dependencies, {res['counts']['routes']} routes, {res['counts']['documents']} documents.")
    print(f"Manifest written to {MANIFEST_FILE} (hash: {res['manifest_hash'][:12]})")
