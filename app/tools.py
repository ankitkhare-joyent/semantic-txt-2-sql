"""The tools the model can call, per mode.

  semantic  query_metrics + dimension_values   (Cube: governed, compiled SQL)
  sql       run_sql                            (Trino: model-written SQL behind sqlguard)
  hybrid    all three; prefer the semantic layer, fall back to SQL and say so

Tool design rules that matter more than the model choice:
  * One tool per intent, with a description that says when to use it and when not.
  * Errors come back as tool results (is_error), worded so the model can fix the call.
  * Results are capped (max_rows) so one bad query can't flood the context window.
"""
import json
import logging
import time
from dataclasses import dataclass, field

from .semantic import CubeClient, SemanticQueryError
from .sqlguard import UnsafeSQL

log = logging.getLogger("app.tools")

QUERY_METRICS = {
    "name": "query_metrics",
    "description": (
        "Query the governed semantic layer (Cube). Pick measures, dimensions and segments by their exact "
        "names from the catalog in your instructions; Cube writes the SQL, applies business rules and the "
        "caller's row-level security. Use this for every question the catalog can express."),
    "input_schema": {
        "type": "object",
        "properties": {
            "measures": {"type": "array", "items": {"type": "string"}, "description": "e.g. fleet.running_instance_hours"},
            "dimensions": {"type": "array", "items": {"type": "string"}, "description": "group-by members, e.g. fleet.team"},
            "segments": {"type": "array", "items": {"type": "string"}, "description": "predefined filters, e.g. fleet.latest_snapshot"},
            "timeDimensions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dimension": {"type": "string", "description": "a time dimension, e.g. fleet.snapshot_ts"},
                        "dateRange": {"description": "['2026-06-01', '2026-06-30'] (inclusive) or a phrase like 'last 7 days'",
                                      "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                        "granularity": {"type": "string", "enum": ["hour", "day", "week", "month", "quarter", "year"],
                                        "description": "omit to aggregate over the whole range"},
                    },
                    "required": ["dimension"],
                },
            },
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "member": {"type": "string"},
                        "operator": {"type": "string", "enum": ["equals", "notEquals", "contains", "notContains",
                                                                "gt", "gte", "lt", "lte", "set", "notSet"]},
                        "values": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["member", "operator"],
                },
            },
            "order": {"type": "object", "additionalProperties": {"type": "string", "enum": ["asc", "desc"]},
                      "description": "e.g. {\"fleet.estimated_cost_usd\": \"desc\"}"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    },
}

DIMENSION_VALUES = {
    "name": "dimension_values",
    "description": ("List the distinct values of one dimension (optionally only those containing a substring). "
                    "Use it to map the user's words to real values before filtering, e.g. 'checkout' -> 'checkout-api'."),
    "input_schema": {
        "type": "object",
        "properties": {"dimension": {"type": "string"}, "contains": {"type": "string"}},
        "required": ["dimension"],
    },
}

RUN_SQL = {
    "name": "run_sql",
    "description": ("Run one read-only Trino SQL query against the tables in your instructions. "
                    "Results are capped at 200 rows. Use fully qualified names like lake.ec2.instances."),
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string"}, "purpose": {"type": "string", "description": "one line: what this query checks"}},
        "required": ["sql"],
    },
}

SUBMIT_ANSWER = {
    "name": "submit_answer",
    "description": "Give your final answer. Call exactly once, after you have the data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "description": "the answer for the user, one to three sentences"},
            "value": {"type": ["number", "null"],
                      "description": "the single number that answers the question, unrounded; null if it is a list or unanswerable"},
            "unit": {"type": ["string", "null"], "description": "e.g. instances, hours, USD, percent"},
        },
        "required": ["answer", "value"],
    },
}

TOOLS_BY_MODE = {
    "semantic": [QUERY_METRICS, DIMENSION_VALUES, SUBMIT_ANSWER],
    "sql": [RUN_SQL, SUBMIT_ANSWER],
    "hybrid": [QUERY_METRICS, DIMENSION_VALUES, RUN_SQL, SUBMIT_ANSWER],
}


@dataclass
class ToolOutcome:
    content: str                 # what the model sees
    is_error: bool = False
    audit: dict = field(default_factory=dict)   # query / generated_sql / row_count for the audit log


class Toolbox:
    def __init__(self, mode: str, cube: CubeClient | None, trino, max_rows: int):
        self.mode, self.cube, self.trino, self.max_rows = mode, cube, trino, max_rows
        self.definitions = TOOLS_BY_MODE[mode]

    def execute(self, name: str, args: dict) -> ToolOutcome:
        t0 = time.perf_counter()
        try:
            if name == "query_metrics":
                out = self._query_metrics(args)
            elif name == "dimension_values":
                out = self._dimension_values(args)
            elif name == "run_sql":
                out = self._run_sql(args)
            else:
                out = ToolOutcome(f"Unknown tool {name}", is_error=True)
        except (SemanticQueryError, UnsafeSQL) as e:
            out = ToolOutcome(str(e), is_error=True, audit={"query": args, "error": str(e)})
        except Exception as e:  # noqa: BLE001 - engine errors (bad column, type mismatch...) go back to the model
            msg = f"{type(e).__name__}: {str(e)[:600]}"
            out = ToolOutcome(msg, is_error=True, audit={"query": args, "error": msg})
        out.audit["duration_ms"] = round((time.perf_counter() - t0) * 1000)
        return out

    # ------------------------------------------------------------------ semantic
    def _query_metrics(self, args: dict) -> ToolOutcome:
        query = {k: v for k, v in args.items() if v not in (None, [], {})}
        query["limit"] = min(int(query.get("limit", self.max_rows)), self.max_rows)
        result = self.cube.load(query)
        rows = result.get("data", [])
        generated = None
        try:
            generated = self.cube.sql(query)
        except SemanticQueryError:
            log.warning("could not fetch generated SQL for audit", exc_info=True)
        used = list((result.get("usedPreAggregations") or {}).keys())
        payload = {"rows": rows, "row_count": len(rows), "truncated": len(rows) >= query["limit"]}
        if used:
            payload["served_from_pre_aggregation"] = True
        return ToolOutcome(_dump(payload), audit={"query": query, "generated_sql": generated,
                                                  "row_count": len(rows), "pre_aggregations": used})

    def _dimension_values(self, args: dict) -> ToolOutcome:
        dim = args["dimension"]
        query = {"dimensions": [dim], "order": {dim: "asc"}, "limit": 100}
        if args.get("contains"):
            query["filters"] = [{"member": dim, "operator": "contains", "values": [args["contains"]]}]
        rows = self.cube.load(query).get("data", [])
        values = [r.get(dim) for r in rows]
        return ToolOutcome(_dump({"dimension": dim, "values": values}),
                           audit={"query": query, "row_count": len(values)})

    # ------------------------------------------------------------------ raw SQL
    def _run_sql(self, args: dict) -> ToolOutcome:
        res = self.trino.run(args["sql"])
        payload = {"columns": res["columns"], "rows": res["rows"], "row_count": res["row_count"],
                   "truncated": res["row_count"] >= self.max_rows}
        return ToolOutcome(_dump(payload), audit={"query": {"sql": args["sql"]}, "generated_sql": res["sql"],
                                                  "row_count": res["row_count"]})


def _dump(obj) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))
