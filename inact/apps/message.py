"""
Agent messaging — send and receive messages between agents.

mount_message(prefix, storage) registers:

  POST   {prefix}/send                   send a message
                                         body: {"from": "1", "to": "2", "body": "..."}
  GET    {prefix}/inbox                  list received messages (paginated)
                                         ?agent_id=<id>  or  X-Agent-Id header
                                         ?page=1&per_page=20  ?unread=1
  GET    {prefix}/inbox/{id}             read message (auto-marks read)
  DELETE {prefix}/inbox/{id}             delete message
  GET    {prefix}/sent                   list sent messages (paginated)
                                         ?agent_id=<id>  or  X-Agent-Id header
                                         ?page=1&per_page=20
  GET    {prefix}/agents                 list known agents (paginated)
                                         agents who have sent at least one message
                                         ?page=1&per_page=20

Agent identity is passed via X-Agent-Id header or ?agent_id= query param.

*storage* accepts a :class:`~inact.storage.Storage` object or any URL/path
accepted by :func:`~inact.storage.make_storage`.
"""

from __future__ import annotations

import time
import uuid

from flask import request

from ..storage import Storage
from ..utils import text_response, html_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS messages (
        id         TEXT    PRIMARY KEY,
        from_id    TEXT    NOT NULL,
        to_id      TEXT    NOT NULL,
        body       TEXT    NOT NULL DEFAULT '',
        read       INTEGER NOT NULL DEFAULT 0,
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


class MessageStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)

    def send(self, from_id: str, to_id: str, body: str) -> str:
        msg_id = str(uuid.uuid4())
        self._s.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, from_id, to_id, body, 0, int(time.time())),
        )
        return msg_id

    def count_inbox(self, to_id: str, unread_only: bool = False) -> int:
        q = "SELECT COUNT(*) AS cnt FROM messages WHERE to_id = ?"
        params: list = [to_id]
        if unread_only:
            q += " AND read = 0"
        row = self._s.fetchone(q, tuple(params))
        return row["cnt"] if row else 0

    def inbox(self, to_id: str, page: int = 1, per_page: int = _DEFAULT_PER_PAGE,
              unread_only: bool = False) -> list[dict]:
        offset = (page - 1) * per_page
        q = "SELECT * FROM messages WHERE to_id = ?"
        params: list = [to_id]
        if unread_only:
            q += " AND read = 0"
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [per_page, offset]
        return self._s.fetchall(q, tuple(params))

    def count_sent(self, from_id: str) -> int:
        row = self._s.fetchone(
            "SELECT COUNT(*) AS cnt FROM messages WHERE from_id = ?", (from_id,)
        )
        return row["cnt"] if row else 0

    def sent(self, from_id: str, page: int = 1,
             per_page: int = _DEFAULT_PER_PAGE) -> list[dict]:
        offset = (page - 1) * per_page
        return self._s.fetchall(
            "SELECT * FROM messages WHERE from_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (from_id, per_page, offset),
        )

    def get(self, msg_id: str) -> dict | None:
        m = self._s.fetchone("SELECT * FROM messages WHERE id = ?", (msg_id,))
        if m:
            self._s.execute("UPDATE messages SET read = 1 WHERE id = ?", (msg_id,))
        return m

    def delete(self, msg_id: str) -> bool:
        return self._s.execute("DELETE FROM messages WHERE id = ?", (msg_id,)) > 0

    def count_agents(self) -> int:
        row = self._s.fetchone(
            "SELECT COUNT(DISTINCT from_id) AS cnt FROM messages"
        )
        return row["cnt"] if row else 0

    def list_agents(self, page: int = 1,
                    per_page: int = _DEFAULT_PER_PAGE) -> list[dict]:
        offset = (page - 1) * per_page
        return self._s.fetchall(
            "SELECT from_id, COUNT(*) AS sent_count, MAX(created_at) AS last_seen "
            "FROM messages GROUP BY from_id ORDER BY last_seen DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        )


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_message(inact_app, prefix: str, store: MessageStore,
                   agents_prefix: str = "/agents") -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_msg_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _agent_id() -> str:
        return (
            request.args.get("agent_id", "")
            or request.headers.get("X-Agent-Id", "")
        ).strip()

    def _send():
        body = request.get_json(force=True, silent=True) or {}
        from_id = str(body.get("from") or "").strip()
        to_id   = str(body.get("to")   or "").strip()
        text    = (body.get("body")    or "").strip()
        if not from_id:
            return text_response(
                "ERROR 400: 'from' required\n"
                f"POST {prefix}/send\n"
                '  Body: {"from": "1", "to": "2", "body": "..."}\n',
                400,
            )
        if not to_id:
            return text_response("ERROR 400: 'to' required\n", 400)
        if not text:
            return text_response("ERROR 400: 'body' required\n", 400)
        msg_id = store.send(from_id, to_id, text)
        return text_response(f"OK\nid = {toml_str(msg_id)}\n")

    def _inbox():
        to_id = _agent_id()
        if not to_id:
            return text_response(
                "ERROR 400: agent_id required\n"
                f"Usage: GET {prefix}/inbox?agent_id=<id>\n"
                "       or set X-Agent-Id header\n",
                400,
            )
        unread_only = request.args.get("unread", "0") == "1"
        page, per_page = _parse_page_params()
        total = store.count_inbox(to_id, unread_only)
        msgs = store.inbox(to_id, page, per_page, unread_only)
        lines = [
            f"# Inbox (agent {to_id})\n",
            _page_header(page, per_page, total),
            "# tip: ?unread=1 to filter unread\n\n",
        ]
        for m in msgs:
            lines.append("[[messages]]\n")
            lines.append(f"id   = {toml_str(m['id'])}\n")
            lines.append(f"from = {toml_str(m['from_id'])}\n")
            lines.append(f"read = {str(bool(m['read'])).lower()}\n")
            lines.append(f"date = {toml_str(_fmt_ts(m['created_at']))}\n")
            lines.append(f"url  = {toml_str(prefix + '/inbox/' + m['id'])}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _msg(msg_id: str):
        if request.method == "DELETE":
            ok = store.delete(msg_id)
            return text_response(
                "OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404
            )
        m = store.get(msg_id)
        if not m:
            return text_response("ERROR 404: message not found\n", 404)
        return text_response(
            f"id   = {toml_str(m['id'])}\n"
            f"from = {toml_str(m['from_id'])}\n"
            f"to   = {toml_str(m['to_id'])}\n"
            f"date = {toml_str(_fmt_ts(m['created_at']))}\n"
            "\n---\n\n"
            + m["body"] + "\n"
        )

    def _sent():
        from_id = _agent_id()
        if not from_id:
            return text_response(
                "ERROR 400: agent_id required\n"
                f"Usage: GET {prefix}/sent?agent_id=<id>\n"
                "       or set X-Agent-Id header\n",
                400,
            )
        page, per_page = _parse_page_params()
        total = store.count_sent(from_id)
        msgs = store.sent(from_id, page, per_page)
        lines = [
            f"# Sent (agent {from_id})\n",
            _page_header(page, per_page, total),
            "\n",
        ]
        for m in msgs:
            lines.append("[[messages]]\n")
            lines.append(f"id   = {toml_str(m['id'])}\n")
            lines.append(f"to   = {toml_str(m['to_id'])}\n")
            lines.append(f"date = {toml_str(_fmt_ts(m['created_at']))}\n")
            lines.append(f"url  = {toml_str(prefix + '/inbox/' + m['id'])}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _agents():
        page, per_page = _parse_page_params()
        total = store.count_agents()
        agents = store.list_agents(page, per_page)
        lines = [
            "# Known agents\n",
            "# agents who have sent at least one message\n",
            _page_header(page, per_page, total),
            "\n",
        ]
        for a in agents:
            lines.append("[[agents]]\n")
            lines.append(f"id        = {toml_str(a['from_id'])}\n")
            lines.append(f"sent      = {a['sent_count']}\n")
            lines.append(f"last_seen = {toml_str(_fmt_ts(a['last_seen']))}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _human():
        from ..render import render_template
        from ..utils import html_response
        html = render_template(
            "message_human.html",
            title="Chat",
            prefix=prefix,
            agents_prefix=agents_prefix,
            register_url="/_human" + agents_prefix,
        )
        return html_response(html)

    flask_app.add_url_rule(
        prefix + "/send",
        endpoint=ep + "_send", view_func=_send, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/inbox",
        endpoint=ep + "_inbox", view_func=_inbox)
    flask_app.add_url_rule(
        prefix + "/inbox/<msg_id>",
        endpoint=ep + "_msg", view_func=_msg, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/sent",
        endpoint=ep + "_sent", view_func=_sent)
    flask_app.add_url_rule(
        prefix + "/agents",
        endpoint=ep + "_agents", view_func=_agents)
    inact_app._human_views[prefix] = lambda path: _human()


def mount_message(inact_app, prefix: str, storage,
                  agents_prefix: str = "/agents") -> None:
    """
    Mount an agent messaging service at *prefix*.

    Agents send plain-text messages to each other by ID. Inbox and sent folders
    are paginated. ``/agents`` lists agents who have sent at least one message.

    *storage*       — a database URL/path or a :class:`~inact.storage.Storage` instance.
    *agents_prefix* — prefix of the register app; used by the ``/_human`` chat
                      page to list all registered agents (default ``"/agents"``).

    Example::

        mount_message(app, "/msg", "./data/messages.db")
        mount_message(app, "/msg", "./data/messages.db", agents_prefix="/agents")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    attach_message(inact_app, p, MessageStore(backend),
                   agents_prefix="/" + agents_prefix.strip("/"))
    inact_app._app_mounts.append((p, (
        f"\nAgent messaging: {p}\n"
        f'  POST   {p}/send          send  body: {{"from":"1","to":"2","body":"..."}}\n'
        f"  GET    {p}/inbox         received messages  (?agent_id=<id>  ?unread=1  ?page=1)\n"
        f"  GET    {p}/inbox/{{id}}    read message (marks read)\n"
        f"  DELETE {p}/inbox/{{id}}    delete message\n"
        f"  GET    {p}/sent          sent messages  (?agent_id=<id>  ?page=1)\n"
        f"  GET    {p}/agents        known agents  (?page=1)\n"
        f"  # identity: X-Agent-Id header or ?agent_id= param\n"
    )))
