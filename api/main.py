"""ChronosOps FastAPI application, REST endpoints, and WebSocket event stream."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Header, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from falkordb import FalkorDB

from agents.actions import ActionConflictError, ActionNotFoundError, approve_action, reject_action
from agents.governance import governance_node
from agents.knowledge import bus_factor, decision_provenance, resolve_service_id, who_owns
from agents.remediation import remediation_node
from agents.triage import triage_node
from agents.workflow import IncidentState, create_incident_workflow
from agents.settings import graph_names
from api.contracts import (
    ApprovalRequest,
    EventEnvelope,
    IncidentCreateRequest,
    QuestionRequest,
    RejectionRequest,
    authenticate_tenant,
    get_tenant_id,
    manager,
    validate_origin,
)
from integrations.monitoring import MonitoringClient
from integrations.alerts import AlertEvidenceUnavailable, ingest_alerts
from integrations.deployment import bound_deployment
from integrations.health_store import HealthEvidenceConflict, persist_health_samples
from redis.exceptions import RedisError
from seed.generate import DOCS_DIR
from seed.load import get_config

ROOT = Path(__file__).resolve().parents[1]
UI_DIST = ROOT / "ui" / "dist"

app = FastAPI(title="ChronosOps API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080", "http://127.0.0.1:8080",
        "http://localhost:5173", "http://localhost:8000", "http://127.0.0.1:8000",
        *([os.environ["PUBLIC_ORIGIN"].rstrip("/")] if os.environ.get("PUBLIC_ORIGIN") else []),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

workflow_engine = create_incident_workflow(triage_node, remediation_node, governance_node)


class AlertNotification(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    startsAt: str = Field(min_length=1, max_length=64)
    fingerprint: str = Field(min_length=1, max_length=128)


class AlertPayload(BaseModel):
    version: str = "4"
    status: str = "firing"
    receiver: str = ""
    alerts: list[AlertNotification] = Field(min_length=1, max_length=20)


def verify_internal_token(authorization: str | None = Header(None)) -> bool:
    expected_token = os.environ.get("ALERT_WEBHOOK_TOKEN")
    if not expected_token:
        raise HTTPException(status_code=503, detail="Internal alert authentication is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid bearer token")
    token = authorization.split("Bearer ", 1)[1].strip()
    if not hmac.compare_digest(token.encode(), expected_token.encode()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return True


def _topology_graph() -> Any:
    """Return the server-owned topology graph used for tenant ownership checks."""
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    return db.select_graph(graph_names().topology)


def _create_simulation_ownership(
    *,
    tenant_id: str,
    session_id: str,
    run_id: str,
    incident_id: str,
    service_id: str,
) -> None:
    """Persist server-generated ownership before emitting a run event."""
    created_at = datetime.now(timezone.utc).isoformat()
    _topology_graph().query(
        "MERGE (s:Session {tenant_id: $tenant_id, session_id: $session_id}) "
        "ON CREATE SET s.created_at = $created_at "
        "SET s.updated_at = $created_at "
        "CREATE (r:Run {run_id: $run_id, tenant_id: $tenant_id, "
        "               incident_id: $incident_id, execution_mode: 'simulation', "
        "               created_at: $created_at}) "
        "MERGE (i:Incident {incident_id: $incident_id, tenant_id: $tenant_id}) "
        "SET i.service_id = $service_id, i.status = 'investigating', "
        "    i.starts_at = $created_at "
        "MERGE (s)-[:HAS_RUN]->(r) "
        "MERGE (r)-[:FOR_INCIDENT]->(i)",
        {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "run_id": run_id,
            "incident_id": incident_id,
            "service_id": service_id,
            "created_at": created_at,
        },
    )


def _update_simulation_incident(
    *, incident_id: str, tenant_id: str, incident_status: str, approval_id: str | None
) -> None:
    _topology_graph().query(
        "MATCH (i:Incident {incident_id: $incident_id, tenant_id: $tenant_id}) "
        "SET i.status = $incident_status, i.approval_id = $approval_id, "
        "    i.updated_at = $updated_at",
        {
            "incident_id": incident_id,
            "tenant_id": tenant_id,
            "incident_status": incident_status,
            "approval_id": approval_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _run_belongs_to_tenant(run_id: str, tenant_id: str) -> bool:
    rows = _topology_graph().ro_query(
        "MATCH (r:Run {run_id: $run_id, tenant_id: $tenant_id}) RETURN r.run_id LIMIT 1",
        {"run_id": run_id, "tenant_id": tenant_id},
    ).result_set
    return bool(rows)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    if not os.environ.get("CHRONOSOPS_PROXY_TOKEN"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proxy authentication is not configured",
        )
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    try:
        res = db.select_graph(graph_names().topology).ro_query("MATCH (m:Meta {id: 'ops_topology_meta'}) RETURN m.ready").result_set
        ready = bool(res and res[0][0] is True)
        return {"status": "ready" if ready else "not_ready", "ready": ready}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unready: {e}")


@app.get("/api/monitoring")
async def get_monitoring_status(
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    if tenant_id != "acme":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Monitoring status not found")
    try:
        binding = await asyncio.to_thread(bound_deployment, get_config())
    except (ValueError, RedisError):
        return {"monitoring_healthy": False, "status": "monitoring_unknown",
                "reason": "No active verified workload binding", "target": None, "samples": []}
    client = MonitoringClient(deployment_id=binding["deployment_id"])
    health = await asyncio.to_thread(client.check_health, binding["endpoint"])
    if health.samples:
        try:
            await asyncio.to_thread(persist_health_samples, binding, health.samples)
        except (HealthEvidenceConflict, RedisError):
            return {"monitoring_healthy": False, "status": "monitoring_unknown",
                    "reason": "Observation changed or deployment is inactive", "target": binding["endpoint"],
                    "samples": []}
    return {**health.model_dump(mode="json"), "monitoring_healthy": health.healthy,
            "target": binding["endpoint"],
            "observed_at": max((sample.observed_at for sample in health.samples), default=None),
            "checked_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/topology")
async def get_topology_for_visualization(
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """Return nodes and edges formatted for Cytoscape.js rendering."""
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    graph = db.select_graph(graph_names().topology)

    # Fetch nodes
    node_rows = graph.ro_query(
        "MATCH (s:Service) RETURN s.service_id, s.name, s.owner_team, s.health, s.tier, s.origin"
    ).result_set

    # Fetch dependency edges
    dep_rows = graph.ro_query(
        "MATCH (src:Service)-[r:DEPENDS_ON]->(dst:Service) RETURN src.service_id, dst.service_id, r.origin"
    ).result_set

    # Fetch route edges
    route_rows = graph.ro_query(
        "MATCH (src:Service)-[r:ROUTES_TO]->(dst:Service) RETURN src.service_id, dst.service_id, r.latency_ms, r.cost, r.health"
    ).result_set

    elements = []
    for r in node_rows:
        elements.append({
            "data": {
                "id": r[0],
                "label": r[1],
                "owner_team": r[2],
                "health": r[3],
                "tier": r[4],
                "origin": r[5] or "synthetic",
            }
        })

    for r in dep_rows:
        elements.append({
            "data": {
                "id": f"dep-{r[0]}-{r[1]}",
                "source": r[0],
                "target": r[1],
                "type": "DEPENDS_ON",
                "origin": r[2] or "synthetic",
            }
        })

    for r in route_rows:
        elements.append({
            "data": {
                "id": f"route-{r[0]}-{r[1]}",
                "source": r[0],
                "target": r[1],
                "type": "ROUTES_TO",
                "latency_ms": r[2],
                "cost": r[3],
                "health": r[4],
            }
        })

    return {"elements": elements, "node_count": len(node_rows), "edge_count": len(dep_rows) + len(route_rows)}


@app.post("/api/questions")
async def ask_question(
    req: QuestionRequest,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """Question answering route over Company Brain with citations and router."""
    validate_origin(request)
    q = req.question.strip().lower()

    # Route question
    resolved_id = await resolve_service_id(req.service_id or "payments-primary", tenant=tenant_id)
    target_service = resolved_id or "payments-primary"

    if "who owns" in q or "owner" in q:
        res = await who_owns(target_service, tenant=tenant_id)
        team = res.data.get("owner_team")
        members = [m.get("name") for m in res.data.get("team_members", [])]
        answer = f"Service `{target_service}` is owned by Team {team}. Key team members: {', '.join(members)}."
        citations = res.evidence.get("citations", [])
        return {"answer": answer, "tool_result": res.model_dump(), "citations": citations}

    elif "bus factor" in q or "contributor" in q:
        res = await bus_factor(target_service, tenant=tenant_id)
        proxy = res.data.get("bus_factor_proxy")
        lead = res.data.get("primary_maintainer")
        warning = "WARNING: Single maintainer risk detected!" if res.data.get("bus_factor_warning") else "Healthy distributed authorship."
        answer = f"Knowledge concentration proxy for `{target_service}` is {proxy} (Maintainer: {lead}). {warning}"
        return {"answer": answer, "tool_result": res.model_dump()}

    elif "why" in q or "failover" in q or "decision" in q or "backup" in q:
        res = await decision_provenance(target_service, tenant=tenant_id)
        sources = res.data.get("sources", [])
        answer = f"Found {len(sources)} documented decisions and sources regarding `{target_service}`."
        return {"answer": answer, "tool_result": res.model_dump(), "sources": sources}

    else:
        # Default knowledge summary
        res = await who_owns(target_service, tenant=tenant_id)
        return {"answer": f"Service summary for `{target_service}`.", "tool_result": res.model_dump()}


@app.post("/api/incidents")
async def create_incident(
    req: IncidentCreateRequest,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """Trigger an incident workflow run (simulation mode)."""
    validate_origin(request)
    incident_id = f"inc-sim-{uuid.uuid4().hex[:8]}"
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    _create_simulation_ownership(
        tenant_id=tenant_id,
        session_id=req.session_id,
        run_id=run_id,
        incident_id=incident_id,
        service_id=req.service_id,
    )


    initial_state = IncidentState(
        incident_id=incident_id,
        tenant_id=tenant_id,
        session_id=req.session_id,
        service_id=req.service_id,
        execution_mode="simulation",
        max_cost=req.max_cost,
    )

    # Broadcast run_started event
    await manager.broadcast(
        run_id,
        EventEnvelope(
            run_id=run_id,
            sequence=1,
            type="run_started",
            payload={"incident_id": incident_id, "service_id": req.service_id, "mode": "simulation"},
        ),
    )

    # Execute LangGraph workflow
    final_state_dict = await workflow_engine.ainvoke(initial_state)
    state = IncidentState(**final_state_dict)
    _update_simulation_incident(
        incident_id=incident_id,
        tenant_id=tenant_id,
        incident_status=state.status,
        approval_id=state.approval_id,
    )


    # Broadcast proposal_ready event
    await manager.broadcast(
        run_id,
        EventEnvelope(
            run_id=run_id,
            sequence=2,
            type="proposal_ready",
            payload={
                "approval_id": state.approval_id,
                "proposed_action": state.proposed_action,
                "status": state.status,
            },
        ),
    )

    return {
        "incident_id": incident_id,
        "run_id": run_id,
        "status": state.status,
        "approval_id": state.approval_id,
        "proposed_action": state.proposed_action,
        "blast_radius": state.evidence.get("blast_radius"),
    }


@app.get("/api/incidents")
async def list_incidents(
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """List recent incidents for the authenticated tenant."""
    graph = _topology_graph()
    rows = graph.ro_query(
        "MATCH (i:Incident {tenant_id: $tenant}) "
        "RETURN i.incident_id, i.service_id, i.status, i.starts_at "
        "ORDER BY i.starts_at DESC LIMIT 20",
        {"tenant": tenant_id},
    ).result_set

    appr_rows = graph.ro_query(
        "MATCH (a:ApprovalRecord {tenant_id: $tenant}) "
        "RETURN a.approval_id, a.incident_id, a.service_id, a.action_kind, a.status, a.expires_at "
        "ORDER BY a.created_at DESC LIMIT 20",
        {"tenant": tenant_id},
    ).result_set

    incidents = [{"incident_id": r[0], "service_id": r[1], "status": r[2], "starts_at": r[3]} for r in rows]
    approvals = [{"approval_id": r[0], "incident_id": r[1], "service_id": r[2], "action": r[3], "status": r[4]} for r in appr_rows]

    return {"incidents": incidents, "approvals": approvals}


@app.get("/api/incidents/{incident_id}")
async def get_incident(
    incident_id: str,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    graph = _topology_graph()
    rows = graph.ro_query(
        "MATCH (i:Incident {incident_id: $id, tenant_id: $tenant}) "
        "OPTIONAL MATCH (a:ApprovalRecord {incident_id: $id, tenant_id: $tenant}) "
        "RETURN i.service_id, i.status, a.approval_id, a.action_kind, "
        "       a.status, a.proposed_action, a.expires_at "
        "LIMIT 1",
        {"id": incident_id, "tenant": tenant_id},
    ).result_set

    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Incident '{incident_id}' not found")

    r = rows[0]
    return {
        "incident_id": incident_id,
        "approval_id": r[2],
        "service_id": r[0],
        "action_kind": r[3],
        "status": r[4] or r[1],
        "proposed_action": json.loads(r[5]) if r[5] else {},
        "expires_at": r[6],
    }


@app.post("/api/incidents/{incident_id}/approve")
async def approve(
    incident_id: str,
    req: ApprovalRequest,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    validate_origin(request)
    try:
        result = await approve_action(
            incident_id=incident_id,
            approval_id=req.approval_id,
            tenant_id=tenant_id,
        )
        return result
    except ActionNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ActionConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.post("/api/incidents/{incident_id}/reject")
async def reject(
    incident_id: str,
    req: RejectionRequest,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    validate_origin(request)
    try:
        return await reject_action(incident_id=incident_id, approval_id=req.approval_id, tenant_id=tenant_id)
    except ActionNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ActionConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@app.get("/api/sources/{document_id}")
async def get_source_document(
    document_id: str,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """Retrieve full text and metadata of a source document."""
    # Look in DOCS_DIR
    target = DOCS_DIR / f"{document_id}.md"
    if not target.is_file():
        # Try raw document_id
        target = DOCS_DIR / document_id
        if not target.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document '{document_id}' not found")

    text = target.read_text(encoding="utf-8")
    return {
        "document_id": document_id,
        "filename": target.name,
        "content": text,
    }


@app.websocket("/api/runs/{run_id}/events")
async def websocket_events(websocket: WebSocket, run_id: str) -> None:
    try:
        tenant_id = authenticate_tenant(
            websocket.headers.get("X-Tenant-ID"),
            websocket.headers.get("X-ChronosOps-Proxy-Token"),
        )
        validate_origin(websocket, require_origin=True)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if not _run_belongs_to_tenant(run_id, tenant_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(run_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(run_id, websocket)


@app.post("/internal/alerts")
async def receive_alert(
    payload: AlertPayload,
    authorized: bool = Depends(verify_internal_token),
) -> dict[str, Any]:
    """Accept only allowlisted notifications corroborated by fresh probe evidence."""
    if payload.version != "4":
        raise HTTPException(status_code=422, detail="Unsupported Alertmanager payload version")
    try:
        return await ingest_alerts([alert.model_dump() for alert in payload.alerts])
    except (AlertEvidenceUnavailable, HealthEvidenceConflict, RedisError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


# Mount built UI when dist directory exists
if UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(UI_DIST), html=True), name="static")
