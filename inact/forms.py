"""
Agent forms — create machine-readable forms and collect structured responses.

mount_forms(prefix, storage) registers:

  GET    {prefix}/                      list all forms (TOML)
  POST   {prefix}/                      create form
                                        body: {"title":"...","description":"...","fields":[...]}
                                        field: {"name":"q","type":"string","required":true,"description":"...","options":["a","b"]}
  GET    {prefix}/{id}                  form definition (TOML)
  DELETE {prefix}/{id}                  delete form + all responses
  POST   {prefix}/{id}/submit           submit a response  body: {"field_name": value, ...}
  GET    {prefix}/{id}/responses        all responses (TOML)

*storage* accepts a :class:`~inact.storage.Storage` object or any URL/path
accepted by :func:`~inact.storage.make_storage`.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from flask import request

from .storage import Storage
from .utils import text_response, toml_str

_BARE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_DDL = [
    """CREATE TABLE IF NOT EXISTS forms (
        id          TEXT    PRIMARY KEY,
        title       TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        fields_json TEXT    NOT NULL DEFAULT '[]',
        created_at  BIGINT  NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS responses (
        id           TEXT    PRIMARY KEY,
        form_id      TEXT    NOT NULL,
        data_json    TEXT    NOT NULL,
        submitted_at BIGINT  NOT NULL,
        submitter    TEXT    NOT NULL DEFAULT ''
    )""",
]


class FormStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)

    def create(self, title: str, description: str, fields: list) -> str:
        form_id = str(uuid.uuid4())
        self._s.execute(
            "INSERT INTO forms VALUES (?,?,?,?,?)",
            (form_id, title, description, json.dumps(fields), int(time.time())),
        )
        return form_id

    def list_all(self) -> list[dict]:
        return self._s.fetchall("SELECT * FROM forms ORDER BY created_at DESC")

    def get(self, form_id: str) -> dict | None:
        row = self._s.fetchone("SELECT * FROM forms WHERE id=?", (form_id,))
        if not row:
            return None
        row["fields"] = json.loads(row.pop("fields_json"))
        return row

    def submit(self, form_id: str, data: dict, submitter: str = "") -> str:
        resp_id = str(uuid.uuid4())
        self._s.execute(
            "INSERT INTO responses VALUES (?,?,?,?,?)",
            (resp_id, form_id, json.dumps(data), int(time.time()), submitter),
        )
        return resp_id

    def responses(self, form_id: str) -> list[dict]:
        rows = self._s.fetchall(
            "SELECT * FROM responses WHERE form_id=? ORDER BY submitted_at DESC",
            (form_id,),
        )
        for row in rows:
            row["data"] = json.loads(row.pop("data_json"))
        return rows

    def response_count(self, form_id: str) -> int:
        rows = self._s.fetchall(
            "SELECT COUNT(*) AS cnt FROM responses WHERE form_id=?", (form_id,)
        )
        return rows[0]["cnt"] if rows else 0

    def delete(self, form_id: str) -> bool:
        n = self._s.execute("DELETE FROM forms WHERE id=?", (form_id,))
        self._s.execute("DELETE FROM responses WHERE form_id=?", (form_id,))
        return n > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _toml_key(k: str) -> str:
    return k if _BARE_KEY_RE.match(k) else toml_str(k)


def _value_toml(v) -> str:
    if isinstance(v, str):
        return toml_str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return toml_str(json.dumps(v))


def _field_toml(field: dict) -> str:
    lines = ["[[fields]]\n"]
    lines.append(f"name = {toml_str(field.get('name', ''))}\n")
    lines.append(f"type = {toml_str(field.get('type', 'string'))}\n")
    if field.get("description"):
        lines.append(f"description = {toml_str(field['description'])}\n")
    if "required" in field:
        lines.append(f"required = {str(bool(field['required'])).lower()}\n")
    if field.get("options"):
        opts = "[" + ", ".join(toml_str(str(o)) for o in field["options"]) + "]"
        lines.append(f"options = {opts}\n")
    lines.append("\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_forms(inact_app, prefix: str, store: FormStore) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_forms_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _root():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            title = (body.get("title") or "").strip()
            if not title:
                return text_response(
                    "ERROR 400: 'title' required\n"
                    f"POST {prefix}/\n"
                    '  Body: {"title": "...", "description": "...", "fields": [...]}\n'
                    '\nField: {"name": "q", "type": "string", "required": true, "description": "..."}\n',
                    400,
                )
            description = (body.get("description") or "").strip()
            fields = body.get("fields") or []
            form_id = store.create(title, description, fields)
            return text_response(
                f"OK\n"
                f"id  = {toml_str(form_id)}\n"
                f"url = {toml_str(prefix + '/' + form_id)}\n"
            )

        forms = store.list_all()
        lines = [f"# Forms\n# {len(forms)} form(s)\n\n"]
        for f in forms:
            n_fields = len(json.loads(f["fields_json"]))
            n_resp = store.response_count(f["id"])
            lines += [
                "[[forms]]\n",
                f"id          = {toml_str(f['id'])}\n",
                f"title       = {toml_str(f['title'])}\n",
                f"description = {toml_str(f['description'])}\n",
                f"fields      = {n_fields}\n",
                f"responses   = {n_resp}\n",
                f"url         = {toml_str(prefix + '/' + f['id'])}\n",
                f"submit      = {toml_str(prefix + '/' + f['id'] + '/submit')}\n",
                "\n",
            ]
        return text_response("".join(lines))

    def _form(form_id: str):
        if request.method == "DELETE":
            ok = store.delete(form_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)
        form = store.get(form_id)
        if not form:
            return text_response("ERROR 404: form not found\n", 404)
        lines = [
            f"# {form['title']}\n\n",
            f"id          = {toml_str(form['id'])}\n",
            f"title       = {toml_str(form['title'])}\n",
            f"description = {toml_str(form['description'])}\n",
            f"created_at  = {toml_str(_fmt_ts(form['created_at']))}\n",
            f"submit      = {toml_str(prefix + '/' + form_id + '/submit')}\n",
            f"responses   = {toml_str(prefix + '/' + form_id + '/responses')}\n",
            "\n",
        ]
        for field in form["fields"]:
            lines.append(_field_toml(field))
        return text_response("".join(lines))

    def _submit(form_id: str):
        form = store.get(form_id)
        if not form:
            return text_response("ERROR 404: form not found\n", 404)
        data = request.get_json(force=True, silent=True) or {}
        for field in form["fields"]:
            if field.get("required") and field["name"] not in data:
                return text_response(
                    f"ERROR 400: required field '{field['name']}' missing\n", 400
                )
        submitter = request.headers.get("X-Agent-Id", "")
        resp_id = store.submit(form_id, data, submitter)
        return text_response(f"OK\nid = {toml_str(resp_id)}\n")

    def _responses(form_id: str):
        form = store.get(form_id)
        if not form:
            return text_response("ERROR 404: form not found\n", 404)
        resps = store.responses(form_id)
        lines = [f"# Responses: {form['title']}\n# {len(resps)} response(s)\n\n"]
        for r in resps:
            lines.append("[[responses]]\n")
            lines.append(f"id           = {toml_str(r['id'])}\n")
            lines.append(f"submitted_at = {toml_str(_fmt_ts(r['submitted_at']))}\n")
            if r["submitter"]:
                lines.append(f"submitter    = {toml_str(r['submitter'])}\n")
            for k, v in r["data"].items():
                lines.append(f"{_toml_key(k)} = {_value_toml(v)}\n")
            lines.append("\n")
        return text_response("".join(lines))

    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_root", view_func=_root, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/<form_id>",
        endpoint=ep + "_form", view_func=_form, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<form_id>/submit",
        endpoint=ep + "_submit", view_func=_submit, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<form_id>/responses",
        endpoint=ep + "_responses", view_func=_responses)
