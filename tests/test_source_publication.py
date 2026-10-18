"""Application publication boundaries using explicitly synthetic SDK-shaped chunks."""
from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Event

import pytest
from fastapi.testclient import TestClient

from seed.sources import (
    SourceNotFound, SourceUnavailable, _graph, enqueue_source, publish_source_request,
    evidence_state_guard, get_published_source, run_source_worker, wait_for_publication,
)

DOCUMENT = 'rfc-001-payments-failover'


async def synthetic_processor(request):
    """Stand in for extraction only; this is not proof of the Bedrock/SDK gate."""
    graph = _graph()
    graph.query('MATCH (d:Document {id:$id})-[:PART_OF]->(c:Chunk) DETACH DELETE c',
                {'id': request['document_id']})
    if request['operation'] != 'delete':
        graph.query(
            'MERGE (d:Document {id:$id}) MERGE (c:Chunk {id:$chunk}) '
            'SET d.content_hash=$hash, c.index=0, c.text=$text MERGE (d)-[:PART_OF]->(c) '
            'WITH c MATCH (s:Service {services__service_id:$service}) '
            'MERGE (s)-[:MENTIONED_IN]->(c)',
            {'id': request['document_id'], 'chunk': 'synthetic-'+request['content_hash'],
             'text': request['text'], 'service': request['service_id'], 'hash': request['content_hash']})


@pytest.mark.asyncio
async def test_update_withdraws_old_evidence_then_publishes_hash_and_changed_author():
    text = '---\nauthor: user-bob\nservice_id: payments-primary\n---\nRevised rollback policy.'
    request = enqueue_source(DOCUMENT, text)
    with pytest.raises(SourceUnavailable):
        get_published_source(DOCUMENT)
    await run_source_worker(synthetic_processor, once=True)
    published = await wait_for_publication(request['request_id'])
    assert published['content_hash'] == hashlib.sha256(text.encode()).hexdigest()
    assert published['author_id'] == 'user-bob'
    assert published['chunks'][0]['text'] == text
    assert get_published_source('rfc-002-checkout-resilience')['author_id'] == 'user-carol'
    rows = _graph().ro_query(
        'MATCH (c:Contribution)-[:CONTRIBUTION_FOR]->(s:SourceRecord {document_id:$id}) '
        'WHERE c.active=true RETURN c.author_id', {'id': DOCUMENT}).result_set
    assert rows == [['user-bob']]
    again = enqueue_source(DOCUMENT, text)
    assert again['revision'] == published['revision']


@pytest.mark.asyncio
async def test_failed_worker_keeps_source_unavailable_and_readiness_false():
    request = enqueue_source(DOCUMENT, 'new version')
    async def fail(_):
        raise RuntimeError('synthetic extraction failure')
    with pytest.raises(SourceUnavailable):
        await run_source_worker(fail, once=True)
    with pytest.raises(SourceUnavailable):
        get_published_source(DOCUMENT)
    with pytest.raises(SourceUnavailable):
        await wait_for_publication(request['request_id'])
    assert _graph().ro_query("MATCH (m:Meta {id:'company_brain_meta'}) RETURN m.ready").result_set == [[False]]


@pytest.mark.asyncio
async def test_incomplete_sdk_document_cannot_publish_chunks_as_grounded_evidence():
    request = enqueue_source(DOCUMENT, 'Partial extraction must remain unavailable.')
    await synthetic_processor(request)
    _graph().query(
        'MATCH (d:Document {id:$id}) SET d.content_hash=null',
        {'id': DOCUMENT},
    )
    with pytest.raises(SourceUnavailable, match='certify complete'):
        publish_source_request(request['request_id'])
    with pytest.raises(SourceUnavailable):
        get_published_source(DOCUMENT)
    assert _graph().ro_query(
        'MATCH (r:SourceRequest {request_id:$id}) RETURN r.state',
        {'id': request['request_id']},
    ).result_set == [['pending']]


@pytest.mark.asyncio
async def test_deleted_source_cannot_be_resurrected_by_an_older_event():
    enqueue_source(DOCUMENT, '', delete=True, source_order=20)
    with pytest.raises(SourceNotFound):
        get_published_source(DOCUMENT)
    await run_source_worker(synthetic_processor, once=True)
    old = enqueue_source(DOCUMENT, 'late stale text', allow_create=True, source_order=19)
    assert old['status'] == 'ignored'
    with pytest.raises(SourceNotFound):
        get_published_source(DOCUMENT)
    assert _graph().ro_query('MATCH (r:SourceRequest {document_id:$id}) WHERE r.text <> \'\' RETURN r.text',
                              {'id': DOCUMENT}).result_set == []


