"""
Registry — agents and humans share a single identity store.

  POST   {prefix}/                register
                                  body: {"name":"...","kind":"agent"|"human",
                                         "email":"...","callback":"http://..."}
                                  returns: id, api_key, url
  GET    {prefix}/                list all  ?kind=agent|human  ?page=1&per_page=20
  GET    {prefix}/{id}            public profile
  POST   {prefix}/{id}/.email     set email   (own X-Api-Key or admin_key)
  POST   {prefix}/{id}/.callback  set callback url  (own X-Api-Key or admin_key)
  DELETE {prefix}/{id}            deregister  (own X-Api-Key or admin_key)

Admin endpoints  (require X-Admin-Key header):
  POST   {prefix}/.admin/create   create agent/human with chosen name/kind
  GET    {prefix}/.admin/list     full list including api_keys
  DELETE {prefix}/.admin/{id}     force-delete any entry

*storage* accepts a :class:`~inact.storage.Storage` object or URL/path.
"""

from __future__ import annotations

import secrets
import time

from flask import request

from ...storage import Storage
from ...utils import text_response, html_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS agents (
        id           INTEGER PRIMARY KEY,
        api_key      TEXT    NOT NULL UNIQUE,
        name         TEXT    NOT NULL DEFAULT '',
        kind         TEXT    NOT NULL DEFAULT 'agent',
        email        TEXT    NOT NULL DEFAULT '',
        callback_url TEXT    NOT NULL DEFAULT '',
        created_at   BIGINT  NOT NULL
    )""",
]

_MIGRATIONS = [
    "ALTER TABLE agents ADD COLUMN email        TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN callback_url TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN kind         TEXT NOT NULL DEFAULT 'agent'",
]

_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100


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


def _page_header(page: int, per_page: int, total: int) -> str:
    total_pages = max(1, (total + per_page - 1) // per_page)
    lines = [f"# page {page} of {total_pages} ({total} total)\n"]
    if page > 1:
        lines.append(f"# ?page={page - 1}&per_page={per_page} for prev\n")
    if page < total_pages:
        lines.append(f"# ?page={page + 1}&per_page={per_page} for next\n")
    return "".join(lines)


class AgentRegistry:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)
        for sql in _MIGRATIONS:
            try:
                self._s.execute(sql)
            except Exception:
                pass  # column already exists

    def register(self, name: str = "", email: str = "",
                 callback_url: str = "",
                 kind: str = "agent") -> tuple[int, str]:
        if kind not in ("agent", "human"):
            kind = "agent"
        api_key = secrets.token_urlsafe(32)
        ts = int(time.time())
        self._s.execute(
            "INSERT INTO agents (id, api_key, name, kind, email, callback_url, created_at) "
            "SELECT COALESCE(MAX(id), 0) + 1, ?, ?, ?, ?, ?, ? FROM agents",
            (api_key, name, kind, email, callback_url, ts),
        )
        row = self._s.fetchone("SELECT id FROM agents WHERE api_key = ?", (api_key,))
        return row["id"], api_key

    def count(self, kind: str | None = None) -> int:
        if kind:
            row = self._s.fetchone("SELECT COUNT(*) AS cnt FROM agents WHERE kind=?", (kind,))
        else:
            row = self._s.fetchone("SELECT COUNT(*) AS cnt FROM agents")
        return row["cnt"] if row else 0

    def list_agents(self, page: int = 1, per_page: int = _DEFAULT_PER_PAGE,
                    kind: str | None = None) -> list[dict]:
        offset = (page - 1) * per_page
        if kind:
            return self._s.fetchall(
                "SELECT id, name, kind, email, callback_url, created_at FROM agents "
                "WHERE kind=? ORDER BY id ASC LIMIT ? OFFSET ?",
                (kind, per_page, offset),
            )
        return self._s.fetchall(
            "SELECT id, name, kind, email, callback_url, created_at FROM agents "
            "ORDER BY id ASC LIMIT ? OFFSET ?",
            (per_page, offset),
        )

    def list_all_with_keys(self) -> list[dict]:
        """Admin: returns api_key too."""
        return self._s.fetchall(
            "SELECT id, name, kind, email, callback_url, api_key, created_at "
            "FROM agents ORDER BY id ASC"
        )

    def get(self, agent_id: int) -> dict | None:
        return self._s.fetchone(
            "SELECT id, name, kind, email, callback_url, created_at FROM agents WHERE id = ?",
            (agent_id,)
        )

    def get_by_key(self, api_key: str) -> dict | None:
        """Look up a full agent record by API key (for auth in other apps)."""
        return self._s.fetchone(
            "SELECT id, name, kind, email, callback_url, created_at FROM agents WHERE api_key = ?",
            (api_key,)
        )

    def force_delete(self, agent_id: int) -> bool:
        """Admin: delete without checking api_key."""
        return self._s.execute("DELETE FROM agents WHERE id = ?", (agent_id,)) > 0

    def regenerate_key(self, agent_id: int) -> str | None:
        """Admin: issue a fresh api_key for an existing entry."""
        new_key = secrets.token_urlsafe(32)
        ok = self._s.execute(
            "UPDATE agents SET api_key = ? WHERE id = ?", (new_key, agent_id)
        ) > 0
        return new_key if ok else None

    def set_callback(self, agent_id: int, api_key: str, callback_url: str) -> bool:
        return self._s.execute(
            "UPDATE agents SET callback_url = ? WHERE id = ? AND api_key = ?",
            (callback_url, agent_id, api_key),
        ) > 0

    def set_email(self, agent_id: int, api_key: str, email: str) -> bool:
        return self._s.execute(
            "UPDATE agents SET email = ? WHERE id = ? AND api_key = ?",
            (email, agent_id, api_key),
        ) > 0

    def delete(self, agent_id: int, api_key: str) -> bool:
        return self._s.execute(
            "DELETE FROM agents WHERE id = ? AND api_key = ?", (agent_id, api_key)
        ) > 0


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_register(inact_app, prefix: str, registry: AgentRegistry,
                    notify_fn=None, admin_key: str = "") -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_register_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _is_admin() -> bool:
        return bool(admin_key and
                    request.headers.get("X-Admin-Key", "").strip() == admin_key)

    def _root():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            name         = (body.get("name")     or "").strip()
            email        = (body.get("email")    or "").strip()
            callback_url = (body.get("callback") or "").strip()
            kind         = (body.get("kind")     or "agent").strip()
            agent_id, api_key = registry.register(name, email, callback_url, kind)
            if callback_url and notify_fn:
                notify_fn(str(agent_id), callback_url)
            lines = [
                "OK\n",
                f"id      = {agent_id}\n",
                f"kind    = {toml_str(kind)}\n",
                f"api_key = {toml_str(api_key)}\n",
                f"url     = {toml_str(prefix + '/' + str(agent_id))}\n",
            ]
            if email:
                lines.append(f"email    = {toml_str(email)}\n")
            if callback_url:
                lines.append(f"callback = {toml_str(callback_url)}\n")
            return text_response("".join(lines))

        kind_filter = request.args.get("kind", "").strip() or None
        page, per_page = _parse_page_params()
        total = registry.count(kind_filter)
        agents = registry.list_agents(page, per_page, kind_filter)
        label = f"# {'Humans' if kind_filter=='human' else 'Agents' if kind_filter=='agent' else 'All'}\n"
        lines = [label, _page_header(page, per_page, total), "\n"]
        for a in agents:
            lines.append("[[agents]]\n")
            lines.append(f"id         = {a['id']}\n")
            lines.append(f"kind       = {toml_str(a.get('kind','agent'))}\n")
            if a["name"]:
                lines.append(f"name       = {toml_str(a['name'])}\n")
            if a["email"]:
                lines.append(f"email      = {toml_str(a['email'])}\n")
            lines.append(f"created_at = {toml_str(_fmt_ts(a['created_at']))}\n")
            lines.append(f"url        = {toml_str(prefix + '/' + str(a['id']))}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _agent(agent_id: str):
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: agent id must be an integer\n", 400)
        if request.method == "DELETE":
            if _is_admin():
                ok = registry.force_delete(aid)
            else:
                api_key = request.headers.get("X-Api-Key", "").strip()
                if not api_key:
                    return text_response("ERROR 401: X-Api-Key or X-Admin-Key required\n", 401)
                ok = registry.delete(aid, api_key)
            if not ok:
                return text_response("ERROR 404: not found or key mismatch\n", 404)
            return text_response("OK\n")
        agent = registry.get(aid)
        if not agent:
            return text_response("ERROR 404: agent not found\n", 404)
        kind    = agent.get("kind", "agent")
        display = agent["name"] or f"{kind} {aid}"
        lines = [
            f"# {display}\n\n",
            f"id         = {agent['id']}\n",
            f"kind       = {toml_str(kind)}\n",
        ]
        if agent["name"]:
            lines.append(f"name         = {toml_str(agent['name'])}\n")
        if agent["email"]:
            lines.append(f"email        = {toml_str(agent['email'])}\n")
        if agent["callback_url"]:
            lines.append(f"callback     = {toml_str(agent['callback_url'])}\n")
        lines.append(f"created_at   = {toml_str(_fmt_ts(agent['created_at']))}\n")
        lines.append(f"url          = {toml_str(prefix + '/' + str(agent['id']))}\n")
        hints = []
        if not agent["email"]:
            hints.append(f"POST {prefix}/{aid}/.email     to set email")
        if not agent["callback_url"]:
            hints.append(f"POST {prefix}/{aid}/.callback  to set notification callback")
        if hints:
            lines.append("\n# " + "\n# ".join(hints) + "\n")
        return text_response("".join(lines))

    def _set_email(agent_id: str):
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: agent id must be an integer\n", 400)
        if not _is_admin():
            api_key = request.headers.get("X-Api-Key", "").strip()
            if not api_key:
                return text_response("ERROR 401: X-Api-Key or X-Admin-Key required\n", 401)
            body = request.get_json(force=True, silent=True) or {}
            email = (body.get("email") or "").strip()
            if not email:
                return text_response("ERROR 400: 'email' required\n", 400)
            ok = registry.set_email(aid, api_key, email)
            if not ok:
                return text_response("ERROR 404: not found or api_key mismatch\n", 404)
            return text_response(f"OK\nemail = {toml_str(email)}\n")
        # Admin path — no api_key check
        body = request.get_json(force=True, silent=True) or {}
        email = (body.get("email") or "").strip()
        if not email:
            return text_response("ERROR 400: 'email' required\n", 400)
        ok = registry._s.execute("UPDATE agents SET email=? WHERE id=?", (email, aid)) > 0
        if not ok:
            return text_response("ERROR 404: agent not found\n", 404)
        return text_response(f"OK\nemail = {toml_str(email)}\n")

    def _human():
        from ...render import render_template
        from ...utils import html_response
        html = render_template(
            "register_human.html",
            title="Register",
            prefix=prefix,
            agents_api=prefix,
            chat_url="/_human" + prefix.rstrip("/").rsplit("/", 1)[0] + "/msg"
                     if "/" in prefix.strip("/") else "/_human/msg",
        )
        return html_response(html)

    def _set_callback(agent_id: str):
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: agent id must be an integer\n", 400)
        api_key = request.headers.get("X-Api-Key", "").strip()
        if not api_key:
            return text_response(
                "ERROR 401: X-Api-Key header required\n"
                f"Usage: POST {prefix}/{{id}}/.callback\n"
                "  Header: X-Api-Key: <your api_key>\n"
                '  Body:   {"callback": "http://host/wake"}\n',
                401,
            )
        body = request.get_json(force=True, silent=True) or {}
        callback_url = (body.get("callback") or "").strip()
        if not callback_url:
            return text_response("ERROR 400: 'callback' required\n", 400)
        ok = registry.set_callback(aid, api_key, callback_url)
        if not ok:
            return text_response("ERROR 404: agent not found or api_key mismatch\n", 404)
        if notify_fn:
            notify_fn(str(aid), callback_url)
        return text_response(f"OK\ncallback = {toml_str(callback_url)}\n")

    def _admin_require() -> bool | tuple:
        if not admin_key:
            return text_response("ERROR 503: admin_key not configured on this server\n", 503)
        if request.headers.get("X-Admin-Key", "").strip() != admin_key:
            return text_response("ERROR 401: X-Admin-Key required\n", 401)
        return True

    def _admin_list():
        auth = _admin_require()
        if auth is not True: return auth
        entries = registry.list_all_with_keys()
        lines = [f"# All entries ({len(entries)})\n\n"]
        for e in entries:
            lines += [
                "[[entries]]\n",
                f"id         = {e['id']}\n",
                f"kind       = {toml_str(e.get('kind','agent'))}\n",
                f"name       = {toml_str(e['name'])}\n",
                f"api_key    = {toml_str(e['api_key'])}\n",
                f"email      = {toml_str(e['email'])}\n",
                f"created_at = {toml_str(_fmt_ts(e['created_at']))}\n",
                "\n",
            ]
        return text_response("".join(lines))

    def _admin_create():
        auth = _admin_require()
        if auth is not True: return auth
        body = request.get_json(force=True, silent=True) or {}
        name  = (body.get("name")  or "").strip()
        kind  = (body.get("kind")  or "agent").strip()
        email = (body.get("email") or "").strip()
        cb    = (body.get("callback") or "").strip()
        agent_id, api_key = registry.register(name, email, cb, kind)
        if cb and notify_fn:
            notify_fn(str(agent_id), cb)
        return text_response(
            f"OK\nid      = {agent_id}\nkind    = {toml_str(kind)}\n"
            f"api_key = {toml_str(api_key)}\n"
        )

    def _admin_delete(agent_id: str):
        auth = _admin_require()
        if auth is not True: return auth
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: integer id required\n", 400)
        ok = registry.force_delete(aid)
        return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)

    def _admin_rekey(agent_id: str):
        auth = _admin_require()
        if auth is not True: return auth
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: integer id required\n", 400)
        new_key = registry.regenerate_key(aid)
        if not new_key:
            return text_response("ERROR 404: not found\n", 404)
        return text_response(f"OK\napi_key = {toml_str(new_key)}\n")

    def _me():
        """Return the caller's own profile, identified by X-Api-Key or cookie."""
        api_key = (
            request.headers.get("X-Api-Key", "")
            or request.cookies.get("_inact_key", "")
        ).strip()
        if not api_key:
            return text_response("ERROR 401: X-Api-Key required\n", 401)
        agent = registry.get_by_key(api_key)
        if not agent:
            return text_response("ERROR 403: invalid api_key\n", 403)
        kind    = agent.get("kind", "agent")
        display = agent["name"] or f"{kind} {agent['id']}"
        lines = [
            f"# {display}\n\n",
            f"id         = {agent['id']}\n",
            f"kind       = {toml_str(kind)}\n",
        ]
        if agent["name"]:
            lines.append(f"name       = {toml_str(agent['name'])}\n")
        if agent["email"]:
            lines.append(f"email      = {toml_str(agent['email'])}\n")
        lines.append(f"url        = {toml_str(prefix + '/' + str(agent['id']))}\n")
        return text_response("".join(lines))

    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_root", view_func=_root, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/.me",
        endpoint=ep + "_me", view_func=_me)
    flask_app.add_url_rule(
        prefix + "/<agent_id>",
        endpoint=ep + "_agent", view_func=_agent, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<agent_id>/.email",
        endpoint=ep + "_email", view_func=_set_email, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<agent_id>/.callback",
        endpoint=ep + "_callback", view_func=_set_callback, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/.admin/list",
        endpoint=ep + "_admin_list", view_func=_admin_list)
    flask_app.add_url_rule(
        prefix + "/.admin/create",
        endpoint=ep + "_admin_create", view_func=_admin_create, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/.admin/<agent_id>/delete",
        endpoint=ep + "_admin_delete", view_func=_admin_delete, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/.admin/<agent_id>/rekey",
        endpoint=ep + "_admin_rekey", view_func=_admin_rekey, methods=["POST"])
    _COOKIE = "_inact_admin"

    def _admin_human():
        from inact.render import render_template
        from inact.utils import html_response
        from flask import make_response, redirect

        # No admin key configured → 404
        if not admin_key:
            from inact.utils import text_response
            return text_response("ERROR 404: not found\n", 404)

        # POST — validate submitted key, set cookie, redirect
        if request.method == "POST":
            submitted = (request.form.get("key") or "").strip()
            if submitted == admin_key:
                resp = make_response(redirect(request.path))
                resp.set_cookie(_COOKIE, admin_key,
                                httponly=True, samesite="Lax",
                                max_age=8 * 3600)
                return resp
            html = render_template("admin_login.html", error="Incorrect key.")
            return make_response(html_response(html)[0], 401,
                                 {"Content-Type": "text/html; charset=utf-8"})

        # GET — logout
        if request.args.get("logout"):
            resp = make_response(redirect(request.path))
            resp.delete_cookie(_COOKIE)
            return resp

        # GET — check cookie
        if request.cookies.get(_COOKIE) != admin_key:
            html = render_template("admin_login.html", error=None)
            return html_response(html)

        return html_response(render_template("admin_human.html",
            title="Admin", prefix=prefix, nav="", pills=[],
            admin_key=admin_key))

    # Register both GET and POST so the login form can submit
    ep_admin = ep + "_admin_human"
    inact_app.app.add_url_rule(
        "/_human" + prefix + "/.admin",
        endpoint=ep_admin, view_func=_admin_human, methods=["GET", "POST"])

    inact_app._human_views[prefix] = lambda path: _human()
    # Also catch sub-paths under /.admin so the cookie redirect lands correctly
    inact_app.app.add_url_rule(
        "/_human" + prefix + "/.admin/",
        endpoint=ep_admin + "_slash", view_func=_admin_human, methods=["GET", "POST"])


