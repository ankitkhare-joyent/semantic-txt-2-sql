"""Talk to the semantic layer's MCP server the way an MCP client (Claude Code, Claude Desktop) does.

    mise run -q lab:mcp                         # list tools and resources, call query_metrics once
    SL_PERSONA=agent-data mise run -q lab:mcp   # the same, as the data team

Starts app/mcp_server.py as a subprocess over stdio, then speaks MCP to it. No Claude call.
"""
import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

QUERY = {"measures": ["fleet.running_instances"], "dimensions": ["fleet.team"], "segments": ["fleet.latest_snapshot"]}


async def main() -> None:
    params = StdioServerParameters(command="uv", args=["run", "python", "-m", "app.mcp_server"], env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        print("server:", init.server_info.name, "| instructions:", (init.instructions or "")[:90], "…")
        print("tools:", [t.name for t in (await s.list_tools()).tools])
        print("resources:", [str(x.uri) for x in (await s.list_resources()).resources])
        res = await s.call_tool("query_metrics", QUERY)
        print("query_metrics:", json.loads(res.content[0].text)["rows"])
        bad = await s.call_tool("query_metrics", {"measures": ["fleet.cost"]})
        print("bad call: isError =", bad.is_error, "|", bad.content[0].text[:120])


if __name__ == "__main__":
    asyncio.run(main())
