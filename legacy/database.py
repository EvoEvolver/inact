"""
Relational database app — Notion/Airtable-style typed tables with relations.

mount_db(inact_app, prefix, storage) registers:

  GET  {prefix}/                      list tables
  POST {prefix}/                      create table
                                      body: {"name":"tasks","columns":[...]}
  GET  {prefix}/{table}               list rows  (?filter=col:op:val  ?sort=col
                                                   ?sort_dir=asc|desc  ?page=1)
  POST {prefix}/{table}               insert row  body: {"col":"val",...}
  GET  {prefix}/{table}/.schema       column definitions
  DELETE {prefix}/{table}             drop table  (X-Api-Key required)
  GET  {prefix}/{table}/{id}          get row (relations resolved inline)
  POST {prefix}/{table}/{id}          update fields  body: {"col":"new_val"}
  DELETE {prefix}/{table}/{id}        delete row

Column types:
  text | number | boolean | date | datetime | select | relation

Column definition:
  {"name":"title","type":"text","required":false}
  {"name":"status","type":"select","options":["todo","done","blocked"]}
  {"name":"project","type":"relation","target":"projects"}

Filter operators  (?filter=col:op:val):
  eq  ne  contains  startswith  gt  gte  lt  lte
"""

from __future__ import annotations

import json
import re
import time

from flask import request

from ...storage import Storage
from ...utils import text_response, toml_str

_VALID_NAME   = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_VALID_TYPES  = {"text", "number", "boolean", "date", "datetime", "select", "relation"}
_DEFAULT_PER_PAGE = 50
_MAX_PER_PAGE     = 500

