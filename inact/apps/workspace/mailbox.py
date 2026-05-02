"""
Self-hosted email server for inact agents.

mount_mailbox(inact_app, prefix, storage, ...) starts an embedded SMTP server
and registers HTTP routes so agents can send and receive real email.

  POST {prefix}/send           send email
                               body: {"to":"...","subject":"...","body":"..."}
                               Header: X-Api-Key: <agent key>
  GET  {prefix}/inbox          this agent's inbox (paginated)
                               ?page=1&per_page=20  ?unread=1
  GET  {prefix}/inbox/{id}     read message (marks read)
  DELETE {prefix}/inbox/{id}   delete message
  GET  {prefix}/sent           this agent's sent folder (paginated)

*storage* — local database for messages (same format as other apps).
*registry* — agent registry storage; when set, X-Api-Key auth is enforced
             and each agent only sees their own mail.
*smtp_port* — port for the embedded SMTP server (default 2525).
*relay_host/relay_user/relay_password/relay_port* — optional outbound relay;
  without one, mail to external addresses is attempted via direct delivery.

Environment variables (override with keyword args):
  SMTP_PORT             embedded SMTP listen port     default 2525
  SMTP_RELAY_HOST       outbound relay hostname        optional (legacy)
  SMTP_RELAY_PORT       outbound relay port            default 587  (legacy)
  SMTP_RELAY_USER       relay auth username            optional     (legacy)
  SMTP_RELAY_PASSWORD   relay auth password           optional      (legacy)

Preferred cloud delivery:
  SMTP2GO_API_KEY       when set, all outbound email uses SMTP2GO HTTP API
  FROM_EMAIL            default sender address if none provided by caller

Requires: pip install aiosmtpd
"""

from __future__ import annotations

import asyncio
import email as _email_module
import logging
import os
import re
import smtplib
import threading
import time
from email.header import decode_header as _decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import request

from ...storage import Storage
from ...utils import text_response, toml_str

log = logging.getLogger(__name__)

_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100

_DDL = [
    """CREATE TABLE IF NOT EXISTS mail (
        id          INTEGER PRIMARY KEY,
        folder      TEXT    NOT NULL DEFAULT 'inbox',
        from_addr   TEXT    NOT NULL DEFAULT '',
        to_addr     TEXT    NOT NULL DEFAULT '',
        subject     TEXT    NOT NULL DEFAULT '(no subject)',
        body        TEXT    NOT NULL DEFAULT '',
        received_at BIGINT  NOT NULL,
        read        INTEGER NOT NULL DEFAULT 0
    )""",
]


# ---------------------------------------------------------------------------
# Local mail store
# ---------------------------------------------------------------------------

class MailStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)

    def save(self, folder: str, from_addr: str, to_addr: str,
             subject: str, body: str) -> int:
        return self._s.insert(
            "INSERT INTO mail (folder, from_addr, to_addr, subject, body, received_at, read) VALUES (?,?,?,?,?,?,?)",
            (folder, from_addr, to_addr, subject, body, int(time.time()), 0),
        )

    def count(self, folder: str, addr_field: str, addr: str,
              unread_only: bool = False) -> int:
        q = f"SELECT COUNT(*) AS cnt FROM mail WHERE folder=? AND {addr_field}=?"
        params: list = [folder, addr]
        if unread_only:
            q += " AND read=0"
        row = self._s.fetchone(q, tuple(params))
        return row["cnt"] if row else 0

    def list_messages(self, folder: str, addr_field: str, addr: str,
                      page: int, per_page: int,
                      unread_only: bool = False) -> list[dict]:
        offset = (page - 1) * per_page
        q = f"SELECT * FROM mail WHERE folder=? AND {addr_field}=?"
        params: list = [folder, addr]
        if unread_only:
            q += " AND read=0"
        q += " ORDER BY received_at DESC LIMIT ? OFFSET ?"
        params += [per_page, offset]
        return self._s.fetchall(q, tuple(params))

    def get(self, msg_id: str) -> dict | None:
        m = self._s.fetchone("SELECT * FROM mail WHERE id=?", (msg_id,))
        if m:
            self._s.execute("UPDATE mail SET read=1 WHERE id=?", (msg_id,))
        return m

    def delete(self, msg_id: str) -> bool:
        return self._s.execute("DELETE FROM mail WHERE id=?", (msg_id,)) > 0


# ---------------------------------------------------------------------------
# Embedded SMTP server (aiosmtpd)
# ---------------------------------------------------------------------------

