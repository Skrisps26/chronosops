"""Generate the Prometheus file-SD target from the authoritative deployment binding."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from integrations.deployment import bound_deployment
from seed.load import get_config

ROOT = Path(__file__).resolve().parents[1]
TARGET_FILE = ROOT / "monitoring" / "targets" / "demo.json"
PROMETHEUS_TARGET_FILE = "/etc/prometheus/targets/demo.json"
REQUIRED_BINDING_FIELDS = frozenset(
    {"deployment_id", "endpoint", "service_id", "tenant_id", "expires_at"}
)


class MonitoringNotReadyError(RuntimeError):
    """The authoritative deployment cannot safely be monitored yet."""


def _write_targets(targets: list[dict[str, Any]], target_file: Path = TARGET_FILE) -> None:
    """Atomically replace the file-SD payload so stale targets cannot survive."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.parent.chmod(0o755)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target_file.parent,
            prefix=f".{target_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            os.fchmod(temporary.fileno(), 0o644)
            json.dump(targets, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target_file)
        directory = os.open(target_file.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _workload_target(binding: Mapping[str, Any]) -> dict[str, Any]:
    missing = REQUIRED_BINDING_FIELDS.difference(binding)
    if missing:
        raise MonitoringNotReadyError(
            f"Published deployment binding is missing: {', '.join(sorted(missing))}"
        )

    deployment_id = binding["deployment_id"]
    service_id = binding["service_id"]
    tenant_id = binding["tenant_id"]
    endpoint = binding["endpoint"]
    expires_at = binding["expires_at"]
    if not all(isinstance(value, str) and value for value in (
        deployment_id,
        service_id,
        tenant_id,
        endpoint,
        expires_at,
    )):
        raise MonitoringNotReadyError("Published deployment binding contains empty required fields")
    if service_id != "payments-primary" or tenant_id != "acme":
        raise MonitoringNotReadyError("Published deployment binding is not the configured workload")

    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/healthz"
        or parsed.query
        or parsed.fragment
    ):
        raise MonitoringNotReadyError("Published deployment endpoint is not an exact HTTP health endpoint")

    return {
        "targets": [endpoint],
        "labels": {
            "service": service_id,
            "tenant_id": tenant_id,
            "deployment_id": deployment_id,
            "probe_module": "http_2xx",
        },
    }


def configure_monitoring(
    config: Mapping[str, str] | None = None,
    *,
    target_file: Path = TARGET_FILE,
) -> dict[str, str]:
    """Write one trusted workload target, or clear stale targets when unbound."""
    try:
        binding = bound_deployment(dict(config) if config is not None else get_config())
        target = _workload_target(binding)
    except Exception as error:
        _write_targets([], target_file)
        raise MonitoringNotReadyError(
            f"Monitoring is not ready; cleared {target_file}: {error}"
        ) from error

    _write_targets([target], target_file)
    return {
        "status": "configured",
        "target_file": str(target_file),
        "prometheus_target_file": PROMETHEUS_TARGET_FILE,
        "deployment_id": target["labels"]["deployment_id"],
    }


def main() -> int:
    try:
        result = configure_monitoring()
    except MonitoringNotReadyError as error:
        print(str(error), file=sys.stderr)
        return 75
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
