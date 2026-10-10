"""Database access. Two databases, two very different levels of trust:

* source  - the provided data/ridewise.db. Opened immutable + read-only, so SQLite can
            never write to it or create -wal/-shm side files. Contains raw PII; only
            used by the build script and by narrowly scoped lookups (a customer's own record).
* derived - data/derived.db, built from source by scripts/build_derived.py. Cleaned and
            masked; this is the only database the LLM's SQL ever runs against.
            Opened read-only with an authorizer that allows SELECT and nothing else.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.config import ROOT

SOURCE_DB = ROOT / "data" / "ridewise.db"
DERIVED_DB = ROOT / "data" / "derived.db"

# Statement types a read-only query may need (see sqlite3 authorizer action codes).
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _ro_uri(path: Path, immutable: bool) -> str:
    uri = path.resolve().as_uri() + "?mode=ro"
    return uri + "&immutable=1" if immutable else uri


def open_source() -> sqlite3.Connection:
    """Read-only, immutable connection to the provided database (never modifies the file)."""
    if not SOURCE_DB.exists():
        raise FileNotFoundError(f"Provided database not found at {SOURCE_DB}")
    conn = sqlite3.connect(_ro_uri(SOURCE_DB, immutable=True), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _authorizer(action, arg1, arg2, db_name, trigger):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def open_derived(path: Path = DERIVED_DB) -> sqlite3.Connection:
    """Read-only connection to the cleaned database. Only SELECT statements are permitted."""
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing. Run: python -m scripts.build_derived")
    conn = sqlite3.connect(_ro_uri(path, immutable=True), uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)
    return conn


class QueryTimeout(RuntimeError):
    pass


def query(conn: sqlite3.Connection, sql: str, params: tuple | dict = (),
          max_rows: int = 200, timeout_seconds: float = 2.0) -> tuple[list[str], list[tuple], bool]:
    """Run a read query with a time limit and a row cap.

    Returns (columns, rows, truncated). The time limit covers fetching rows too.
    """
    deadline = time.monotonic() + timeout_seconds
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise QueryTimeout(f"Query took longer than {timeout_seconds}s and was stopped.") from e
        raise
    finally:
        conn.set_progress_handler(None, 0)
    columns = [d[0] for d in cur.description or []]
    return columns, [tuple(r) for r in rows[:max_rows]], len(rows) > max_rows