_META_DDL = [
    """CREATE TABLE IF NOT EXISTS _db_meta (
        name       TEXT    PRIMARY KEY,
        columns    TEXT    NOT NULL,
        created_at BIGINT  NOT NULL
    )""",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(name: str) -> bool:
    return bool(_VALID_NAME.match(name))


def _tbl(name: str) -> str:
    return f"_db_{name}"


def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


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


def _parse_filters() -> list[tuple[str, str, str]]:
    """Parse repeated ?filter=col:op:val query params."""
    filters = []
    for raw in request.args.getlist("filter"):
        parts = raw.split(":", 2)
        if len(parts) == 3:
            filters.append((parts[0], parts[1], parts[2]))
    return filters


def _apply_filters(rows: list[dict], filters: list[tuple]) -> list[dict]:
    if not filters:
        return rows
    out = []
    for row in rows:
        ok = True
        for col, op, val in filters:
            fv = row.get(col, "")
            fv_s = str(fv).lower()
            v_s  = val.lower()
            try:
                if op == "eq":         ok = fv_s == v_s
                elif op == "ne":       ok = fv_s != v_s
                elif op == "contains": ok = v_s in fv_s
                elif op == "startswith": ok = fv_s.startswith(v_s)
                elif op in ("gt", "gte", "lt", "lte"):
                    n, v = float(fv), float(val)
                    if op == "gt":    ok = n > v
                    elif op == "gte": ok = n >= v
                    elif op == "lt":  ok = n < v
                    elif op == "lte": ok = n <= v
                else:
                    ok = True
            except (ValueError, TypeError):
                ok = False
            if not ok:
                break
        if ok:
            out.append(row)
    return out


def _coerce(value, col_type: str):
    """Coerce a raw value to its column type."""
    if value is None:
        return None
    if col_type == "number":
        try:
            v = float(value)
            return int(v) if v == int(v) else v
        except (ValueError, TypeError):
            return value
    if col_type == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")
    return str(value)


def _val_toml(value, col_type: str) -> str:
    if value is None:
        return '""'
    if col_type == "boolean":
        return "true" if value else "false"
    if col_type == "number":
        try:
            return str(float(value)) if "." in str(value) else str(int(float(value)))
        except (ValueError, TypeError):
            pass
    return toml_str(str(value))


def _row_to_toml(row: dict, schema: list[dict], prefix: str, table: str) -> str:
    lines = [
        "[[rows]]\n",
        f"id         = {row['id']}\n",
        f"created_at = {toml_str(_fmt_ts(row['created_at']))}\n",
        f"updated_at = {toml_str(_fmt_ts(row['updated_at']))}\n",
    ]
    for col in schema:
        val = row.get(col["name"])
        if val is None:
            continue
        col_type = col.get("type", "text")
        if col_type == "relation" and val:
            target = col.get("target", "")
            lines.append(f"{col['name']} = {toml_str(str(val))}\n")
            if target:
                lines.append(f"{col['name']}_url = {toml_str(f'{prefix}/{target}/{val}')}\n")
            if col.get("_resolved"):
                for k, rv in col["_resolved"].items():
                    lines.append(f"{col['name']}__{k} = {toml_str(str(rv))}\n")
        else:
            lines.append(f"{col['name']} = {_val_toml(val, col_type)}\n")
    row_url = f"{prefix}/{table}/{row['id']}"
    lines.append(f"url = {toml_str(row_url)}\n")
    lines.append("\n")
    return "".join(lines)


def _schema_toml(schema: list[dict]) -> str:
    lines = []
    for col in schema:
        lines.append("[[columns]]\n")
        lines.append(f"name     = {toml_str(col['name'])}\n")
        lines.append(f"type     = {toml_str(col.get('type', 'text'))}\n")
        lines.append(f"required = {str(col.get('required', False)).lower()}\n")
        if col.get("options"):
            opts = ", ".join(f'"{o}"' for o in col["options"])
            lines.append(f"options  = [{opts}]\n")
        if col.get("target"):
            lines.append(f"target   = {toml_str(col['target'])}\n")
        if col.get("default") is not None:
            lines.append(f"default  = {toml_str(str(col['default']))}\n")
        lines.append("\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

class DbStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_META_DDL)

    # -- Schema management --

    def tables(self) -> list[dict]:
        return self._s.fetchall(
            "SELECT name, columns, created_at FROM _db_meta ORDER BY name"
        )

    def get_schema(self, name: str) -> list[dict] | None:
        row = self._s.fetchone(
            "SELECT columns FROM _db_meta WHERE name = ?", (name,)
        )
        return json.loads(row["columns"]) if row else None

    def create_table(self, name: str, columns: list[dict]) -> None:
        if not _safe(name):
            raise ValueError(f"invalid table name {name!r} — use letters, digits, underscore")
        for col in columns:
            if not _safe(col.get("name", "")):
                raise ValueError(f"invalid column name {col.get('name')!r}")
            if col.get("type", "text") not in _VALID_TYPES:
                raise ValueError(f"invalid type {col.get('type')!r}")
        self._s.execute(
            f"CREATE TABLE IF NOT EXISTS {_tbl(name)} "
            f"(id INTEGER PRIMARY KEY, data TEXT NOT NULL DEFAULT '{{}}', "
            f"created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)"
        )
        self._s.execute(
            "INSERT OR REPLACE INTO _db_meta VALUES (?,?,?)",
            (name, json.dumps(columns), int(time.time())),
        )

    def drop_table(self, name: str) -> bool:
        if not self.get_schema(name):
            return False
        self._s.execute(f"DROP TABLE IF EXISTS {_tbl(name)}")
        self._s.execute("DELETE FROM _db_meta WHERE name = ?", (name,))
        return True

    # -- Row operations --

    def insert(self, name: str, data: dict) -> int:
        now = int(time.time())
        return self._s.insert(
            f"INSERT INTO {_tbl(name)} (data, created_at, updated_at) VALUES (?,?,?)",
            (json.dumps(data), now, now),
        )

    def _deserialize(self, raw) -> dict:
        return {
            "id":         raw["id"],
            "created_at": raw["created_at"],
            "updated_at": raw["updated_at"],
            **json.loads(raw["data"]),
        }

    def get(self, name: str, row_id: str) -> dict | None:
        raw = self._s.fetchone(
            f"SELECT * FROM {_tbl(name)} WHERE id = ?", (row_id,)
        )
        return self._deserialize(raw) if raw else None

    def update(self, name: str, row_id: str, fields: dict) -> bool:
        raw = self._s.fetchone(f"SELECT data FROM {_tbl(name)} WHERE id = ?", (row_id,))
        if not raw:
            return False
        data = {**json.loads(raw["data"]), **fields}
        self._s.execute(
            f"UPDATE {_tbl(name)} SET data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(data), int(time.time()), row_id),
        )
        return True

    def delete(self, name: str, row_id: str) -> bool:
        return self._s.execute(
            f"DELETE FROM {_tbl(name)} WHERE id = ?", (row_id,)
        ) > 0

    def list_rows(self, name: str, filters: list[tuple],
                  sort_col: str | None, sort_dir: str,
                  page: int, per_page: int) -> tuple[list[dict], int]:
        # Fetch all (filtering happens in Python; acceptable for workspace scale)
        raws = self._s.fetchall(
            f"SELECT * FROM {_tbl(name)} ORDER BY created_at DESC"
        )
        rows = [self._deserialize(r) for r in raws]
        rows = _apply_filters(rows, filters)
        if sort_col:
            rows.sort(key=lambda r: (r.get(sort_col) is None, r.get(sort_col, "")),
                      reverse=(sort_dir == "desc"))
        total = len(rows)
        return rows[(page - 1) * per_page : page * per_page], total

    def count(self, name: str) -> int:
        row = self._s.fetchone(f"SELECT COUNT(*) AS cnt FROM {_tbl(name)}")
        return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_db(inact_app, prefix: str, store: DbStore) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_db_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    # -- Table management --

    def _tables():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            name    = (body.get("name") or "").strip()
            columns = body.get("columns") or []
            if not name:
                return text_response(
                    "ERROR 400: 'name' required\n"
                    f"POST {prefix}/\n"
                    '  Body: {"name":"tasks","columns":[{"name":"title","type":"text"}]}\n',
                    400,
                )
            try:
                store.create_table(name, columns)
            except ValueError as e:
                return text_response(f"ERROR 400: {e}\n", 400)
            except Exception as e:
                return text_response(f"ERROR 409: {e}\n", 409)
            return text_response(
                f"OK\nname = {toml_str(name)}\n"
                f"url  = {toml_str(prefix + '/' + name)}\n"
                f"schema_url = {toml_str(prefix + '/' + name + '/.schema')}\n"
            )

        tables = store.tables()
        lines  = [f"# Database\n# {len(tables)} table(s)\n\n"]
        for t in tables:
            cols = json.loads(t["columns"])
            row_count = store.count(t["name"])
            lines += [
                "[[tables]]\n",
                f"name       = {toml_str(t['name'])}\n",
                f"columns    = {len(cols)}\n",
                f"rows       = {row_count}\n",
                f"created_at = {toml_str(_fmt_ts(t['created_at']))}\n",
                f"url        = {toml_str(prefix + '/' + t['name'])}\n",
                "\n",
            ]
        lines.append(
            f"# tip: POST {prefix}/  body: "
            '{"name":"mytable","columns":[{"name":"title","type":"text"}]}\n'
        )
        return text_response("".join(lines))

    def _schema(table: str):
        schema = store.get_schema(table)
        if schema is None:
            return text_response(f"ERROR 404: table {table!r} not found\n", 404)
        lines = [
            f"# Schema: {table}\n",
            f"# {len(schema)} column(s),  {store.count(table)} rows\n\n",
            f"table    = {toml_str(table)}\n",
            f"rows_url = {toml_str(prefix + '/' + table)}\n\n",
            _schema_toml(schema),
        ]
        return text_response("".join(lines))

    def _drop_table(table: str):
        ok = store.drop_table(table)
        return text_response("OK\n" if ok else "ERROR 404: table not found\n",
                             200 if ok else 404)

    # -- Row operations --

    def _rows(table: str):
        schema = store.get_schema(table)
        if schema is None:
            return text_response(f"ERROR 404: table {table!r} not found\n", 404)

        if request.method == "POST":
            raw = request.get_json(force=True, silent=True) or {}
            col_map = {c["name"]: c for c in schema}
            # Validate required + coerce types
            data: dict = {}
            errors: list[str] = []
            for col in schema:
                name = col["name"]
                val  = raw.get(name)
                if val is None and col.get("default") is not None:
                    val = col["default"]
                if val is None and col.get("required"):
                    errors.append(f"'{name}' is required")
                    continue
                if val is not None:
                    if col.get("type") == "select" and col.get("options"):
                        if str(val) not in col["options"]:
                            errors.append(f"'{name}' must be one of: {col['options']}")
                            continue
                    data[name] = _coerce(val, col.get("type", "text"))
            # Also store any extra fields not in schema
            for k, v in raw.items():
                if k not in col_map:
                    data[k] = v
            if errors:
                return text_response("ERROR 400: " + "; ".join(errors) + "\n", 400)
            row_id = store.insert(table, data)
            return text_response(
                f"OK\nid  = {row_id}\n"
                f"url = {toml_str(prefix + '/' + table + '/' + str(row_id))}\n"
            )

        # GET: list rows
        filters  = _parse_filters()
        sort_col = request.args.get("sort", "").strip() or None
        sort_dir = request.args.get("sort_dir", "asc").strip()
        page, per_page = _parse_page_params()

        rows, total = store.list_rows(table, filters, sort_col, sort_dir, page, per_page)
        total_pages = max(1, (total + per_page - 1) // per_page)

        lines = [
            f"# {table}\n",
            f"# {total} row(s)  —  page {page} of {total_pages}\n",
        ]
        if page > 1:
            lines.append(f"# ?page={page - 1}&per_page={per_page} for prev\n")
        if page < total_pages:
            lines.append(f"# ?page={page + 1}&per_page={per_page} for next\n")
        if filters:
            lines.append(f"# filtered by: {filters}\n")
        lines.append("\n")

        for row in rows:
            lines.append(_row_to_toml(row, schema, prefix, table))
        return text_response("".join(lines))

    def _row(table: str, row_id: str):
        schema = store.get_schema(table)
        if schema is None:
            return text_response(f"ERROR 404: table {table!r} not found\n", 404)

        if request.method == "DELETE":
            ok = store.delete(table, row_id)
            return text_response("OK\n" if ok else "ERROR 404: row not found\n",
                                 200 if ok else 404)

        if request.method == "POST":
            row = store.get(table, row_id)
            if not row:
                return text_response("ERROR 404: row not found\n", 404)
            fields = request.get_json(force=True, silent=True) or {}
            store.update(table, row_id, fields)
            return text_response("OK\n")

        row = store.get(table, row_id)
        if not row:
            return text_response("ERROR 404: row not found\n", 404)

        # Resolve relations one level deep
        enriched_schema = []
        for col in schema:
            new_col = dict(col)
            if col.get("type") == "relation" and col.get("target"):
                rel_id = row.get(col["name"])
                if rel_id:
                    rel_schema = store.get_schema(col["target"])
                    if rel_schema:
                        rel_row = store.get(col["target"], str(rel_id))
                        if rel_row:
                            new_col["_resolved"] = {
                                k: v for k, v in rel_row.items()
                                if not k.startswith("_") and k not in ("id", "created_at", "updated_at")
                            }
            enriched_schema.append(new_col)

        return text_response(_row_to_toml(row, enriched_schema, prefix, table))

    # Register routes
    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_tables", view_func=_tables, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/<table>/.schema",
        endpoint=ep + "_schema", view_func=_schema)
    flask_app.add_url_rule(
        prefix + "/<table>",
        endpoint=ep + "_rows", view_func=_rows, methods=["GET", "POST", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<table>/<row_id>",
        endpoint=ep + "_row", view_func=_row, methods=["GET", "POST", "DELETE"])

    def _human(path: str):
        from inact.render import render_template
        from inact.utils import html_response
        from inact.render import workspace_nav
        return html_response(render_template("db_human.html",
            title="Database", prefix=prefix, nav="", pills=[],
            workspace_links=workspace_nav("/_human/data/"),
            show_identity=True))

    inact_app._human_views[prefix] = _human
    inact_app.add_nav_item(prefix.rsplit("/", 1)[-1] or prefix.strip("/"),
                           "/_human" + prefix + "/")


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_db(inact_app, prefix: str, storage) -> None:
    """
    Mount a relational database at *prefix*.

    Agents can create typed tables, insert/update/delete rows, filter and sort
    results, and reference rows across tables via relation columns.

    *storage* — database URL/path or Storage instance (can share with workspace).

    Column types: text | number | boolean | date | datetime | select | relation

    Example::

        mount_db(app, "/db", "./workspace.db")

        # Create a table
        # POST /db/  body: {"name":"projects","columns":[
        #   {"name":"title","type":"text","required":true},
        #   {"name":"status","type":"select","options":["active","done"]},
        #   {"name":"owner","type":"relation","target":"agents"}
        # ]}

        # Insert a row
        # POST /db/projects/  body: {"title":"Inact v2","status":"active"}

        # Filter + sort
        # GET /db/projects/?filter=status:eq:active&sort=title
    """
    from ...storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    store = DbStore(backend)
    attach_db(inact_app, p, store)
    inact_app._app_mounts.append((p, (
        f"\nDatabase: {p}\n"
        f"  GET    {p}/                  list tables\n"
        f'  POST   {p}/                  create table  body: {{"name":"...","columns":[...]}}\n'
        f"  GET    {p}/{{table}}            list rows  (?filter=col:op:val  ?sort=col)\n"
        f"  POST   {p}/{{table}}            insert row\n"
        f"  GET    {p}/{{table}}/.schema    column definitions\n"
        f"  DELETE {p}/{{table}}            drop table\n"
        f"  GET    {p}/{{table}}/{{id}}       get row (relations resolved)\n"
        f"  POST   {p}/{{table}}/{{id}}       update fields\n"
        f"  DELETE {p}/{{table}}/{{id}}       delete row\n"
        f"  # column types: text | number | boolean | date | datetime | select | relation\n"
    )))
