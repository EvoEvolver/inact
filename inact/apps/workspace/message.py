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

from ...storage import Storage
from ...utils import text_response, html_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS messages (
        id         TEXT    PRIMARY KEY,
        from_id    TEXT    NOT NULL,
        to_id      TEXT    NOT NULL,
        body       TEXT    NOT NULL DEFAULT '',
        read       INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT  NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS groups (
        id         TEXT    PRIMARY KEY,
        name       TEXT    NOT NULL DEFAULT '',
        created_by TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS group_members (
        group_id   TEXT    NOT NULL,
        agent_id   TEXT    NOT NULL,
        joined_at  BIGINT  NOT NULL,
        PRIMARY KEY (group_id, agent_id)
    )""",
    """CREATE TABLE IF NOT EXISTS group_messages (
        id         TEXT    PRIMARY KEY,
        group_id   TEXT    NOT NULL,
        from_id    TEXT    NOT NULL,
        body       TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS group_message_reads (
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

    # ---------- group chats ----------

    def create_group(self, name: str, created_by: str, member_ids: list) -> str:
        group_id = str(uuid.uuid4())
        ts = int(time.time())
        self._s.execute(
            "INSERT INTO groups VALUES (?, ?, ?, ?)",
            (group_id, name, created_by, ts),
        )
        for agent_id in list({str(m) for m in member_ids} | {str(created_by)}):
            try:
                self._s.execute(
                    "INSERT INTO group_members VALUES (?, ?, ?)",
                    (group_id, agent_id, ts),
                )
            except Exception:
                pass
        return group_id

    def get_group(self, group_id: str) -> dict | None:
        return self._s.fetchone("SELECT * FROM groups WHERE id = ?", (group_id,))

    def list_groups(self, agent_id: str) -> list[dict]:
        return self._s.fetchall(
            "SELECT g.* FROM groups g "
            "JOIN group_members gm ON g.id = gm.group_id "
            "WHERE gm.agent_id = ? ORDER BY g.created_at DESC",
            (agent_id,),
        )

    def get_group_members(self, group_id: str) -> list[str]:
        rows = self._s.fetchall(
            "SELECT agent_id FROM group_members WHERE group_id = ? ORDER BY joined_at ASC",
            (group_id,),
        )
        return [r["agent_id"] for r in rows]

    def add_group_member(self, group_id: str, agent_id: str) -> None:
        try:
            self._s.execute(
                "INSERT INTO group_members VALUES (?, ?, ?)",
                (group_id, str(agent_id), int(time.time())),
            )
        except Exception:
            pass

    def send_group_message(self, group_id: str, from_id: str, body: str) -> str:
        msg_id = str(uuid.uuid4())
        self._s.execute(
            "INSERT INTO group_messages VALUES (?, ?, ?, ?, ?)",
            (msg_id, group_id, from_id, body, int(time.time())),
        )
        return msg_id

    def count_group_messages(self, group_id: str,
                              agent_id: str = "", unread_only: bool = False) -> int:
        if unread_only and agent_id:
            row = self._s.fetchone(
                "SELECT COUNT(*) AS cnt FROM group_messages "
                "WHERE group_id = ? AND from_id != ? "
                "AND NOT EXISTS (SELECT 1 FROM group_message_reads "
                "  WHERE message_id = group_messages.id AND agent_id = ?)",
                (group_id, agent_id, agent_id),
            )
        else:
            row = self._s.fetchone(
                "SELECT COUNT(*) AS cnt FROM group_messages WHERE group_id = ?",
                (group_id,),
            )
        return row["cnt"] if row else 0

    def get_group_messages(self, group_id: str, page: int = 1,
                            per_page: int = _DEFAULT_PER_PAGE,
                            agent_id: str = "",
                            unread_only: bool = False) -> list[dict]:
        offset = (page - 1) * per_page
        if unread_only and agent_id:
            rows = self._s.fetchall(
                "SELECT * FROM group_messages "
                "WHERE group_id = ? AND from_id != ? "
                "AND NOT EXISTS (SELECT 1 FROM group_message_reads "
                "  WHERE message_id = group_messages.id AND agent_id = ?) "
                "ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (group_id, agent_id, agent_id, per_page, offset),
            )
        else:
            rows = self._s.fetchall(
                "SELECT * FROM group_messages WHERE group_id = ? "
                "ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (group_id, per_page, offset),
            )
        if agent_id and rows:
            for m in rows:
                try:
                    self._s.execute(
                        "INSERT INTO group_message_reads VALUES (?, ?)",
                        (m["id"], agent_id),
                    )
                except Exception:
                    pass
        return rows

    def list_groups_with_unread(self, agent_id: str) -> list[dict]:
        groups = self.list_groups(agent_id)
        for g in groups:
            row = self._s.fetchone(
                "SELECT COUNT(*) AS cnt FROM group_messages "
                "WHERE group_id = ? AND from_id != ? "
                "AND NOT EXISTS (SELECT 1 FROM group_message_reads "
                "  WHERE message_id = group_messages.id AND agent_id = ?)",
                (g["id"], agent_id, agent_id),
            )
            g["unread"] = row["cnt"] if row else 0
        return groups

    def list_conversations(self, agent_id: str, page: int = 1,
                           per_page: int = _DEFAULT_PER_PAGE) -> tuple[list[dict], int]:
        """Return (page_items, total) sorted by most-recent message descending."""
        # DM conversations: one row per distinct peer, latest timestamp + unread count
        dms = self._s.fetchall(
            "SELECT peer_id, MAX(last_ts) AS last_ts, SUM(unread) AS unread "
            "FROM ("
            "  SELECT "
            "    CASE WHEN from_id = ? THEN to_id ELSE from_id END AS peer_id,"
            "    created_at AS last_ts,"
            "    CASE WHEN to_id = ? AND read = 0 THEN 1 ELSE 0 END AS unread"
            "  FROM messages WHERE from_id = ? OR to_id = ?"
            ") GROUP BY peer_id",
            (agent_id, agent_id, agent_id, agent_id),
        )
        # Group conversations: latest message timestamp + unread for this agent
        grps = self._s.fetchall(
            "SELECT g.id AS group_id, g.name AS group_name,"
            "  COALESCE(MAX(gm.created_at), g.created_at) AS last_ts,"
            "  (SELECT COUNT(*) FROM group_messages gm2"
            "   WHERE gm2.group_id = g.id AND gm2.from_id != ?"
            "   AND NOT EXISTS (SELECT 1 FROM group_message_reads r"
            "     WHERE r.message_id = gm2.id AND r.agent_id = ?)) AS unread"
            " FROM groups g"
            " JOIN group_members mem ON g.id = mem.group_id AND mem.agent_id = ?"
            " LEFT JOIN group_messages gm ON gm.group_id = g.id"
            " GROUP BY g.id",
            (agent_id, agent_id, agent_id),
        )
        all_convs: list[dict] = []
        for d in dms:
            all_convs.append({
                "type": "dm",
                "id": d["peer_id"],
                "name": "",
                "last_ts": d["last_ts"] or 0,
                "unread": d["unread"] or 0,
            })
        for g in grps:
            all_convs.append({
                "type": "group",
                "id": g["group_id"],
                "name": g["group_name"] or "",
                "last_ts": g["last_ts"] or 0,
                "unread": g["unread"] or 0,
            })
        all_convs.sort(key=lambda c: c["last_ts"], reverse=True)
        total = len(all_convs)
        offset = (page - 1) * per_page
        return all_convs[offset:offset + per_page], total

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
                   agents_prefix: str = "/agents",
                   notify_fn=None) -> None:
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
            f"# Messaging\n\n",
            f"send_url   = {toml_str(prefix + '/send')}\n",
            f"inbox_url  = {toml_str(prefix + '/inbox')}\n",
            f"sent_url   = {toml_str(prefix + '/sent')}\n",
            f"agents_url = {toml_str(prefix + '/agents')}\n",
            f"human_url  = {toml_str('/_human' + prefix)}\n",
        ]
        if agent_id:
            unread = store.count_inbox(agent_id, unread_only=True)
            total  = store.count_inbox(agent_id)
            lines.append(f"\nagent_id   = {toml_str(agent_id)}\n")
            lines.append(f"unread     = {unread}\n")
            lines.append(f"total      = {total}\n")
        else:
            lines.append(f"\n# tip: set X-Agent-Id header or ?agent_id= to see your inbox stats\n")
        return text_response("".join(lines))

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
        if notify_fn:
            notify_fn(to_id, from_id, text)
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
            lines.append(f"body = {toml_str(m['body'])}\n")
            lines.append(f"url  = {toml_str(prefix + '/sent/' + m['id'])}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _sent_msg(msg_id: str):
        """Read a sent message by ID — does NOT mark it as read (sender's view)."""
        m = store._s.fetchone("SELECT * FROM messages WHERE id = ?", (msg_id,))
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

    def _conversations():
        agent_id = _agent_id()
        if not agent_id:
            return text_response("ERROR 400: agent_id required\n", 400)
        page, per_page = _parse_page_params()
        convs, total = store.list_conversations(agent_id, page, per_page)
        lines = [
            f"# Conversations for agent {agent_id}\n",
            _page_header(page, per_page, total),
            "\n",
        ]
        for c in convs:
            lines.append("[[conversations]]\n")
            lines.append(f"type      = {toml_str(c['type'])}\n")
            lines.append(f"id        = {toml_str(c['id'])}\n")
            if c["name"]:
                lines.append(f"name      = {toml_str(c['name'])}\n")
            lines.append(f"last_date = {toml_str(_fmt_ts(c['last_ts']))}\n")
            lines.append(f"unread    = {c['unread']}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _groups():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            name = (body.get("name") or "").strip()
            created_by = str(body.get("created_by") or _agent_id()).strip()
            members = [str(m).strip() for m in (body.get("members") or []) if str(m).strip()]
            if not name:
                return text_response("ERROR 400: 'name' required\n", 400)
            if not created_by:
                return text_response("ERROR 400: 'created_by' required (or set X-Agent-Id)\n", 400)
            group_id = store.create_group(name, created_by, members)
            if notify_fn:
                for member_id in members:
                    if str(member_id) != str(created_by):
                        notify_fn(str(member_id), created_by,
                                  f"[group:{group_id}] You were added to group '{name}'")
            return text_response(
                f"OK\n"
                f"id   = {toml_str(group_id)}\n"
                f"name = {toml_str(name)}\n"
                f"url  = {toml_str(prefix + '/groups/' + group_id)}\n"
            )
        # GET — list groups for this agent
        agent_id = _agent_id()
        if not agent_id:
            return text_response("ERROR 400: agent_id required\n", 400)
        groups = store.list_groups_with_unread(agent_id)
        lines = [f"# Groups for agent {agent_id}\n\n"]
        for g in groups:
            members = store.get_group_members(g["id"])
            lines.append("[[groups]]\n")
            lines.append(f"id         = {toml_str(g['id'])}\n")
            lines.append(f"name       = {toml_str(g['name'])}\n")
            lines.append(f"created_by = {toml_str(g['created_by'])}\n")
            lines.append(f"created_at = {toml_str(_fmt_ts(g['created_at']))}\n")
            lines.append(f"members    = {len(members)}\n")
            lines.append(f"unread     = {g.get('unread', 0)}\n")
            lines.append(f"url        = {toml_str(prefix + '/groups/' + g['id'])}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _group_detail(group_id: str):
        g = store.get_group(group_id)
        if not g:
            return text_response("ERROR 404: group not found\n", 404)
        members = store.get_group_members(group_id)
        lines = [
            f"# Group: {g['name']}\n\n",
            f"id           = {toml_str(g['id'])}\n",
            f"name         = {toml_str(g['name'])}\n",
            f"created_by   = {toml_str(g['created_by'])}\n",
            f"created_at   = {toml_str(_fmt_ts(g['created_at']))}\n",
            f"member_count = {len(members)}\n",
            f"messages_url = {toml_str(prefix + '/groups/' + group_id + '/messages')}\n",
            f"send_url     = {toml_str(prefix + '/groups/' + group_id + '/send')}\n",
            "\n",
        ]
        for m in members:
            lines.append(f"[[members]]\nagent_id = {toml_str(m)}\n\n")
        return text_response("".join(lines))

    def _group_send(group_id: str):
        g = store.get_group(group_id)
        if not g:
            return text_response("ERROR 404: group not found\n", 404)
        body = request.get_json(force=True, silent=True) or {}
        from_id = str(body.get("from") or _agent_id()).strip()
        text_body = (body.get("body") or "").strip()
        if not from_id:
            return text_response("ERROR 400: 'from' required\n", 400)
        if not text_body:
            return text_response("ERROR 400: 'body' required\n", 400)
        msg_id = store.send_group_message(group_id, from_id, text_body)
        if notify_fn:
            for member_id in store.get_group_members(group_id):
                if str(member_id) != str(from_id):
                    notify_fn(str(member_id), from_id,
                              f"[group:{group_id}] {text_body}")
        return text_response(f"OK\nid = {toml_str(msg_id)}\n")

    def _group_messages(group_id: str):
        g = store.get_group(group_id)
        if not g:
            return text_response("ERROR 404: group not found\n", 404)
        agent_id = _agent_id()
        unread_only = request.args.get("unread", "0") == "1"
        page, per_page = _parse_page_params()
        total = store.count_group_messages(group_id, agent_id, unread_only)
        msgs = store.get_group_messages(group_id, page, per_page, agent_id, unread_only)
        lines = [
            f"# Messages in '{g['name']}'\n",
            _page_header(page, per_page, total),
            "\n",
        ]
        for m in msgs:
            lines.append("[[messages]]\n")
            lines.append(f"id   = {toml_str(m['id'])}\n")
            lines.append(f"from = {toml_str(m['from_id'])}\n")
            lines.append(f"body = {toml_str(m['body'])}\n")
            lines.append(f"date = {toml_str(_fmt_ts(m['created_at']))}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _group_members(group_id: str):
        g = store.get_group(group_id)
        if not g:
            return text_response("ERROR 404: group not found\n", 404)
        body = request.get_json(force=True, silent=True) or {}
        agent_id = str(body.get("agent_id") or "").strip()
        if not agent_id:
            return text_response("ERROR 400: 'agent_id' required\n", 400)
        store.add_group_member(group_id, agent_id)
        if notify_fn:
            notify_fn(agent_id, g["created_by"],
                      f"[group:{group_id}] You were added to group '{g['name']}'")
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
        prefix + "/sent/<msg_id>",
        endpoint=ep + "_sent_msg", view_func=_sent_msg)
    flask_app.add_url_rule(
        prefix + "/agents",
        endpoint=ep + "_agents", view_func=_agents)
    flask_app.add_url_rule(
        prefix + "/conversations",
        endpoint=ep + "_conversations", view_func=_conversations)
    flask_app.add_url_rule(
        prefix + "/groups",
        endpoint=ep + "_groups", view_func=_groups, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/groups/<group_id>",
        endpoint=ep + "_group_detail", view_func=_group_detail)
    flask_app.add_url_rule(
        prefix + "/groups/<group_id>/send",
        endpoint=ep + "_group_send", view_func=_group_send, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/groups/<group_id>/messages",
        endpoint=ep + "_group_messages", view_func=_group_messages)
    flask_app.add_url_rule(
        prefix + "/groups/<group_id>/members",
        endpoint=ep + "_group_members", view_func=_group_members, methods=["POST"])
    inact_app._human_views[prefix] = lambda path: _human()


def mount_message(inact_app, prefix: str, storage,
                  agents_prefix: str = "/agents",
                  notify_storage=None) -> None:
    """
    Mount an agent messaging service at *prefix*.

    *storage*        — database URL/path or Storage instance.
    *agents_prefix*  — register app prefix for agent listing in the chat UI.
    *notify_storage* — if provided (same URL/Storage as :func:`mount_notify`),
                       every sent message fires a notification to the recipient
                       so registered callbacks are woken immediately.

    Example::

        mount_message(app, "/msg", "./msg.db")
        mount_message(app, "/msg", "./msg.db",
                      notify_storage="./notify.db")   # wake agents on send
    """
    from ...storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage

    notify_fn = None
    if notify_storage is not None:
        from ..notify import NotifyStore, _push
        ns = make_storage(notify_storage) if isinstance(notify_storage, str) else notify_storage
        nstore = NotifyStore(ns)

        def notify_fn(to_id: str, from_id: str, message: str) -> None:
            notif_id = nstore.send(to_id, message, from_id)
            _push(nstore, to_id, notif_id, message, from_id)

    attach_message(inact_app, p, MessageStore(backend),
                   agents_prefix="/" + agents_prefix.strip("/"),
                   notify_fn=notify_fn)
    inact_app._app_mounts.append((p, (
        f"\nAgent messaging: {p}\n"
        f'  POST   {p}/send                    send DM  body: {{"from":"1","to":"2","body":"..."}}\n'
        f"  GET    {p}/inbox                   received messages  (?agent_id=<id>  ?unread=1  ?page=1)\n"
        f"  GET    {p}/inbox/{{id}}              read message (marks read)\n"
        f"  DELETE {p}/inbox/{{id}}              delete message\n"
        f"  GET    {p}/sent                    sent messages  (?agent_id=<id>  ?page=1)\n"
        f"  GET    {p}/agents                  known agents  (?page=1)\n"
        f'  POST   {p}/groups                  create group  body: {{"name":"...","created_by":"1","members":["2","3"]}}\n'
        f"  GET    {p}/groups                  list groups  (?agent_id=<id>)\n"
        f"  GET    {p}/groups/{{id}}             group details + members\n"
        f"  POST   {p}/groups/{{id}}/send        send group message  body: {{\"from\":\"1\",\"body\":\"...\"}}\n"
        f"  GET    {p}/groups/{{id}}/messages    group messages  (?agent_id=<id>  ?unread=1  ?page=1)\n"
        f"  POST   {p}/groups/{{id}}/members     add member  body: {{\"agent_id\":\"2\"}}\n"
        f"  # identity: X-Agent-Id header or ?agent_id= param\n"
    )))
