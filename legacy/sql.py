"""
SQL database connector for inact — run SQL and browse results as paginated TOML.

mount_sql(inact_app, prefix, db_url) registers:

  GET  {prefix}/                    overview: list tables + row counts
  GET  {prefix}/tables              list tables
  GET  {prefix}/tables/{name}       describe table (columns + types)
  GET  {prefix}/tables/{name}/rows  browse rows
                                    ?page=1&per_page=50&order_by=col
  POST {prefix}/query               run any SQL
                                    body: {"sql": "SELECT ..."}
                                    ?page=1&per_page=50

*db_url* — any SQLAlchemy-compatible URL:
  sqlite:///./data.db          SQLite  (no extra driver needed)
  postgresql://user:pw@host/db PostgreSQL  (pip install psycopg2)
  mysql+pymysql://user:pw@host/db  MySQL  (pip install pymysql)

*read_only* — if True, reject any non-SELECT statement.

Requires: pip install sqlalchemy

Example::

    mount_sql(app, "/db", "sqlite:///./data.db")
    mount_sql(app, "/db", "postgresql://localhost/myapp", read_only=True)
"""

from __future__ import annotations

import re

from flask import request

from inact.utils import text_response, toml_str

_DEFAULT_PER_PAGE = 50
_MAX_PER_PAGE = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_page_params() -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(_MAX_PER_PAGE, max(1, int(request.args.get("per_page", _DEFAULT_PER_PAGE))))
    except (ValueError, TypeError):
        per_page = _DEFAULT_PER_PAGE
    return page, per_page


