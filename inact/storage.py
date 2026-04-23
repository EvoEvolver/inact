"""
Storage backends for inact apps (mailbox, forms, …).

Usage::

    from inact.storage import make_storage

    storage = make_storage("./mail.db")                    # SQLite (bare path)
    storage = make_storage("sqlite:///./mail.db")          # SQLite (explicit)
    storage = make_storage("postgresql://user:pw@host/db") # PostgreSQL
    storage = make_storage("postgres://…")                 # PostgreSQL alias

All backends expose the same four methods so app code stays database-agnostic.
SQL is written with ``?`` placeholders (SQLite style); PostgreSQL backends
translate them to ``%s`` automatically.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager


class Storage:
    """Abstract storage interface used by inact apps."""

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        """Return all rows as a list of dicts."""
        raise NotImplementedError

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        """Return the first row as a dict, or None."""
        raise NotImplementedError

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Execute one DML statement; return rowcount."""
        raise NotImplementedError

    def batch(self, ops: list[tuple[str, tuple]]) -> None:
        """Execute multiple DML statements in a single transaction."""
        raise NotImplementedError

    def init(self, ddl_statements: list[str]) -> None:
        """Run DDL statements (CREATE TABLE IF NOT EXISTS …)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

class SqliteStorage(Storage):
    """SQLite backend. Thread-safe via an internal write lock."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._path = path
        self._lock = threading.Lock()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._conn() as conn:
            c = conn.execute(sql, params)
            conn.commit()
            return c.rowcount

    def batch(self, ops: list[tuple[str, tuple]]) -> None:
        with self._lock, self._conn() as conn:
            for sql, params in ops:
                conn.execute(sql, params)
            conn.commit()

    def init(self, ddl_statements: list[str]) -> None:
        with self._lock, self._conn() as conn:
            for stmt in ddl_statements:
                conn.execute(stmt)
            conn.commit()


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------

class PostgresStorage(Storage):
    """
    PostgreSQL backend via psycopg2.

    Requires: ``pip install psycopg2-binary``

    The import is deferred until the first connection so that the object
    can be constructed (and URLs can be validated) without psycopg2 installed.
    """

    def __init__(self, url: str):
        self._url = url

    def _import(self):
        try:
            import psycopg2
            import psycopg2.extras
            return psycopg2, psycopg2.extras
        except ImportError:
            raise RuntimeError(
                "psycopg2 is required for PostgreSQL support.\n"
                "Install it with: pip install psycopg2-binary"
            )

    @contextmanager
    def _conn(self):
        pg, _ = self._import()
        conn = pg.connect(self._url)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _p(sql: str) -> str:
        """Convert ? placeholders to %s for psycopg2."""
        return sql.replace("?", "%s")

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        _, extras = self._import()
        with self._conn() as conn:
            with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                cur.execute(self._p(sql), params)
                return [dict(r) for r in cur.fetchall()]

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        _, extras = self._import()
        with self._conn() as conn:
            with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                cur.execute(self._p(sql), params)
                row = cur.fetchone()
                return dict(row) if row else None

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(self._p(sql), params)
                return cur.rowcount

    def batch(self, ops: list[tuple[str, tuple]]) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                for sql, params in ops:
                    cur.execute(self._p(sql), params)

    def init(self, ddl_statements: list[str]) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                for stmt in ddl_statements:
                    cur.execute(stmt)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_storage(source: str) -> Storage:
    """
    Return a :class:`Storage` backend for the given URL or file path.

    ================================  =============================
    ``sqlite:///path/to/file.db``     :class:`SqliteStorage`
    ``./path/to/file.db``             :class:`SqliteStorage`
    ``postgresql://user:pw@host/db``  :class:`PostgresStorage`
    ``postgres://…``                  :class:`PostgresStorage`
    ================================  =============================
    """
    s = source.strip()
    if s.startswith(("postgresql://", "postgres://")):
        return PostgresStorage(s)
    if s.startswith("sqlite:///"):
        return SqliteStorage(s[len("sqlite:///"):])
    return SqliteStorage(s)