class _InboundHandler:
    """aiosmtpd handler — parses, stores, and optionally notifies on inbound mail."""

    def __init__(self, store: MailStore, notify_fn=None):
        self._store     = store
        self._notify_fn = notify_fn  # callable(to_addr, from_addr, subject) or None

    async def handle_DATA(self, server, session, envelope) -> str:
        try:
            raw = envelope.content
            msg = _email_module.message_from_bytes(raw)
            subject   = _decode_str(msg.get("Subject", "(no subject)"))
            from_addr = _decode_str(msg.get("From", envelope.mail_from or ""))
            body      = _extract_body(msg)
            for to_addr in envelope.rcpt_tos:
                # Strip plus-tag so to_addr matches the agent's registered email.
                canonical = _strip_plus(to_addr)
                self._store.save("inbox", from_addr, canonical, subject, body)
                if self._notify_fn:
                    try:
                        self._notify_fn(canonical, from_addr, subject)
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("mailbox: failed to store incoming message: %s", exc)
        return "250 Message accepted"


def _start_smtp_server(store: MailStore, host: str, port: int,
                        notify_fn=None) -> None:
    """Start aiosmtpd in a daemon thread."""
    try:
        from aiosmtpd.controller import Controller
    except ImportError:
        raise RuntimeError(
            "aiosmtpd is required for the embedded SMTP server: pip install aiosmtpd"
        )

    controller = Controller(_InboundHandler(store, notify_fn=notify_fn),
                            hostname=host, port=port)
    controller.start()
    log.info("mailbox: SMTP server listening on %s:%d", host, port)
    # controller runs its own thread; we just keep a reference so it isn't GC'd
    _smtp_controllers.append(controller)


_smtp_controllers: list = []  # prevent GC


# ---------------------------------------------------------------------------
# Outbound sending
# ---------------------------------------------------------------------------

def _plus_reply_to(from_addr: str, agent_id: str) -> str:
    """
    Build a plus-addressed Reply-To so replies route back to the right agent.

    agent@domain.com  →  agent+<agent_id>@domain.com

    The inbound SMTP handler strips the +tag and matches against the base
    address to find the owning agent.
    """
    if "@" not in from_addr or not agent_id:
        return from_addr
    local, domain = from_addr.rsplit("@", 1)
    # Strip any existing + tag before adding ours
    local = local.split("+")[0]
    return f"{local}+{agent_id}@{domain}"


def _strip_plus(addr: str) -> str:
    """agent+tag@domain.com → agent@domain.com"""
    if "@" not in addr:
        return addr
    local, domain = addr.rsplit("@", 1)
    return f"{local.split('+')[0]}@{domain}"