def mount_register(inact_app, prefix: str, storage,
                   notify_storage=None, admin_key: str = "") -> None:
    """
    Mount an agent registry at *prefix*.

    *storage*        — database URL/path or Storage instance.
    *notify_storage* — if provided (same storage as mount_notify), callback URLs
                       registered at agent creation are automatically forwarded to
                       the notification system — no separate POST /notify/register needed.

    Example::

        mount_register(app, "/agents", "./agents.db")
        mount_register(app, "/agents", "./agents.db", notify_storage="./notify.db")
    """
    from ...storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage

    notify_fn = None
    if notify_storage is not None:
        from ..notify import NotifyStore
        ns = make_storage(notify_storage) if isinstance(notify_storage, str) else notify_storage
        nstore = NotifyStore(ns)
        def notify_fn(agent_id: str, callback_url: str) -> None:
            nstore.register_callback(agent_id, callback_url)

    attach_register(inact_app, p, AgentRegistry(backend),
                    notify_fn=notify_fn, admin_key=admin_key)
    inact_app._app_mounts.append((p, (
        f"\nAgent registry: {p}\n"
        f'  POST   {p}/               register  body: {{"name":"...","email":"...","callback":"http://..."}}\n'
        f"  GET    {p}/               list agents\n"
        f"  GET    {p}/{{id}}            agent profile\n"
        f"  POST   {p}/{{id}}/.email     set email      (X-Api-Key required)\n"
        f"  POST   {p}/{{id}}/.callback  set callback   (X-Api-Key required)\n"
        f"  DELETE {p}/{{id}}            deregister     (X-Api-Key required)\n"
    )))
