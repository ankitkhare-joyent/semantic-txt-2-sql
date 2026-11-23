"""The semantic layer as an MCP server, so any MCP client (Claude Code, Claude Desktop,
another agent framework) gets the same governed metrics our own agent uses.

    claude mcp add fleet-metrics -- uv --directory ~/workstuff/semantic-txt-2-sql run python -m app.mcp_server

It logs in to OpenBao with the persona in SL_PERSONA and calls Cube with that persona's
identity token, so row-level security follows the MCP client too. Raw SQL is deliberately
NOT exposed here: an external client gets governed metrics only.
"""
import json
import logging
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .bao import OpenBao
from .config import Settings
from .semantic import CubeClient, SemanticQueryError

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

settings = Settings()
bao = OpenBao(settings.bao_addr, settings.approle_dir)
cube = CubeClient(settings.cube_url, bao.identity_token)

mcp = MCPServer(
    name="fleet-metrics",
    title="EC2 fleet metrics (semantic layer)",
    version="1.0.0",
    instructions=("Governed EC2 fleet metrics from 2-hourly snapshots (Apr-Sep 2026). Read the "
                  "semantic://catalog resource first, then call query_metrics with exact member names. "
                  "Use running_instances only with the latest_snapshot segment or a single snapshot; use the "
                  "*_hours and estimated_cost_usd measures for time ranges."),
)


@mcp.resource("semantic://catalog", title="Semantic layer catalog", mime_type="text/markdown")
def catalog() -> str:
    """Every view, measure, dimension and segment, with what each one means."""
    return cube.catalog_text()


@mcp.tool(annotations=READ_ONLY, title="Query governed metrics")
def query_metrics(
    measures: Annotated[list[str] | None, Field(description="e.g. ['fleet.estimated_cost_usd']")] = None,
    dimensions: Annotated[list[str] | None, Field(description="group-by members, e.g. ['fleet.team']")] = None,
    segments: Annotated[list[str] | None, Field(description="e.g. ['fleet.latest_snapshot']")] = None,
    date_range: Annotated[list[str] | None, Field(description="[from, to] inclusive, YYYY-MM-DD, on fleet.snapshot_ts")] = None,
    granularity: Literal["hour", "day", "week", "month", "quarter", "year"] | None = None,
    filters: Annotated[list[dict] | None, Field(description="[{member, operator, values}], operators: equals, notEquals, contains, gt, gte, lt, lte, set, notSet")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
) -> str:
    """Run a semantic-layer query. Cube compiles it to SQL with the business rules and your row-level security."""
    query: dict = {"measures": measures, "dimensions": dimensions, "segments": segments,
                   "filters": filters, "limit": limit}
    if date_range or granularity:
        td = {"dimension": "fleet.snapshot_ts"}
        if date_range:
            td["dateRange"] = date_range
        if granularity:
            td["granularity"] = granularity
        query["timeDimensions"] = [td]
    query = {k: v for k, v in query.items() if v not in ([], None)}
    try:
        rows = cube.load(query).get("data", [])
    except SemanticQueryError as e:
        raise ToolError(str(e)) from None
    return json.dumps({"rows": rows, "row_count": len(rows)}, default=str)


@mcp.tool(annotations=READ_ONLY, title="List a dimension's values")
def dimension_values(dimension: str, contains: str | None = None) -> str:
    """Distinct values of one dimension, to map the user's words to real values (e.g. 'checkout' -> 'checkout-api')."""
    query = {"dimensions": [dimension], "limit": 100}
    if contains:
        query["filters"] = [{"member": dimension, "operator": "contains", "values": [contains]}]
    try:
        rows = cube.load(query).get("data", [])
    except SemanticQueryError as e:
        raise ToolError(str(e)) from None
    return json.dumps([r.get(dimension) for r in rows])


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    mcp.run("stdio")
