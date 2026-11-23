"""Direct Trino access for the raw text-to-SQL path.

The agent connects as its persona's Trino user, so Trino's file-based access control
(stack/trino/etc/rules.json) applies its row filters. Locally Trino trusts the user name it
is told; in production Trino authenticates it (JWT / OAuth2 over TLS - it can verify the
same OpenBao identity token Cube does) so an agent cannot claim to be someone else.
"""
import time

import trino

from .sqlguard import check


class TrinoRunner:
    def __init__(self, host: str, port: int, user: str, allowed_tables, max_rows: int):
        self._conn = trino.dbapi.connect(host=host, port=port, user=user, catalog="lake", schema="ec2",
                                         session_properties={"query_max_execution_time": "60s"},
                                         source="semantic-lab-agent")
        self.allowed_tables = allowed_tables
        self.max_rows = max_rows

    def run(self, sql: str) -> dict:
        checked = check(sql, self.allowed_tables, self.max_rows)
        t0 = time.perf_counter()
        cur = self._conn.cursor()
        cur.execute(checked.sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return {"sql": checked.sql, "columns": cols, "rows": [list(r) for r in rows],
                "row_count": len(rows), "elapsed_ms": round((time.perf_counter() - t0) * 1000)}

    def schema_text(self) -> str:
        """Tables, columns, types and the column docs from the Iceberg schema.

        This is all the raw-SQL agent knows about the data: the same documentation a
        careful data engineer leaves in the table schema, but none of the business rules
        the semantic layer encodes.
        """
        cur = self._conn.cursor()
        lines = []
        for table in self.allowed_tables:
            catalog, schema, name = table.split(".")
            cur.execute(f'SHOW COLUMNS FROM {catalog}.{schema}."{name}"')   # column, type, extra, comment
            lines.append(f"## {table}")
            for col, typ, _extra, comment in cur.fetchall():
                lines.append(f"- {col} {typ}" + (f" -- {comment}" if comment else ""))
        return "\n".join(lines)