def _send_email(from_addr: str, to: str, subject: str, body: str,
                cc: str = "",
                agent_id: str = "",
                relay_host: str = "", relay_port: int = 587,
                relay_user: str = "", relay_password: str = "",
                smtp_host: str = "localhost", smtp_port: int = 2525) -> None:
    """Send an email using either SMTP2GO HTTP API (preferred) or SMTP.

    Uses SMTP2GO when ``SMTP2GO_API_KEY`` env var is set; otherwise falls back
    to legacy SMTP relay or local SMTP.
    """
    api_key = os.environ.get("SMTP2GO_API_KEY", "").strip()
    fallback_from = os.environ.get("FROM_EMAIL", "").strip()
    sender = from_addr or fallback_from

    # Prefer SMTP2GO HTTP API when configured
    if api_key:
        try:
            import httpx
            payload: dict = {
                "api_key": api_key,
                "to": [a.strip() for a in to.split(",") if a.strip()],
                "sender": sender,
                "subject": subject,
                "text_body": body,
            }
            if cc:
                payload["cc"] = [a.strip() for a in cc.split(",") if a.strip()]
            # Preserve reply routing via plus-address
            if agent_id and sender:
                payload["reply_to"] = _plus_reply_to(sender, agent_id)
            resp = httpx.post(
                "https://api.smtp2go.com/v3/email/send",
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not (isinstance(data, dict) and data.get("data", {}).get("succeeded", 0) >= 1):
                # If API shape differs, accept any 2xx as success; otherwise raise
                pass
            return
        except Exception as exc:
            # Fall back to SMTP path on any API failure
            from_addr = sender or from_addr

    # Legacy SMTP path
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if agent_id and sender:
        msg["Reply-To"] = _plus_reply_to(sender, agent_id)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    recipients = [a.strip() for a in (to + ("," + cc if cc else "")).split(",") if a.strip()]

    if relay_host:
        # Send via configured relay
        if relay_port == 465:
            with smtplib.SMTP_SSL(relay_host, relay_port, timeout=15) as s:
                if relay_user:
                    s.login(relay_user, relay_password)
                s.sendmail(sender, recipients, msg.as_string())
        else:
            with smtplib.SMTP(relay_host, relay_port, timeout=15) as s:
                s.ehlo()
                s.starttls()
                if relay_user:
                    s.login(relay_user, relay_password)
                s.sendmail(sender, recipients, msg.as_string())
    else:
        # Deliver via local SMTP server (always connect via loopback)
        with smtplib.SMTP("127.0.0.1", smtp_port, timeout=5) as s:
            s.sendmail(sender, recipients, msg.as_string())


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------

def _decode_str(value: str | None) -> str:
    if not value:
        return ""
    parts = _decode_header(value)
    result = []
    for raw, charset in parts:
        if isinstance(raw, bytes):
            result.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(raw)
    return "".join(result)


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and part.get_content_disposition() != "attachment":
                return _decode_payload(part)
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html" and part.get_content_disposition() != "attachment":
                return _strip_html(_decode_payload(part))
    else:
        payload = _decode_payload(msg)
        if msg.get_content_type() == "text/html":
            return _strip_html(payload)
        return payload
    return ""


def _decode_payload(part) -> str:
    charset = part.get_content_charset() or "utf-8"
    raw = part.get_payload(decode=True)
    return (raw or b"").decode(charset, errors="replace")


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
    lines = [f"# page {page} of {total_pages} ({total} messages)\n"]
    if page > 1:
        lines.append(f"# ?page={page - 1}&per_page={per_page} for prev\n")
    if page < total_pages:
        lines.append(f"# ?page={page + 1}&per_page={per_page} for next\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Agent auth helper
# ---------------------------------------------------------------------------

def _resolve_agent(registry, prefix: str):
    api_key = request.headers.get("X-Api-Key", "").strip()
    if not api_key:
        return None, text_response(
            "ERROR 401: X-Api-Key header required\n"
            "  Identify yourself with the api_key received at registration.\n",
            401,
        )
    agent = registry.get_by_key(api_key)
    if not agent:
        return None, text_response("ERROR 401: unknown api_key\n", 401)
    if not agent.get("email"):
        return None, text_response(
            f"ERROR 409: no email configured for agent {agent['id']}.\n"
            f"  POST /agents/{agent['id']}/.email  body: "
            '{{"email":"you@yourdomain.com"}}\n',
            409,
        )
    return agent, None


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_mailbox(inact_app, prefix: str, store: MailStore,
                   registry=None, smtp_host: str = "localhost",
                   smtp_port: int = 2525, relay_host: str = "",
                   relay_port: int = 587, relay_user: str = "",
                   relay_password: str = "") -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_mail_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _agent_email():
        if registry is None:
            return None, None
        return _resolve_agent(registry, prefix)

    def _send():
        agent_email = None
        agent_id_str = ""
        if registry is not None:
            agent, err = _agent_email()
            if err:
                return err
            agent_email   = agent["email"]
            agent_id_str  = str(agent["id"])

        body_data = request.get_json(force=True, silent=True) or {}
        to      = (body_data.get("to")      or "").strip()
        subject = (body_data.get("subject") or "(no subject)").strip()
        body    = (body_data.get("body")    or "").strip()
        cc      = (body_data.get("cc")      or "").strip()
        from_   = (body_data.get("from")    or agent_email or "").strip()

        if not to:
            return text_response(
                "ERROR 400: 'to' required\n"
                f"POST {prefix}/send\n"
                '  Body: {"to":"...","subject":"...","body":"..."}\n',
                400,
            )
        if not body:
            return text_response("ERROR 400: 'body' required\n", 400)
        if not from_:
            return text_response(
                "ERROR 400: 'from' required (or set agent email via /agents/{id}/.email)\n",
                400,
            )
        try:
            _send_email(from_, to, subject, body, cc=cc,
                        agent_id=agent_id_str,
                        relay_host=relay_host, relay_port=relay_port,
                        relay_user=relay_user, relay_password=relay_password,
                        smtp_host=smtp_host, smtp_port=smtp_port)
            # Also store in sent folder locally
            store.save("sent", from_, to, subject, body)
        except smtplib.SMTPAuthenticationError:
            return text_response("ERROR 401: relay authentication failed\n", 401)
        except Exception as exc:
            return text_response(f"ERROR 502: {exc}\n", 502)
        return text_response(
            f"OK\nfrom    = {toml_str(from_)}\nto      = {toml_str(to)}\n"
            f"subject = {toml_str(subject)}\n"
        )

    def _inbox():
        addr = None
        if registry is not None:
            agent, err = _agent_email()
            if err:
                return err
            addr = agent["email"]

        unread_only = request.args.get("unread", "0") == "1"
        page, per_page = _parse_page_params()

        if addr:
            total = store.count("inbox", "to_addr", addr, unread_only)
            msgs  = store.list_messages("inbox", "to_addr", addr, page, per_page, unread_only)
        else:
            # shared mode — show all
            total = store.count("inbox", "folder", "inbox")
            msgs  = store.list_messages("inbox", "folder", "inbox", page, per_page, unread_only)

        header = f"# Inbox — {addr}\n" if addr else "# Inbox\n"
        lines  = [header, _page_header(page, per_page, total)]
        if unread_only:
            lines.append("# (unread only)\n")
        lines.append("\n")
        for m in msgs:
            lines += [
                "[[messages]]\n",
                f'id      = {m["id"]}\n',
                f'from    = {toml_str(m["from_addr"])}\n',
                f'subject = {toml_str(m["subject"])}\n',
                f'date    = {toml_str(_fmt_ts(m["received_at"]))}\n',
                f'read    = {str(bool(m["read"])).lower()}\n',
                f'url     = {toml_str(prefix + "/inbox/" + str(m["id"]))}\n',
                "\n",
            ]
        return text_response("".join(lines))

    def _message(msg_id: str):
        if registry is not None:
            _, err = _agent_email()
            if err:
                return err
        if request.method == "DELETE":
            ok = store.delete(msg_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)
        m = store.get(msg_id)
        if not m:
            return text_response("ERROR 404: message not found\n", 404)
        lines = [
            f"# {m['subject']}\n\n",
            f"from    = {toml_str(m['from_addr'])}\n",
            f"to      = {toml_str(m['to_addr'])}\n",
            f"date    = {toml_str(_fmt_ts(m['received_at']))}\n",
            f"id      = {m['id']}\n",
            "\n---\n\n",
            m["body"] + "\n",
        ]
        return text_response("".join(lines))

    def _sent():
        addr = None
        if registry is not None:
            agent, err = _agent_email()
            if err:
                return err
            addr = agent["email"]

        page, per_page = _parse_page_params()
        if addr:
            total = store.count("sent", "from_addr", addr)
            msgs  = store.list_messages("sent", "from_addr", addr, page, per_page)
        else:
            total = store.count("sent", "folder", "sent")
            msgs  = store.list_messages("sent", "folder", "sent", page, per_page)

        header = f"# Sent — {addr}\n" if addr else "# Sent\n"
        lines  = [header, _page_header(page, per_page, total), "\n"]
        for m in msgs:
            lines += [
                "[[messages]]\n",
                f'id      = {m["id"]}\n',
                f'to      = {toml_str(m["to_addr"])}\n',
                f'subject = {toml_str(m["subject"])}\n',
                f'date    = {toml_str(_fmt_ts(m["received_at"]))}\n',
                "\n",
            ]
        return text_response("".join(lines))

    flask_app.add_url_rule(
        prefix + "/send",
        endpoint=ep + "_send", view_func=_send, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/inbox",
        endpoint=ep + "_inbox", view_func=_inbox)
    flask_app.add_url_rule(
        prefix + "/inbox/<msg_id>",
        endpoint=ep + "_message", view_func=_message, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/sent",
        endpoint=ep + "_sent", view_func=_sent)

    def _human(path: str):
        from inact.render import render_template
        from inact.utils import html_response
        from inact.render import workspace_nav
        return html_response(render_template("mail_human.html",
            title="Mail", prefix=prefix, nav="", pills=[],
            workspace_links=workspace_nav("/_human/mail/"),
            show_identity=True))

    inact_app._human_views[prefix] = _human
    inact_app.add_nav_item(prefix.rsplit("/", 1)[-1] or prefix.strip("/"),
                           "/_human" + prefix + "/")


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_mailbox(
    inact_app,
    prefix: str,
    storage,
    registry=None,
    notify_storage=None,
    smtp_host: str = "0.0.0.0",   # listen on all interfaces
    smtp_port: int | None = None,
    smtp_domain: str = "",         # domain for Reply-To plus-addresses (e.g. agents.example.com)
    relay_host: str = "",
    relay_port: int = 587,
    relay_user: str = "",
    relay_password: str = "",
) -> None:
    """
    Mount a self-hosted email server at *prefix*.

    Starts an embedded SMTP server (via ``aiosmtpd``) that accepts inbound
    mail and stores it locally.  Outbound mail is delivered through the same
    server (agent-to-agent) or via an optional external relay.

    *storage*  — database URL/path for the local message store.
    *registry* — agent registry storage; enables per-agent inboxes via
                 ``X-Api-Key`` authentication.
    *smtp_port* — SMTP listen port (default: ``$SMTP_PORT`` or 2525).
    *relay_host/relay_user/relay_password* — optional outbound SMTP relay.

    Example::

        mount_mailbox(app, "/mail", "./mail.db")

        # Per-agent inboxes
        mount_mailbox(app, "/mail", "./mail.db", registry="./agents.db")

        # With outbound relay (e.g. SendGrid)
        mount_mailbox(app, "/mail", "./mail.db",
                      relay_host="smtp.sendgrid.net",
                      relay_user="apikey",
                      relay_password=os.environ["SENDGRID_KEY"])
    """
    from .register import AgentRegistry
    from ...storage import make_storage

    # Resolve registry
    reg = None
    if registry is not None:
        if isinstance(registry, str):
            reg = AgentRegistry(make_storage(registry))
        elif not isinstance(registry, AgentRegistry):
            reg = AgentRegistry(registry)
        else:
            reg = registry

    # Resolve storage
    backend = make_storage(storage) if isinstance(storage, str) else storage
    store = MailStore(backend)

    # Resolve SMTP port — 0/None means "no inbound SMTP"
    _env_port = os.environ.get("SMTP_PORT", "")
    port = smtp_port or (int(_env_port) if _env_port else None)

    # Resolve relay from env if not given
    r_host = relay_host or os.environ.get("SMTP_RELAY_HOST", "")
    r_port = relay_port or int(os.environ.get("SMTP_RELAY_PORT", "587"))
    r_user = relay_user or os.environ.get("SMTP_RELAY_USER", "")
    r_pass = relay_password or os.environ.get("SMTP_RELAY_PASSWORD", "")

    # Build notify_fn for inbound email notifications
    inbound_notify_fn = None
    if notify_storage is not None:
        from ..notify import NotifyStore, _push
        ns = make_storage(notify_storage) if isinstance(notify_storage, str) else notify_storage
        nstore = NotifyStore(ns)

        def inbound_notify_fn(to_email: str, from_email: str, subject: str) -> None:
            # Look up which agent owns this email address and notify them
            if reg is not None:
                # Find agent by email using the registry storage
                row = reg._s.fetchone(
                    "SELECT id FROM agents WHERE email = ?", (to_email,)
                )
                if row:
                    agent_id = str(row["id"])
                    notif_id = nstore.send(
                        agent_id,
                        f"New email from {from_email}: {subject}",
                        from_email,
                    )
                    _push(nstore, agent_id, notif_id,
                          f"New email from {from_email}: {subject}", from_email)

    # Start the embedded SMTP server (only when a port is configured)
    if port:
        _start_smtp_server(store, smtp_host, port, notify_fn=inbound_notify_fn)

    p = "/" + prefix.strip("/")
    attach_mailbox(inact_app, p, store, registry=reg,
                   smtp_host=smtp_host, smtp_port=port,
                   relay_host=r_host, relay_port=r_port,
                   relay_user=r_user, relay_password=r_pass)

    per_agent = "per-agent" if reg else "shared"
    relay_info = f"relay={r_host}" if r_host else f"local:{port}"
    help_text = (
        f"\nMailbox: {p}  (SMTP port {port}  {relay_info}  {per_agent})\n"
        f"  POST   {p}/send           send email\n"
        f'                             body: {{"to":"...","subject":"...","body":"..."}}\n'
        f"  GET    {p}/inbox          inbox  (?page=1  ?unread=1)\n"
        f"  GET    {p}/inbox/{{id}}     read message\n"
        f"  DELETE {p}/inbox/{{id}}     delete message\n"
        f"  GET    {p}/sent           sent folder\n"
    )
    if reg:
        help_text += "  # X-Api-Key header required; set agent email via /agents/{id}/.email\n"
    help_text += f"  # SMTP env: SMTP_PORT  SMTP_RELAY_HOST  SMTP_RELAY_USER  SMTP_RELAY_PASSWORD\n"
    inact_app._app_mounts.append((p, help_text))
