"""Official FalkorDB MCP client integration with strict read-only and tenant isolation."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
SECRETS_FILE = ROOT / ".secrets" / "db.env"
MCP_BIN = ROOT / "mcp" / "node_modules" / ".bin" / "falkordb-mcp"


def get_db_secrets() -> dict[str, str]:
    values = {k: v for k, v in dotenv_values(SECRETS_FILE).items() if isinstance(v, str) and v}
    values.update({k: v for k, v in os.environ.items() if v})
    return values


class FalkorDBMcpClient:
    """Provides managed async stdio sessions to the official FalkorDB MCP server."""

    def __init__(self, tenant: str = "acme", host: str | None = None, port: int | None = None) -> None:
        self.tenant = tenant.lower()
        self.secrets = get_db_secrets()
        self.host = host or self.secrets.get("FALKORDB_HOST", "127.0.0.1")
        self.port = port or int(self.secrets.get("FALKORDB_PORT", 6379))

        upper = self.tenant.upper()
        self.username = self.secrets.get(f"FALKORDB_READER_{upper}_USERNAME", f"reader_{self.tenant}")
        self.password = self.secrets.get(f"FALKORDB_READER_{upper}_PASSWORD", "")
        if not self.password:
            # Fallback to admin if tenant password is not set
            self.username = self.secrets.get("FALKORDB_ADMIN_USERNAME", "admin")
            self.password = self.secrets.get("FALKORDB_ADMIN_PASSWORD", "")

    def _get_server_params(self, readonly: bool = True) -> StdioServerParameters:
        if not MCP_BIN.is_file():
            raise FileNotFoundError(f"Official FalkorDB MCP binary not found at {MCP_BIN}")

        env = {
            **os.environ,
            "FALKORDB_HOST": self.host,
            "FALKORDB_PORT": str(self.port),
            "FALKORDB_USERNAME": self.username,
            "FALKORDB_PASSWORD": self.password,
            "FALKORDB_DEFAULT_READONLY": "true" if readonly else "false",
            "FALKORDB_STRICT_READONLY": "true" if readonly else "false",
        }
        return StdioServerParameters(command=str(MCP_BIN), env=env)

    async def query_readonly(
        self,
        graph_name: str,
        query: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float = 5.0,
    ) -> tuple[list[dict[str, Any]], float]:
        """Execute a query via query_graph_readonly tool; returns (rows, duration_ms)."""
        server_params = self._get_server_params(readonly=True)
        start_time = time.perf_counter()

        async with Client(server_params, mode="legacy", read_timeout_seconds=timeout_seconds) as client:
            result = await client.call_tool(
                "query_graph_readonly",
                {
                    "graphName": graph_name,
                    "query": query,
                    "params": params or {},
                },
            )
            duration_ms = (time.perf_counter() - start_time) * 1000.0

            if result.is_error:
                error_msg = result.content[0].text if result.content else "Unknown error"
                raise RuntimeError(f"MCP query_graph_readonly error: {error_msg}")

            # Parse content from tool response
            text = result.content[0].text if result.content else "[]"
            try:
                data = json.loads(text)
            except Exception:
                data = [{"raw": text}]

            if isinstance(data, dict) and "data" in data:
                rows = data["data"]
            elif isinstance(data, list):
                rows = data
            else:
                rows = [data]

            return rows, duration_ms
