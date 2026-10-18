#!/usr/bin/env python3
"""Run only currently observable ChronosOps demonstration evidence.

The runner never seeds data, edits sources, approves actions, or consolidates
memory.  It records a new manifest for this invocation and returns non-zero
until every requested chapter has current authoritative proof.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "track-demo.json"
CREDENTIALS = ROOT / ".secrets" / "proxy-basic-auth.env"


class DemoBlocked(RuntimeError):
    """A required observation could not be made."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def blocked(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "blocked", "reason": reason, **json_safe(details)}


def passed(**details: Any) -> dict[str, Any]:
    return {"status": "passed", **json_safe(details)}


def write_manifest(report: dict[str, Any]) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class AuthenticatedApp:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self._authorization, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = self.base_url
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read().decode("utf-8")
                if response.status < 200 or response.status >= 300:
                    raise DemoBlocked(f"HTTP {response.status} from {path}")
                decoded = json.loads(payload)
                if not isinstance(decoded, dict):
                    raise DemoBlocked(f"Non-object response from {path}")
                return decoded
        except urllib.error.HTTPError as error:
            try:
                detail = error.read().decode("utf-8")
            except OSError:
                detail = ""
            raise DemoBlocked(f"HTTP {error.code} from {path}: {detail[:400]}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise DemoBlocked(f"HTTP observation unavailable for {path}: {error}") from error


def configured_app() -> AuthenticatedApp | None:
    credentials = env_file(CREDENTIALS)
    username = credentials.get("ACME_BASIC_AUTH_USERNAME")
    password = credentials.get("ACME_BASIC_AUTH_PASSWORD")
    if not username or not password:
        return None
    base_url = os.environ.get("CHRONOSOPS_DEMO_URL") or os.environ.get("PUBLIC_ORIGIN") or "http://127.0.0.1:8080"
    return AuthenticatedApp(base_url, username, password)


def response_tool_ok(response: Mapping[str, Any], *, citations_required: bool) -> tuple[bool, dict[str, Any]]:
    tool = response.get("tool_result")
    if not isinstance(tool, Mapping):
        return False, {"reason": "application response omitted tool_result"}
    evidence = tool.get("evidence")
    citations = response.get("citations") or response.get("sources")
    if citations is None and isinstance(evidence, Mapping):
        citations = evidence.get("citations")
    observed = {"tool_status": tool.get("status"), "citations": citations or []}
    return tool.get("status") == "OK" and (not citations_required or bool(citations)), observed


async def trusted_company_brain() -> dict[str, Any]:
    from agents.knowledge import decision_provenance, who_owns

    ownership, provenance = await asyncio.gather(
        who_owns("payments-primary", tenant="acme"),
        decision_provenance("payments-primary", tenant="acme"),
    )
    ownership_citations = ownership.evidence.get("citations", []) if isinstance(ownership.evidence, Mapping) else []
    provenance_citations = provenance.evidence.get("citations", []) if isinstance(provenance.evidence, Mapping) else []
    if ownership.status != "OK" or provenance.status != "OK" or not ownership_citations or not provenance_citations:
        return blocked(
            "authoritative ownership and provenance citations are unavailable",
            ownership_status=ownership.status,
            provenance_status=provenance.status,
            ownership_citations=ownership_citations,
            provenance_citations=provenance_citations,
        )
    return passed(
        owner_team=ownership.data.get("owner_team"),
        ownership_citations=ownership_citations,
        provenance_documents=provenance.data.get("sources", []),
        provenance_citations=provenance_citations,
    )


async def trusted_route_evidence() -> dict[str, Any]:
    from agents.tools import blast_radius, failover_routes

    radius, routes = await asyncio.gather(
        blast_radius("payments-primary", tenant="acme"),
        failover_routes("checkout", "payments-backup", 12, tenant="acme"),
    )
    rejected = routes.evidence.get("rejected_routes", []) if isinstance(routes.evidence, Mapping) else []
    safe = routes.data if isinstance(routes.data, Mapping) else {}
    refusal = [item for item in rejected if isinstance(item, Mapping) and "cost" in str(item.get("reason", "")).lower()]
    route_is_safe = (
        routes.status == "OK"
        and isinstance(safe.get("recommended_route"), list)
        and isinstance(safe.get("cost"), int)
        and safe["cost"] <= 12
    )
    if radius.status != "OK" or not route_is_safe or not refusal:
        return blocked(
            "current graph route evidence does not prove both a safe route and a cost-cap refusal",
            blast_radius_status=radius.status,
            route_status=routes.status,
            route_data=safe,
            rejected_routes=rejected,
        )
    return passed(
        affected_services=radius.data.get("affected_services", []),
        recommended_route=safe["recommended_route"],
        cost=safe["cost"],
        rejected_routes=refusal,
        topology_version=safe.get("topology_version"),
    )


async def headless_simulation(route: dict[str, Any]) -> dict[str, Any]:
    """Create only a genuine pending proposal; a person must approve through the app."""
    from agents.governance import governance_node
    from agents.remediation import remediation_node
    from agents.triage import triage_node
    from agents.workflow import IncidentState, create_incident_workflow

    incident_id = f"inc-demo-{int(time.time() * 1000)}"
    engine = create_incident_workflow(triage_node, remediation_node, governance_node)
    result = await engine.ainvoke(
        IncidentState(
            incident_id=incident_id,
            tenant_id="acme",
            service_id="payments-primary",
            execution_mode="simulation",
            max_cost=12,
            session_id="demo-headless",
        )
    )
    approval_id = result.get("approval_id")
    if result.get("status") != "awaiting_approval" or not isinstance(approval_id, str):
        return blocked("trusted workflow did not create a current pending simulation approval", workflow=result)
    return blocked(
        "HEADLESS: a human must approve this simulation in the authenticated operator app before TrafficState can be observed",
        incident_id=incident_id,
        approval_id=approval_id,
        proposed_action=result.get("proposed_action"),
        route_evidence=route,
    )


def observed_traffic_state(incident_id: str) -> dict[str, Any] | None:
    from falkordb import FalkorDB
    from agents.settings import graph_names
    from seed.load import get_config

    config = get_config()
    graph = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    ).select_graph(graph_names().topology)
    rows = graph.ro_query(
        "MATCH (t:TrafficState {tenant_id:$tenant_id,incident_id:$incident_id}) RETURN properties(t) LIMIT 2",
        {"tenant_id": "acme", "incident_id": incident_id},
        timeout=3000,
    ).result_set
    if len(rows) != 1:
        return None
    return dict(rows[0][0])


