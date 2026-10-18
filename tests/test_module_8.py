"""Module 8 acceptance tests: demo mode parsing, fail-closed observation, and readiness."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from api import main
from api.contracts import get_tenant_id


ROOT = Path(__file__).resolve().parents[1]
DEMO_SCRIPT = ROOT / "scripts" / "demo.sh"
DEMO_PY = ROOT / "scripts" / "demo.py"


def test_demo_script_mode_parsing():
    """scripts/demo.sh requires explicit --mode simulation|real; invalid exits with code 2."""
    res_no_args = subprocess.run([str(DEMO_SCRIPT)], capture_output=True, text=True)
    assert res_no_args.returncode == 2

    res_invalid = subprocess.run([str(DEMO_SCRIPT), "--mode", "unsupported"], capture_output=True, text=True)
    assert res_invalid.returncode == 2
    assert "expected simulation or real" in (res_invalid.stderr or res_invalid.stdout)


def test_demo_py_mode_parsing():
    """scripts/demo.py exits 2 on invalid arguments."""
    res = subprocess.run([sys.executable, str(DEMO_PY), "--mode", "fake"], capture_output=True, text=True)
    assert res.returncode == 2


def test_demo_runner_fails_closed_when_live_prerequisites_blocked(tmp_path, monkeypatch):
    """When prerequisites or authoritative evidence are blocked, demo runner exits nonzero and records blocked status."""
    manifest_path = tmp_path / "track-demo.json"
    monkeypatch.setattr("scripts.demo.ARTIFACT", manifest_path)
    monkeypatch.setenv("CHRONOSOPS_DEMO_URL", "http://127.0.0.1:9999")  # Unreachable

    res = subprocess.run(
        [sys.executable, str(DEMO_PY), "--mode", "simulation", "--wait-seconds", "0"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1

    manifest = json.loads(Path(ROOT / "artifacts" / "track-demo.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"
    assert manifest["tracks"]["agent_memory"]["status"] == "blocked"


def test_readyz_fails_closed_without_proxy_token(monkeypatch):
    """Missing CHRONOSOPS_PROXY_TOKEN causes readyz to return 503."""
    monkeypatch.delenv("CHRONOSOPS_PROXY_TOKEN", raising=False)
    with TestClient(main.app) as client:
        res = client.get("/readyz")
        assert res.status_code == 503


def test_untrusted_cors_origin_is_rejected():
    """Unapproved quick tunnel origins are rejected with 403."""
    try:
        main.app.dependency_overrides[get_tenant_id] = lambda: "acme"
        with TestClient(main.app) as client:
            res = client.post(
                "/api/incidents",
                json={"service_id": "payments-primary"},
                headers={"Origin": "https://malicious-tunnel.trycloudflare.com"},
            )
            assert res.status_code == 403
    finally:
        main.app.dependency_overrides.clear()

def test_foreign_memory_episode_denial():
    """Tenant Globex receives 404 for Acme's memory episodes."""
    try:
        main.app.dependency_overrides[get_tenant_id] = lambda: "globex"
        with TestClient(main.app) as client:
            res = client.get("/api/memory/episodes/ep-nonexistent-or-acme")
            assert res.status_code == 404
    finally:
        main.app.dependency_overrides.clear()
