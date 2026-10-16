"""Read-only operator probe using the configured official AWS MCP proxy."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def probe(args: argparse.Namespace) -> dict:
    report = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": "https://aws-mcp.us-east-1.api.aws/mcp",
        "profile": args.profile,
        "region": "us-east-1",
        "status": "blocked",
        "calls": [],
    }
    report["authenticated_transport"] = not args.public
    async with asyncio.timeout(180), AsyncExitStack() as stack:
        if args.public:
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(report["endpoint"]))
        else:
            from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
            read, write, _ = await stack.enter_async_context(
                aws_iam_streamablehttp_client(
                    endpoint=report["endpoint"], aws_service="aws-mcp",
                    aws_region="us-east-1", aws_profile=args.profile,
                    terminate_on_close=False))
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            report["server"] = init.serverInfo.model_dump()
            tools = await session.list_tools()
            report["tools"] = [{"name": t.name, "description": t.description,
                                "inputSchema": t.inputSchema} for t in tools.tools]
            if args.tool:
                if args.tool not in {t.name for t in tools.tools}:
                    raise ValueError("Requested tool not present in actual discovery")
                result = await session.call_tool(args.tool, json.loads(args.arguments))
                report["calls"].append({"tool": args.tool,
                                        "arguments": json.loads(args.arguments),
                                        "result": result.model_dump(mode="json")})
                structured = result.structuredContent or {}
                failed = result.isError or structured.get("status") in {"error", "failed"}
                report["status"] = "failed" if failed else "passed"
            else:
                report["status"] = "discovered"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "sai"))
    parser.add_argument("--public", action="store_true",
                        help="Discover public documentation tools without claiming AWS identity verification")
    parser.add_argument("--tool")
    parser.add_argument("--arguments", default="{}")
    parser.add_argument("--output", default="artifacts/aws-mcp-discovery.json")
    args = parser.parse_args()
    try:
        report = asyncio.run(probe(args))
    except Exception as exc:
        import traceback
        traceback.print_exception(exc)
        report = {"status": "blocked", "observed_at": datetime.now(timezone.utc).isoformat(),
                  "error": f"{type(exc).__name__}: {exc}"}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(path),
                      "tools": [t["name"] for t in report.get("tools", [])]}))
    return 0 if report["status"] in {"discovered", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
