"""Prometheus blackbox monitoring evidence for the verified workload."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field


WORKLOAD_JOB = "chronosops_workload"
PROBE_MODULE = "http_2xx"
MAX_RAW_SAMPLES = 32
DEFAULT_MAX_AGE_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 5.0


class HealthSample(BaseModel):
    """One synchronized, raw Prometheus scrape of the controlled workload."""

    id: str
    deployment_id: str
    observed_at: float
    probe_success: bool
    http_status: int
    duration_seconds: float
    exporter_up: bool
    endpoint: str


class HealthAssessment(BaseModel):
    """Typed monitoring evidence; unknown monitoring is never healthy."""

    status: Literal["healthy", "unhealthy", "monitoring_unknown"]
    healthy: bool
    reason: str
    samples: list[HealthSample] = Field(default_factory=list)

    @property
    def sample_ids(self) -> list[str]:
        return [sample.id for sample in self.samples]

    @property
    def sample_timestamp(self) -> float | None:
        return self.samples[-1].observed_at if self.samples else None

    @property
    def sample_age_seconds(self) -> float | None:
        if not self.samples:
            return None
        return max(0.0, time.time() - self.samples[-1].observed_at)


class MonitoringClient:
    """Read raw, label-bound evidence from Prometheus without target fallback."""

    def __init__(
        self,
        prometheus_url: str | None = None,
        *,
        deployment_id: str,
    ) -> None:
        if not deployment_id:
            raise ValueError("deployment_id is required for monitoring evidence")
        self.prometheus_url = (
            prometheus_url or os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
        ).rstrip("/")
        self.deployment_id = deployment_id

    @staticmethod
    def _escape_label_value(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    def _selector(self, endpoint: str) -> str:
        labels = {
            "deployment_id": self.deployment_id,
            "instance": endpoint,
            "job": WORKLOAD_JOB,
            "probe_module": PROBE_MODULE,
        }
        return "{" + ",".join(
            f'{key}="{self._escape_label_value(value)}"' for key, value in sorted(labels.items())
        ) + "}"

    def _request_query(self, query: str) -> dict[str, Any]:
        encoded = urllib.parse.urlencode({"query": query})
        request = urllib.request.Request(
            f"{self.prometheus_url}/api/v1/query?{encoded}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError("Prometheus returned a non-success response")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("Prometheus response has no data object")
        return data

    def _labels_match(self, labels: Any, endpoint: str) -> bool:
        if not isinstance(labels, Mapping):
            return False
        return all(
            labels.get(key) == value
            for key, value in {
                "deployment_id": self.deployment_id,
                "instance": endpoint,
                "job": WORKLOAD_JOB,
                "probe_module": PROBE_MODULE,
            }.items()
        )

    @staticmethod
    def _finite_number(value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("metric value is not numeric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("metric value is not finite")
        return number

    def _raw_series(
        self,
        metric: str,
        *,
        endpoint: str,
        max_age_seconds: float,
    ) -> dict[float, float]:
        # query_range, it does not manufacture points at evaluation steps.
        data = self._request_query(f"{metric}{self._selector(endpoint)}[{max_age_seconds:g}s]")
        if data.get("resultType") != "matrix":
            raise ValueError(f"{metric} did not return a raw matrix")
        result = data.get("result")
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError(f"{metric} did not return exactly one bound series")
        series = result[0]
        if not isinstance(series, Mapping) or not self._labels_match(series.get("metric"), endpoint):
            raise ValueError(f"{metric} labels do not match the verified deployment")
        values = series.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError(f"{metric} contains no raw samples")
        if len(values) > MAX_RAW_SAMPLES:
            raise ValueError(f"{metric} exceeds the raw sample bound")

        parsed: dict[float, float] = {}
        for point in values:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"{metric} contains a malformed sample")
            observed_at = self._finite_number(point[0])
            value = self._finite_number(point[1])
            if observed_at in parsed:
                raise ValueError(f"{metric} contains duplicate scrape timestamps")
            parsed[observed_at] = value
        return parsed

    def _latest_metric_timestamp(self, endpoint: str) -> float:
        data = self._request_query(f"timestamp(probe_success{self._selector(endpoint)})")
        if data.get("resultType") != "vector":
            raise ValueError("timestamp(probe_success) did not return a vector")
        result = data.get("result")
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError("timestamp(probe_success) did not return exactly one bound series")
        row = result[0]
        if not isinstance(row, Mapping) or not self._labels_match(row.get("metric"), endpoint):
            raise ValueError("timestamp(probe_success) labels do not match the verified deployment")
        value = row.get("value")
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("timestamp(probe_success) contains a malformed value")
        return self._finite_number(value[1])

    @staticmethod
    def _sample_id(deployment_id: str, endpoint: str, observed_at: float) -> str:
        """Use the immutable scrape identity, never mutable probe values."""
        facts = f"{deployment_id}\x1f{endpoint}\x1f{observed_at:.9f}"
        return "health-" + hashlib.sha256(facts.encode("utf-8")).hexdigest()[:24]

    def _unknown(self, reason: str) -> HealthAssessment:
        return HealthAssessment(
            status="monitoring_unknown",
            healthy=False,
            reason=reason,
        )

    def check_health(
        self,
        endpoint: str,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    ) -> HealthAssessment:
        """Return only fresh, synchronized scrape evidence for this deployment."""
        if (
            not math.isfinite(max_age_seconds)
            or max_age_seconds <= 0
            or max_age_seconds > DEFAULT_MAX_AGE_SECONDS
        ):
            return self._unknown("Monitoring freshness limit must be between zero and 15 seconds")

        try:
            selector_data = {
                "probe_success": self._raw_series(
                    "probe_success", endpoint=endpoint, max_age_seconds=max_age_seconds
                ),
                "probe_http_status_code": self._raw_series(
                    "probe_http_status_code", endpoint=endpoint, max_age_seconds=max_age_seconds
                ),
                "probe_duration_seconds": self._raw_series(
                    "probe_duration_seconds", endpoint=endpoint, max_age_seconds=max_age_seconds
                ),
                "up": self._raw_series("up", endpoint=endpoint, max_age_seconds=max_age_seconds),
            }
            common_timestamps = set.intersection(*(set(series) for series in selector_data.values()))
            if not common_timestamps:
                return self._unknown("Required metrics have no synchronized raw scrape")
            observed_at = max(common_timestamps)
            latest_probe_timestamp = self._latest_metric_timestamp(endpoint)
            if not math.isclose(latest_probe_timestamp, observed_at, rel_tol=0.0, abs_tol=0.000001):
                return self._unknown("Latest probe timestamp does not match raw scrape evidence")
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ) as error:
            return self._unknown(f"Prometheus evidence unavailable: {error}")
        sample_age = time.time() - observed_at
        if not math.isfinite(sample_age) or sample_age < 0 or sample_age > max_age_seconds:
            return self._unknown(
                f"Raw scrape evidence is stale ({sample_age:.1f}s old; limit {max_age_seconds:.1f}s)"
            )

        probe_value = selector_data["probe_success"][observed_at]
        status_value = selector_data["probe_http_status_code"][observed_at]
        duration_seconds = selector_data["probe_duration_seconds"][observed_at]
        exporter_value = selector_data["up"][observed_at]
        if probe_value not in (0.0, 1.0) or exporter_value not in (0.0, 1.0):
            return self._unknown("Probe or exporter metric is not boolean")
        if not status_value.is_integer() or status_value < 0 or duration_seconds < 0:
            return self._unknown("Probe status or duration metric is malformed")

        sample = HealthSample(
            id=self._sample_id(self.deployment_id, endpoint, observed_at),
            deployment_id=self.deployment_id,
            observed_at=observed_at,
            probe_success=probe_value == 1.0,
            http_status=int(status_value),
            duration_seconds=duration_seconds,
            exporter_up=exporter_value == 1.0,
            endpoint=endpoint,
        )
        if not sample.exporter_up:
            return self._unknown("Prometheus cannot reach the workload exporter")
        if sample.probe_success and sample.http_status == 200:
            return HealthAssessment(
                status="healthy",
                healthy=True,
                reason="Fresh marker-checked HTTP health probe succeeded",
                samples=[sample],
            )
        return HealthAssessment(
            status="unhealthy",
            healthy=False,
            reason="Fresh marker-checked HTTP health probe failed",
            samples=[sample],
        )

    async def verify_recovery_samples(
        self,
        endpoint: str,
        *,
        after: float,
        required_samples: int = 3,
        min_spacing_seconds: float = 5.0,
        timeout_seconds: float = 60.0,
    ) -> HealthAssessment:
        """Require distinct healthy raw scrapes strictly after command completion."""
        if (
            not math.isfinite(after)
            or required_samples < 1
            or min_spacing_seconds <= 0
            or timeout_seconds <= 0
        ):
            return self._unknown("Invalid recovery verification parameters")

        deadline = time.monotonic() + timeout_seconds
        supporting: list[HealthSample] = []
        seen_timestamps: set[float] = set()
        while time.monotonic() < deadline:
            assessment = await asyncio.to_thread(self.check_health, endpoint)
            if assessment.status == "healthy" and len(assessment.samples) == 1:
                sample = assessment.samples[0]
                adequately_spaced = (
                    not supporting
                    or sample.observed_at - supporting[-1].observed_at >= min_spacing_seconds
                )
                if (
                    sample.observed_at > after
                    and sample.observed_at not in seen_timestamps
                    and adequately_spaced
                ):
                    supporting.append(sample)
                    seen_timestamps.add(sample.observed_at)
                    if len(supporting) == required_samples:
                        return HealthAssessment(
                            status="healthy",
                            healthy=True,
                            reason=(
                                f"{required_samples} distinct healthy post-command "
                                "scrapes prove recovery"
                            ),
                            samples=supporting,
                        )
                elif sample.observed_at <= after or sample.observed_at in seen_timestamps:
                    # Repeated or pre-command evidence cannot keep an earlier
                    # success sequence alive.
                    supporting.clear()
                    seen_timestamps.clear()
                elif not adequately_spaced:
                    # A separate scrape which arrives too soon is not usable
                    # proof, but it is not an unhealthy transition.
                    pass
            else:
                supporting.clear()
                seen_timestamps.clear()
            await asyncio.sleep(min_spacing_seconds)

        return HealthAssessment(
            status="monitoring_unknown",
            healthy=False,
            reason="Timed out waiting for distinct healthy post-command scrapes",
        )
