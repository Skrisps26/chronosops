from __future__ import annotations

from scripts.probe import dependency_status, valid_aws_mcp_evidence


def test_dependency_status_classifies_missing_prerequisites() -> None:
    assert dependency_status(RuntimeError("Unable to locate credentials")) == "blocked"
    assert dependency_status(ConnectionRefusedError("Connection refused")) == "blocked"
    assert dependency_status(RuntimeError("unexpected response shape")) == "failed"


def test_aws_mcp_gate_requires_successful_observed_sts_call() -> None:
    observed = {
        "status": "passed",
        "authenticated_transport": True,
        "tools": [{"name": "aws___run_script"}],
        "calls": [{
            "result": {
                "structuredContent": {
                    "return_value": {"identity": {"account": "123"}},
                    "api_calls": [{"service": "sts", "operation": "GetCallerIdentity", "status": "success"}],
                },
            },
        }],
    }

    assert valid_aws_mcp_evidence(observed)
    assert not valid_aws_mcp_evidence({**observed, "authenticated_transport": False})
    assert not valid_aws_mcp_evidence({**observed, "calls": []})
    assert not valid_aws_mcp_evidence({**observed, "status": "blocked"})
