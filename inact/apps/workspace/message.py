"""
Session-based agent messaging — any set of participants share a session.

  POST   {prefix}/sessions                   create session
                                             body: {"name":"opt","members":["1","2"]}
                                             X-Agent-Id = creator (auto-added to members)
  GET    {prefix}/sessions                   list my sessions
                                             X-Agent-Id or ?agent_id=  ?page=1
  GET    {prefix}/sessions/{id}              session details + members
  POST   {prefix}/sessions/{id}/send         send message
                                             body: {"body":"..."}
                                             X-Agent-Id or body "from"
  GET    {prefix}/sessions/{id}/messages     paginated messages (marks read)
                                             ?page=1&per_page=50  ?unread=1
  POST   {prefix}/sessions/{id}/members      add member
                                             body: {"agent_id":"3"}

Identity: X-Agent-Id header or ?agent_id= query param.
"""

from __future__ import annotations

import time
import uuid

from flask import request

from ...storage import Storage
from ...utils import text_response, html_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS sessions (
        id         INTEGER PRIMARY KEY,
        name       TEXT    NOT NULL DEFAULT '',
        created_by TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS session_members (
        session_id INTEGER NOT NULL,
        agent_id   TEXT    NOT NULL,
        joined_at  BIGINT  NOT NULL,
        PRIMARY KEY (session_id, agent_id)
    )""",
    """CREATE TABLE IF NOT EXISTS session_messages (
        id         TEXT    PRIMARY KEY,
        session_id INTEGER NOT NULL,
        from_id    TEXT    NOT NULL,
        body       TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS session_message_reads (
        message_id TEXT    NOT NULL,
        agent_id   TEXT    NOT NULL,
        PRIMARY KEY (message_id, agent_id)
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


class SessionStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)

    def create(self, name: str, created_by: str, member_ids: list) -> int:
        ts = int(time.time())
        session_id = self._s.insert(
            "INSERT INTO sessions (name, created_by, created_at) VALUES (?, ?, ?)",
            (name, created_by, ts),
        )
        for agent_id in list({str(m) for m in member_ids} | {str(created_by)}):
            try:
                self._s.execute(
                    "INSERT INTO session_members VALUES (?, ?, ?)",
                    (session_id, agent_id, ts),
                )
            except Exception:
                pass
        return session_id

    def get(self, session_id: str) -> dict | None:
        return self._s.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))

    def get_members(self, session_id: str) -> list[str]:
        rows = self._s.fetchall(
            "SELECT agent_id FROM session_members WHERE session_id = ? ORDER BY joined_at ASC",
            (session_id,),
        )
        return [r["agent_id"] for r in rows]

    def add_member(self, session_id: str, agent_id: str) -> None:
        try:
            self._s.execute(
                "INSERT INTO session_members VALUES (?, ?, ?)",
                (session_id, str(agent_id), int(time.time())),
            )
        except Exception:
            pass

    def list_with_unread(self, agent_id: str) -> list[dict]:
        sessions = self._s.fetchall(
            "SELECT s.* FROM sessions s "
            "JOIN session_members sm ON s.id = sm.session_id "
            "WHERE sm.agent_id = ? ORDER BY s.created_at DESC",
            (agent_id,),
        )
        for s in sessions:
            last = self._s.fetchone(
                "SELECT MAX(created_at) AS last_ts FROM session_messages WHERE session_id = ?",
                (s["id"],),
            )
            s["last_ts"] = (last["last_ts"] if last and last["last_ts"] else s["created_at"])
            row = self._s.fetchone(
                "SELECT COUNT(*) AS cnt FROM session_messages "
                "WHERE session_id = ? AND from_id != ? "
                "AND NOT EXISTS (SELECT 1 FROM session_message_reads "
                "  WHERE message_id = session_messages.id AND agent_id = ?)",
                (s["id"], agent_id, agent_id),
            )
            s["unread"] = row["cnt"] if row else 0
            members = self.get_members(s["id"])
            s["member_ids"] = ",".join(members)
            s["member_count"] = len(members)
        sessions.sort(key=lambda x: x["last_ts"], reverse=True)
        return sessions

    def send(self, session_id: str, from_id: str, body: str) -> str:
        msg_id = str(uuid.uuid4())
        self._s.execute(
            "INSERT INTO session_messages VALUES (?, ?, ?, ?, ?)",
            (msg_id, session_id, from_id, body, int(time.time())),
        )
        return msg_id

    def count_messages(self, session_id: str, agent_id: str = "",
                       unread_only: bool = False) -> int:
        if unread_only and agent_id:
            row = self._s.fetchone(
                "SELECT COUNT(*) AS cnt FROM session_messages "
                "WHERE session_id = ? AND from_id != ? "
                "AND NOT EXISTS (SELECT 1 FROM session_message_reads "
                "  WHERE message_id = session_messages.id AND agent_id = ?)",
                (session_id, agent_id, agent_id),
            )
        else:
            row = self._s.fetchone(
                "SELECT COUNT(*) AS cnt FROM session_messages WHERE session_id = ?",
                (session_id,),
            )
        return row["cnt"] if row else 0

    def get_messages(self, session_id: str, page: int = 1,
                     per_page: int = _DEFAULT_PER_PAGE,
                     agent_id: str = "", unread_only: bool = False) -> list[dict]:
        offset = (page - 1) * per_page
        if unread_only and agent_id:
            rows = self._s.fetchall(
                "SELECT * FROM session_messages "
                "WHERE session_id = ? AND from_id != ? "
                "AND NOT EXISTS (SELECT 1 FROM session_message_reads "
                "  WHERE message_id = session_messages.id AND agent_id = ?) "
                "ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (session_id, agent_id, agent_id, per_page, offset),
            )
        else:
            rows = self._s.fetchall(
                "SELECT * FROM session_messages WHERE session_id = ? "
                "ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (session_id, per_page, offset),
            )
        if agent_id and rows:
            for m in rows:
                try:
                    self._s.execute(
                        "INSERT INTO session_message_reads VALUES (?, ?)",
                        (m["id"], agent_id),
                    )
                except Exception:
                    pass
        return rows


