"""
Notification system for agents — push + persistent inbox.

mount_notify(inact_app, prefix, storage) registers:

  POST {prefix}/register        register a callback URL for an agent
                                body: {"agent_id":"1","callback":"http://host/wake"}
  POST {prefix}/send            send a notification
                                body: {"to":"1","message":"...","from":"optional"}
                                immediately POSTs to the agent's callback if registered
  GET  {prefix}/inbox           list notifications  (X-Agent-Id header required)
                                ?unread=1  ?page=1&per_page=20
  GET  {prefix}/inbox/{id}      read notification (marks read)
  DELETE {prefix}/inbox/{id}    dismiss

A background thread re-fires any unread notification callbacks every
*revival_interval* seconds (default 600 = 10 min) so agents that were
offline when a notification arrived are eventually woken up.
"""

from __future__ import annotations

import threading
import time
import uuid

from flask import request

from ..storage import Storage
from ..utils import text_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS notifications (
        id         TEXT    PRIMARY KEY,
        to_id      TEXT    NOT NULL,
        from_id    TEXT    NOT NULL DEFAULT '',
        message    TEXT    NOT NULL DEFAULT '',
        read       INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT  NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS notify_callbacks (
        agent_id     TEXT    PRIMARY KEY,
        url          TEXT    NOT NULL,
        registered_at BIGINT NOT NULL
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


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

class NotifyStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)

    # callbacks

    def register_callback(self, agent_id: str, url: str) -> None:
        existing = self._s.fetchone(
            "SELECT agent_id FROM notify_callbacks WHERE agent_id = ?", (agent_id,)
        )
        if existing:
            self._s.execute(
                "UPDATE notify_callbacks SET url = ?, registered_at = ? WHERE agent_id = ?",
                (url, int(time.time()), agent_id),
            )
        else:
            self._s.execute(
                "INSERT INTO notify_callbacks VALUES (?, ?, ?)",
                (agent_id, url, int(time.time())),
            )

    def get_callback(self, agent_id: str) -> str | None:
        row = self._s.fetchone(
            "SELECT url FROM notify_callbacks WHERE agent_id = ?", (agent_id,)
        )
        return row["url"] if row else None

    # notifications

    def send(self, to_id: str, message: str, from_id: str = "") -> str:
        notif_id = str(uuid.uuid4())
        self._s.execute(
            "INSERT INTO notifications VALUES (?, ?, ?, ?, ?, ?)",
            (notif_id, to_id, from_id, message, 0, int(time.time())),
        )
        return notif_id

    def count(self, to_id: str, unread_only: bool = False) -> int:
        q = "SELECT COUNT(*) AS cnt FROM notifications WHERE to_id = ?"
        params: tuple = (to_id,)
        if unread_only:
            q += " AND read = 0"
        row = self._s.fetchone(q, params)
        return row["cnt"] if row else 0

    def list_inbox(self, to_id: str, page: int, per_page: int,
                   unread_only: bool = False) -> list[dict]:
        offset = (page - 1) * per_page
        q = "SELECT * FROM notifications WHERE to_id = ?"
        params: list = [to_id]
        if unread_only:
            q += " AND read = 0"
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [per_page, offset]
        return self._s.fetchall(q, tuple(params))

    def get(self, notif_id: str) -> dict | None:
        n = self._s.fetchone("SELECT * FROM notifications WHERE id = ?", (notif_id,))
        if n:
            self._s.execute("UPDATE notifications SET read = 1 WHERE id = ?", (notif_id,))
        return n

    def delete(self, notif_id: str) -> bool:
        return self._s.execute("DELETE FROM notifications WHERE id = ?", (notif_id,)) > 0

    def agents_with_unread(self) -> list[tuple[str, int]]:
        """Return [(agent_id, unread_count)] for all agents with unread notifications."""
        rows = self._s.fetchall(
            "SELECT to_id, COUNT(*) AS cnt FROM notifications "
            "WHERE read = 0 GROUP BY to_id"
        )
        return [(r["to_id"], r["cnt"]) for r in rows]


# ---------------------------------------------------------------------------
# Callback delivery
# ---------------------------------------------------------------------------

def _fire_callback(url: str, payload: dict) -> None:
    try:
        import httpx
        httpx.post(url, json=payload, timeout=5)
    except Exception:
        pass


def _push(store: NotifyStore, to_id: str, notif_id: str,
          message: str, from_id: str, from_kind: str = "") -> None:
    url = store.get_callback(to_id)
    if url:
        payload: dict = {
            "type": "notification",
            "id": notif_id,
            "from": from_id,
            "message": message,
        }
        if from_kind:
            payload["from_kind"] = from_kind
        threading.Thread(
            target=_fire_callback,
            args=(url, payload),
            daemon=True,
        ).start()


def _start_revival(store: NotifyStore, interval: int) -> None:
    """Background thread: re-fire callbacks for unread notifications."""
    def _loop():
        while True:
            time.sleep(interval)
            for agent_id, count in store.agents_with_unread():
                url = store.get_callback(agent_id)
                if url:
                    threading.Thread(
                        target=_fire_callback,
                        args=(url, {
                            "type": "revival",
                            "unread": count,
                            "agent_id": agent_id,
                        }),
                        daemon=True,
                    ).start()

    threading.Thread(target=_loop, daemon=True, name="notify-revival").start()


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_notify(inact_app, prefix: str, store: NotifyStore,
                  kind_fn=None) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_notify_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _register():
        body = request.get_json(force=True, silent=True) or {}
        agent_id = str(body.get("agent_id") or "").strip()
        callback = (body.get("callback") or "").strip()
        if not agent_id:
            return text_response("ERROR 400: 'agent_id' required\n", 400)
        if not callback:
            return text_response("ERROR 400: 'callback' required\n", 400)
        store.register_callback(agent_id, callback)
        return text_response(
            f"OK\nagent_id = {toml_str(agent_id)}\ncallback = {toml_str(callback)}\n"
        )

    def _send():
        body = request.get_json(force=True, silent=True) or {}
        to_id   = str(body.get("to")      or "").strip()
        message = (body.get("message")    or "").strip()
        from_id = str(body.get("from")    or "").strip()
        if not to_id:
            return text_response(
                "ERROR 400: 'to' required\n"
                f"POST {prefix}/send\n"
                '  Body: {"to":"1","message":"...","from":"optional"}\n',
                400,
            )
        if not message:
            return text_response("ERROR 400: 'message' required\n", 400)
        notif_id = store.send(to_id, message, from_id)
        _push(store, to_id, notif_id, message, from_id)
        return text_response(f"OK\nid = {toml_str(notif_id)}\n")

    def _inbox():
        agent_id = (
            request.args.get("agent_id", "")
            or request.headers.get("X-Agent-Id", "")
        ).strip()
        if not agent_id:
            return text_response(
                "ERROR 400: agent_id required\n"
                f"Usage: GET {prefix}/inbox?agent_id=<id>\n"
                "       or set X-Agent-Id header\n",
                400,
            )
        unread_only = request.args.get("unread", "0") == "1"
        page, per_page = _parse_page_params()
        total = store.count(agent_id, unread_only)
        notifs = store.list_inbox(agent_id, page, per_page, unread_only)
        lines = [
            f"# Notifications (agent {agent_id})\n",
            _page_header(page, per_page, total),
            "# tip: ?unread=1 to filter unread\n\n",
        ]
        for n in notifs:
            fk = kind_fn(n['from_id']) if kind_fn and n['from_id'] else ""
            lines += ["[[notifications]]\n",
                      f"id        = {toml_str(n['id'])}\n",
                      f"from      = {toml_str(n['from_id'])}\n"]
            if fk:
                lines.append(f"from_kind = {toml_str(fk)}\n")
            lines += [f"message   = {toml_str(n['message'])}\n",
                      f"date      = {toml_str(_fmt_ts(n['created_at']))}\n",
                      f"read      = {str(bool(n['read'])).lower()}\n",
                      f"url       = {toml_str(prefix + '/inbox/' + n['id'])}\n",
                      "\n"]
        return text_response("".join(lines))

    def _notif(notif_id: str):
        if request.method == "DELETE":
            ok = store.delete(notif_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)
        n = store.get(notif_id)
        if not n:
            return text_response("ERROR 404: notification not found\n", 404)
        fk = kind_fn(n['from_id']) if kind_fn and n['from_id'] else ""
        return text_response(
            f"id        = {toml_str(n['id'])}\n"
            f"from      = {toml_str(n['from_id'])}\n"
            + (f"from_kind = {toml_str(fk)}\n" if fk else "")
            + f"to        = {toml_str(n['to_id'])}\n"
            f"message   = {toml_str(n['message'])}\n"
            f"date      = {toml_str(_fmt_ts(n['created_at']))}\n"
            f"read      = {str(bool(n['read'])).lower()}\n"
        )

    flask_app.add_url_rule(
        prefix + "/register",
        endpoint=ep + "_register", view_func=_register, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/send",
        endpoint=ep + "_send", view_func=_send, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/inbox",
        endpoint=ep + "_inbox", view_func=_inbox)
    flask_app.add_url_rule(
        prefix + "/inbox/<notif_id>",
        endpoint=ep + "_notif", view_func=_notif, methods=["GET", "DELETE"])


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_notify(
    inact_app,
    prefix: str,
    storage,
    revival_interval: int = 600,
    registry=None,
) -> None:
    """
    Mount the notification system at *prefix*.

    *storage*          — database URL/path or Storage instance.
    *revival_interval* — seconds between revival checks (default 600 = 10 min).
                         Set to 0 to disable the revival thread.

    Example::

        mount_notify(app, "/notify", "./notify.db")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    store = NotifyStore(backend)

    kind_fn = None
    if registry is not None:
        from .workspace.register import AgentRegistry
        _reg = registry if isinstance(registry, AgentRegistry) \
               else AgentRegistry(make_storage(registry) if isinstance(registry, str) else registry)

        def kind_fn(from_id: str, _r=_reg) -> str:
            if not from_id:
                return "agent"
            try:
                row = _r.get(int(from_id))
                return (row.get("kind", "agent") if row else "agent")
            except Exception:
                return "agent"

    if revival_interval > 0:
        _start_revival(store, revival_interval)

    attach_notify(inact_app, p, store, kind_fn=kind_fn)
    inact_app._app_mounts.append((p, (
        f"\nNotifications: {p}\n"
        f'  POST {p}/register   register callback  body: {{"agent_id":"1","callback":"http://..."}}\n'
        f'  POST {p}/send       send notification  body: {{"to":"1","message":"..."}}\n'
        f"  GET  {p}/inbox      inbox  (?agent_id=<id>  ?unread=1)\n"
        f"  GET  {p}/inbox/{{id}}  read notification\n"
        f"  DELETE {p}/inbox/{{id}} dismiss\n"
        f"  # revival thread fires callbacks every {revival_interval}s for unread\n"
    )))
