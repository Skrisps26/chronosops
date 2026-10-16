"""ChronosOps FastAPI application and internal alert receiver."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Header, Request, status
from pydantic import BaseModel
from falkordb import FalkorDB
from dotenv import dotenv_values

from seed.load import get_config

app = FastAPI(title="ChronosOps API", version="0.1.0")


class AlertPayload(BaseModel):
    version: str = "4"
    status: str = "firing"
    receiver: str = ""
    alerts: list[dict[str, Any]] = []


def verify_internal_token(authorization: str | None = Header(None)) -> bool:
    expected_token = os.environ.get("ALERT_WEBHOOK_TOKEN", "demo-internal-alert-secret")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid bearer token")
    token = authorization.split("Bearer ", 1)[1].strip()
    if token != expected_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return True


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    try:
        res = db.select_graph("ops_topology").ro_query("MATCH (m:Meta {id: 'ops_topology_meta'}) RETURN m.ready").result_set
        ready = bool(res and res[0][0] is True)
        return {"status": "ready" if ready else "not_ready", "ready": ready}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unready: {e}")


@app.post("/internal/alerts")
async def receive_alert(
    payload: AlertPayload,
    authorized: bool = Depends(verify_internal_token),
) -> dict[str, Any]:
    """Internal webhook receiver for Alertmanager notifications."""
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    graph = db.select_graph("ops_topology")
    now_ts = datetime.now(timezone.utc).isoformat()

    recorded = []
    for alert in payload.alerts:
        alertname = alert.get("labels", {}).get("alertname", "UnknownAlert")
        service = alert.get("labels", {}).get("service", "unknown")
        status_val = alert.get("status", "firing")
        starts_at = alert.get("startsAt", now_ts)
        fingerprint = alert.get("fingerprint", f"{alertname}-{service}")

        # Idempotently record incident in ops_topology
        incident_id = f"inc-{fingerprint}"
        graph.query(
            "MERGE (i:Incident {incident_id: $incident_id}) "
            "SET i.alertname = $alertname, "
            "    i.service_id = $service, "
            "    i.status = $status, "
            "    i.starts_at = $starts_at, "
            "    i.recorded_at = $now_ts",
            {
                "incident_id": incident_id,
                "alertname": alertname,
                "service": service,
                "status": status_val,
                "starts_at": starts_at,
                "now_ts": now_ts,
            },
        )
        recorded.append({"incident_id": incident_id, "service": service, "status": status_val})

    return {"status": "recorded", "incidents": recorded}
