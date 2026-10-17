"""API request/response contracts, event envelopes, and connection management."""
from __future__ import annotations

import os
import hmac
import asyncio
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field
from fastapi import Header, HTTPException, Request, status

EventType = Literal[
    "run_started",
    "health_observed",
    "tool_started",
    "tool_finished",
    "proposal_ready",
    "approval_required",
    "action_submitted",
    "recovery_verifying",
    "recovery_observed",
    "action_applied",
    "slack_notification",
    "memory_consolidated",
    "answer",
    "error",
    "run_finished",
]


class EventEnvelope(BaseModel):
    run_id: str
    sequence: int
    type: EventType
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    payload: dict[str, Any] = Field(default_factory=dict)


class QuestionRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    session_id: str = "default"
    service_id: str | None = None


class IncidentCreateRequest(BaseModel):
    execution_mode: Literal["simulation"] = "simulation"
    service_id: str = Field(..., min_length=1, max_length=100)
    max_cost: int = Field(default=12, gt=0, le=10000)
    session_id: str = "default"


class ApprovalRequest(BaseModel):
    approval_id: str = Field(..., min_length=1, max_length=100)


class RejectionRequest(BaseModel):
    approval_id: str = Field(..., min_length=1, max_length=100)


def authenticate_tenant(tenant: str | None, proxy_token: str | None) -> str:
    """Trust only identities injected by the authenticated local reverse proxy."""
    expected = os.environ.get("CHRONOSOPS_PROXY_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Proxy authentication is not configured")
    if not proxy_token or not hmac.compare_digest(proxy_token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Authenticated proxy required")
    if tenant not in {"acme", "globex"}:
        raise HTTPException(status_code=401, detail="Unknown authenticated tenant")
    return tenant


def get_tenant_id(
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
    x_proxy_token: str | None = Header(None, alias="X-ChronosOps-Proxy-Token"),
) -> str:
    return authenticate_tenant(x_tenant_id, x_proxy_token)


def validate_origin(request: Request, require_origin: bool = False) -> bool:
    """Validate the exact configured browser origin, including before WS accept."""
    origin = request.headers.get("origin")
    if not origin:
        if require_origin:
            raise HTTPException(status_code=403, detail="Browser Origin required")
        return True
    allowed = {
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    }
    custom_public = os.environ.get("PUBLIC_ORIGIN")
    if custom_public:
        allowed.add(custom_public.rstrip("/"))

    if origin.rstrip("/") not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cross-origin request from '{origin}' not permitted",
        )
    return True


class ConnectionManager:
    """In-memory WebSocket manager broadcasting run events to subscribed clients."""

    def __init__(self) -> None:
        self.active_connections: dict[str, list[Any]] = {}
        self.event_history: dict[str, list[EventEnvelope]] = {}

    async def connect(self, run_id: str, websocket: Any) -> None:
        await websocket.accept()
        self.active_connections.setdefault(run_id, []).append(websocket)
        # Replay past events on reconnect
        for event in self.event_history.get(run_id, []):
            await websocket.send_text(event.model_dump_json())

    def disconnect(self, run_id: str, websocket: Any) -> None:
        if run_id in self.active_connections:
            if websocket in self.active_connections[run_id]:
                self.active_connections[run_id].remove(websocket)

    async def broadcast(self, run_id: str, event: EventEnvelope) -> None:
        self.event_history.setdefault(run_id, []).append(event)
        connections = list(self.active_connections.get(run_id, []))
        for conn in connections:
            try:
                await conn.send_text(event.model_dump_json())
            except Exception:
                self.disconnect(run_id, conn)


manager = ConnectionManager()
