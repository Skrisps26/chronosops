"""API request/response contracts, event envelopes, and connection management."""
from __future__ import annotations

import os
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


def get_tenant_id(
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
    x_consumer_custom_id: str | None = Header(None, alias="X-Consumer-Custom-ID"),
) -> str:
    """Extract authenticated tenant identity, defaulting to acme."""
    tenant = x_tenant_id or x_consumer_custom_id or "acme"
    return tenant.strip().lower()


def validate_origin(request: Request) -> bool:
    """Validate Origin header against allowed public origins."""
    origin = request.headers.get("origin")
    if not origin:
        return True  # Non-browser or direct requests allowed
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

    if origin.rstrip("/") not in allowed and not origin.endswith(".trycloudflare.com"):
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
