"""Server-owned graph routing; graph names are never request parameters."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

TENANTS = frozenset({"acme", "globex"})


def require_tenant(tenant: str) -> str:
    if tenant not in TENANTS:
        raise ValueError("Unknown tenant")
    return tenant


@dataclass(frozen=True)
class GraphNames:
    prefix: str = ""

    def __post_init__(self) -> None:
        if self.prefix and re.fullmatch(r"test_[a-z0-9_]+", self.prefix) is None:
            raise ValueError("Graph prefix must be empty or a test_ namespace")

    @property
    def company(self) -> str:
        return f"{self.prefix}company_brain"

    @property
    def topology(self) -> str:
        return f"{self.prefix}ops_topology"

    def memory_for(self, tenant: str) -> str:
        return f"{self.prefix}agent_memory_{require_tenant(tenant)}"


def graph_names() -> GraphNames:
    return GraphNames(os.environ.get("CHRONOSOPS_GRAPH_PREFIX", ""))
