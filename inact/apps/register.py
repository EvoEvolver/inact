"""
Agent registry — auto-incrementing integer IDs and API keys for agents.

mount_register(prefix, storage) registers:

  POST   {prefix}/             register a new agent
                               body: {"name": "optional"}
                               returns: id (integer starting at 1), api_key
  GET    {prefix}/             list all agents (paginated)
                               ?page=1&per_page=20
  GET    {prefix}/{id}         get agent public profile (id, name, created_at)
  DELETE {prefix}/{id}         deregister agent
                               requires X-Api-Key header matching agent's key

*storage* accepts a :class:`~inact.storage.Storage` object or any URL/path
accepted by :func:`~inact.storage.make_storage`.
"""

from __future__ import annotations

import secrets
import time

from flask import request

from ..storage import Storage
from ..utils import text_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS agents (
        id         INTEGER PRIMARY KEY,
        api_key    TEXT    NOT NULL UNIQUE,
        name       TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL
    )""",
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

    def register(self, name: str = "") -> tuple[int, str]:
        api_key = secrets.token_urlsafe(32)
        ts = int(time.time())
        # Single atomic statement: compute next id via subquery
        self._s.execute(
            "INSERT INTO agents (id, api_key, name, created_at) "
            "SELECT COALESCE(MAX(id), 0) + 1, ?, ?, ? FROM agents",
            (api_key, name, ts),
        )
        row = self._s.fetchone("SELECT id FROM agents WHERE api_key = ?", (api_key,))
        return row["id"], api_key

    def count(self) -> int:
        row = self._s.fetchone("SELECT COUNT(*) AS cnt FROM agents")
        return row["cnt"] if row else 0

    def list_agents(self, page: int = 1, per_page: int = _DEFAULT_PER_PAGE) -> list[dict]:
        offset = (page - 1) * per_page
        return self._s.fetchall(
            "SELECT id, name, created_at FROM agents ORDER BY id ASC LIMIT ? OFFSET ?",
            (per_page, offset),
        )

    def get(self, agent_id: int) -> dict | None:
        return self._s.fetchone(
            "SELECT id, name, created_at FROM agents WHERE id = ?", (agent_id,)
        )

    def delete(self, agent_id: int, api_key: str) -> bool:
        return self._s.execute(
            "DELETE FROM agents WHERE id = ? AND api_key = ?", (agent_id, api_key)
        ) > 0


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_register(inact_app, prefix: str, registry: AgentRegistry) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_register_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _root():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            name = (body.get("name") or "").strip()
            agent_id, api_key = registry.register(name)
            return text_response(
                f"OK\nid      = {agent_id}\napi_key = {toml_str(api_key)}\n"
            )
        page, per_page = _parse_page_params()
        total = registry.count()
        agents = registry.list_agents(page, per_page)
        lines = ["# Agents\n", _page_header(page, per_page, total), "\n"]
        for a in agents:
            lines.append("[[agents]]\n")
            lines.append(f"id         = {a['id']}\n")
            if a["name"]:
                lines.append(f"name       = {toml_str(a['name'])}\n")
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
        lines = [f"id         = {agent['id']}\n"]
        if agent["name"]:
            lines.append(f"name       = {toml_str(agent['name'])}\n")
        lines.append(f"created_at = {toml_str(_fmt_ts(agent['created_at']))}\n")
        return text_response("".join(lines))

    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_root", view_func=_root, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/<agent_id>",
        endpoint=ep + "_agent", view_func=_agent, methods=["GET", "DELETE"])


def mount_register(inact_app, prefix: str, storage) -> None:
    """
    Mount an agent registry at *prefix*.

    Agents POST to register and receive an auto-incrementing integer ID and API key.
    The registry is publicly listable for agent discovery.

    *storage* — a database URL/path or a :class:`~inact.storage.Storage` instance.

    Example::

        app.mount_register("/agents", "./data/agents.db")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    attach_register(inact_app, p, AgentRegistry(backend))
    inact_app._app_mounts.append((p, (
        f"\nAgent registry: {p}\n"
        f'  POST   {p}/      register  body: {{"name":"optional"}}  → id, api_key\n'
        f"  GET    {p}/      list agents  (?page=1&per_page=20)\n"
        f"  GET    {p}/{{id}}  agent profile\n"
        f"  DELETE {p}/{{id}}  deregister  (X-Api-Key header required)\n"
    )))
