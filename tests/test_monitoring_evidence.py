"""Behavior tests and a reusable local Prometheus HTTP fixture."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from integrations.monitoring import MonitoringClient


ENDPOINT = "http://203.0.113.7/healthz"
DEPLOYMENT_ID = "test-deployment"


def _labels(**overrides: str) -> dict[str, str]:
    labels = {
        "job": "chronosops_workload",
        "instance": ENDPOINT,
        "deployment_id": DEPLOYMENT_ID,
        "probe_module": "http_2xx",
    }
    labels.update(overrides)
    return labels


def prometheus_snapshot(
    observed_at: float,
    *,
    probe_success: float = 1,
    http_status: float = 200,
    duration: float = 0.03,
    exporter_up: float = 1,
    labels: dict[str, str] | None = None,
    values: dict[str, list[list[float | str]]] | None = None,
    latest_timestamp: float | None = None,
) -> dict[str, Any]:
    """Build one raw scrape response for :class:`PrometheusHTTPFixture`."""
    default_values: dict[str, list[list[float | str]]] = {
        "probe_success": [[observed_at, str(probe_success)]],
        "probe_http_status_code": [[observed_at, str(http_status)]],
        "probe_duration_seconds": [[observed_at, str(duration)]],
        "up": [[observed_at, str(exporter_up)]],
    }
    if values:
        default_values.update(values)
    return {
        "labels": labels or _labels(),
        "values": default_values,
        "timestamp": observed_at if latest_timestamp is None else latest_timestamp,
    }


class PrometheusHTTPFixture:
    """Small local HTTP Prometheus API supporting matrices and timestamp()."""

    def __init__(self, snapshots: list[dict[str, Any]]) -> None:
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self.snapshots: list[dict[str, Any]] = []
        self.snapshot_index = 0
        self.set_samples(snapshots)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                request = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(request.query).get("query", [""])[0]
                with owner._lock:
                    owner.requests.append((request.path, query))
                    snapshot = owner.snapshots[owner.snapshot_index]
                    payload = owner._payload(query, snapshot)
                    # MonitoringClient asks timestamp(probe_success) last, so
                    # advancing here keeps all metric matrices synchronized.
                    if query.startswith("timestamp(probe_success"):
                        owner.snapshot_index = min(
                            owner.snapshot_index + 1, len(owner.snapshots) - 1
                        )
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def set_samples(self, samples: list[dict[str, Any]]) -> None:
        """Replace the next raw scrape snapshots served by this local endpoint."""
        if not samples:
            raise ValueError("At least one Prometheus scrape snapshot is required")
        with self._lock:
            self.snapshots = samples
            self.snapshot_index = 0
            self.requests.clear()

    def _payload(self, query: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        if query.startswith("timestamp(probe_success"):
            return {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    # The vector evaluation timestamp is deliberately fresh;
                    # freshness comes from timestamp(metric)'s returned value.
                    "result": [
                        {
                            "metric": snapshot["labels"],
                            "value": [time.time(), str(snapshot["timestamp"])],
                        }
                    ],
                },
            }
        metric = next(
            (
                name
                for name in (
                    "probe_success",
                    "probe_http_status_code",
                    "probe_duration_seconds",
                    "up",
                )
                if query.startswith(name + "{")
            ),
            None,
        )
        result: list[dict[str, Any]] = []
        if metric is not None and metric in snapshot["values"]:
            result = [{"metric": snapshot["labels"], "values": snapshot["values"][metric]}]
        return {"status": "success", "data": {"resultType": "matrix", "result": result}}

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def prometheus_http() -> Iterator[PrometheusHTTPFixture]:
    fixture = PrometheusHTTPFixture([prometheus_snapshot(time.time() - 1)])
    fixture.start()
    try:
        yield fixture
    finally:
        fixture.stop()


def _client(prometheus: PrometheusHTTPFixture) -> MonitoringClient:
    return MonitoringClient(prometheus.url, deployment_id=DEPLOYMENT_ID)


def test_uses_raw_scrape_timestamps_and_marker_checked_metrics(
    prometheus_http: PrometheusHTTPFixture,
) -> None:
    assessment = _client(prometheus_http).check_health(ENDPOINT)

    assert assessment.status == "healthy"
    assert assessment.healthy is True
    assert assessment.samples[0].deployment_id == DEPLOYMENT_ID
    assert assessment.samples[0].endpoint == ENDPOINT
    assert all(path == "/api/v1/query" for path, _ in prometheus_http.requests)
    assert any("probe_success" in query and "[15s]" in query for _, query in prometheus_http.requests)
    assert any(query.startswith("timestamp(probe_success") for _, query in prometheus_http.requests)


def test_old_raw_sample_is_unknown_even_when_the_query_response_is_new() -> None:
    fixture = PrometheusHTTPFixture([prometheus_snapshot(time.time() - 30)])
    fixture.start()
    try:
        assessment = _client(fixture).check_health(ENDPOINT)
    finally:
        fixture.stop()

    assert assessment.status == "monitoring_unknown"
    assert assessment.samples == []


@pytest.mark.parametrize(
    ("labels", "missing_metric"),
    [
        (_labels(instance="http://wrong-target/healthz"), None),
        (_labels(probe_module="tcp_connect"), None),
        (_labels(deployment_id="foreign-deployment"), None),
        (_labels(), "up"),
    ],
)
def test_wrong_binding_or_missing_exporter_is_unknown(
    labels: dict[str, str], missing_metric: str | None
) -> None:
    snapshot = prometheus_snapshot(time.time() - 1, labels=labels)
    if missing_metric:
        del snapshot["values"][missing_metric]
    fixture = PrometheusHTTPFixture([snapshot])
    fixture.start()
    try:
        assessment = _client(fixture).check_health(ENDPOINT)
    finally:
        fixture.stop()

    assert assessment.status == "monitoring_unknown"
    assert assessment.healthy is False

def test_exporter_disconnection_is_monitoring_unknown() -> None:
    fixture = PrometheusHTTPFixture([prometheus_snapshot(time.time() - 1, exporter_up=0)])
    fixture.start()
    try:
        assessment = _client(fixture).check_health(ENDPOINT)
    finally:
        fixture.stop()

    assert assessment.status == "monitoring_unknown"
    assert assessment.samples == []


def test_same_scrape_identity_is_stable_across_probe_value_changes() -> None:
    observed_at = time.time() - 1
    fixture = PrometheusHTTPFixture(
        [
            prometheus_snapshot(observed_at),
            prometheus_snapshot(observed_at, probe_success=0, http_status=200),
        ]
    )
    fixture.start()
    try:
        healthy = _client(fixture).check_health(ENDPOINT)
        unhealthy = _client(fixture).check_health(ENDPOINT)
    finally:
        fixture.stop()

    assert healthy.status == "healthy"
    assert unhealthy.status == "unhealthy"
    assert healthy.sample_ids == unhealthy.sample_ids

@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("probe_duration_seconds", "NaN"),
        ("probe_http_status_code", "not-a-number"),
    ],
)
def test_malformed_metric_values_are_unknown(metric: str, value: str) -> None:
    observed_at = time.time() - 1
    fixture = PrometheusHTTPFixture(
        [prometheus_snapshot(observed_at, values={metric: [[observed_at, value]]})]
    )
    fixture.start()
    try:
        assessment = _client(fixture).check_health(ENDPOINT)
    finally:
        fixture.stop()

    assert assessment.status == "monitoring_unknown"


def test_timestamp_metric_must_match_the_latest_raw_scrape() -> None:
    observed_at = time.time() - 1
    fixture = PrometheusHTTPFixture(
        [prometheus_snapshot(observed_at, latest_timestamp=observed_at - 1)]
    )
    fixture.start()
    try:
        assessment = _client(fixture).check_health(ENDPOINT)
    finally:
        fixture.stop()

    assert assessment.status == "monitoring_unknown"


def test_duplicate_raw_timestamps_are_not_health_evidence() -> None:
    observed_at = time.time() - 1
    duplicate_success = [[observed_at, "1"], [observed_at, "1"]]
    fixture = PrometheusHTTPFixture(
        [prometheus_snapshot(observed_at, values={"probe_success": duplicate_success})]
    )
    fixture.start()
    try:
        assessment = _client(fixture).check_health(ENDPOINT)
    finally:
        fixture.stop()

    assert assessment.status == "monitoring_unknown"
    assert assessment.samples == []


def test_http_200_without_the_blackbox_success_marker_is_unhealthy() -> None:
    fixture = PrometheusHTTPFixture(
        [prometheus_snapshot(time.time() - 1, probe_success=0, http_status=200)]
    )
    fixture.start()
    try:
        assessment = _client(fixture).check_health(ENDPOINT)
    finally:
        fixture.stop()

    assert assessment.status == "unhealthy"
    assert assessment.healthy is False
    assert assessment.samples[0].http_status == 200
    assert assessment.samples[0].probe_success is False


def test_recovery_requires_three_distinct_post_command_scrapes() -> None:
    now = time.time()
    after = now - 12
    fixture = PrometheusHTTPFixture(
        [
            prometheus_snapshot(after),
            prometheus_snapshot(now - 8),
            prometheus_snapshot(now - 4),
            prometheus_snapshot(now - 1),
        ]
    )
    fixture.start()
    try:
        assessment = asyncio.run(
            _client(fixture).verify_recovery_samples(
                ENDPOINT,
                after=after,
                required_samples=3,
                min_spacing_seconds=0.001,
                timeout_seconds=1,
            )
        )
    finally:
        fixture.stop()

    assert assessment.status == "healthy"
    assert len(assessment.samples) == 3
    assert all(sample.observed_at > after for sample in assessment.samples)
    assert len(set(assessment.sample_ids)) == 3


def test_bad_sample_resets_recovery_evidence_sequence() -> None:
    now = time.time()
    fixture = PrometheusHTTPFixture(
        [
            prometheus_snapshot(now - 5),
            prometheus_snapshot(now - 4, probe_success=0, http_status=200),
            prometheus_snapshot(now - 3),
            prometheus_snapshot(now - 2),
        ]
    )
    fixture.start()
    try:
        assessment = asyncio.run(
            _client(fixture).verify_recovery_samples(
                ENDPOINT,
                after=now - 6,
                required_samples=3,
                min_spacing_seconds=0.001,
                timeout_seconds=0.05,
            )
        )
    finally:
        fixture.stop()

    assert assessment.status == "monitoring_unknown"
    assert assessment.samples == []


@pytest.mark.parametrize("exporter_up,expected", [(1, "unhealthy"), (0, "monitoring_unknown")])
def test_verification_distinguishes_failed_application_from_missing_monitoring(
    exporter_up: float, expected: str,
) -> None:
    now = time.time()
    fixture = PrometheusHTTPFixture([
        prometheus_snapshot(now - 1, probe_success=0, http_status=0, exporter_up=exporter_up),
    ])
    fixture.start()
    try:
        assessment = asyncio.run(_client(fixture).verify_recovery_samples(
            ENDPOINT, after=now - 3, min_spacing_seconds=0.01, timeout_seconds=2.0,
        ))
    finally:
        fixture.stop()
    assert assessment.status == expected
    assert assessment.healthy is False
    if expected == "unhealthy":
        assert assessment.samples[0].observed_at > now - 3
        assert assessment.samples[0].probe_success is False
    else:
        assert assessment.samples == []
