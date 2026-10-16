"""Prometheus blackbox monitoring client and health check verification."""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any


class MonitoringClient:
    def __init__(self, prometheus_url: str = "http://localhost:9090") -> None:
        self.prometheus_url = prometheus_url.rstrip("/")

    def query_prometheus(self, query: str) -> list[dict[str, Any]]:
        url = f"{self.prometheus_url}/api/v1/query?query={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("status") == "success":
                    return data.get("data", {}).get("result", [])
        except Exception:
            return []
        return []

    def check_health(self, target_url: str, max_age_seconds: float = 15.0) -> dict[str, Any]:
        """Query probe_success for target_url; returns health assessment and sample metadata."""
        query = f'probe_success{{instance="{target_url}"}}'
        results = self.query_prometheus(query)

        if not results:
            return {
                "status": "monitoring_unknown",
                "healthy": False,
                "reason": "No probe metrics found for target",
                "samples": [],
            }

        sample = results[0]
        ts, val = sample.get("value", [0, "0"])
        sample_age = time.time() - float(ts)

        if sample_age > max_age_seconds:
            return {
                "status": "monitoring_unknown",
                "healthy": False,
                "reason": f"Probe data is stale ({sample_age:.1f}s old > {max_age_seconds}s limit)",
                "sample_age_seconds": sample_age,
            }

        is_healthy = val == "1"
        return {
            "status": "healthy" if is_healthy else "unhealthy",
            "healthy": is_healthy,
            "probe_value": int(val),
            "sample_timestamp": float(ts),
            "sample_age_seconds": sample_age,
            "target": target_url,
        }

    def verify_recovery_samples(
        self,
        target_url: str,
        required_samples: int = 3,
        min_spacing_seconds: float = 4.0,
        timeout_seconds: float = 60.0,
    ) -> bool:
        """Wait for required consecutive healthy samples spaced appropriately."""
        deadline = time.time() + timeout_seconds
        healthy_count = 0
        last_ts = 0.0

        while time.time() < deadline:
            res = self.check_health(target_url)
            if res.get("healthy"):
                sample_ts = res.get("sample_timestamp", 0.0)
                if sample_ts > last_ts + min_spacing_seconds:
                    healthy_count += 1
                    last_ts = sample_ts
                    if healthy_count >= required_samples:
                        return True
            else:
                healthy_count = 0

            time.sleep(min_spacing_seconds)

        return False
