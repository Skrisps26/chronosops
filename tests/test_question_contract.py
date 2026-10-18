"""Question endpoint contracts for graph evidence and generation failures."""
from __future__ import annotations

from fastapi import HTTPException
from starlette.requests import Request

import pytest

from agents.answering import GenerationUnavailableError
from agents.schemas import ToolResult, ToolTrace
from api import main
from api.contracts import QuestionRequest


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "headers": []})


def _result(status: str, data: object, citations: list[dict[str, object]] | None = None) -> ToolResult:
    return ToolResult(
        status=status,
        data=data,
        evidence={"citations": citations or []},
        trace=ToolTrace(tool="test", graph="company", cypher="", params={}),
    )



@pytest.mark.asyncio
async def test_unknown_service_never_falls_back_to_payments(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(service_id: str, *, tenant: str) -> ToolResult:
        assert service_id == "unknown-service"
        return _result("NOT_FOUND", "unknown-service is absent")

    monkeypatch.setattr(main, "who_owns", missing)

    with pytest.raises(HTTPException) as error:
        await main.ask_question(
            QuestionRequest(question="Who owns this?", service_id="unknown-service"),
            _request(),
            tenant_id="acme",
        )

    assert error.value.status_code == 404
    assert error.value.detail["code"] == "service_not_found"


@pytest.mark.asyncio
async def test_unavailable_generation_returns_graph_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    citation = {
        "document_id": "rfc-001",
        "chunk_id": "rfc-001-0",
        "content_hash": "rfc-hash-v1",
        "source_version": "1",
        "revision": 1,
        "text": "Queue failover is approved because rollback stays within the cost cap.",
    }

    async def provenance(service_id: str, *, tenant: str) -> ToolResult:
        assert service_id == "payments-primary"
        return _result(
            "OK",
            {"service_id": service_id, "sources": [{"document_id": "rfc-001"}]},
            [citation],
        )

    async def unavailable(**_: object) -> str:
        raise GenerationUnavailableError("unavailable")

    monkeypatch.setattr(main, "decision_provenance", provenance)
    monkeypatch.setattr(main, "synthesize_from_citations", unavailable)

    with pytest.raises(HTTPException) as error:
        await main.ask_question(
            QuestionRequest(question="Why did payments-primary fail over?", service_id="payments-primary"),
            _request(),
            tenant_id="acme",
        )

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "generation_unavailable"
    assert error.value.detail["citations"] == [citation]
    assert error.value.detail["tool_result"]["evidence"]["citations"] == [citation]
