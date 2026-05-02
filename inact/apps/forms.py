"""
Human-facing forms — agents create forms and share a URL; humans fill them out in a browser.

mount_forms(prefix, storage) registers:

  GET    {prefix}/                      list all forms (TOML, for agents)
  POST   {prefix}/                      create form (JSON, for agents)
                                        body: {"title":"...","description":"...","fields":[...]}
                                        field: {"name":"q","type":"string","required":true,"description":"...","options":["a","b"]}
                                        field types: string · text · email · number · integer · float · boolean
  GET    {prefix}/{id}                  HTML form page for humans
                                        (TOML definition when Accept: text/plain)
  DELETE {prefix}/{id}                  delete form + all responses
  POST   {prefix}/{id}/submit           submit a response
                                        form-encoded (browser) or JSON (agent)
  GET    {prefix}/{id}/responses        all responses (TOML, for agents)

*storage* accepts a :class:`~inact.storage.Storage` object or any URL/path
accepted by :func:`~inact.storage.make_storage`.
"""

from __future__ import annotations

import html as _html
import json
import re
import time

from flask import make_response, request

from ..storage import Storage
from ..utils import text_response, toml_str

_BARE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_DDL = [
    """CREATE TABLE IF NOT EXISTS forms (
        id          INTEGER PRIMARY KEY,
        title       TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        fields_json TEXT    NOT NULL DEFAULT '[]',
        created_at  BIGINT  NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS responses (
        id           INTEGER PRIMARY KEY,
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

    def create(self, title: str, description: str, fields: list) -> int:
        return self._s.insert(
            "INSERT INTO forms (title, description, fields_json, created_at) VALUES (?,?,?,?)",
            (title, description, json.dumps(fields), int(time.time())),
        )

    def list_all(self) -> list[dict]:
        return self._s.fetchall("SELECT * FROM forms ORDER BY created_at DESC")

    def get(self, form_id: str) -> dict | None:
        row = self._s.fetchone("SELECT * FROM forms WHERE id=?", (form_id,))
        if not row:
            return None
        row["fields"] = json.loads(row.pop("fields_json"))
        return row

    def submit(self, form_id, data: dict, submitter: str = "") -> int:
        return self._s.insert(
            "INSERT INTO responses (form_id, data_json, submitted_at, submitter) VALUES (?,?,?,?)",
            (form_id, json.dumps(data), int(time.time()), submitter),
        )

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
# TOML helpers
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
# HTML helpers
# ---------------------------------------------------------------------------

_CSS = """
body{font-family:system-ui,sans-serif;max-width:620px;margin:2.5rem auto;padding:0 1.2rem;color:#1a1a1a;line-height:1.5}
h1{margin-bottom:.2rem;font-size:1.6rem}
.desc{color:#555;margin:.2rem 0 1.5rem}
.field{margin-bottom:1.3rem;display:flex;flex-direction:column;gap:.3rem}
label{font-weight:600;font-size:.95rem}
.hint{color:#666;font-size:.82rem}
input:not([type=checkbox]),textarea,select{padding:.45rem .6rem;border:1px solid #ccc;border-radius:5px;font-size:1rem;width:100%;box-sizing:border-box;background:#fff}
input:focus,textarea:focus,select:focus{outline:2px solid #2563eb;outline-offset:1px;border-color:transparent}
textarea{min-height:110px;resize:vertical}
.checkbox-row{display:flex;flex-direction:row;align-items:center;gap:.55rem}
.checkbox-row input{width:auto}
button{background:#2563eb;color:#fff;border:none;padding:.55rem 1.5rem;border-radius:5px;font-size:1rem;cursor:pointer;margin-top:.4rem}
button:hover{background:#1d4ed8}
.msg{padding:.75rem 1rem;border-radius:5px;margin-bottom:1.2rem}
.ok{background:#dcfce7;color:#166534;border:1px solid #bbf7d0}
.err{background:#fee2e2;color:#991b1b;border:1px solid #fecaca}
""".strip()


def _html_page(title: str, body: str) -> str:
    t = _html.escape(title)
    return (
        f"<!DOCTYPE html><html lang='en'><head>"
        f"<meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{t}</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _input_html(field: dict) -> str:
    name = _html.escape(field.get("name", ""), quote=True)
    ftype = field.get("type", "string")
    required = field.get("required", False)
    options = field.get("options", [])
    req = " required" if required else ""

    if options:
        opts = "\n".join(
            f'<option value="{_html.escape(str(o), quote=True)}">{_html.escape(str(o))}</option>'
            for o in options
        )
        return (
            f'<select name="{name}" id="{name}"{req}>'
            f'<option value="">— select —</option>\n{opts}</select>'
        )
    if ftype in ("text", "textarea"):
        return f'<textarea name="{name}" id="{name}"{req}></textarea>'
    if ftype in ("number", "integer", "float"):
        return f'<input type="number" name="{name}" id="{name}"{req}>'
    if ftype == "boolean":
        return (
            f'<div class="checkbox-row">'
            f'<input type="checkbox" name="{name}" id="{name}" value="true">'
            f'<span>Yes</span></div>'
        )
    if ftype == "email":
        return f'<input type="email" name="{name}" id="{name}"{req}>'
    return f'<input type="text" name="{name}" id="{name}"{req}>'


def _render_form_fields(form: dict) -> str:
    parts = []
    for field in form["fields"]:
        name = field.get("name", "")
        desc_f = field.get("description", "")
        hint = f'<span class="hint">{_html.escape(desc_f)}</span>' if desc_f else ""
        label = f'<label for="{_html.escape(name, quote=True)}">{_html.escape(name)}</label>'
        parts.append(f'<div class="field">{label}{hint}{_input_html(field)}</div>')
    return "\n".join(parts)


def _html_form_page(form: dict, submit_url: str, error: str = "") -> str:
    title = form["title"]
    desc_html = f'<p class="desc">{_html.escape(form.get("description", ""))}</p>' if form.get("description") else ""
    err_html = f'<div class="msg err">{_html.escape(error)}</div>' if error else ""
    body = (
        f"<h1>{_html.escape(title)}</h1>{desc_html}{err_html}"
        f'<form method="post" action="{_html.escape(submit_url, quote=True)}">'
        f"{_render_form_fields(form)}"
        f'<button type="submit">Submit</button></form>'
    )
    return _html_page(title, body)


def _html_success_page(form: dict) -> str:
    title = form["title"]
    body = (
        f"<h1>{_html.escape(title)}</h1>"
        f'<div class="msg ok">Thank you — your response has been recorded.</div>'
    )
    return _html_page(f"Thank you — {title}", body)


def _html_response(content: str, status: int = 200):
    resp = make_response(content, status)
    resp.content_type = "text/html; charset=utf-8"
    return resp


def _wants_html() -> bool:
    return "text/html" in request.headers.get("Accept", "")


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
                    '\nField: {"name": "q", "type": "string", "required": true, "description": "..."}\n'
                    'Types: string · text · email · number · integer · float · boolean\n',
                    400,
                )
            description = (body.get("description") or "").strip()
            fields = body.get("fields") or []
            form_id = store.create(title, description, fields)
            return text_response(
                f"OK\n"
                f"id  = {form_id}\n"
                f"url = {toml_str(prefix + '/' + str(form_id))}\n"
            )

        forms = store.list_all()
        lines = [f"# Forms\n# {len(forms)} form(s)\n\n"]
        for f in forms:
            n_fields = len(json.loads(f["fields_json"]))
            n_resp = store.response_count(f["id"])
            lines += [
                "[[forms]]\n",
                f"id          = {f['id']}\n",
                f"title       = {toml_str(f['title'])}\n",
                f"description = {toml_str(f['description'])}\n",
                f"fields      = {n_fields}\n",
                f"responses   = {n_resp}\n",
                f"url         = {toml_str(prefix + '/' + str(f['id']))}\n",
                f"submit      = {toml_str(prefix + '/' + str(f['id']) + '/submit')}\n",
                "\n",
            ]
        return text_response("".join(lines))

    def _form(form_id: str):
        if request.method == "DELETE":
            ok = store.delete(form_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)

        form = store.get(form_id)
        if not form:
            if _wants_html():
                return _html_response(
                    _html_page("Not Found", "<h1>Not Found</h1><p>This form does not exist.</p>"),
                    404,
                )
            return text_response("ERROR 404: form not found\n", 404)

        if _wants_html():
            submit_url = prefix + "/" + form_id + "/submit"
            return _html_response(_html_form_page(form, submit_url))

        # TOML definition for agents / API clients
        lines = [
            f"# {form['title']}\n\n",
            f"id          = {form['id']}\n",
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

        ct = request.content_type or ""
        is_browser = "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct
        submit_url = prefix + "/" + form_id + "/submit"

        if is_browser:
            raw = request.form.to_dict()
            data: dict = {}
            for field in form["fields"]:
                fname = field["name"]
                ftype = field.get("type", "string")
                if ftype == "boolean":
                    data[fname] = fname in raw
                elif fname in raw and raw[fname] != "":
                    val = raw[fname]
                    if ftype in ("number", "integer"):
                        try:
                            data[fname] = int(val)
                        except ValueError:
                            try:
                                data[fname] = float(val)
                            except ValueError:
                                data[fname] = val
                    elif ftype == "float":
                        try:
                            data[fname] = float(val)
                        except ValueError:
                            data[fname] = val
                    else:
                        data[fname] = val
        else:
            data = request.get_json(force=True, silent=True) or {}

        for field in form["fields"]:
            if field.get("required"):
                fname = field["name"]
                val = data.get(fname)
                if val is None or val == "" or val == []:
                    msg = f"'{fname}' is required."
                    if is_browser:
                        return _html_response(
                            _html_form_page(form, submit_url, error=msg), 400
                        )
                    return text_response(
                        f"ERROR 400: required field '{fname}' missing\n", 400
                    )

        submitter = request.headers.get("X-Agent-Id", "")
        resp_id = store.submit(form_id, data, submitter)

        if is_browser:
            return _html_response(_html_success_page(form))
        return text_response(f"OK\nid = {resp_id}\n")

    def _responses(form_id: str):
        form = store.get(form_id)
        if not form:
            return text_response("ERROR 404: form not found\n", 404)
        resps = store.responses(form_id)
        lines = [f"# Responses: {form['title']}\n# {len(resps)} response(s)\n\n"]
        for r in resps:
            lines.append("[[responses]]\n")
            lines.append(f"id           = {r['id']}\n")
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


def mount_forms(inact_app, prefix: str, storage) -> None:
    """
    Mount a human-facing form builder at *prefix*.

    Agents create forms with typed fields and share the URL with humans.
    Humans fill out the form in a browser; agents read back the responses.

    *storage* — a database URL/path or a :class:`~inact.storage.Storage` instance.

    Example::

        app.mount_forms("/forms", "./data/forms.db")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    attach_forms(inact_app, p, FormStore(backend))
    inact_app._app_mounts.append((p, (
        f"\nForms: {p}\n"
        f"  GET    {p}/                  list forms (TOML)\n"
        f"  POST   {p}/                  create form (JSON)\n"
        f"  GET    {p}/{{id}}              HTML form for humans\n"
        f"  DELETE {p}/{{id}}              delete form\n"
        f"  POST   {p}/{{id}}/submit       submit response (form or JSON)\n"
        f"  GET    {p}/{{id}}/responses    list responses (TOML)\n"
    )))
