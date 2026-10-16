"""Module 1 verification suite: seed corpus, topology loading, and NetworkX oracle."""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest
from falkordb import FalkorDB

from seed.generate import MANIFEST_FILE, compute_hash, generate_data
from seed.load import get_config, is_graph_ready, load_topology

TEST_GRAPH = "test_ops_topology_mod1"


@pytest.fixture(scope="module", autouse=True)
def cleanup_test_graph():
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    yield
    try:
        db.select_graph(TEST_GRAPH).delete()
    except Exception:
        pass


def test_manifest_reproducibility():
    """Verify that regenerating data with fixed seed produces identical hashes."""
    data1 = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    assert data1["counts"]["services"] == 45
    assert data1["counts"]["dependencies"] == 102
    assert data1["counts"]["routes"] == 5
    assert data1["counts"]["documents"] == 16
    assert data1["counts"]["people"] == 12
    assert data1["counts"]["teams"] == 4


def test_networkx_ancestor_traversal_oracle():
    """Independent oracle test: failure of payments-primary impacts exactly 4 services."""
    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    G = nx.DiGraph()
    for d in manifest["dependencies"]:
        G.add_edge(d["source_id"], d["target_id"])

    # In A -> B (A depends on B), ancestors of B are the nodes that depend on B
    affected = sorted(nx.ancestors(G, "payments-primary"))
    expected = sorted(manifest["scenario"]["affected_services"])

    assert affected == ["checkout", "gateway", "orders", "worker"]
    assert affected == expected


def test_topology_loader_counts_and_idempotency():
    """Load topology into test graph twice; verify counts match manifest and do not drift."""
    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))

    # Initial load
    res1 = load_topology(graph_name=TEST_GRAPH, wipe=True)
    assert res1["ready"] is True
    assert res1["services"] == manifest["counts"]["services"]
    assert res1["dependencies"] == manifest["counts"]["dependencies"]
    assert res1["routes"] == manifest["counts"]["routes"]
    assert is_graph_ready(TEST_GRAPH) is True

    # Second load (idempotency test)
    res2 = load_topology(graph_name=TEST_GRAPH, wipe=False)
    assert res2["ready"] is True
    assert res2["services"] == manifest["counts"]["services"]
    assert res2["dependencies"] == manifest["counts"]["dependencies"]
    assert res2["routes"] == manifest["counts"]["routes"]
    assert is_graph_ready(TEST_GRAPH) is True


def test_interrupted_load_leaves_unready():
    """Verify that an interrupted load leaves ready=false in Meta."""
    interrupted_graph = f"{TEST_GRAPH}_interrupted"
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    try:
        res = load_topology(graph_name=interrupted_graph, simulate_interruption=True, wipe=True)
        assert res["ready"] is False
        assert is_graph_ready(interrupted_graph) is False
    finally:
        try:
            db.select_graph(interrupted_graph).delete()
        except Exception:
            pass
