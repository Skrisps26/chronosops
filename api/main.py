"""ChronosOps FastAPI application, REST endpoints, and WebSocket event stream."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Header, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from falkordb import FalkorDB

from agents.actions import (
    ActionConflictError, ActionNotFoundError, ActionUnavailableError,
    approve_action, reject_action, mark_abandoned_executions,
)
from agents.governance import governance_node
from agents.answering import GenerationUnavailableError, synthesize_from_citations
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
from seed.sources import SourceNotFound, SourceUnavailable, get_published_source
from seed.load import get_config

ROOT = Path(__file__).resolve().parents[1]
UI_DIST = ROOT / "ui" / "dist"

@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.recovery_ready = False
    try:
        await asyncio.to_thread(mark_abandoned_executions)
    except (ActionUnavailableError, RedisError):
        pass  # Read surfaces remain available; mutation endpoints fail closed.
    else:
        application.state.recovery_ready = True
    yield


app = FastAPI(title="ChronosOps API", version="0.1.0", lifespan=lifespan)
app.state.recovery_ready = False

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


def _topology_graph(tenant_id: str = "acme", *, write: bool = False) -> Any:
    """Use a tenant reader or validated application writer, never administrator."""
    from agents.settings import require_tenant
    tenant_id = require_tenant(tenant_id)
    config = get_config()
    role = "WRITER" if write else "READER"
    username = config.get(f"FALKORDB_{role}_{tenant_id.upper()}_USERNAME")
    password = config.get(f"FALKORDB_{role}_{tenant_id.upper()}_PASSWORD")
    if not username or not password:
        raise HTTPException(status_code=503, detail="Tenant database credentials unavailable")
    db = FalkorDB(
        host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
        username=username, password=password,
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
    _topology_graph(tenant_id, write=True).query(
        "MERGE (s:Session {tenant_id: $tenant_id, session_id: $session_id}) "
        "ON CREATE SET s.created_at = $created_at "
        "SET s.updated_at = $created_at "
        "CREATE (r:Run {run_id: $run_id, tenant_id: $tenant_id, "
        "               incident_id: $incident_id, execution_mode: 'simulation', "
        "               created_at: $created_at}) "
        "MERGE (i:Incident {incident_id: $incident_id, tenant_id: $tenant_id}) "
        "SET i.service_id = $service_id, i.status = 'investigating', "
        "    i.execution_mode='simulation',i.incident_type='simulated_failover',i.origin='synthetic', "
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
    _topology_graph(tenant_id, write=True).query(
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
    rows = _topology_graph(tenant_id).ro_query(
        "MATCH (r:Run {run_id: $run_id, tenant_id: $tenant_id}) RETURN r.run_id LIMIT 1",
        {"run_id": run_id, "tenant_id": tenant_id},
    ).result_set
    return bool(rows)


@app.get("/api/identity")
async def get_identity(tenant_id: str = Depends(get_tenant_id)) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "execution_mode": "simulation",
        "real_recovery_enabled": os.environ.get("REAL_RECOVERY_ENABLED", "false").lower() == "true",
        "data_origin": "synthetic_demo_topology",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


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
    try:
        res = _topology_graph().ro_query(
            "MATCH (m:Meta {id:'ops_topology_meta'}) RETURN m.ready LIMIT 1",
            timeout=3000,
        ).result_set
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


@app.get("/api/connectors/slack")
async def get_slack_connector_status(
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """Expose observed connector state without granting ownership or source freshness."""
    from integrations.slack import get_connector_status

    try:
        return await asyncio.to_thread(get_connector_status, tenant_id)
    except (SourceUnavailable, RedisError) as error:
        raise HTTPException(status_code=503, detail="Slack connector status unavailable") from error


@app.get("/api/topology")
async def get_topology_for_visualization(
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """Return nodes and edges formatted for Cytoscape.js rendering."""
    graph = _topology_graph(tenant_id)

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


def _question_kind(question: str) -> str | None:
    """Select one supported graph-backed question type without model routing."""
    normalized = question.casefold()
    if "who owns" in normalized or "owner" in normalized:
        return "ownership"
    if "bus factor" in normalized or "contributor" in normalized:
        return "bus_factor"
    if any(term in normalized for term in ("why", "failover", "decision", "rationale", "backup")):
        return "decision"
    if any(term in normalized for term in ("describe", "summary", "summarize", "what is", "tell me about")):
        return "description"
    if any(term in normalized for term in ("prior incident", "previous incident", "past incident", "incident history", "prior outage", "last failure")):
        return "prior_incident"
    return None


def _service_candidates(question: str) -> list[str]:
    """Return bounded, explicit identifiers and names literally present in a question."""
    candidates: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip(" \t\r\n.,;:?!")
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for match in re.finditer(r"[`'\"]([^`'\"]+)[`'\"]", question):
        add(match.group(1))
    for match in re.finditer(r"(?<!\w)[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+(?!\w)", question):
        add(match.group(0))

    words = re.findall(r"[A-Za-z0-9_-]+", question)
    for width in range(min(4, len(words)), 0, -1):
        for start in range(len(words) - width + 1):
            add(" ".join(words[start : start + width]))
            if len(candidates) == 40:
                return candidates
    return candidates


async def _resolve_question_service(req: QuestionRequest, tenant_id: str) -> str:
    """Require a supplied service ID or one exact canonical ID/alias in the question."""
    if req.service_id is not None:
        requested = req.service_id.strip()
        if not requested:
            raise HTTPException(status_code=422, detail={"code": "invalid_service_id"})
        return requested

    candidates = _service_candidates(req.question)
    resolved = {
        service_id
        for candidate in candidates
        if (service_id := await resolve_service_id(candidate, tenant=tenant_id)) is not None
    }
    if len(resolved) == 1:
        return next(iter(resolved))
    if len(resolved) > 1:
        raise HTTPException(
            status_code=422,
            detail={"code": "ambiguous_service", "message": "Question identifies multiple services"},
        )

    non_service_words = {
        "a", "about", "and", "backup", "bus", "contributor", "decision", "describe",
        "factor", "for", "is", "me", "of", "owner", "owns", "rationale", "service",
        "summary", "tell", "the", "this", "what", "who", "why",
    }
    for candidate in reversed(candidates):
        if " " not in candidate and candidate.casefold() not in non_service_words:
            # The read tool distinguishes an unknown identifier from an unready graph.
            return candidate
    raise HTTPException(
        status_code=422,
        detail={
            "code": "service_id_required",
            "message": "Provide service_id or an exact canonical service ID or alias in the question",
        },
    )


def _citations(result: Any) -> list[dict[str, Any]]:
    """Return only the bounded citations emitted by the graph tool."""
    citations = result.evidence.get("citations", [])
    return citations if isinstance(citations, list) else []


def _raise_tool_failure(result: Any) -> None:
    """Map graph tool abstentions to explicit API failures without hiding evidence."""
    citations = _citations(result)
    payload = {
        "code": {
            "NOT_FOUND": "service_not_found",
            "NOT_READY": "company_brain_unready",
            "ERROR": "tool_error",
            "QUERY_TIMEOUT": "tool_error",
        }.get(result.status, "evidence_insufficient"),
        "message": str(result.data),
        "tool_result": result.model_dump(mode="json"),
        "citations": citations,
    }
    if result.status == "NOT_FOUND":
        raise HTTPException(status_code=404, detail=payload)
    if result.status in {"NOT_READY", "ERROR", "QUERY_TIMEOUT"}:
        raise HTTPException(status_code=503, detail=payload)
    raise HTTPException(status_code=422, detail=payload)


def _ownership_answer(service_id: str, data: dict[str, Any]) -> str:
    """Render only the canonical ownership facts returned by the graph tool."""
    members = [member.get("name") for member in data.get("team_members", []) if member.get("name")]
    answer = f"Service `{service_id}` is owned by Team {data['owner_team']}."
    if members:
        answer += f" Recorded team members: {', '.join(members)}."
    return answer


def _bus_factor_answer(service_id: str, data: dict[str, Any]) -> str:
    """Describe the proxy without treating missing attribution as healthy distribution."""
    proxy = data["bus_factor_proxy"]
    if data.get("knowledge_concentration") == "unknown":
        return (
            f"Published evidence has no recorded contributors for `{service_id}`; "
            "knowledge concentration is unknown."
        )
    lead = data.get("primary_maintainer")
    concentration = data.get("knowledge_concentration")
    answer = (
        f"Recorded contributor count for `{service_id}` is {proxy}; "
        f"the knowledge-concentration proxy is {concentration}."
    )
    if lead:
        answer += f" Primary recorded maintainer: {lead}."
    return answer


@app.post("/api/questions")
async def ask_question(
    req: QuestionRequest,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """Answer supported Company Brain questions with graph evidence and bounded citations."""
    validate_origin(request)
    kind = _question_kind(req.question)
    if kind is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "question_unsupported",
                "message": "The question cannot be answered from the supported Company Brain evidence",
            },
        )
    service_id = await _resolve_question_service(req, tenant_id)

    if kind == "ownership":
        result = await who_owns(service_id, tenant=tenant_id)
        if result.status != "OK":
            _raise_tool_failure(result)
        citations = _citations(result)
        canonical_id = result.data["service_id"]
        return {
            "answer": _ownership_answer(canonical_id, result.data),
            "tool_result": result.model_dump(mode="json"),
            "citations": citations,
        }

    if kind == "bus_factor":
        result = await bus_factor(service_id, tenant=tenant_id)
        if result.status != "OK":
            _raise_tool_failure(result)
        citations = _citations(result)
        canonical_id = result.data["service_id"]
        return {
            "answer": _bus_factor_answer(canonical_id, result.data),
            "tool_result": result.model_dump(mode="json"),
            "citations": citations,
        }
    if kind == "prior_incident":
        from agents.memory import retrieve_tenant_memory
        memory = await retrieve_tenant_memory(service_id=service_id, tenant_id=tenant_id)
        episodes = memory.get("episodes", [])
        if not episodes:
            answer = f"No prior incidents recorded for service `{service_id}`."
        else:
            latest = episodes[0]
            answer = f"Service `{service_id}` has {len(episodes)} recorded incident(s). Latest incident `{latest['incident_id']}` ({latest['execution_mode']}) executed `{latest['action_kind']}` with status `{latest['status']}`."
            facts = memory.get("active_facts", [])
            if facts:
                answer += f" Active exclusions: {len(facts)} route fact(s) recorded."
        return {
            "answer": answer,
            "tool_result": {
                "status": "OK",
                "data": memory,
                "evidence": {"citations": []},
                "trace": {
                    "tool": "retrieve_tenant_memory",
                    "graph": f"agent_memory_{tenant_id}",
                    "cypher": "MATCH (s:ServiceRef)<-[:AFFECTED_SERVICE]-(ep:IncidentEpisode)",
                    "params": {"service": service_id},
                },
            },
            "citations": [],
        }

    result = await decision_provenance(service_id, tenant=tenant_id)
    if result.status != "OK":
        _raise_tool_failure(result)
    citations = _citations(result)
    canonical_id = result.data["service_id"]
    try:
        answer = await synthesize_from_citations(
            question=req.question,
            service_id=canonical_id,
            citations=citations,
        )
    except GenerationUnavailableError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "generation_unavailable",
                "message": "Bedrock generation is unavailable; retrieved graph evidence is attached",
                "answer": None,
                "tool_result": result.model_dump(mode="json"),
                "citations": citations,
                "sources": result.data.get("sources", []),
            },
        ) from None
    return {
        "answer": answer,
        "tool_result": result.model_dump(mode="json"),
        "citations": citations,
        "sources": result.data.get("sources", []),
    }


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

    persisted = await get_incident(incident_id, tenant_id=tenant_id)
    return {
        "incident_id": incident_id,
        "run_id": run_id,
        "status": state.status,
        "approval_id": state.approval_id,
        "proposed_action": state.proposed_action,
        "blast_radius": state.evidence.get("blast_radius"),
        "expires_at": persisted.get("expires_at"),
        "tool_results": [
            state.evidence[key]
            for key in ("blast_radius_tool", "routes")
            if isinstance(state.evidence.get(key), dict)
        ],
    }


@app.get("/api/incidents")
async def list_incidents(
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """List recent incidents for the authenticated tenant."""
    def read() -> dict[str, Any]:
        graph = _topology_graph(tenant_id)
        rows = graph.ro_query(
            "MATCH (i:Incident {tenant_id:$tenant}) "
            "RETURN i.incident_id,i.service_id,i.status,i.starts_at "
            "ORDER BY i.starts_at DESC LIMIT 20", {"tenant": tenant_id}, timeout=3000,
        ).result_set
        approval_rows = graph.ro_query(
            "MATCH (i:Incident {tenant_id:$tenant}), (a:ApprovalRecord {tenant_id:$tenant}) "
            "WHERE i.approval_id=a.approval_id AND i.incident_id=a.incident_id "
            "RETURN properties(a) ORDER BY a.created_at DESC LIMIT 20",
            {"tenant": tenant_id}, timeout=3000,
        ).result_set
        approvals = []
        for row in approval_rows:
            record = row[0]
            approvals.append({
                **{key: record.get(key) for key in (
                    "approval_id", "incident_id", "service_id", "status", "expires_at",
                    "command_id", "ssm_status", "recovery_sample_ids",
                    "memory_pending", "notification_pending",
                )},
                "action": record.get("action_kind"),
                "proposed": json.loads(record.get("proposed_action") or "{}"),
            })
        return {
            "incidents": [{"incident_id": r[0], "service_id": r[1], "status": r[2], "starts_at": r[3]} for r in rows],
            "approvals": approvals,
        }
    return await asyncio.to_thread(read)


@app.get("/api/incidents/{incident_id}")
async def get_incident(
    incident_id: str,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        rows = _topology_graph(tenant_id).ro_query(
            "MATCH (i:Incident {incident_id:$id,tenant_id:$tenant}) "
            "OPTIONAL MATCH (a:ApprovalRecord {approval_id:i.approval_id,tenant_id:$tenant}) "
            "RETURN properties(i),properties(a) LIMIT 2",
            {"id": incident_id, "tenant": tenant_id}, timeout=3000,
        ).result_set
        if not rows:
            raise HTTPException(status_code=404, detail="Incident not found")
        if len(rows) != 1:
            raise HTTPException(status_code=503, detail="Incident identity is ambiguous")
        incident, approval = rows[0][0], rows[0][1] or {}
        return {
            "incident_id": incident_id, "approval_id": approval.get("approval_id"),
            "service_id": incident["service_id"], "execution_mode": incident.get("execution_mode"),
            "action_kind": approval.get("action_kind"),
            "status": approval.get("status") or incident.get("status"),
            "proposed_action": json.loads(approval.get("proposed_action") or "{}"),
            "expires_at": approval.get("expires_at"),
            **{key: approval.get(key) for key in (
                "command_id", "ssm_status", "command_completed_at", "error_code",
                "recovery_sample_ids", "recovered_at", "applied_at",
                "memory_pending", "notification_pending",
            )},
        }
    return await asyncio.to_thread(read)

@app.get("/api/memory/episodes/{episode_id}")
async def get_memory_episode(
    episode_id: str,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        from agents.memory import get_tenant_db, get_memory_graph_name
        db = get_tenant_db(tenant_id, admin=False)
        graph = db.select_graph(get_memory_graph_name(tenant_id))
        rows = graph.ro_query(
            "MATCH (ep:IncidentEpisode {id: $id, tenant_id: $tenant}) "
            "OPTIONAL MATCH (ep)-[:RECORDED_DECISION]->(d:DecisionLog) "
            "RETURN properties(ep), properties(d) LIMIT 2",
            {"id": episode_id, "tenant": tenant_id},
            timeout=3000,
        ).result_set
        if not rows:
            raise HTTPException(status_code=404, detail="Episode not found")
        if len(rows) != 1:
            raise HTTPException(status_code=503, detail="Episode identity is ambiguous")
        ep_props = dict(rows[0][0])
        decision_props = dict(rows[0][1]) if rows[0][1] else {}
        return {
            "episode_id": ep_props.get("id"),
            "incident_id": ep_props.get("incident_id"),
            "tenant_id": ep_props.get("tenant_id"),
            "execution_mode": ep_props.get("execution_mode"),
            "root_service": ep_props.get("root_service"),
            "action_kind": ep_props.get("action_kind"),
            "status": ep_props.get("status"),
            "eligible": ep_props.get("eligible"),
            "command_id": ep_props.get("command_id"),
            "topology_version": ep_props.get("topology_version"),
            "completed_at": ep_props.get("completed_at") or ep_props.get("timestamp"),
            "decision": decision_props,
        }
    return await asyncio.to_thread(read)


@app.post("/api/incidents/{incident_id}/approve")
async def approve(
    incident_id: str,
    req: ApprovalRequest,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    validate_origin(request)
    if not request.app.state.recovery_ready:
        raise HTTPException(status_code=503, detail="Recovery state is unavailable; no action was submitted")
    try:
        result = await approve_action(
            incident_id=incident_id,
            approval_id=req.approval_id,
            tenant_id=tenant_id,
        )
        try:
            from agents.memory import consolidate_incident
            await consolidate_incident(incident_id, tenant_id=tenant_id)
        except Exception:
            pass
        return result
    except ActionNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ActionConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ActionUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except RedisError:
        raise HTTPException(status_code=503, detail="Action state is unavailable; refresh before another decision")


@app.post("/api/incidents/{incident_id}/reject")
async def reject(
    incident_id: str,
    req: RejectionRequest,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    validate_origin(request)
    if not request.app.state.recovery_ready:
        raise HTTPException(status_code=503, detail="Recovery state is unavailable")
    try:
        return await reject_action(incident_id=incident_id, approval_id=req.approval_id, tenant_id=tenant_id)
    except ActionNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ActionConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ActionUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/sources/{document_id}")
async def get_source_document(
    document_id: str,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """Return only the current published SDK source revision, never a local file."""
    try:
        source = await asyncio.to_thread(get_published_source, document_id, tenant_id)
    except SourceNotFound as error:
        raise HTTPException(status_code=404, detail="Source not found") from error
    except (SourceUnavailable, RedisError) as error:
        raise HTTPException(status_code=503, detail="Current source evidence unavailable") from error
    return {**source, "content": "\n\n".join(chunk["text"] for chunk in source["chunks"])}


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
