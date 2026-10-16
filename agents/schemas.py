"""Pydantic schemas and typed results for graph operational tools."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field

ToolStatus = Literal[
    "OK",
    "NOT_FOUND",
    "EVIDENCE_INSUFFICIENT",
    "NO_SAFE_ROUTE",
    "QUERY_TIMEOUT",
    "NOT_READY",
    "ERROR",
]


class ToolTrace(BaseModel):
    tool: str
    graph: str
    cypher: str
    params: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0.0
    algorithm_result: Any | None = None


class ToolBounds(BaseModel):
    max_depth: int = 5
    limit: int = 100
    truncated: bool = False
    depth_frontier_hit: bool = False


class ToolResult(BaseModel):
    status: ToolStatus
    data: Any = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    trace: ToolTrace
    bounds: ToolBounds = Field(default_factory=ToolBounds)


# Input validation models
class BlastRadiusInput(BaseModel):
    service_id: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_\-]+$")


class FailoverRoutesInput(BaseModel):
    source: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_\-]+$")
    target: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_\-]+$")
    max_cost: int = Field(..., gt=0, le=10000)


class ServiceQueryInput(BaseModel):
    service_id: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_\-]+$")