def _page_header(page: int, per_page: int, total: int | None) -> str:
    if total is None:
        return f"# page {page}\n"
    total_pages = max(1, (total + per_page - 1) // per_page)
    lines = [f"# page {page} of {total_pages} ({total} rows total)\n"]
    if page > 1:
        lines.append(f"# ?page={page - 1}&per_page={per_page} for prev\n")
    if page < total_pages:
        lines.append(f"# ?page={page + 1}&per_page={per_page} for next\n")
    return "".join(lines)


def _col_key(name: str) -> str:
    """Sanitise a column name into a valid TOML bare key."""
    key = re.sub(r"[^a-zA-Z0-9_-]", "_", str(name)).strip("_") or "col"
    if key[0].isdigit():
        key = "_" + key
    return key


def _val(v) -> str:
    """Render a Python value as a TOML value string."""
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return toml_str(str(v))


def _is_select(sql: str) -> bool:
    first = sql.strip().split()[0].upper() if sql.strip() else ""
    return first in ("SELECT", "WITH", "EXPLAIN", "SHOW", "PRAGMA", "DESCRIBE")


# ---------------------------------------------------------------------------
# SQLConnector
# ---------------------------------------------------------------------------

class SQLConnector:
    """
    Thin wrapper around a SQLAlchemy engine exposing the operations
    needed by the SQL mount routes.
    """

    def __init__(self, db_url: str):
        try:
            from sqlalchemy import create_engine
        except ImportError:
            raise RuntimeError("sqlalchemy is required: pip install sqlalchemy")
        self._engine = create_engine(db_url)
        self._db_url = db_url

    def tables(self) -> list[str]:
        from sqlalchemy import inspect
        return sorted(inspect(self._engine).get_table_names())

    def count(self, table: str) -> int:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            row = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).fetchone()
            return row[0] if row else 0

    def describe(self, table: str) -> list[dict]:
        from sqlalchemy import inspect
        insp = inspect(self._engine)
        pk_cols = set(insp.get_pk_constraint(table).get("constrained_columns", []))
        return [
            {
                "name": c["name"],
                "type": str(c["type"]),
                "nullable": c.get("nullable", True),
                "primary_key": c["name"] in pk_cols,
                "default": str(c["default"]) if c.get("default") is not None else "",
            }
            for c in insp.get_columns(table)
        ]

    def browse(self, table: str, page: int, per_page: int,
               order_by: str | None = None) -> tuple[list[dict], int]:
        from sqlalchemy import text
        total = self.count(table)
        order = f' ORDER BY "{order_by}"' if order_by else ""
        offset = (page - 1) * per_page
        sql = f'SELECT * FROM "{table}"{order} LIMIT :lim OFFSET :off'
        with self._engine.connect() as conn:
            result = conn.execute(text(sql), {"lim": per_page, "off": offset})
            cols = list(result.keys())
            rows = [dict(zip(cols, r)) for r in result.fetchall()]
        return rows, total

    def execute(self, sql: str, page: int, per_page: int) -> dict:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            if _is_select(sql):
                # total count via subquery
                try:
                    total = conn.execute(
                        text(f"SELECT COUNT(*) FROM ({sql}) AS _q")
                    ).scalar()
                except Exception:
                    total = None

                offset = (page - 1) * per_page
                result = conn.execute(
                    text(f"SELECT * FROM ({sql}) AS _q LIMIT :lim OFFSET :off"),
                    {"lim": per_page, "off": offset},
                )
                cols = list(result.keys())
                rows = [dict(zip(cols, r)) for r in result.fetchall()]
                return {"select": True, "rows": rows, "columns": cols, "total": total}
            else:
                result = conn.execute(text(sql))
                conn.commit()
                return {"select": False, "rowcount": result.rowcount}


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_sql(inact_app, prefix: str, connector: SQLConnector,
               read_only: bool = False) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_sql_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _overview():
        try:
            tables = connector.tables()
        except Exception as exc:
            return text_response(f"ERROR 502: {exc}\n", 502)
        lines = [f"# Database\n# {len(tables)} table(s)\n\n"]
        for t in tables:
            lines.append("[[tables]]\n")
            lines.append(f"name = {toml_str(t)}\n")
            try:
                lines.append(f"rows = {connector.count(t)}\n")
            except Exception:
                pass
            lines.append(f"url  = {toml_str(prefix + '/tables/' + t)}\n")
            lines.append(f"rows_url = {toml_str(prefix + '/tables/' + t + '/rows')}\n")
            lines.append("\n")
        lines.append(f"# tip: POST {prefix}/query  body: {{\"sql\": \"SELECT ...\"}}\n")
        return text_response("".join(lines))

    def _describe(name: str):
        try:
            cols = connector.describe(name)
            total = connector.count(name)
        except Exception as exc:
            return text_response(f"ERROR 404: {exc}\n", 404)
        lines = [
            f"# Table: {name}\n",
            f"# {len(cols)} columns, {total} rows\n\n",
            f"rows_url = {toml_str(prefix + '/tables/' + name + '/rows')}\n\n",
        ]
        for c in cols:
            lines.append("[[columns]]\n")
            lines.append(f'name        = {toml_str(c["name"])}\n')
            lines.append(f'type        = {toml_str(c["type"])}\n')
            lines.append(f'nullable    = {str(c["nullable"]).lower()}\n')
            lines.append(f'primary_key = {str(c["primary_key"]).lower()}\n')
            if c["default"]:
                lines.append(f'default     = {toml_str(c["default"])}\n')
            lines.append("\n")
        return text_response("".join(lines))

    def _rows(name: str):
        page, per_page = _parse_page_params()
        order_by = request.args.get("order_by", "").strip() or None
        try:
            rows, total = connector.browse(name, page, per_page, order_by)
        except Exception as exc:
            return text_response(f"ERROR 400: {exc}\n", 400)
        lines = [f"# {name}\n", _page_header(page, per_page, total)]
        if order_by:
            lines.append(f"# ordered by: {order_by}\n")
        lines.append("\n")
        for row in rows:
            lines.append("[[rows]]\n")
            for col, val in row.items():
                lines.append(f"{_col_key(col)} = {_val(val)}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _query():
        body = request.get_json(force=True, silent=True) or {}
        sql = (body.get("sql") or "").strip()
        if not sql:
            return text_response(
                "ERROR 400: 'sql' required\n"
                f"POST {prefix}/query\n"
                '  Body: {"sql": "SELECT * FROM users LIMIT 10"}\n',
                400,
            )
        if read_only and not _is_select(sql):
            return text_response(
                "ERROR 403: this database is mounted read-only\n", 403
            )
        page, per_page = _parse_page_params()
        try:
            result = connector.execute(sql, page, per_page)
        except Exception as exc:
            return text_response(f"ERROR 400: {exc}\n", 400)

        if not result["select"]:
            return text_response(f"OK\nrows_affected = {result['rowcount']}\n")

        cols = result["columns"]
        rows = result["rows"]
        total = result.get("total")
        lines = [
            "# Query results\n",
            _page_header(page, per_page, total),
            f"# columns: {', '.join(cols)}\n\n",
        ]
        for row in rows:
            lines.append("[[rows]]\n")
            for col, val in row.items():
                lines.append(f"{_col_key(col)} = {_val(val)}\n")
            lines.append("\n")
        return text_response("".join(lines))

    flask_app.add_url_rule(
        prefix + "/", endpoint=ep + "_overview", view_func=_overview)
    flask_app.add_url_rule(
        prefix + "/tables", endpoint=ep + "_tables", view_func=_overview)
    flask_app.add_url_rule(
        prefix + "/tables/<name>", endpoint=ep + "_describe", view_func=_describe)
    flask_app.add_url_rule(
        prefix + "/tables/<name>/rows", endpoint=ep + "_rows", view_func=_rows)
    flask_app.add_url_rule(
        prefix + "/query", endpoint=ep + "_query",
        view_func=_query, methods=["POST"])


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_sql(
    inact_app,
    prefix: str,
    db_url: str,
    read_only: bool = False,
) -> None:
    """
    Mount a SQL database at *prefix*.

    *db_url* — SQLAlchemy connection URL.
    *read_only* — if True, reject non-SELECT statements.

    Requires: ``pip install sqlalchemy``

    Example::

        mount_sql(app, "/db", "sqlite:///./data.db")
        mount_sql(app, "/db", "postgresql://localhost/myapp", read_only=True)
    """
    p = "/" + prefix.strip("/")
    connector = SQLConnector(db_url)
    attach_sql(inact_app, p, connector, read_only=read_only)

    help_text = (
        f"\nSQL: {p}  ({db_url})\n"
        f"  GET  {p}/                     overview (tables + row counts)\n"
        f"  GET  {p}/tables/{{name}}         describe table\n"
        f"  GET  {p}/tables/{{name}}/rows    browse rows  ?page=1&per_page=50\n"
        f"  POST {p}/query                 run SQL  body: {{\"sql\":\"SELECT ...\"}}\n"
        + (f"  # read-only: non-SELECT statements are rejected\n" if read_only else "")
    )
    inact_app._app_mounts.append((p, help_text))
