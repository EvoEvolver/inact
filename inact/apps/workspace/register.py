"""
Registry — agents and humans share a single identity store.

  GET    {prefix}/                list all  ?kind=agent|human  ?page=1&per_page=20
  GET    {prefix}/{id}            public profile
  POST   {prefix}/{id}/.email     set email        (X-Api-Key required)
  POST   {prefix}/{id}/.callback  set callback url (X-Api-Key required)
  DELETE {prefix}/{id}            deregister       (X-Api-Key required)

Admin endpoints live at a separate prefix — see attach_admin / mount_admin.

*storage* accepts a :class:`~inact.storage.Storage` object or URL/path.
"""

from __future__ import annotations

import secrets
import time

from fastapi import Request
from starlette.responses import RedirectResponse

from ...storage import Storage
from ...utils import text_response, html_response, toml_str, _body, caller_id

_DDL = [
    """CREATE TABLE IF NOT EXISTS agents (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
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
    "ALTER TABLE agents ADD COLUMN description  TEXT NOT NULL DEFAULT ''",
]

_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100


def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_page_params(request: Request) -> tuple[int, int]:
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(_MAX_PER_PAGE, max(1, int(request.query_params.get("per_page", _DEFAULT_PER_PAGE))))
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
                 kind: str = "agent",
                 api_key: str = "",
                 description: str = "") -> tuple[int, str]:
        if kind not in ("agent", "human"):
            kind = "agent"
        if not api_key:
            api_key = secrets.token_urlsafe(32)
        elif self._s.fetchone("SELECT id FROM agents WHERE api_key = ?", (api_key,)):
            raise ValueError("api_key already in use")
        ts = int(time.time())
        self._s.execute(
            "INSERT INTO agents (id, api_key, name, kind, email, callback_url, description, created_at) "
            "SELECT COALESCE(MAX(id), 0) + 1, ?, ?, ?, ?, ?, ?, ? FROM agents",
            (api_key, name, kind, email, callback_url, description, ts),
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
                "SELECT id, name, kind, email, callback_url, description, created_at FROM agents "
                "WHERE kind=? ORDER BY id ASC LIMIT ? OFFSET ?",
                (kind, per_page, offset),
            )
        return self._s.fetchall(
            "SELECT id, name, kind, email, callback_url, description, created_at FROM agents "
            "ORDER BY id ASC LIMIT ? OFFSET ?",
            (per_page, offset),
        )

    def list_all_with_keys(self) -> list[dict]:
        """Admin: returns api_key and description too."""
        return self._s.fetchall(
            "SELECT id, name, kind, email, callback_url, description, api_key, created_at "
            "FROM agents ORDER BY id ASC"
        )

    def get(self, agent_id: int) -> dict | None:
        return self._s.fetchone(
            "SELECT id, name, kind, email, callback_url, description, created_at FROM agents WHERE id = ?",
            (agent_id,)
        )

    def get_by_key(self, api_key: str) -> dict | None:
        """Look up a full agent record by API key (for auth in other apps)."""
        return self._s.fetchone(
            "SELECT id, name, kind, email, callback_url, description, created_at FROM agents WHERE api_key = ?",
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

    def update(self, agent_id: int, fields: dict) -> bool:
        """Admin: update any subset of name, kind, email, description, api_key, callback_url."""
        allowed = {"name", "kind", "email", "description", "api_key", "callback_url"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        if "api_key" in updates:
            conflict = self._s.fetchone(
                "SELECT id FROM agents WHERE api_key = ? AND id != ?",
                (updates["api_key"], agent_id),
            )
            if conflict:
                raise ValueError("api_key already in use")
        if "kind" in updates and updates["kind"] not in ("agent", "human"):
            updates["kind"] = "agent"
        cols = list(updates.keys())
        vals = [updates[c] for c in cols]
        return self._s.execute(
            "UPDATE agents SET " + ", ".join(f"{c} = ?" for c in cols) + " WHERE id = ?",
            vals + [agent_id],
        ) > 0

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
                    notify_fn=None) -> None:
    prefix = "/" + prefix.strip("/")
    fastapi_app = inact_app.app

    def _root(request: Request):
        kind_filter = request.query_params.get("kind", "").strip() or None
        page, per_page = _parse_page_params(request)
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

    def _agent(agent_id: str, request: Request):
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: agent id must be an integer\n", 400)
        if request.method == "DELETE":
            api_key = request.headers.get("x-api-key", "").strip()
            if not api_key:
                return text_response("ERROR 401: X-Api-Key required\n", 401)
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

    def _set_email(agent_id: str, request: Request):
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: agent id must be an integer\n", 400)
        api_key = request.headers.get("x-api-key", "").strip()
        if not api_key:
            return text_response("ERROR 401: X-Api-Key required\n", 401)
        body = _body(request)
        email = (body.get("email") or "").strip()
        if not email:
            return text_response("ERROR 400: 'email' required\n", 400)
        ok = registry.set_email(aid, api_key, email)
        if not ok:
            return text_response("ERROR 404: not found or api_key mismatch\n", 404)
        return text_response(f"OK\nemail = {toml_str(email)}\n")

    def _human():
        from ...render import render_template, workspace_nav
        msg_prefix = "/_human" + prefix.rstrip("/").rsplit("/", 1)[0] + "/msg" \
                     if "/" in prefix.strip("/") else "/_human/msg"
        html = render_template(
            "register_human.html",
            title="Register",
            prefix=prefix,
            agents_api=prefix,
            chat_url=msg_prefix,
            workspace_links=workspace_nav("/_human/members/"),
            show_identity=True,
        )
        return html_response(html)

    def _set_callback(agent_id: str, request: Request):
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: agent id must be an integer\n", 400)
        api_key = request.headers.get("x-api-key", "").strip()
        if not api_key:
            return text_response(
                "ERROR 401: X-Api-Key header required\n"
                f"Usage: POST {prefix}/{{id}}/.callback\n"
                "  Header: X-Api-Key: <your api_key>\n"
                '  Body:   {"callback": "http://host/wake"}\n',
                401,
            )
        body = _body(request)
        callback_url = (body.get("callback") or "").strip()
        if not callback_url:
            return text_response("ERROR 400: 'callback' required\n", 400)
        ok = registry.set_callback(aid, api_key, callback_url)
        if not ok:
            return text_response("ERROR 404: agent not found or api_key mismatch\n", 404)
        if notify_fn:
            notify_fn(str(aid), callback_url)
        return text_response(f"OK\ncallback = {toml_str(callback_url)}\n")

    def _me(request: Request):
        aid = caller_id(request)
        if not aid:
            return text_response("ERROR 401: X-Api-Key required\n", 401)
        agent = registry.get(int(aid))
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

    fastapi_app.add_api_route(prefix + "/", _root, methods=["GET"])
    fastapi_app.add_api_route(prefix + "/.me", _me, methods=["GET"])
    fastapi_app.add_api_route(prefix + "/{agent_id}", _agent, methods=["GET", "DELETE"])
    fastapi_app.add_api_route(prefix + "/{agent_id}/.email", _set_email, methods=["POST"])
    fastapi_app.add_api_route(prefix + "/{agent_id}/.callback", _set_callback, methods=["POST"])

    inact_app._human_views[prefix] = lambda path: _human()
    inact_app.add_nav_item("agents", "/_human" + prefix + "/")


def attach_admin(inact_app, prefix: str, registry: AgentRegistry,
                 admin_key: str, notify_fn=None) -> None:
    prefix = "/" + prefix.strip("/")
    fastapi_app = inact_app.app
    _COOKIE = "_inact_admin"

    def _admin_require(request: Request):
        if not admin_key:
            return text_response("ERROR 503: admin_key not configured\n", 503)
        if request.headers.get("x-admin-key", "").strip() != admin_key:
            return text_response("ERROR 401: X-Admin-Key required\n", 401)
        return True

    def _admin_list(request: Request):
        auth = _admin_require(request)
        if auth is not True: return auth
        entries = registry.list_all_with_keys()
        lines = [f"# All entries ({len(entries)})\n\n"]
        for e in entries:
            lines += [
                "[[entries]]\n",
                f"id          = {e['id']}\n",
                f"kind        = {toml_str(e.get('kind','agent'))}\n",
                f"name        = {toml_str(e['name'])}\n",
                f"api_key     = {toml_str(e['api_key'])}\n",
                f"email       = {toml_str(e['email'])}\n",
                f"description = {toml_str(e.get('description',''))}\n",
                f"created_at  = {toml_str(_fmt_ts(e['created_at']))}\n",
                "\n",
            ]
        return text_response("".join(lines))

    def _admin_create(request: Request):
        auth = _admin_require(request)
        if auth is not True: return auth
        body = _body(request)
        name        = (body.get("name")        or "").strip()
        kind        = (body.get("kind")        or "agent").strip()
        email       = (body.get("email")       or "").strip()
        cb          = (body.get("callback")    or "").strip()
        api_key     = (body.get("api_key")     or "").strip()
        description = (body.get("description") or "").strip()
        try:
            agent_id, api_key = registry.register(
                name, email, cb, kind, api_key=api_key, description=description)
        except ValueError as e:
            return text_response(f"ERROR 409: {e}\n", 409)
        if cb and notify_fn:
            notify_fn(str(agent_id), cb)
        return text_response(
            f"OK\nid      = {agent_id}\nkind    = {toml_str(kind)}\n"
            f"api_key = {toml_str(api_key)}\n"
        )

    def _admin_delete(agent_id: str, request: Request):
        auth = _admin_require(request)
        if auth is not True: return auth
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: integer id required\n", 400)
        ok = registry.force_delete(aid)
        return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)

    def _admin_rekey(agent_id: str, request: Request):
        auth = _admin_require(request)
        if auth is not True: return auth
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: integer id required\n", 400)
        new_key = registry.regenerate_key(aid)
        if not new_key:
            return text_response("ERROR 404: not found\n", 404)
        return text_response(f"OK\napi_key = {toml_str(new_key)}\n")

    def _admin_update(agent_id: str, request: Request):
        auth = _admin_require(request)
        if auth is not True: return auth
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: integer id required\n", 400)
        body = _body(request)
        fields = {
            k: str(body[k] or "").strip()
            for k in ("name", "kind", "email", "description", "api_key", "callback_url")
            if k in body
        }
        if not fields:
            return text_response("ERROR 400: no fields provided\n", 400)
        try:
            ok = registry.update(aid, fields)
        except ValueError as e:
            return text_response(f"ERROR 409: {e}\n", 409)
        return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)

    async def _admin_human(request: Request):
        from inact.render import render_template
        from inact.utils import html_response

        if not admin_key:
            return text_response("ERROR 404: not found\n", 404)

        if request.method == "POST":
            form = await request.form()
            submitted = (form.get("key") or "").strip()
            if submitted == admin_key:
                resp = RedirectResponse(url=request.url.path, status_code=302)
                resp.set_cookie(_COOKIE, admin_key,
                                httponly=True, samesite="lax",
                                max_age=8 * 3600)
                return resp
            html = render_template("admin_login.html", error="Incorrect key.")
            return html_response(html, 401)

        if request.query_params.get("logout"):
            resp = RedirectResponse(url=request.url.path, status_code=302)
            resp.delete_cookie(_COOKIE)
            return resp

        if request.cookies.get(_COOKIE) != admin_key:
            html = render_template("admin_login.html", error=None)
            return html_response(html)

        from inact.render import workspace_nav
        return html_response(render_template("admin_human.html",
            title="Admin", prefix=prefix, nav="", pills=[],
            admin_key=admin_key,
            workspace_links=workspace_nav("/_human" + prefix),
            show_identity=False))

    fastapi_app.add_api_route(prefix + "/list", _admin_list, methods=["GET"])
    fastapi_app.add_api_route(prefix + "/create", _admin_create, methods=["POST"])
    fastapi_app.add_api_route(prefix + "/{agent_id}/delete", _admin_delete, methods=["POST"])
    fastapi_app.add_api_route(prefix + "/{agent_id}/rekey", _admin_rekey, methods=["POST"])
    fastapi_app.add_api_route(prefix + "/{agent_id}/update", _admin_update, methods=["POST"])

    inact_app.app.add_api_route("/_human" + prefix, _admin_human, methods=["GET", "POST"])
    inact_app.app.add_api_route("/_human" + prefix + "/", _admin_human, methods=["GET", "POST"])
    inact_app.add_nav_item("admin", "/_human" + prefix)


def mount_register(inact_app, prefix: str, storage,
                   notify_storage=None) -> None:
    """
    Mount an agent registry at *prefix*.

    *storage*        — database URL/path or Storage instance.
    *notify_storage* — if provided (same storage as mount_notify), callback URLs
                       registered at agent creation are automatically forwarded to
                       the notification system — no separate POST /notify/register needed.

    Example::

        mount_register(app, "/members", "./agents.db")
        mount_register(app, "/members", "./agents.db", notify_storage="./notify.db")
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

    attach_register(inact_app, p, AgentRegistry(backend), notify_fn=notify_fn)
    inact_app._app_mounts.append((p, (
        f"\nAgent registry: {p}\n"
        f"  GET    {p}/               list agents\n"
        f"  GET    {p}/{{id}}            agent profile\n"
        f"  POST   {p}/{{id}}/.email     set email      (X-Api-Key required)\n"
        f"  POST   {p}/{{id}}/.callback  set callback   (X-Api-Key required)\n"
        f"  DELETE {p}/{{id}}            deregister     (X-Api-Key required)\n"
    )))


def mount_admin(inact_app, prefix: str, storage, admin_key: str,
                notify_storage=None) -> None:
    """
    Mount the admin panel at *prefix* (e.g. ``"/admin"``).

    Every route requires ``X-Admin-Key: <admin_key>`` — completely independent
    of the regular agent api_key auth.  Human UI at ``/_human{prefix}``.

    Example::

        mount_admin(app, "/admin", "./workspace.db", admin_key="secret")
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

    attach_admin(inact_app, p, AgentRegistry(backend), admin_key=admin_key, notify_fn=notify_fn)
    inact_app._app_mounts.append((p, (
        f"\nAdmin panel: {p}\n"
        f"  GET    {p}/list           list all members (includes api_keys)\n"
        f"  POST   {p}/create         create member\n"
        f"  POST   {p}/{{id}}/delete   force-delete\n"
        f"  POST   {p}/{{id}}/rekey    regenerate key\n"
        f"  Auth:  X-Admin-Key header required on all routes\n"
    )))
