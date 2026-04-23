"""
Agent mailbox — persistent inbox/sent/threads.

mount_mailbox(prefix, storage) registers:

  GET    {prefix}/inbox                 list inbox (TOML)
  GET    {prefix}/inbox?unread=1        unread only
  GET    {prefix}/inbox/{id}            read message (auto-marks read)
  DELETE {prefix}/inbox/{id}            delete message
  POST   {prefix}/inbox/{id}/.reply     reply  body: {"body": "...", "from": "..."}
  GET    {prefix}/sent                  list sent (TOML)
  POST   {prefix}/compose               send    body: {"to":"...","subject":"...","body":"...","from":"..."}
  GET    {prefix}/thread/{id}           full conversation thread (TOML)

*storage* accepts a :class:`~inact.storage.Storage` object or any URL/path
accepted by :func:`~inact.storage.make_storage`.
"""

from __future__ import annotations

import time
import uuid

from flask import request

from ..storage import Storage
from ..utils import text_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS messages (
        id        TEXT    PRIMARY KEY,
        folder    TEXT    NOT NULL DEFAULT 'inbox',
        from_addr TEXT    NOT NULL DEFAULT '',
        to_addr   TEXT    NOT NULL DEFAULT '',
        subject   TEXT    NOT NULL DEFAULT '(no subject)',
        body      TEXT    NOT NULL DEFAULT '',
        ts        BIGINT  NOT NULL,
        read      INTEGER NOT NULL DEFAULT 0,
        thread_id TEXT    NOT NULL DEFAULT ''
    )""",
]


class Mailbox:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)

    def compose(self, from_addr: str, to_addr: str, subject: str, body: str,
                thread_id: str = "") -> str:
        msg_id = str(uuid.uuid4())
        ts = int(time.time())
        if not thread_id:
            thread_id = msg_id
        ops = [
            ("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
             (msg_id, "sent", from_addr, to_addr, subject, body, ts, 1, thread_id)),
        ]
        if "://" not in to_addr:
            ops.append((
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "inbox", from_addr, to_addr,
                 subject, body, ts, 0, thread_id),
            ))
        self._s.batch(ops)
        return msg_id

    def inbox(self, unread_only: bool = False) -> list[dict]:
        q = "SELECT * FROM messages WHERE folder='inbox'"
        if unread_only:
            q += " AND read=0"
        return self._s.fetchall(q + " ORDER BY ts DESC")

    def sent(self) -> list[dict]:
        return self._s.fetchall(
            "SELECT * FROM messages WHERE folder='sent' ORDER BY ts DESC"
        )

    def get(self, msg_id: str) -> dict | None:
        m = self._s.fetchone("SELECT * FROM messages WHERE id=?", (msg_id,))
        if not m:
            return None
        self._s.execute("UPDATE messages SET read=1 WHERE id=?", (msg_id,))
        return m

    def thread(self, thread_id: str) -> list[dict]:
        return self._s.fetchall(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY ts ASC",
            (thread_id,),
        )

    def delete(self, msg_id: str) -> bool:
        return self._s.execute("DELETE FROM messages WHERE id=?", (msg_id,)) > 0

    def unread_count(self) -> int:
        rows = self._s.fetchall(
            "SELECT COUNT(*) AS cnt FROM messages WHERE folder='inbox' AND read=0"
        )
        return rows[0]["cnt"] if rows else 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _msg_toml(m: dict, prefix: str) -> str:
    return (
        "[[messages]]\n"
        f"id      = {toml_str(m['id'])}\n"
        f"from    = {toml_str(m['from_addr'])}\n"
        f"to      = {toml_str(m['to_addr'])}\n"
        f"subject = {toml_str(m['subject'])}\n"
        f"date    = {toml_str(_fmt_ts(m['ts']))}\n"
        f"read    = {str(bool(m['read'])).lower()}\n"
        f"url     = {toml_str(prefix + '/inbox/' + m['id'])}\n"
        f"thread  = {toml_str(prefix + '/thread/' + m['thread_id'])}\n"
        "\n"
    )


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_mailbox(inact_app, prefix: str, mailbox: Mailbox) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_mail_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _inbox():
        unread_only = request.args.get("unread", "0") == "1"
        msgs = mailbox.inbox(unread_only)
        unread = mailbox.unread_count()
        lines = [
            "# Inbox\n",
            f"# {len(msgs)} message(s), {unread} unread\n",
            "# tip: ?unread=1 to filter unread\n\n",
        ]
        for m in msgs:
            lines.append(_msg_toml(m, prefix))
        return text_response("".join(lines))

    def _msg(msg_id: str):
        if request.method == "DELETE":
            ok = mailbox.delete(msg_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)
        m = mailbox.get(msg_id)
        if not m:
            return text_response("ERROR 404: message not found\n", 404)
        return text_response(
            f"# {m['subject']}\n\n"
            f"from   = {toml_str(m['from_addr'])}\n"
            f"to     = {toml_str(m['to_addr'])}\n"
            f"date   = {toml_str(_fmt_ts(m['ts']))}\n"
            f"thread = {toml_str(prefix + '/thread/' + m['thread_id'])}\n"
            "\n---\n\n"
            + m["body"] + "\n"
        )

    def _reply(msg_id: str):
        original = mailbox.get(msg_id)
        if not original:
            return text_response("ERROR 404: message not found\n", 404)
        body_data = request.get_json(force=True, silent=True) or {}
        body_text = (body_data.get("body") or "").strip()
        if not body_text:
            return text_response(
                "ERROR 400: 'body' required\n"
                f'Body: {{"body": "reply text", "from": "optional"}}\n', 400
            )
        from_addr = (body_data.get("from") or "").strip()
        subject = original["subject"]
        if not subject.startswith("Re:"):
            subject = "Re: " + subject
        new_id = mailbox.compose(
            from_addr, original["from_addr"], subject, body_text,
            thread_id=original["thread_id"],
        )
        return text_response(f"OK\nid = {toml_str(new_id)}\n")

    def _sent():
        msgs = mailbox.sent()
        lines = [f"# Sent\n# {len(msgs)} message(s)\n\n"]
        for m in msgs:
            lines.append(_msg_toml(m, prefix))
        return text_response("".join(lines))

    def _compose():
        body_data = request.get_json(force=True, silent=True) or {}
        to_addr   = (body_data.get("to")      or "").strip()
        subject   = (body_data.get("subject") or "(no subject)").strip()
        body_text = (body_data.get("body")    or "").strip()
        from_addr = (body_data.get("from")    or "").strip()
        if not to_addr:
            return text_response(
                "ERROR 400: 'to' required\n"
                f"POST {prefix}/compose\n"
                '  Body: {"to": "...", "subject": "...", "body": "...", "from": "optional"}\n',
                400,
            )
        if not body_text:
            return text_response("ERROR 400: 'body' required\n", 400)
        msg_id = mailbox.compose(from_addr, to_addr, subject, body_text)
        return text_response(f"OK\nid = {toml_str(msg_id)}\n")

    def _thread(thread_id: str):
        msgs = mailbox.thread(thread_id)
        if not msgs:
            return text_response("ERROR 404: thread not found\n", 404)
        lines = [f"# Thread ({len(msgs)} messages)\n\n"]
        for m in msgs:
            lines.append(_msg_toml(m, prefix))
        return text_response("".join(lines))

    flask_app.add_url_rule(
        prefix + "/inbox",
        endpoint=ep + "_inbox", view_func=_inbox)
    flask_app.add_url_rule(
        prefix + "/inbox/<msg_id>",
        endpoint=ep + "_msg", view_func=_msg, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/inbox/<msg_id>/.reply",
        endpoint=ep + "_reply", view_func=_reply, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/sent",
        endpoint=ep + "_sent", view_func=_sent)
    flask_app.add_url_rule(
        prefix + "/compose",
        endpoint=ep + "_compose", view_func=_compose, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/thread/<thread_id>",
        endpoint=ep + "_thread", view_func=_thread)


def mount_mailbox(inact_app, prefix: str, storage) -> None:
    """
    Mount a persistent mailbox at *prefix*.

    Provides inbox, sent folder, threading, compose, and reply.
    Messages sent to local addresses (no ``://``) are delivered to the inbox.

    *storage* — a database URL/path or a :class:`~inact.storage.Storage` instance.

    Example::

        app.mount_mailbox("/mail", "./data/mail.db")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    attach_mailbox(inact_app, p, Mailbox(backend))
    inact_app._app_mounts.append((p, (
        f"\nMailbox: {p}\n"
        f"  GET    {p}/inbox             list inbox  (?unread=1)\n"
        f"  GET    {p}/inbox/{{id}}        read message (marks read)\n"
        f"  DELETE {p}/inbox/{{id}}        delete message\n"
        f"  POST   {p}/inbox/{{id}}/.reply reply\n"
        f"  GET    {p}/sent              list sent\n"
        f'  POST   {p}/compose           send  body: {{"to":"...","subject":"...","body":"..."}}\n'
        f"  GET    {p}/thread/{{id}}       full thread\n"
    )))
