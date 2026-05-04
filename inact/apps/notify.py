"""
Notification system for agents — push + persistent inbox.

mount_notify(inact_app, prefix, storage) registers:

  POST {prefix}/register          register a raw callback URL for an agent
                                  body: {"agent_id":"1","callback":"http://host/wake","secret":"optional"}
  POST {prefix}/webhook/register  register a Hermes webhook route for an agent
                                  body: {"agent_id":"1","hermes_url":"http://localhost:8644","route":"my-route","secret":"..."}
                                  constructs callback as {hermes_url}/webhooks/{route}
  POST {prefix}/send              send a notification
                                  body: {"to":"1","message":"...","from":"optional"}
                                  immediately POSTs to the agent's callback if registered
  GET  {prefix}/inbox             list notifications  (X-Agent-Id header required)
                                  ?unread=1  ?page=1&per_page=20
  GET  {prefix}/inbox/{id}        read notification (marks read)
  DELETE {prefix}/inbox/{id}      dismiss

Push delivery: if a secret is stored for the agent, every outgoing POST includes
an X-Webhook-Signature header (raw HMAC-SHA256 hex digest) compatible with the
Hermes generic webhook format.

A background thread re-fires any unread notification callbacks every
*revival_interval* seconds (default 600 = 10 min) so agents that were
offline when a notification arrived are eventually woken up.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time

from flask import request

from ..storage import Storage
from ..utils import text_response, toml_str
from ..apps.workspace.mailbox import _send_email

_DDL = [
    """CREATE TABLE IF NOT EXISTS notifications (
        id         INTEGER PRIMARY KEY,
        to_id      TEXT    NOT NULL,
        from_id    TEXT    NOT NULL DEFAULT '',
        message    TEXT    NOT NULL DEFAULT '',
        read       INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT  NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS notify_callbacks (
        agent_id     TEXT    PRIMARY KEY,
        url          TEXT    NOT NULL,
        secret       TEXT    NOT NULL DEFAULT '',
        registered_at BIGINT NOT NULL
    )""",
]

try:
    import logfire as _logfire  # type: ignore
except ModuleNotFoundError:
    _logfire = None  # type: ignore

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
        self._s.execute("DELETE FROM notifications WHERE id = 'none' OR id IS NULL")
        try:
            self._s.execute(
                "ALTER TABLE notify_callbacks ADD COLUMN secret TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass

    # callbacks

    def register_callback(self, agent_id: str, url: str, secret: str = "") -> None:
        existing = self._s.fetchone(
            "SELECT agent_id FROM notify_callbacks WHERE agent_id = ?", (agent_id,)
        )
        if existing:
            self._s.execute(
                "UPDATE notify_callbacks SET url = ?, secret = ?, registered_at = ? WHERE agent_id = ?",
                (url, secret, int(time.time()), agent_id),
            )
        else:
            self._s.execute(
                "INSERT INTO notify_callbacks (agent_id, url, secret, registered_at) VALUES (?, ?, ?, ?)",
                (agent_id, url, secret, int(time.time())),
            )

    def get_callback(self, agent_id: str) -> tuple[str, str] | None:
        """Returns (url, secret) or None."""
        row = self._s.fetchone(
            "SELECT url, secret FROM notify_callbacks WHERE agent_id = ?", (agent_id,)
        )
        return (row["url"], row["secret"] or "") if row else None

    # notifications

    def send(self, to_id: str, message: str, from_id: str = "") -> int:
        return self._s.insert(
            "INSERT INTO notifications (to_id, from_id, message, read, created_at) VALUES (?, ?, ?, ?, ?)",
            (to_id, from_id, message, 0, int(time.time())),
        )

    def count(self, to_id: str, unread_only: bool = False) -> int:
        q = "SELECT COUNT(*) AS cnt FROM notifications WHERE to_id = ? AND id IS NOT NULL"
        params: tuple = (to_id,)
        if unread_only:
            q += " AND read = 0"
        row = self._s.fetchone(q, params)
        return row["cnt"] if row else 0

    def list_inbox(self, to_id: str, page: int, per_page: int,
                   unread_only: bool = False) -> list[dict]:
        offset = (page - 1) * per_page
        q = "SELECT * FROM notifications WHERE to_id = ? AND id IS NOT NULL"
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

def _fire_callback(url: str, payload: dict, secret: str = "") -> None:
    try:
        import httpx
        body = json.dumps(payload).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if secret:
            sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Webhook-Signature"] = sig
        if _logfire:
            _logfire.info(
                "notify webhook → {url}",
                url=url,
                payload=payload,
                signed=bool(secret),
            )
        r = httpx.post(url, content=body, headers=headers, timeout=5)
        if _logfire:
            _logfire.info(
                "notify webhook ← {status}",
                url=url,
                status=r.status_code,
                response=r.text[:500],
            )
    except Exception as exc:
        if _logfire:
            _logfire.warning(
                "notify webhook failed → {url}: {exc}",
                url=url,
                exc=str(exc),
            )


def _push(store: NotifyStore, to_id: str, notif_id: str,
          message: str, from_id: str, from_kind: str = "") -> None:
    info = store.get_callback(to_id)
    if info:
        url, secret = info
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
            args=(url, payload, secret),
            daemon=True,
        ).start()


def _start_revival(store: NotifyStore, interval: int) -> None:
    """Background thread: re-fire callbacks for unread notifications."""
    def _loop():
        while True:
            time.sleep(interval)
            for agent_id, count in store.agents_with_unread():
                info = store.get_callback(agent_id)
                if info:
                    url, secret = info
                    threading.Thread(
                        target=_fire_callback,
                        args=(url, {
                            "type": "revival",
                            "unread": count,
                            "agent_id": agent_id,
                        }, secret),
                        daemon=True,
                    ).start()

    threading.Thread(target=_loop, daemon=True, name="notify-revival").start()


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_notify(inact_app, prefix: str, store: NotifyStore,
                  kind_fn=None, member_fn=None, email_fn=None,
                  agents_prefix: str = "/agents") -> None:
    prefix = "/" + prefix.strip("/")
    agents_prefix = "/" + agents_prefix.strip("/")
    ep = "_inact_notify_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _from_str(agent_id: str) -> str:
        if not agent_id:
            return agent_id
        info = member_fn(agent_id) if member_fn else {"name": "", "kind": "agent"}
        name = info["name"] or (
            "Human #" + agent_id if info["kind"] == "human" else "Agent #" + agent_id
        )
        return f"{name}#{agent_id}"

    def _register():
        body = request.get_json(force=True, silent=True) or {}
        agent_id = str(body.get("agent_id") or "").strip()
        callback = (body.get("callback") or "").strip()
        secret   = (body.get("secret")   or "").strip()
        if not agent_id:
            return text_response("ERROR 400: 'agent_id' required\n", 400)
        if not callback:
            return text_response("ERROR 400: 'callback' required\n", 400)
        store.register_callback(agent_id, callback, secret)
        return text_response(
            f"OK\nagent_id = {toml_str(agent_id)}\ncallback = {toml_str(callback)}\n"
        )

    def _webhook_register():
        """Register a Hermes webhook endpoint for an agent.

        Body: {"agent_id":"1","hermes_url":"http://localhost:8644","route":"my-route","secret":"..."}
        Constructs callback as {hermes_url}/webhooks/{route} and stores the HMAC secret.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_id   = str(body.get("agent_id")   or "").strip()
        hermes_url = (body.get("hermes_url")     or "").strip().rstrip("/")
        route      = (body.get("route")          or "").strip().strip("/")
        secret     = (body.get("secret")         or "").strip()
        if not agent_id:
            return text_response("ERROR 400: 'agent_id' required\n", 400)
        if not hermes_url:
            return text_response("ERROR 400: 'hermes_url' required\n", 400)
        if not route:
            return text_response("ERROR 400: 'route' required\n", 400)
        if not secret:
            return text_response("ERROR 400: 'secret' required\n", 400)
        callback = f"{hermes_url}/webhooks/{route}"
        store.register_callback(agent_id, callback, secret)
        return text_response(
            f"OK\nagent_id    = {toml_str(agent_id)}\n"
            f"webhook_url = {toml_str(callback)}\n"
            f"route       = {toml_str(route)}\n"
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

        # If sending to a human and we have an email address configured,
        # send an email copy of the notification.
        try:
            if kind_fn and email_fn and kind_fn(to_id) == "human":
                to_email = (email_fn(to_id) or "").strip()
                if to_email:
                    # Prefer sender's email if available; otherwise fall back.
                    from_email = (email_fn(from_id) or "").strip() if from_id else ""
                    if not from_email:
                        domain = os.environ.get("DOMAIN", "") or "localhost"
                        from_email = (
                            os.environ.get("FROM_EMAIL")
                            or os.environ.get("SMTP_FROM")
                            or f"notify@{domain}"
                        )

                    # Build a friendly subject using member_fn if available
                    display_from = f"Agent #{from_id}" if from_id else "Agent"
                    if member_fn and from_id:
                        info = member_fn(from_id) or {"name": "", "kind": "agent"}
                        name = (info.get("name") or "").strip()
                        if name:
                            display_from = name
                        elif info.get("kind") == "human":
                            display_from = f"Human #{from_id}"

                    subject = f"New notification from {display_from}"
                    # Relay/local SMTP settings via env (match mailbox configuration)
                    r_host = os.environ.get("SMTP_RELAY_HOST", "")
                    r_port = int(os.environ.get("SMTP_RELAY_PORT", "587") or 587)
                    r_user = os.environ.get("SMTP_RELAY_USER", "")
                    r_pass = os.environ.get("SMTP_RELAY_PASSWORD", "")
                    s_port = int(os.environ.get("SMTP_PORT", "2525") or 2525)
                    try:
                        _send_email(
                            from_email, to_email, subject, message,
                            relay_host=r_host, relay_port=r_port,
                            relay_user=r_user, relay_password=r_pass,
                            smtp_port=s_port,
                        )
                    except Exception:
                        # Email delivery best-effort; ignore failures.
                        pass
        except Exception:
            # Never let email side-effects break the notification API
            pass
        return text_response(f"OK\nid = {notif_id}\n")

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
        show_all = request.args.get("all", "0") == "1"
        unread_only = not show_all
        page, per_page = _parse_page_params()
        total = store.count(agent_id, unread_only)
        notifs = store.list_inbox(agent_id, page, per_page, unread_only)
        lines = [
            f"# Notifications (agent {agent_id})\n",
            _page_header(page, per_page, total),
            "# tip: ?all=1 to include read notifications\n\n",
        ]
        for n in notifs:
            fk = kind_fn(n['from_id']) if kind_fn and n['from_id'] else ""
            lines += ["[[notifications]]\n",
                      f"id        = {n['id']}\n",
                      f"from      = {toml_str(_from_str(n['from_id']))}\n"]
            if fk:
                lines.append(f"from_kind = {toml_str(fk)}\n")
            lines += [f"message   = {toml_str(n['message'])}\n",
                      f"date      = {toml_str(_fmt_ts(n['created_at']))}\n",
                      f"read      = {str(bool(n['read'])).lower()}\n",
                      f"url       = {toml_str(prefix + '/inbox/' + str(n['id']))}\n",
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
            f"id        = {n['id']}\n"
            f"from      = {toml_str(_from_str(n['from_id']))}\n"
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
        prefix + "/webhook/register",
        endpoint=ep + "_webhook_register", view_func=_webhook_register, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/send",
        endpoint=ep + "_send", view_func=_send, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/inbox",
        endpoint=ep + "_inbox", view_func=_inbox)
    flask_app.add_url_rule(
        prefix + "/inbox/<notif_id>",
        endpoint=ep + "_notif", view_func=_notif, methods=["GET", "DELETE"])

    def _human(_path: str):
        from ..render import render_template, workspace_nav
        from ..utils import html_response
        html = render_template(
            "notify_human.html",
            title="Notifications",
            prefix=prefix,
            agents_prefix=agents_prefix,
            workspace_links=workspace_nav("/_human" + prefix + "/"),
            show_identity=True,
        )
        return html_response(html)

    inact_app._human_views[prefix] = _human
    inact_app.add_nav_item("notify", "/_human" + prefix + "/")


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_notify(
    inact_app,
    prefix: str,
    storage,
    revival_interval: int = 600,
    registry=None,
    agents_prefix: str = "/agents",
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

        def member_fn(from_id: str, _r=_reg) -> dict:
            if not from_id:
                return {"name": "", "kind": "agent"}
            try:
                row = _r.get(int(from_id))
                if row:
                    return {"name": row.get("name", "") or "", "kind": row.get("kind", "agent") or "agent"}
            except Exception:
                pass
            return {"name": "", "kind": "agent"}

        def email_fn(agent_id: str, _r=_reg) -> str:
            try:
                row = _r._s.fetchone("SELECT email FROM agents WHERE id = ?", (int(agent_id),))
                return (row["email"] or "") if row else ""
            except Exception:
                return ""
    else:
        member_fn = None
        email_fn  = None

    if revival_interval > 0:
        _start_revival(store, revival_interval)

    ap = "/" + agents_prefix.strip("/")
    attach_notify(inact_app, p, store, kind_fn=kind_fn, member_fn=member_fn, email_fn=email_fn,
                  agents_prefix=ap)
    inact_app._app_mounts.append((p, (
        f"\nNotifications: {p}\n"
        f'  POST {p}/register          register callback  body: {{"agent_id":"1","callback":"http://...","secret":"optional"}}\n'
        f'  POST {p}/webhook/register  register Hermes webhook  body: {{"agent_id":"1","hermes_url":"http://localhost:8644","route":"my-route","secret":"..."}}\n'
        f'  POST {p}/send              send notification  body: {{"to":"1","message":"..."}}\n'
        f"  GET  {p}/inbox             inbox  unread only by default  (?agent_id=<id>  ?all=1 for all)\n"
        f"  GET  {p}/inbox/{{id}}        read notification\n"
        f"  DELETE {p}/inbox/{{id}}      dismiss\n"
        f"  # revival thread fires callbacks every {revival_interval}s for unread\n"
        f"  # Hermes webhooks: payload signed with X-Webhook-Signature (HMAC-SHA256)\n"
    )))
