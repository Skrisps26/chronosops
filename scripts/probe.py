"""Exercise ChronosOps Module 0 dependencies against configured local services.

The probe intentionally creates only ``probe_*`` graphs and deletes them in a
``finally`` block. It never queries, deletes, or alters a demo graph.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
from dotenv import dotenv_values
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

Status = Literal["passed", "failed", "blocked"]
ROOT = Path(__file__).resolve().parents[1]
SECRETS_FILE = ROOT / ".secrets" / "db.env"
ARTIFACT = ROOT / "artifacts" / "probe.json"
PROBE_GRAPHS = ("probe_admin", "probe_acme", "probe_globex", "probe_sdk", "probe_sdk__ontology")


@dataclass(slots=True)
class Check:
    name: str
    status: Status
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


class ProbeBlocked(RuntimeError):
    """A required configured prerequisite is absent rather than broken."""


def settings() -> dict[str, str]:
    """Merge ignored dotenv secrets with explicit process settings."""
    values = {
        key: value
        for key, value in dotenv_values(SECRETS_FILE).items()
        if isinstance(value, str) and value
    }
    values.update({key: value for key, value in os.environ.items() if value})
    for env_var in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        configured = os.environ.get(env_var)
        if configured and not Path(configured).is_file():
            os.environ.pop(env_var, None)
    values.setdefault("FALKORDB_HOST", "127.0.0.1")
    values.setdefault("FALKORDB_PORT", "6379")
    values.setdefault("FALKORDB_ADMIN_USERNAME", "admin")
    values.setdefault("FALKORDB_READER_ACME_USERNAME", "reader_acme")
    values.setdefault("FALKORDB_READER_GLOBEX_USERNAME", "reader_globex")
    values.setdefault("AWS_REGION", "us-east-1")
    values.setdefault("BEDROCK_LLM_MODEL_ID", "us.amazon.nova-lite-v1:0")
    values.setdefault("BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
    return values


def public_error(error: BaseException, secret_values: dict[str, str]) -> str:
    message = "".join(traceback.format_exception(error, limit=0)).strip()
    for value in secret_values.values():
        if value and len(value) >= 4:
            message = message.replace(value, "[redacted]")
    return message[:1000]


def dependency_status(error: BaseException) -> Status:
    """Separate missing live prerequisites from a dependency that actually failed."""
    text = str(error).lower()
    blocked_markers = (
        "unable to locate credentials",
        "no credentials",
        "credentials could not be loaded",
        "accessdenied",
        "access denied",
        "expiredtoken",
        "invalidclienttokenid",
        "could not connect",
        "connection refused",
        "name or service not known",
        "not found: mcp executable",
        "not installed",
    )
    return "blocked" if any(marker in text for marker in blocked_markers) else "failed"


def valid_aws_mcp_evidence(payload: object) -> bool:
    """Require successful observed authenticated STS API evidence."""
    if not isinstance(payload, dict):
        return False
    if payload.get("status") != "passed" or payload.get("authenticated_transport") is not True:
        return False
    if not isinstance(payload.get("tools"), list) or not payload["tools"]:
        return False
    calls = payload.get("calls")
    if not isinstance(calls, list):
        return False
    for call in calls:
        if not isinstance(call, dict):
            continue
        result = call.get("result")
        if not isinstance(result, dict):
            continue
        structured = result.get("structuredContent")
        returned = structured.get("return_value") if isinstance(structured, dict) else None
        identity = returned.get("identity") if isinstance(returned, dict) else None
        if not identity:
            continue
        api_calls = structured.get("api_calls")
        if isinstance(api_calls, list) and any(
            isinstance(api_call, dict)
            and api_call.get("service") == "sts"
            and api_call.get("operation") == "GetCallerIdentity"
            and api_call.get("status") == "success"
            for api_call in api_calls
        ):
            return True
    return False


def result_rows(result: Any) -> list[list[Any]]:
    return list(getattr(result, "result_set", result))


class Probe:
    def __init__(self, config: dict[str, str]) -> None:
        self.config = config
        self.checks: list[Check] = []
        self.db: Any | None = None

    def record(self, name: str, status: Status, detail: str, **evidence: Any) -> None:
        self.checks.append(Check(name, status, detail, evidence))

    def run(self, name: str, action: Callable[[], dict[str, Any]]) -> None:
        try:
            self.record(name, "passed", "completed", **action())
        except ProbeBlocked as error:
            self.record(name, "blocked", public_error(error, self.config))
        except Exception as error:  # Each required live check must be reported.
            self.record(name, dependency_status(error), public_error(error, self.config))

    def require(self, key: str) -> str:
        value = self.config.get(key)
        if not value:
            raise ProbeBlocked(f"missing required setting {key}")
        return value

    def connect_admin(self) -> dict[str, Any]:
        from falkordb import FalkorDB

        self.db = FalkorDB(
            host=self.config["FALKORDB_HOST"],
            port=int(self.config["FALKORDB_PORT"]),
            username=self.config["FALKORDB_ADMIN_USERNAME"],
            password=self.require("FALKORDB_ADMIN_PASSWORD"),
        )
        graph = self.db.select_graph("probe_admin")
        rows = result_rows(graph.query("RETURN 1 AS value"))
        if rows != [[1]]:
            raise RuntimeError(f"unexpected ping result: {rows!r}")
        return {"host": self.config["FALKORDB_HOST"], "port": int(self.config["FALKORDB_PORT"])}

    def admin_graph(self) -> Any:
        if self.db is None:
            raise ProbeBlocked("admin database connection did not succeed")
        return self.db.select_graph("probe_admin")
    def reset_graph(self, name: str = "probe_admin") -> Any:
        if self.db is None:
            raise ProbeBlocked("admin database connection did not succeed")
        graph = self.db.select_graph(name)
        try:
            graph.delete()
        except Exception:
            pass
        return self.db.select_graph(name)


    def probe_ro_query_rejection(self) -> dict[str, Any]:
        try:
            self.admin_graph().ro_query("CREATE (:ProbeReadOnly {id:'must-not-write'})")
        except Exception:
            return {"graph_command": "GRAPH.RO_QUERY", "write_rejected": True}
        raise RuntimeError("GRAPH.RO_QUERY accepted a write")

    def probe_parameter_safety(self) -> dict[str, Any]:
        graph = self.reset_graph("probe_admin")
        payload = "probe'; MATCH (n) DETACH DELETE n //"
        graph.query("CREATE (:ProbeParameter {value: $value})", {"value": payload})
        rows = result_rows(graph.ro_query("MATCH (n:ProbeParameter) RETURN n.value"))
        if rows != [[payload]]:
            raise RuntimeError("parameter payload did not remain literal data")
        fixed_rows = result_rows(graph.ro_query("UNWIND range(1, 5) AS n RETURN n ORDER BY n SKIP 1 LIMIT 2"))
        if fixed_rows != [[2], [3]]:
            raise RuntimeError(f"literal LIMIT/SKIP returned {fixed_rows!r}")
        behavior: dict[str, Any] = {}
        for clause, params, expected in (
            ("LIMIT $limit", {"limit": 2}, [[1], [2]]),
            ("SKIP $skip", {"skip": 1}, [[2], [3], [4], [5]]),
        ):
            try:
                actual = result_rows(graph.ro_query(
                    f"UNWIND range(1, 5) AS n RETURN n ORDER BY n {clause}", params))
            except Exception as error:
                if "parameter" not in str(error).lower():
                    raise
                behavior[clause] = {"supported": False, "reason": str(error)}
            else:
                if actual != expected:
                    raise RuntimeError(f"{clause} returned incorrect rows: {actual!r}")
                behavior[clause] = {"supported": True, "rows": actual}
        return {"payload_preserved": True, "literal_rows": fixed_rows, "parameterized_behavior": behavior}

    def probe_algorithms(self) -> dict[str, Any]:
        graph = self.reset_graph("probe_admin")
        graph.query(
            "UNWIND $nodes AS id CREATE (:ProbeNode {id:id})",
            {"nodes": ["a", "b", "c"]},
        )
        graph.query(
            "MATCH (a:ProbeNode {id:'a'}), (b:ProbeNode {id:'b'}), (c:ProbeNode {id:'c'}) "
            "CREATE (a)-[:ROUTE {latency_ms:1, cost:2}]->(b), "
            "(b)-[:ROUTE {latency_ms:1, cost:2}]->(c), "
            "(a)-[:ROUTE {latency_ms:4, cost:1}]->(c)"
        )
        paths = result_rows(
            graph.ro_query(
                "MATCH (s:ProbeNode {id:'a'}), (t:ProbeNode {id:'c'}) "
                "CALL algo.SPpaths({sourceNode:s,targetNode:t,relTypes:['ROUTE'],"
                "weightProp:'latency_ms',costProp:'cost',maxCost:4,maxLen:3,"
                "relDirection:'outgoing',pathCount:5}) "
                "YIELD path,pathWeight,pathCost RETURN pathWeight,pathCost"
            )
        )
        if [2, 4] not in paths:
            raise RuntimeError(f"SPpaths did not return the bounded two-hop path: {paths!r}")
        centrality = result_rows(
            graph.ro_query(
                "CALL algo.betweenness({nodeLabels:['ProbeNode'],relationshipTypes:['ROUTE'],"
                "samplingSize:32,samplingSeed:42}) YIELD node,score "
                "RETURN node.id,score ORDER BY node.id"
            )
        )
        if len(centrality) != 3:
            raise RuntimeError(f"betweenness result did not cover fixture: {centrality!r}")
        return {"sppaths": paths, "betweenness": centrality}

    def reader_db(self, tenant: str) -> Any:
        from falkordb import FalkorDB

        upper = tenant.upper()
        return FalkorDB(
            host=self.config["FALKORDB_HOST"],
            port=int(self.config["FALKORDB_PORT"]),
            username=self.config[f"FALKORDB_READER_{upper}_USERNAME"],
            password=self.require(f"FALKORDB_READER_{upper}_PASSWORD"),
        )

    def probe_acl_isolation(self) -> dict[str, Any]:
        acme = self.reader_db("acme")
        globex = self.reader_db("globex")
        # Both own graph reads must work before a denial is meaningful.
        for graph_name in ("probe_acme", "probe_globex"):
            self.db.select_graph(graph_name).query("MERGE (:ProbeAcl {id:'readable'})")
        result_rows(acme.select_graph("probe_acme").ro_query("MATCH (n:ProbeAcl) RETURN n.id"))
        result_rows(globex.select_graph("probe_globex").ro_query("MATCH (n:ProbeAcl) RETURN n.id"))
        denials: list[str] = []
        for name, action in (
            (
                "acme_write",
                lambda: acme.select_graph("probe_acme").query(
                    "UNWIND [] AS ignored CREATE (:ProbeAcl {id:'must-not-write'})"
                ),
            ),
            (
                "acme_foreign_read",
                lambda: acme.select_graph("probe_globex").ro_query("RETURN 1"),
            ),
            (
                "globex_foreign_read",
                lambda: globex.select_graph("probe_acme").ro_query("RETURN 1"),
            ),
        ):
            try:
                action()
            except Exception as error:
                if "permission" not in str(error).lower() and "noperm" not in str(error).lower():
                    raise
                denials.append(name)
        if len(denials) != 3:
            raise RuntimeError(f"ACL operation unexpectedly succeeded; denied={denials!r}")
        return {"denied": denials}

    async def bedrock_embedding(self) -> list[float]:
        os.environ["AWS_REGION_NAME"] = self.config["AWS_REGION"]
        if self.config.get("AWS_PROFILE"):
            os.environ["AWS_PROFILE"] = self.config["AWS_PROFILE"]
        from graphrag_sdk import LiteLLMEmbedder

        embedder = LiteLLMEmbedder(
            model=f"bedrock/{self.config['BEDROCK_EMBED_MODEL_ID']}",
            dimensions=256,
            normalize=True,
            aws_region_name=self.config["AWS_REGION"],
            aws_profile_name=self.config.get("AWS_PROFILE"),
        )
        vector = await embedder.aembed_query("ChronosOps probe embedding")
        if len(vector) != 256:
            raise RuntimeError(f"Titan output dimension is {len(vector)}, expected 256")
        return vector

    def probe_bedrock_text_tool_embedding(self) -> dict[str, Any]:
        async def exercise() -> dict[str, Any]:
            os.environ["AWS_REGION_NAME"] = self.config["AWS_REGION"]
            if self.config.get("AWS_PROFILE"):
                os.environ["AWS_PROFILE"] = self.config["AWS_PROFILE"]
            from graphrag_sdk import LiteLLM
            from litellm import acompletion

            llm = LiteLLM(
                model=f"bedrock/{self.config['BEDROCK_LLM_MODEL_ID']}",
                temperature=0,
                max_tokens=64,
                aws_region_name=self.config["AWS_REGION"],
                aws_profile_name=self.config.get("AWS_PROFILE"),
            )
            text = await llm.ainvoke("Reply with the single word chronosops.")
            if not getattr(text, "content", "").strip():
                raise RuntimeError("Bedrock text response was empty")
            tool_response = await acompletion(
                model=f"bedrock/{self.config['BEDROCK_LLM_MODEL_ID']}",
                messages=[{"role": "user", "content": "Call the health tool."}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "health",
                        "description": "Report probe health.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }],
                tool_choice={"type": "function", "function": {"name": "health"}},
                max_tokens=64,
                temperature=0,
                aws_region_name=self.config["AWS_REGION"],
                aws_profile_name=self.config.get("AWS_PROFILE"),
            )
            calls = tool_response.choices[0].message.tool_calls
            if not calls:
                raise RuntimeError("Bedrock tool response contained no tool call")
            vector = await self.bedrock_embedding()
            return {"text_received": True, "tool_calls": len(calls), "embedding_dimension": len(vector)}

        return asyncio.run(exercise())

    def probe_vector_index(self) -> dict[str, Any]:
        vector = asyncio.run(self.bedrock_embedding())
        graph = self.reset_graph("probe_admin")
        graph.query("CREATE (:ProbeVector {id:$id, embedding:vecf32($embedding)})", {"id": "titan-256", "embedding": vector})
        graph.query(
            "CREATE VECTOR INDEX FOR (n:ProbeVector) ON (n.embedding) "
            "OPTIONS {dimension:256, similarityFunction:'cosine'}"
        )
        rows = result_rows(
            graph.ro_query(
                "CALL db.idx.vector.queryNodes('ProbeVector','embedding',1,vecf32($embedding)) "
                "YIELD node,score RETURN node.id,score",
                {"embedding": vector},
            )
        )
        if not rows or rows[0][0] != "titan-256":
            raise RuntimeError(f"256-dimension vector query did not retrieve probe node: {rows!r}")
        return {"embedding_dimension": len(vector), "top_result": rows[0][0]}

    def probe_mcp(self) -> dict[str, Any]:
        package_file = ROOT / "mcp" / "node_modules" / "@falkordb" / "mcpserver" / "package.json"
        if not package_file.is_file():
            raise ProbeBlocked(f"official MCP package metadata is unavailable at {package_file}")
        metadata = json.loads(package_file.read_text(encoding="utf-8"))
        bin_map = metadata.get("bin")
        if not isinstance(bin_map, dict) or len(bin_map) != 1:
            raise RuntimeError("official MCP package has an unexpected bin declaration")
        bin_name = next(iter(bin_map))
        command = ROOT / "mcp" / "node_modules" / ".bin" / bin_name
        if not command.is_file():
            raise ProbeBlocked(f"MCP executable not installed: {command}")

        async def exercise() -> dict[str, Any]:
            from mcp import Client, StdioServerParameters

            env = {
                "FALKORDB_HOST": self.config["FALKORDB_HOST"],
                "FALKORDB_PORT": self.config["FALKORDB_PORT"],
                "FALKORDB_USERNAME": self.config["FALKORDB_READER_ACME_USERNAME"],
                "FALKORDB_PASSWORD": self.require("FALKORDB_READER_ACME_PASSWORD"),
                "FALKORDB_DEFAULT_READONLY": "true",
                "FALKORDB_STRICT_READONLY": "false",
            }
            params = StdioServerParameters(command=str(command), env={**os.environ, **env})
            async with Client(params, mode="legacy", read_timeout_seconds=15) as client:
                listing = await client.list_tools(cache_mode="bypass")
                schemas = {tool.name: tool.input_schema for tool in listing.tools}
                readonly = schemas.get("query_graph_readonly")
                writer = schemas.get("query_graph")
                if not readonly or not writer or "params" not in readonly.get("properties", {}):
                    raise RuntimeError("official MCP tool schema lacks query_graph_readonly params or query_graph")
                read = await client.call_tool(
                    "query_graph_readonly",
                    {"graphName": "probe_acme", "query": "MATCH (n:ProbeAcl) RETURN n.id", "params": {}},
                )
                if read.is_error:
                    raise RuntimeError("reader MCP session could not execute an allowed read")
                write = await client.call_tool(
                    "query_graph",
                    {
                        "graphName": "probe_acme",
                        "query": "UNWIND [] AS ignored CREATE (:ProbeMcp {id:'must-not-write'})",
                        "params": {},
                        "readOnly": False,
                    },
                )
                if not write.is_error:
                    raise RuntimeError("reader MCP session accepted a write")
                return {
                    "package": metadata.get("name"),
                    "version": metadata.get("version"),
                    "bin": bin_name,
                    "tool_count": len(schemas),
                    "query_graph_readonly_schema": readonly,
                    "query_graph_schema": writer,
                }

        return asyncio.run(exercise())

    def probe_sdk_required_contract(self) -> dict[str, Any]:
        """Confirm the pinned official SDK exposes the required Module 3 API."""
        try:
            from graphrag_sdk import Attribute, Entity, GraphRAG, Ontology, Relation
        except ModuleNotFoundError as error:
            raise ProbeBlocked(f"Pinned SDK dependency unavailable: {error.name}") from error
        except ImportError as error:
            raise RuntimeError(
                "SDK API mismatch: graphrag-sdk must export Ontology, Entity, Relation, Attribute, and GraphRAG"
            ) from error
        expected = {"connection", "llm", "embedder", "ontology", "embedding_dimension", "enable_cypher"}
        actual = set(inspect.signature(GraphRAG).parameters)
        missing = sorted(expected - actual)
        if missing:
            raise RuntimeError(
                "SDK API mismatch: pinned GraphRAG constructor lacks required Module 3 parameter(s) "
                f"{missing}."
            )
        Ontology(
            entities=[Entity(label="ProbeService", properties=[Attribute(name="service_id", type="STRING")])],
            relations=[Relation(label="PROBE_RELATES", patterns=[("ProbeService", "ProbeService")])],
        )
        return {"required_parameters": sorted(expected)}

    def probe_sdk(self) -> dict[str, Any]:
        """Exercise the pinned SDK ontology, ingestion, update, and citation lifecycle."""
        async def exercise() -> dict[str, Any]:
            try:
                from graphrag_sdk import Attribute, ConnectionConfig, Entity, GraphRAG, LiteLLM, LiteLLMEmbedder, Ontology, Relation
                from graphrag_sdk.ingestion.extraction_strategies.entity_extractors import LLMExtractor
                from graphrag_sdk.ingestion.extraction_strategies.graph_extraction import GraphExtraction
            except ImportError as error:
                raise RuntimeError(
                    "SDK API mismatch: graphrag-sdk must export Ontology, Entity, Relation, Attribute, GraphRAG, "
                    "ConnectionConfig, LiteLLM, and LiteLLMEmbedder"
                ) from error

            graph = "probe_sdk"
            ontology = Ontology(
                entities=[Entity(label="ProbeService", properties=[Attribute(name="service_id", type="STRING")])],
                relations=[Relation(label="PROBE_RELATES", patterns=[("ProbeService", "ProbeService")])],
            )
            llm = LiteLLM(
                model=f"bedrock/{self.config['BEDROCK_LLM_MODEL_ID']}",
                temperature=0,
                max_tokens=128,
                aws_region_name=self.config["AWS_REGION"],
                aws_profile_name=self.config.get("AWS_PROFILE"),
            )
            embedder = LiteLLMEmbedder(
                model=f"bedrock/{self.config['BEDROCK_EMBED_MODEL_ID']}",
                dimensions=256,
                aws_region_name=self.config["AWS_REGION"],
                aws_profile_name=self.config.get("AWS_PROFILE"),
            )
            connection = ConnectionConfig(
                host=self.config["FALKORDB_HOST"],
                port=int(self.config["FALKORDB_PORT"]),
                username=self.config["FALKORDB_ADMIN_USERNAME"],
                password=self.require("FALKORDB_ADMIN_PASSWORD"),
                graph_name=graph,
                query_timeout_ms=3_000,
            )
            async with GraphRAG(
                connection=connection,
                llm=llm,
                embedder=embedder,
                ontology=ontology,
                embedding_dimension=256,
                enable_cypher=False,
            ) as rag:
                extractor = GraphExtraction(llm=llm, entity_extractor=LLMExtractor(llm=llm))
                await rag.ingest(text="Probe Service service_id probe-service is monitored.", document_id="probe-source", extractor=extractor)
                await rag.finalize(resolve=False, judge=False)
                await rag.update(text="Probe Service service_id probe-service was updated.", document_id="probe-source", extractor=extractor)
                citations = await rag.query(
                    "MATCH (d:Document)-[:PART_OF]->(c:Chunk) RETURN d.id,c.id,c.text LIMIT 5"
                )
                if not citations:
                    raise RuntimeError("SDK ingestion produced no Document-to-Chunk citation path")
                if not any("updated" in str(row[2]) for row in citations):
                    raise RuntimeError("SDK update did not replace citation text")
                return {
                    "ontology_contract": "Ontology/Entity/Relation/Attribute",
                    "citation_rows": len(citations),
                    "citation_ids": [[str(row[0]), str(row[1])] for row in citations],
                }

        return asyncio.run(exercise())

    def probe_aws_mcp_evidence(self) -> dict[str, Any]:
        artifact = ROOT / "artifacts" / "aws-mcp.json"
        if not artifact.is_file():
            raise ProbeBlocked("missing parent-produced artifacts/aws-mcp.json")
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        if not valid_aws_mcp_evidence(payload):
            raise ProbeBlocked("AWS MCP report lacks a successful authenticated STS GetCallerIdentity API call")
        return {"artifact": str(artifact.relative_to(ROOT)), "tool_count": len(payload["tools"])}

    def cleanup(self) -> None:
        if self.db is None:
            return
        for graph_name in PROBE_GRAPHS:
            try:
                self.db.select_graph(graph_name).delete()
            except Exception as error:
                if "Invalid graph operation on empty key" not in str(error):
                    self.record(f"cleanup_{graph_name}", "failed", public_error(error, self.config))

    def execute(self, *, local_only: bool = False) -> dict[str, Any]:
        self.run("database_ping", self.connect_admin)
        self.run("graph_ro_query_write_rejection", self.probe_ro_query_rejection)
        self.run("parameter_safety", self.probe_parameter_safety)
        self.run("algorithms_sppaths_and_betweenness", self.probe_algorithms)
        self.run("tenant_acl_isolation", self.probe_acl_isolation)
        if local_only:
            self.record("bedrock_text_tool_titan256", "blocked", "Local-only diagnostic; live AWS gate remains required")
            self.record("titan256_vector_index_query", "blocked", "Requires a live Titan embedding")
        else:
            self.run("bedrock_text_tool_titan256", self.probe_bedrock_text_tool_embedding)
            if self.checks[-1].status == "passed":
                self.run("titan256_vector_index_query", self.probe_vector_index)
            else:
                self.record("titan256_vector_index_query", "blocked", "Bedrock prerequisite failed; do not retry inference denial")
        self.run("official_mcp_reader_session", self.probe_mcp)
        self.run("graphrag_required_api_contract", self.probe_sdk_required_contract)
        if local_only or not any(c.name == "bedrock_text_tool_titan256" and c.status == "passed" for c in self.checks):
            self.record("graphrag_ontology_ingest_update_citation", "blocked", "Requires verified Bedrock inference")
        else:
            self.run("graphrag_ontology_ingest_update_citation", self.probe_sdk)
        self.run("aws_mcp_observed_discovery", self.probe_aws_mcp_evidence)
        self.cleanup()
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "required_live_checks_passed": all(check.status == "passed" for check in self.checks),
            "checks": [asdict(check) for check in self.checks],
        }
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-only", action="store_true",
                        help="Exercise local checks without retrying a known AWS blocker; never passes the live gate")
    args = parser.parse_args()
    probe = Probe(settings())
    try:
        report = probe.execute(local_only=args.local_only)
    except Exception as error:  # A report must still exist for an unexpected harness failure.
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "required_live_checks_passed": False,
            "checks": [asdict(check) for check in probe.checks]
            + [asdict(Check("probe_harness", "failed", public_error(error, probe.config)))],
            "traceback": traceback.format_exc(limit=5),
        }
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["required_live_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
