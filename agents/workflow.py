"""LangGraph state graph for incident triage, remediation planning, and governance."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END

ExecutionMode = Literal["real", "simulation"]
IncidentStatus = Literal[
    "investigating",
    "awaiting_approval",
    "executing",
    "verifying",
    "recovered",
    "failed",
    "execution_unknown",
    "recovered_externally",
    "applied",
    "rejected",
]


class IncidentState(BaseModel):
    incident_id: str
    tenant_id: str = "acme"
    session_id: str = "default"
    service_id: str
    execution_mode: ExecutionMode = "simulation"
    status: IncidentStatus = "investigating"
    topology_version: str = "v1.0"
    max_cost: int = 12

    # Evidence & Agent outputs
    evidence: dict[str, Any] = Field(default_factory=dict)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    rejected_routes: list[dict[str, Any]] = Field(default_factory=list)
    proposed_action: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = None
    command_id: str | None = None
    recovery_samples: list[dict[str, Any]] = Field(default_factory=list)
    turn_count: int = 0
    error_message: str | None = None


# Node implementations will be wired in triage, remediation, governance
def create_incident_workflow(
    triage_fn,
    remediation_fn,
    governance_fn,
) -> StateGraph:
    """Construct the linear lifecycle graph: START -> triage -> remediation -> governance -> END."""
    workflow = StateGraph(IncidentState)

    workflow.add_node("triage", triage_fn)
    workflow.add_node("remediation", remediation_fn)
    workflow.add_node("governance", governance_fn)

    workflow.add_edge(START, "triage")
    workflow.add_edge("triage", "remediation")
    workflow.add_edge("remediation", "governance")
    workflow.add_edge("governance", END)

    return workflow.compile()
