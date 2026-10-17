"""Missing identity configuration must never widen graph permissions."""
import pytest

from agents.mcp_client import FalkorDBMcpClient
from agents.settings import GraphNames


def test_missing_reader_credentials_do_not_escalate(monkeypatch):
    monkeypatch.setattr(
        "agents.mcp_client.get_db_secrets",
        lambda: {"FALKORDB_ADMIN_USERNAME": "admin", "FALKORDB_ADMIN_PASSWORD": "dummy-admin-only"},
    )
    with pytest.raises(ValueError):
        FalkorDBMcpClient("globex")


@pytest.mark.parametrize("tenant", ["", "ACME", "acme/../globex", "third-tenant"])
def test_unknown_tenant_never_routes_credentials(tenant, monkeypatch):
    def unexpected_credentials():
        raise AssertionError("Unknown tenant reached credential lookup")
    monkeypatch.setattr("agents.mcp_client.get_db_secrets", unexpected_credentials)
    with pytest.raises(ValueError):
        FalkorDBMcpClient(tenant)


@pytest.mark.parametrize("prefix", ["production_", "test_", "test_../", "test_*", "test_ABC_"])
def test_graph_namespace_rejects_unsafe_keys(prefix):
    with pytest.raises(ValueError):
        GraphNames(prefix)
