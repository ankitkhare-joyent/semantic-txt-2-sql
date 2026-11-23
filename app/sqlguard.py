"""Guardrails for model-written SQL, applied BEFORE anything reaches Trino.

An LLM is an untrusted SQL author: it can be wrong, and a prompt-injected question can
make it hostile. So raw SQL is parsed (not regex-matched) and must pass every check:

  1. exactly one statement, and it is a query (SELECT / WITH / UNION ...)
  2. no DDL/DML anywhere in the tree (INSERT, DELETE, CREATE, ... even inside a CTE)
  3. every table it reads is on an allowlist (no system.*, no information_schema)
  4. a LIMIT no larger than max_rows (added if missing)

This is defence in depth, not the only line: Trino's own access control (read-only
catalog, row filters per user) still applies, because a parser can be fooled.
"""
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

DIALECT = "trino"
FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
             exp.Command, exp.TruncateTable, exp.Grant, exp.Set, exp.Use, exp.Copy)


class UnsafeSQL(ValueError):
    """Raised with a message written for the model, so it can fix its query and retry."""


@dataclass(frozen=True)
class Checked:
    sql: str            # the normalised SQL that will actually run
    tables: list[str]   # fully qualified tables it reads


def check(sql: str, allowed_tables, max_rows: int, default_catalog="lake", default_schema="ec2") -> Checked:
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQL(f"SQL does not parse as Trino SQL: {e.errors[0]['description'] if e.errors else e}") from None
    if len(statements) != 1:
        raise UnsafeSQL(f"Send exactly one statement (got {len(statements)}).")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise UnsafeSQL("Only read queries (SELECT / WITH ... SELECT) are allowed.")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN):
            raise UnsafeSQL(f"{node.key.upper()} is not allowed; this tool only reads.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    allowed = {t.lower() for t in allowed_tables}
    tables = []
    for table in tree.find_all(exp.Table):
        if not table.catalog and not table.db and table.name.lower() in cte_names:
            continue
        fq = ".".join([table.catalog or default_catalog, table.db or default_schema, table.name]).lower()
        if fq not in allowed:
            raise UnsafeSQL(f"Table {fq} is not available. You can read: {', '.join(sorted(allowed))}.")
        tables.append(fq)
    if not tables:
        raise UnsafeSQL("The query reads no table.")

    limit = tree.args.get("limit")
    current = None
    if limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.is_int:
        current = int(limit.expression.this)
    if current is None or current > max_rows:
        tree = tree.limit(max_rows, copy=False)
    return Checked(sql=tree.sql(dialect=DIALECT), tables=sorted(set(tables)))