def http_company_brain(app: AuthenticatedApp) -> dict[str, Any]:
    ownership = app.request("POST", "/api/questions", {"question": "Who owns payments-primary?", "service_id": "payments-primary"})
    provenance = app.request("POST", "/api/questions", {"question": "Why is the payments backup route designed this way?", "service_id": "payments-primary"})
    ownership_ok, ownership_observation = response_tool_ok(ownership, citations_required=True)
    provenance_ok, provenance_observation = response_tool_ok(provenance, citations_required=True)
    if not ownership_ok or not provenance_ok:
        return blocked(
            "authenticated application responses lack current cited Company Brain evidence",
            ownership=ownership_observation,
            provenance=provenance_observation,
        )
    return passed(ownership=ownership_observation, provenance=provenance_observation)


def http_simulation(app: AuthenticatedApp, route: dict[str, Any], wait_seconds: int) -> dict[str, Any]:
    created = app.request(
        "POST",
        "/api/incidents",
        {"execution_mode": "simulation", "service_id": "payments-primary", "max_cost": 12, "session_id": "demo-http"},
    )
    incident_id = created.get("incident_id")
    approval_id = created.get("approval_id")
    proposal = created.get("proposed_action")
    if created.get("status") != "awaiting_approval" or not isinstance(incident_id, str) or not isinstance(approval_id, str):
        return blocked("authenticated application did not create a pending simulation approval", creation=created, route_evidence=route)
    if not isinstance(proposal, Mapping) or proposal.get("action_kind") != "simulate_failover":
        return blocked("authenticated application proposed an unexpected simulation action", creation=created, route_evidence=route)

    deadline = time.monotonic() + wait_seconds
    current: dict[str, Any] = created
    while True:
        current = app.request("GET", f"/api/incidents/{incident_id}")
        if current.get("status") != "awaiting_approval" and current.get("status") != "pending":
            break
        if time.monotonic() >= deadline:
            return blocked(
                "awaiting human approval in the authenticated operator app; the runner did not approve it",
                incident_id=incident_id,
                approval_id=approval_id,
                proposed_action=proposal,
                approval_url=f"{app.base_url}/",
                route_evidence=route,
            )
        time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))

    if current.get("status") != "applied":
        return blocked(
            "human decision did not produce an applied simulation action",
            incident_id=incident_id,
            approval_id=approval_id,
            incident=current,
            route_evidence=route,
        )
    try:
        traffic = observed_traffic_state(incident_id)
    except Exception as error:
        return blocked("TrafficState could not be observed from the authoritative topology graph", incident_id=incident_id, error=str(error))
    if not traffic or traffic.get("active_route") != proposal.get("route") or traffic.get("action") != "simulate_failover":
        return blocked("applied simulation has no matching authoritative TrafficState", incident_id=incident_id, traffic_state=traffic)
    return passed(
        incident_id=incident_id,
        approval_id=approval_id,
        human_approval_status=current.get("status"),
        traffic_state=traffic,
        route_evidence=route,
    )


