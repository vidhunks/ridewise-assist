"""Validates SQL written by the LLM before it runs (agent role only).

Layer 1 (here): parse with sqlglot; exactly one read-only SELECT; only allow-listed tables;
                add a LIMIT.
Layer 2 (app/data/db.py): read-only connection + SQLite authorizer + timeout + row cap.
Either layer alone blocks writes; together they also block reading internal tables.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

ALLOWED_TABLES = {"customers", "vehicles", "bookings", "payments", "damage_reports", "booking_facts"}
MAX_ROWS = 200
_FORBIDDEN = tuple(getattr(exp, n) for n in (
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command", "Merge", "Pragma", "Attach",
    "Detach", "TruncateTable", "Transaction", "Commit", "Rollback") if hasattr(exp, n))


class SQLRejected(ValueError):
    """The query is not allowed. The message is safe to show to the model."""


def validate_select(sql: str, max_rows: int = MAX_ROWS) -> str:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise SQLRejected("Empty query.")
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise SQLRejected(f"Could not parse the SQL: {str(e).splitlines()[0]}") from e
    if len(statements) != 1:
        raise SQLRejected("Exactly one SELECT statement is allowed.")
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.SetOperation)) or stmt.find(*_FORBIDDEN):
        raise SQLRejected("Only read-only SELECT queries are allowed. Data cannot be changed from the assistant.")

    cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    for t in stmt.find_all(exp.Table):
        name = t.name.lower()
        if t.db and t.db.lower() != "main":
            raise SQLRejected(f"Schema-qualified table '{t.db}.{t.name}' is not allowed.")
        if name not in ALLOWED_TABLES and name not in cte_names:
            raise SQLRejected(f"Table '{t.name}' is not available. Allowed: {', '.join(sorted(ALLOWED_TABLES))}.")

    if isinstance(stmt, exp.Select) and not stmt.args.get("limit"):
        stmt = stmt.limit(max_rows)
    return stmt.sql(dialect="sqlite")