@pytest.mark.asyncio
async def test_second_writer_is_rejected_while_first_owns_extraction():
    enqueue_source(DOCUMENT, 'serialized update')
    entered, release = asyncio.Event(), asyncio.Event()
    async def pause(request):
        entered.set()
        await release.wait()
        await synthetic_processor(request)
    first = asyncio.create_task(run_source_worker(pause, once=True))
    await entered.wait()
    try:
        with pytest.raises(SourceUnavailable, match='already running'):
            await run_source_worker(synthetic_processor, once=True)
        from seed.ingest import ingest_company_brain
        with pytest.raises(SourceUnavailable, match='already running'):
            await ingest_company_brain(wipe=True)
        assert get_published_source('rfc-002-checkout-resilience')['author_id'] == 'user-carol'
    finally:
        release.set()
        await first


def test_source_http_viewer_excludes_pending_deleted_and_unknown_versions(monkeypatch):
    import api.main as main
    from api.contracts import get_tenant_id
    main.app.dependency_overrides[get_tenant_id] = lambda: 'acme'
    try:
        with TestClient(main.app) as client:
            current = client.get('/api/sources/'+DOCUMENT)
            assert current.status_code == 200
            assert current.json()['content_hash']
            assert client.get('/api/sources/not-a-source').status_code == 404
            enqueue_source(DOCUMENT, 'not yet extracted')
            assert client.get('/api/sources/'+DOCUMENT).status_code == 503
            enqueue_source(DOCUMENT, '', delete=True)
            assert client.get('/api/sources/'+DOCUMENT).status_code == 404
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_nonfixture_service_contributor_and_unknown_author_update():
    from agents.knowledge import bus_factor
    service = _graph().ro_query(
        "MATCH (s:Service) WHERE NOT s.services__owner_team IN ['Payments','Checkout'] "
        "RETURN s.services__service_id LIMIT 1").result_set[0][0]
    document = 'third-service-publication'
    enqueue_source(document, 'A reviewed operational decision.', allow_create=True,
                   service_id=service, author_id='user-bob')
    await run_source_worker(synthetic_processor, once=True)
    recorded = await bus_factor(service)
    assert recorded.data['bus_factor_proxy'] == 1
    assert recorded.data['contributors'][0]['author_id'] == 'user-bob'
    enqueue_source(document, 'A reviewed operational decision.', service_id=service, author_id='unknown-person')
    await run_source_worker(synthetic_processor, once=True)
    unknown = await bus_factor(service)
    assert unknown.data['bus_factor_proxy'] == 0
    assert unknown.data['knowledge_concentration'] == 'unknown'


@pytest.mark.asyncio
async def test_idle_worker_restores_readiness_after_idempotent_initialization():
    _graph().query("MATCH (m:Meta {id:'company_brain_meta'}) SET m.ready=false")
    await run_source_worker(synthetic_processor, once=True)
    assert _graph().ro_query("MATCH (m:Meta {id:'company_brain_meta'}) RETURN m.ready").result_set == [[True]]


def test_document_entrypoint_cannot_replace_a_canonical_table():
    _graph().query(
        "MERGE (s:SourceRecord {document_id:'services.csv'}) "
        "SET s.source_kind='canonical_table', s.status='published', s.revision=1"
    )
    with pytest.raises(ValueError, match='structured operator ingestion'):
        enqueue_source('services.csv', 'untrusted replacement')
    assert _graph().ro_query(
        "MATCH (s:SourceRecord {document_id:'services.csv'}) RETURN s.status,s.revision"
    ).result_set == [['published', 1]]


def test_source_edit_cannot_cross_a_final_approval_version_read():
    original = get_published_source(DOCUMENT)
    started = Event()

    def edit():
        started.set()
        return enqueue_source(DOCUMENT, "Concurrent source revision awaiting extraction.")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with evidence_state_guard():
            pending = pool.submit(edit)
            assert started.wait(timeout=2)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.05)
            assert get_published_source(DOCUMENT)["revision"] == original["revision"]
            with pytest.raises(SourceUnavailable):
                with evidence_state_guard(blocking=False):
                    pytest.fail("A competing final read entered during publication fencing")
        request = pending.result(timeout=3)
    assert request["revision"] == original["revision"] + 1
    with pytest.raises(SourceUnavailable):
        get_published_source(DOCUMENT)