def fault_window_observation() -> dict[str, Any]:
    path = ROOT / "artifacts" / "fault-window.json"
    if not path.is_file():
        return {"present": False, "reason": "fault-window metadata is absent"}
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"present": False, "reason": f"fault-window metadata unreadable: {error}"}
    if not isinstance(observed, Mapping):
        return {"present": False, "reason": "fault-window metadata is malformed"}
    return {"present": True, "state": observed.get("state"), "watchdog_deadline": observed.get("watchdog_deadline")}


def real_report() -> dict[str, Any]:
    from seed.load import get_config

    config = get_config()
    slack = {name: bool(config.get(name)) for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL_ID", "SLACK_SERVICE_ID")}
    aws_profile = bool(config.get("AWS_PROFILE") or config.get("AWS_RECOVERY_PROFILE"))
    disarmed = config.get("REAL_RECOVERY_ENABLED", "false").lower() != "true"
    return {
        "company_brain": blocked("real chapter not run; this runner does not substitute simulation"),
        "agents_that_act": blocked(
            "real recovery is blocked before any approval or AWS action",
            real_recovery_disarmed=disarmed,
            aws_profile_configured=aws_profile,
            slack_prerequisites=slack,
            fault_window=fault_window_observation(),
            human_steps=[
                "Verify the dedicated deployment and active fault window with the operator workflow.",
                "Restore Slack credentials and channel access, then verify the cited source is current.",
                "Only after an operator deliberately arms real recovery may a human approve in the operator app.",
            ],
        ),
        "agent_memory": blocked("no authoritative persistent-memory proof was observed; this chapter remains unfinished"),
    }


async def run_simulation(wait_seconds: int) -> tuple[dict[str, Any], str]:
    app = configured_app()
    if app is None:
        company = await trusted_company_brain()
        route = await trusted_route_evidence()
        if route.get("status") == "passed":
            try:
                action = await headless_simulation(route)
            except Exception as error:
                action = blocked("HEADLESS trusted workflow unavailable", error=str(error), route_evidence=route)
        else:
            action = blocked("safe route evidence is unavailable; no simulation proposal was created", route_evidence=route)
        return {
            "company_brain": company,
            "agents_that_act": action,
            "agent_memory": blocked("no authoritative persistent-memory proof was observed; this chapter remains unfinished"),
        }, "headless_trusted_graph"

    try:
        app.request("GET", "/readyz")
        company = http_company_brain(app)
        route = await trusted_route_evidence()
        action = http_simulation(app, route, wait_seconds) if route.get("status") == "passed" else blocked(
            "safe route evidence is unavailable; no simulation proposal was created", route_evidence=route
        )
    except DemoBlocked as error:
        company = blocked("authenticated HTTP application prerequisite unavailable", error=str(error))
        route = blocked("route was not observed because authenticated application flow is unavailable")
        action = blocked("no simulation proposal was created", error=str(error))
    return {
        "company_brain": company,
        "agents_that_act": action,
        "agent_memory": blocked("no authoritative persistent-memory proof was observed; this chapter remains unfinished"),
    }, "authenticated_http"


async def main_async(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "schema_version": 2,
        "observed_at": utc_now(),
        "requested_mode": args.mode,
        "tracks": {},
    }
    if args.mode == "real":
        report["observation_path"] = "real_prerequisite_gate"
        report["tracks"] = real_report()
    else:
        tracks, path = await run_simulation(args.wait_seconds)
        report["observation_path"] = path
        report["tracks"] = tracks

    statuses = [track.get("status") for track in report["tracks"].values()]
    report["status"] = "complete" if statuses and all(status == "passed" for status in statuses) else "incomplete"
    write_manifest(report)
    if report["observation_path"] == "headless_trusted_graph":
        print("HEADLESS: proxy credentials are unavailable; browser and authenticated-app proof was not observed.")
    print(f"Recorded current observations in {ARTIFACT.relative_to(ROOT)}")
    for name, track in report["tracks"].items():
        print(f"{name}: {track.get('status')}" + (f" — {track.get('reason')}" if track.get("reason") else ""))
    if report["status"] != "complete":
        print("Demo incomplete: no historical artifact or simulated substitute was accepted as proof.", file=sys.stderr)
        return 1
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("simulation", "real"))
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        metavar="SECONDS",
        help="wait only for a human simulation approval in the authenticated app (default: 0)",
    )
    args = parser.parse_args(argv)
    if args.wait_seconds < 0 or args.wait_seconds > 300:
        parser.error("--wait-seconds must be between 0 and 300")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(parse_args(argv if argv is not None else sys.argv[1:])))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
