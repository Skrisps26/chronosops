"""Module 8 verification suite: scripts, packaging, documentation, and three-track demo manifest."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
DOCS_DIR = ROOT / "docs"
ARTIFACTS_DIR = ROOT / "artifacts"


def test_scripts_exist_and_executable():
    """Verify that all operational and demonstration scripts exist and are executable."""
    required_scripts = [
        "bootstrap-local.sh",
        "up.sh",
        "seed.sh",
        "demo.sh",
        "tunnel.sh",
    ]
    for script_name in required_scripts:
        script_path = SCRIPTS_DIR / script_name
        assert script_path.is_file(), f"Missing script {script_name}"
        assert os.access(script_path, os.X_OK), f"Script {script_name} is not executable"


def test_architecture_documentation_structure():
    """Verify that docs/ARCHITECTURE.md covers multigraph isolation, MCP tools, and memory hierarchy."""
    arch_file = DOCS_DIR / "ARCHITECTURE.md"
    assert arch_file.is_file()
    content = arch_file.read_text(encoding="utf-8")
    assert "ops_topology" in content
    assert "company_brain" in content
    assert "agent_memory_acme" in content
    assert "algo.SPpaths" in content
    assert "algo.betweenness" in content
    assert "WorkflowSubgraph" in content
    assert "NEXT_STEP" in content


def test_hackathon_blog_post_evidence():
    """Verify that docs/blog/chronosops.md contains the 100% benchmark table and Cypher queries."""
    blog_file = DOCS_DIR / "blog" / "chronosops.md"
    assert blog_file.is_file()
    content = blog_file.read_text(encoding="utf-8")
    assert "100.0%" in content
    assert "SPpaths" in content
    assert "Bus-Factor" in content or "bus factor" in content.lower()
    assert "agent_memory" in content


def test_three_track_demo_manifest():
    """Verify that artifacts/track-demo.json contains passing statuses for all three tracks."""
    manifest_file = ARTIFACTS_DIR / "track-demo.json"
    assert manifest_file.is_file(), "Run ./scripts/demo.sh to generate the demo manifest"
    data = json.loads(manifest_file.read_text(encoding="utf-8"))

    tracks = data["tracks"]
    assert tracks["company_brain"]["status"] == "passed"
    assert tracks["agents_that_act"]["status"] == "passed"
    assert tracks["agent_memory"]["status"] == "passed"
    assert tracks["agent_memory"]["tenant_isolation_verified"] is True
    assert tracks["agent_memory"]["procedural_llm_calls"] == 0
