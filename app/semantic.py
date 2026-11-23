"""Cube REST client, shaped for an LLM caller.

The agent never writes SQL on this path. It picks members (measures, dimensions,
segments) from the catalog that /meta returns, and Cube compiles them into SQL that
already has the right joins, grain handling, business rules and row-level security.

Two details matter for agents:
  * validate() checks member names before calling Cube and suggests the closest real
    names. Models recover from "did you mean fleet.running_instance_hours?" in one step.
  * catalog_text() renders /meta as compact text for the system prompt: names, types
    and descriptions are the semantic layer's documentation, and the model reads them.
"""
import difflib
import time
from dataclasses import dataclass

import httpx


class SemanticQueryError(ValueError):
    """Raised with a message written for the model."""


@dataclass
class Member:
    name: str
    kind: str          # measure | dimension | segment
    type: str
    description: str


class CubeClient:
    def __init__(self, base_url: str, token_provider, timeout: float = 60):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._token = token_provider          # called per request: tokens expire in 15 min
        self._members: dict[str, Member] | None = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def _get(self, path: str, **params) -> dict:
        r = self._http.get(path, params=params, headers=self._headers())
        if r.status_code >= 400:
            raise SemanticQueryError(_cube_error(r))
        return r.json()

    # ------------------------------------------------------------------ catalog
    def members(self) -> dict[str, Member]:
        if self._members is None:
            out = {}
            for view in self._get("/meta")["cubes"]:
                if not view.get("public", True):
                    continue
                for kind in ("measures", "dimensions", "segments"):
                    for m in view.get(kind, []):
                        if m.get("public", True) is False:
                            continue
                        out[m["name"]] = Member(m["name"], kind[:-1], m.get("type", ""),
                                                " ".join((m.get("description") or "").split()))
            self._members = out
        return self._members

    def catalog_text(self) -> str:
        meta = self._get("/meta")["cubes"]
        lines = []
        for view in meta:
            lines.append(f"## view `{view['name']}`: {' '.join((view.get('description') or '').split())}")
            for kind in ("measures", "dimensions", "segments"):
                lines.append(f"### {kind}")
                for m in view.get(kind, []):
                    desc = " ".join((m.get("description") or "").split())
                    typ = f" ({m['type']})" if m.get("type") else ""
                    lines.append(f"- {m['name']}{typ}" + (f": {desc}" if desc else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------ queries
    def validate(self, query: dict) -> None:
        known = self.members()
        problems = []

        def need(name: str, kind: str):
            m = known.get(name)
            if m is None or m.kind != kind:
                pool = [k for k, v in known.items() if v.kind == kind]
                close = difflib.get_close_matches(name, pool, n=3, cutoff=0.5)
                hint = f" Did you mean: {', '.join(close)}?" if close else f" Valid {kind}s: {', '.join(pool)}."
                problems.append(f"Unknown {kind} '{name}'.{hint}")

        for name in query.get("measures", []):
            need(name, "measure")
        for name in query.get("dimensions", []):
            need(name, "dimension")
        for name in query.get("segments", []):
            need(name, "segment")
        for td in query.get("timeDimensions", []):
            need(td.get("dimension", ""), "dimension")
        for f in query.get("filters", []):
            if f.get("member") not in known:
                need(f.get("member", ""), "dimension")
        if not query.get("measures") and not query.get("dimensions"):
            problems.append("Ask for at least one measure or dimension.")
        if problems:
            raise SemanticQueryError(" ".join(problems))

    def load(self, query: dict, wait_seconds: float = 90) -> dict:
        """POST /load. Cube answers 'Continue wait' while a query runs; poll until done."""
        self.validate(query)
        deadline, delay = time.monotonic() + wait_seconds, 0.5
        while True:
            r = self._http.post("/load", json={"query": query}, headers=self._headers())
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and body.get("error") != "Continue wait":
                return body
            if body.get("error") != "Continue wait":
                raise SemanticQueryError(_cube_error(r))
            if time.monotonic() > deadline:
                raise SemanticQueryError(f"Query still running after {wait_seconds}s; narrow the date range.")
            time.sleep(delay)
            delay = min(delay * 2, 5)

    def sql(self, query: dict) -> str:
        """The SQL Cube generates for a query (compiled only, not run) - for audit and teaching."""
        body = self._get("/sql", query=_json(query))
        sql = body["sql"]["sql"]
        return sql[0] if isinstance(sql, list) else sql


def _json(obj) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"))


def _cube_error(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or r.text
    except ValueError:
        err = r.text
    if r.status_code == 403:
        return f"Access denied by the semantic layer: {err}"
    return f"Cube returned {r.status_code}: {str(err)[:500]}"
