"""Triage node: inspects graph blast radius, service ownership, and decision trail."""
from __future__ import annotations

import asyncio
from typing import Any

from agents.knowledge import decision_provenance, who_owns
from agents.tools import blast_radius
from agents.workflow import IncidentState


async def triage_node(state: IncidentState) -> dict[str, Any]:
    """Execute triage investigation using graph tools."""
    evidence = dict(state.evidence)
    turn = state.turn_count + 1

    # 1. Always evaluate blast radius
    br_res = await blast_radius(state.service_id, tenant=state.tenant_id)
    if br_res.status == "OK":
        evidence["blast_radius"] = br_res.data
        evidence["witness_paths"] = br_res.evidence.get("witness_paths", [])
    else:
        evidence["blast_radius_error"] = br_res.data

    # 2. For real mode or deep inspection, gather ownership and provenance
    if state.execution_mode == "real":
        owns_res = await who_owns(state.service_id, tenant=state.tenant_id)
        if owns_res.status == "OK":
            evidence["ownership"] = owns_res.data

        dec_res = await decision_provenance(state.service_id, tenant=state.tenant_id)
        if dec_res.status == "OK":
            evidence["provenance"] = dec_res.data

    return {
        "evidence": evidence,
        "turn_count": turn,
        "status": "investigating",
    }
