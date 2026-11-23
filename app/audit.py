"""Audit log of every query the agent runs, written with dynamic Postgres credentials.

The writer holds no password. It asks OpenBao for one when it first needs it, gets a
brand-new Postgres user with a 1 hour lease, swaps to a fresh one before that lease
ends, and revokes it on close so the user is dropped immediately rather than at expiry.
Each row records which dynamic user wrote it (column db_user).
"""
import json
import logging

import psycopg

from .bao import DbLease, OpenBao

log = logging.getLogger("app.audit")


class AuditLog:
    def __init__(self, bao: OpenBao, host: str, port: int, dbname: str = "audit", role: str = "audit-writer"):
        self.bao, self.host, self.port, self.dbname, self.role = bao, host, port, dbname, role
        self._lease: DbLease | None = None
        self._conn: psycopg.Connection | None = None

    def _connection(self) -> psycopg.Connection:
        if self._lease is None or self._lease.expiring():
            self.close()
            self._lease = self.bao.database_creds(self.role)
            self._conn = psycopg.connect(host=self.host, port=self.port, dbname=self.dbname, autocommit=True,
                                         user=self._lease.username, password=self._lease.password, connect_timeout=5)
            log.info("audit db: new dynamic user %s", self._lease.username)
        return self._conn

    def record(self, *, persona: str, mode: str, question: str, tool: str, query: dict,
               generated_sql: str | None, row_count: int | None, error: str | None, duration_ms: int) -> None:
        try:
            self._connection().execute(
                """INSERT INTO agent_queries (persona, mode, question, tool, query, generated_sql,
                                              row_count, error, duration_ms)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (persona, mode, question, tool, json.dumps(query), generated_sql, row_count, error, duration_ms))
        except Exception:  # never fail the user's question because the audit store is down
            log.exception("audit write failed")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._lease is not None:
            try:
                self.bao.revoke(self._lease.lease_id)
            except Exception:  # noqa: BLE001 - best effort; the lease expires anyway
                log.warning("could not revoke lease %s; it expires on its own", self._lease.lease_id)
            self._lease = None