# Backward-compat alias
MessageStore = SessionStore


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_message(inact_app, prefix: str, store: SessionStore,
                   agents_prefix: str = "/agents",
                   notify_fn=None, kind_fn=None, member_fn=None) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_msg_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _agent_id() -> str:
        return (
            request.args.get("agent_id", "")
            or request.headers.get("X-Agent-Id", "")
        ).strip()

    def _root():
        agent_id = _agent_id()
        lines = [
            "# Session Messaging\n\n",
            f"sessions_url = {toml_str(prefix + '/sessions')}\n",
        ]
        if agent_id:
            sessions = store.list_with_unread(agent_id)
            total_unread = sum(s["unread"] for s in sessions)
            lines += [
                f"\nagent_id = {toml_str(agent_id)}\n",
                f"sessions = {len(sessions)}\n",
                f"unread   = {total_unread}\n",
            ]
        else:
            lines.append(f"\n# tip: set X-Agent-Id header or ?agent_id= to see your stats\n")
        return text_response("".join(lines))

    def _sessions():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            name = (body.get("name") or "").strip()
            created_by = str(body.get("created_by") or _agent_id()).strip()
            members = [str(m).strip() for m in (body.get("members") or []) if str(m).strip()]
            if not created_by:
                return text_response(
                    "ERROR 400: X-Agent-Id header or 'created_by' required\n"
                    f"POST {prefix}/sessions\n"
                    '  Body: {"name":"opt","members":["1","2"]}\n',
                    400,
                )
            session_id = store.create(name, created_by, members)
            if notify_fn:
                label = name or str(session_id)
                for member_id in members:
                    if str(member_id) != str(created_by):
                        notify_fn(str(member_id), created_by,
                                  f"[session:{session_id}] You were added to '{label}'")
            return text_response(
                f"OK\n"
                f"id   = {session_id}\n"
                f"url  = {toml_str(prefix + '/sessions/' + str(session_id))}\n"
            )

        # GET — list sessions for this agent
        agent_id = _agent_id()
        if not agent_id:
            return text_response(
                "ERROR 400: agent_id required\n"
                f"Usage: GET {prefix}/sessions?agent_id=<id>\n"
                "       or set X-Agent-Id header\n",
                400,
            )
        page, per_page = _parse_page_params()
        all_sessions = store.list_with_unread(agent_id)
        total = len(all_sessions)
        offset = (page - 1) * per_page
        page_sessions = all_sessions[offset:offset + per_page]
        lines = [
            f"# Sessions for {agent_id}\n",
            _page_header(page, per_page, total),
            "\n",
        ]
        for s in page_sessions:
            member_parts = []
            for mid in (s["member_ids"].split(",") if s["member_ids"] else []):
                info = member_fn(mid) if member_fn else {"name": "", "kind": "agent"}
                display = info["name"] or (
                    "Human #" + mid if info["kind"] == "human" else "Agent #" + mid
                )
                member_parts.append(f"{display}#{mid}")
            lines.append("[[sessions]]\n")
            lines.append(f"id        = {s['id']}\n")
            if s["name"]:
                lines.append(f"name      = {toml_str(s['name'])}\n")
            lines.append(f"members   = {toml_str(', '.join(member_parts))}\n")
            lines.append(f"unread    = {s['unread']}\n")
            lines.append(f"last_date = {toml_str(_fmt_ts(s['last_ts']))}\n")
            lines.append(f"url       = {toml_str(prefix + '/sessions/' + str(s['id']))}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _session_detail(session_id: str):
        s = store.get(session_id)
        if not s:
            return text_response("ERROR 404: session not found\n", 404)
        members = store.get_members(session_id)
        lines = [
            f"# Session: {s['name'] or session_id}\n\n",
            f"id           = {s['id']}\n",
            f"created_by   = {toml_str(s['created_by'])}\n",
            f"created_at   = {toml_str(_fmt_ts(s['created_at']))}\n",
            f"member_count = {len(members)}\n",
            f"messages_url = {toml_str(prefix + '/sessions/' + session_id + '/messages')}\n",
            f"send_url     = {toml_str(prefix + '/sessions/' + session_id + '/send')}\n",
            "\n",
        ]
        if s["name"]:
            lines.insert(2, f"name         = {toml_str(s['name'])}\n")
        for m in members:
            info = member_fn(m) if member_fn else {"name": "", "kind": kind_fn(m) if kind_fn else "agent"}
            display = info["name"] or (
                "Human #" + m if info["kind"] == "human" else "Agent #" + m
            )
            lines.append(
                f"[[members]]\n"
                f"id   = {m}\n"
                f"name = {toml_str(display)}\n"
                f"kind = {toml_str(info['kind'])}\n"
                "\n"
            )
        return text_response("".join(lines))

    def _session_send(session_id: str):
        s = store.get(session_id)
        if not s:
            return text_response("ERROR 404: session not found\n", 404)
        body = request.get_json(force=True, silent=True) or {}
        from_id = str(body.get("from") or _agent_id()).strip()
        text_body = (body.get("body") or "").strip()
        if not from_id:
            return text_response("ERROR 400: X-Agent-Id or 'from' required\n", 400)
        if not text_body:
            return text_response("ERROR 400: 'body' required\n", 400)
        msg_id = store.send(session_id, from_id, text_body)
        if notify_fn:
            for member_id in store.get_members(session_id):
                if str(member_id) != str(from_id):
                    notify_fn(str(member_id), from_id,
                              f"[session:{session_id}] {text_body}")
        return text_response(f"OK\nid = {toml_str(msg_id)}\n")

    def _session_messages(session_id: str):
        s = store.get(session_id)
        if not s:
            return text_response("ERROR 404: session not found\n", 404)
        agent_id = _agent_id()
        unread_only = request.args.get("unread", "0") == "1"
        page, per_page = _parse_page_params()
        total = store.count_messages(session_id, agent_id, unread_only)
        msgs = store.get_messages(session_id, page, per_page, agent_id, unread_only)
        lines = [
            f"# Messages in '{s['name'] or session_id[:8]}'\n",
            _page_header(page, per_page, total),
            "\n",
        ]
        for m in msgs:
            fk = kind_fn(m["from_id"]) if kind_fn else ""
            lines.append("[[messages]]\n")
            lines.append(f"id        = {toml_str(m['id'])}\n")
            lines.append(f"from      = {toml_str(m['from_id'])}\n")
            if fk:
                lines.append(f"from_kind = {toml_str(fk)}\n")
            lines.append(f"body      = {toml_str(m['body'])}\n")
            lines.append(f"date      = {toml_str(_fmt_ts(m['created_at']))}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _session_members(session_id: str):
        s = store.get(session_id)
        if not s:
            return text_response("ERROR 404: session not found\n", 404)
        body = request.get_json(force=True, silent=True) or {}
        agent_id = str(body.get("agent_id") or "").strip()
        if not agent_id:
            return text_response("ERROR 400: 'agent_id' required\n", 400)
        store.add_member(session_id, agent_id)
        if notify_fn:
            notify_fn(agent_id, s["created_by"],
                      f"[session:{session_id}] You were added to '{s['name'] or session_id[:8]}'")
        return text_response(f"OK\nagent_id = {toml_str(agent_id)}\n")

    def _human():
        from ...render import render_template, workspace_nav
        from ...utils import html_response
        html = render_template(
            "message_human.html",
            title="Chat",
            prefix=prefix,
            agents_prefix=agents_prefix,
            register_url="/_human" + agents_prefix,
            workspace_links=workspace_nav("/_human/msg/"),
            show_identity=True,
        )
        return html_response(html)

    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_root", view_func=_root)
    flask_app.add_url_rule(
        prefix + "/sessions",
        endpoint=ep + "_sessions", view_func=_sessions, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/sessions/<session_id>",
        endpoint=ep + "_session_detail", view_func=_session_detail)
    flask_app.add_url_rule(
        prefix + "/sessions/<session_id>/send",
        endpoint=ep + "_session_send", view_func=_session_send, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/sessions/<session_id>/messages",
        endpoint=ep + "_session_messages", view_func=_session_messages)
    flask_app.add_url_rule(
        prefix + "/sessions/<session_id>/members",
        endpoint=ep + "_session_members", view_func=_session_members, methods=["POST"])
    inact_app._human_views[prefix] = lambda path: _human()


def mount_message(inact_app, prefix: str, storage,
                  agents_prefix: str = "/agents",
                  notify_storage=None,
                  registry=None) -> None:
    """
    Mount a session-based messaging service at *prefix*.

    A session is a shared conversation between any number of participants
    (humans or agents).  Creating a session with two members is a DM;
    creating one with more is a group chat — the API is identical.

    *storage*        — database URL/path or Storage instance.
    *agents_prefix*  — register app prefix used by the chat UI for agent listing.
    *notify_storage* — if provided, every sent message fires a notification to
                       all other session members.
    *registry*       — agent registry storage; enables ``from_kind`` enrichment
                       so agents can tell humans from bots in every response.

    Example::

        mount_message(app, "/msg", "./msg.db")
        mount_message(app, "/msg", "./msg.db",
                      notify_storage="./notify.db",
                      registry="./agents.db")
    """
    from ...storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage

    # Build kind_fn and member_fn from the registry when available
    _reg_source = registry if registry is not None else storage
    kind_fn = None
    member_fn = None
    if _reg_source is not None:
        from .register import AgentRegistry
        _reg = _reg_source if isinstance(_reg_source, AgentRegistry) \
               else AgentRegistry(make_storage(_reg_source) if isinstance(_reg_source, str) else _reg_source)

        def kind_fn(agent_id: str, _r=_reg) -> str:
            if not agent_id:
                return "agent"
            try:
                row = _r.get(int(agent_id))
                return (row.get("kind", "agent") if row else "agent")
            except Exception:
                return "agent"

        def member_fn(agent_id: str, _r=_reg) -> dict:
            if not agent_id:
                return {"name": "", "kind": "agent"}
            try:
                row = _r.get(int(agent_id))
                if row:
                    return {"name": row.get("name", "") or "", "kind": row.get("kind", "agent") or "agent"}
            except Exception:
                pass
            return {"name": "", "kind": "agent"}

    notify_fn = None
    if notify_storage is not None:
        from ..notify import NotifyStore, _push
        ns = make_storage(notify_storage) if isinstance(notify_storage, str) else notify_storage
        nstore = NotifyStore(ns)

        def notify_fn(to_id: str, from_id: str, message: str) -> None:
            notif_id = nstore.send(to_id, message, from_id)
            fk = kind_fn(from_id) if kind_fn else ""
            _push(nstore, to_id, notif_id, message, from_id, from_kind=fk)

    attach_message(inact_app, p, SessionStore(backend),
                   agents_prefix="/" + agents_prefix.strip("/"),
                   notify_fn=notify_fn, kind_fn=kind_fn, member_fn=member_fn)
    inact_app._app_mounts.append((p, (
        f"\nSession messaging: {p}\n"
        f'  POST   {p}/sessions                    create session  body: {{"name":"opt","members":["1","2"]}}\n'
        f"  GET    {p}/sessions                    list my sessions  (?agent_id=<id>  ?page=1)\n"
        f"  GET    {p}/sessions/{{id}}               session details + members\n"
        f'  POST   {p}/sessions/{{id}}/send          send message  body: {{"body":"..."}}\n'
        f"  GET    {p}/sessions/{{id}}/messages      session messages  (?agent_id=<id>  ?unread=1)\n"
        f"  POST   {p}/sessions/{{id}}/members       add member  body: {{\"agent_id\":\"2\"}}\n"
        f"  # identity: X-Agent-Id header or ?agent_id= param\n"
    )))
