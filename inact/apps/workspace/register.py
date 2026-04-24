"""
Agent registry — auto-incrementing integer IDs and API keys for agents.

mount_register(prefix, storage) registers:

  POST   {prefix}/                register a new agent
                                  body: {"name": "optional", "email": "optional"}
                                  returns: id, api_key, url
  GET    {prefix}/                list all agents (paginated)
                                  ?page=1&per_page=20
  GET    {prefix}/{id}            agent public profile (id, name, email, created_at)
  POST   {prefix}/{id}/.email     set / update email address
                                  requires X-Api-Key header
                                  body: {"email": "agent@example.com"}
  DELETE {prefix}/{id}            deregister agent
                                  requires X-Api-Key header

*storage* accepts a :class:`~inact.storage.Storage` object or any URL/path
accepted by :func:`~inact.storage.make_storage`.
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
        email        TEXT    NOT NULL DEFAULT '',
        callback_url TEXT    NOT NULL DEFAULT '',
        created_at   BIGINT  NOT NULL
    )""",
]

_MIGRATIONS = [
    "ALTER TABLE agents ADD COLUMN email        TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN callback_url TEXT NOT NULL DEFAULT ''",
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
                 callback_url: str = "") -> tuple[int, str]:
        api_key = secrets.token_urlsafe(32)
        ts = int(time.time())
        self._s.execute(
            "INSERT INTO agents (id, api_key, name, email, callback_url, created_at) "
            "SELECT COALESCE(MAX(id), 0) + 1, ?, ?, ?, ?, ? FROM agents",
            (api_key, name, email, callback_url, ts),
        )
        row = self._s.fetchone("SELECT id FROM agents WHERE api_key = ?", (api_key,))
        return row["id"], api_key

    def count(self) -> int:
        row = self._s.fetchone("SELECT COUNT(*) AS cnt FROM agents")
        return row["cnt"] if row else 0

    def list_agents(self, page: int = 1, per_page: int = _DEFAULT_PER_PAGE) -> list[dict]:
        offset = (page - 1) * per_page
        return self._s.fetchall(
            "SELECT id, name, email, callback_url, created_at FROM agents "
            "ORDER BY id ASC LIMIT ? OFFSET ?",
            (per_page, offset),
        )

    def get(self, agent_id: int) -> dict | None:
        return self._s.fetchone(
            "SELECT id, name, email, callback_url, created_at FROM agents WHERE id = ?",
            (agent_id,)
        )

    def get_by_key(self, api_key: str) -> dict | None:
        """Look up a full agent record by API key (for auth in other apps)."""
        return self._s.fetchone(
            "SELECT id, name, email, callback_url, created_at FROM agents WHERE api_key = ?",
            (api_key,)
        )

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
    ep = "_inact_register_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _root():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            name         = (body.get("name")     or "").strip()
            email        = (body.get("email")    or "").strip()
            callback_url = (body.get("callback") or "").strip()
            agent_id, api_key = registry.register(name, email, callback_url)
            # Propagate callback to notify system in one shot
            if callback_url and notify_fn:
                notify_fn(str(agent_id), callback_url)
            lines = [
                "OK\n",
                f"id      = {agent_id}\n",
                f"api_key = {toml_str(api_key)}\n",
                f"url     = {toml_str(prefix + '/' + str(agent_id))}\n",
            ]
            if email:
                lines.append(f"email    = {toml_str(email)}\n")
            if callback_url:
                lines.append(f"callback = {toml_str(callback_url)}\n")
            return text_response("".join(lines))
        page, per_page = _parse_page_params()
        total = registry.count()
        agents = registry.list_agents(page, per_page)
        lines = ["# Agents\n", _page_header(page, per_page, total), "\n"]
        for a in agents:
            lines.append("[[agents]]\n")
            lines.append(f"id         = {a['id']}\n")
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
            api_key = request.headers.get("X-Api-Key", "").strip()
            if not api_key:
                return text_response(
                    "ERROR 401: X-Api-Key header required\n"
                    f"Usage: DELETE {prefix}/{{id}}\n"
                    "  Header: X-Api-Key: <your api_key>\n",
                    401,
                )
            ok = registry.delete(aid, api_key)
            if not ok:
                return text_response(
                    "ERROR 404: agent not found or api_key mismatch\n", 404
                )
            return text_response("OK\n")
        agent = registry.get(aid)
        if not agent:
            return text_response("ERROR 404: agent not found\n", 404)
        display = agent["name"] or f"agent {aid}"
        lines = [
            f"# {display}\n\n",
            f"id         = {agent['id']}\n",
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
        api_key = request.headers.get("X-Api-Key", "").strip()
        if not api_key:
            return text_response(
                "ERROR 401: X-Api-Key header required\n"
                f"Usage: POST {prefix}/{{id}}/.email\n"
                "  Header: X-Api-Key: <your api_key>\n"
                '  Body:   {"email": "you@example.com"}\n',
                401,
            )
        body = request.get_json(force=True, silent=True) or {}
        email = (body.get("email") or "").strip()
        if not email:
            return text_response("ERROR 400: 'email' required\n", 400)
        ok = registry.set_email(aid, api_key, email)
        if not ok:
            return text_response("ERROR 404: agent not found or api_key mismatch\n", 404)
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

    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_root", view_func=_root, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/<agent_id>",
        endpoint=ep + "_agent", view_func=_agent, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<agent_id>/.email",
        endpoint=ep + "_email", view_func=_set_email, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<agent_id>/.callback",
        endpoint=ep + "_callback", view_func=_set_callback, methods=["POST"])
    inact_app._human_views[prefix] = lambda path: _human()


def mount_register(inact_app, prefix: str, storage,
                   notify_storage=None) -> None:
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

    attach_register(inact_app, p, AgentRegistry(backend), notify_fn=notify_fn)
    inact_app._app_mounts.append((p, (
        f"\nAgent registry: {p}\n"
        f'  POST   {p}/               register  body: {{"name":"...","email":"...","callback":"http://..."}}\n'
        f"  GET    {p}/               list agents\n"
        f"  GET    {p}/{{id}}            agent profile\n"
        f"  POST   {p}/{{id}}/.email     set email      (X-Api-Key required)\n"
        f"  POST   {p}/{{id}}/.callback  set callback   (X-Api-Key required)\n"
        f"  DELETE {p}/{{id}}            deregister     (X-Api-Key required)\n"
    )))
