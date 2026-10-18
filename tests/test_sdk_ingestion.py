"""SDK-backed structured ingestion contracts for Company Brain."""
from __future__ import annotations

from pathlib import Path

import pytest

from seed.ingest import load_alias_mappings


def test_alias_collisions_and_unknown_targets_are_refused(tmp_path: Path) -> None:
    """Alias publication never chooses an arbitrary canonical Service target."""
    collision = tmp_path / "aliases.csv"
    collision.write_text(
        "alias,service_id\nPayments API,payments-primary\npayments api,checkout\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicates alias"):
        load_alias_mappings(alias_path=collision, known_service_ids={"payments-primary", "checkout"})

    unknown = tmp_path / "unknown.csv"
    unknown.write_text("alias,service_id\nMissing,not-a-service\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown service_id"):
        load_alias_mappings(alias_path=unknown, known_service_ids={"payments-primary"})
    shadow = tmp_path / "shadow.csv"
    shadow.write_text("alias,service_id\npayments-primary,checkout\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflicts with canonical identity"):
        load_alias_mappings(alias_path=shadow, known_service_ids={"payments-primary", "checkout"})


@pytest.mark.bedrock_live
@pytest.mark.asyncio
async def test_supported_sdk_lifecycle_extracts_on_disposable_company_graph() -> None:
    """Exercise tables, source worker, extraction, and finalization with real Bedrock."""
    from falkordb import FalkorDB

    from agents.settings import graph_names
    from seed.ingest import get_config, ingest_company_brain
    import asyncio
    import hashlib
    from agents.knowledge import update_company_document, who_owns, decision_provenance
    from seed.sources import SourceNotFound, get_published_source, run_source_worker

    result = await ingest_company_brain(wipe=True)

    assert result["ready"] is True

    config = get_config()
    graph = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    ).select_graph(graph_names().company)
    ownership = graph.ro_query(
        "MATCH (s:Service)-[r:RELATES {rel_type: 'OWNED_BY'}]->(t:Team) "
        "WHERE s.services__service_id = 'payments-primary' AND t.teams__team_id IS NOT NULL "
        "RETURN DISTINCT t.teams__team_id, t.is_stub"
    ).result_set
    membership = graph.ro_query(
        "MATCH (p:Person)-[r:RELATES {rel_type: 'MEMBER_OF'}]->(t:Team) "
        "WHERE p.people__person_id = 'user-alice' AND t.teams__team_id IS NOT NULL "
        "RETURN DISTINCT t.teams__team_id, t.is_stub"
    ).result_set
    assert ownership == [["team-payments", False]]
    assert membership == [["team-payments", False]]
    document_id = "rfc-001-payments-failover"
    original = get_published_source(document_id)
    unrelated = get_published_source("rfc-002-checkout-resilience")
    raw = (Path(__file__).resolve().parents[1] / original["path"]).read_text(encoding="utf-8")
    assert original["content_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    owner = await who_owns("payments-primary")
    assert owner.status == "OK"
    assert owner.data["owner_team"] == "Payments"
    assert {person["person_id"] for person in owner.data["team_members"]} == {"user-alice", "user-bob", "user-ivan"}
    provenance = await decision_provenance("payments-primary")
    assert provenance.status == "OK"
    assert document_id in {source["document_id"] for source in provenance.data["sources"]}
    updated = raw.replace('version: "1.0"', 'version: "2.0"') + "\nPrioritize rollback speed in this published revision.\n"
    stop = asyncio.Event()
    worker = asyncio.create_task(run_source_worker(stop_event=stop))
    try:
        published = await update_company_document(document_id, updated, timeout_seconds=300)
        assert published["revision"] > original["revision"]
        assert published["content_hash"] == hashlib.sha256(updated.encode()).hexdigest()
        assert any("Prioritize rollback speed" in chunk["text"] for chunk in published["chunks"])
        assert get_published_source(unrelated["document_id"])["content_hash"] == unrelated["content_hash"]
        same = await update_company_document(document_id, updated)
        assert same["revision"] == published["revision"]
        deleted = await update_company_document(document_id, "", delete=True, timeout_seconds=300)
        assert deleted["status"] == "deleted"
        with pytest.raises(SourceNotFound):
            get_published_source(document_id)
    finally:
        stop.set()
        await worker
