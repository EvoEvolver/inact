"""
External email connector for inact — send and receive real email via SMTP/IMAP.

mount_mailbox(inact_app, prefix) registers:

  POST {prefix}/send           send an email via SMTP
                               body: {"to":"...","subject":"...","body":"...",
                                      "cc":"...","from":"..."}
  GET  {prefix}/inbox          list IMAP inbox (paginated, newest first)
                               ?page=1&per_page=20  ?unread=1
  GET  {prefix}/inbox/{uid}    read message by IMAP UID (marks as Seen)
  DELETE {prefix}/inbox/{uid}  move to Trash / mark deleted
  GET  {prefix}/sent           list IMAP Sent folder (paginated)

No local storage — all messages live on the mail server.

Required environment variables (can be overridden as kwargs to mount_mailbox):

  SMTP_HOST         SMTP server          e.g. smtp.gmail.com
  SMTP_PORT         SMTP port            default: 587 (STARTTLS)
                                         use 465 for SMTP_SSL
  SMTP_USER         Login / sender       e.g. you@gmail.com
  SMTP_PASSWORD     Password or app-pw
  SMTP_FROM         From address         default: SMTP_USER

  IMAP_HOST         IMAP server          e.g. imap.gmail.com
  IMAP_PORT         IMAP port            default: 993 (SSL)
  IMAP_USER         IMAP login           default: SMTP_USER
  IMAP_PASSWORD     IMAP password        default: SMTP_PASSWORD
  IMAP_SENT_FOLDER  Sent folder name     default: Sent
                    (Gmail: "[Gmail]/Sent Mail")

Gmail quick-start:
  1. Enable IMAP in Gmail settings
  2. Create an App Password at myaccount.google.com/apppasswords
  3. Set SMTP_HOST=smtp.gmail.com  SMTP_USER=you@gmail.com
         SMTP_PASSWORD=<app-pw>    IMAP_HOST=imap.gmail.com
"""

from __future__ import annotations

import email as _email_module
import imaplib
import os
import re
import smtplib
import time
from email.header import decode_header as _decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import request

from ..utils import text_response, toml_str

_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class MailConfig:
    """
    Resolved email credentials — kwargs take precedence over env vars.
    All ``mount_mailbox`` keyword arguments map 1-to-1 to the env vars listed
    in the module docstring (lowercase, same name).
    """

    def __init__(self, **kwargs):
        def _v(key: str, default: str = "") -> str:
            return (kwargs.get(key.lower()) or os.environ.get(key) or default).strip()

        self.smtp_host     = _v("SMTP_HOST")
        self.smtp_port     = int(_v("SMTP_PORT", "587"))
        self.smtp_user     = _v("SMTP_USER")
        self.smtp_password = _v("SMTP_PASSWORD")
        self.smtp_from     = _v("SMTP_FROM") or self.smtp_user

        self.imap_host        = _v("IMAP_HOST")
        self.imap_port        = int(_v("IMAP_PORT", "993"))
        self.imap_user        = _v("IMAP_USER") or self.smtp_user
        self.imap_password    = _v("IMAP_PASSWORD") or self.smtp_password
        self.imap_sent_folder = _v("IMAP_SENT_FOLDER", "Sent")

    def smtp_ok(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    def imap_ok(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    def missing(self) -> list[str]:
        needed = []
        if not self.smtp_host:     needed.append("SMTP_HOST")
        if not self.smtp_user:     needed.append("SMTP_USER")
        if not self.smtp_password: needed.append("SMTP_PASSWORD")
        if not self.imap_host:     needed.append("IMAP_HOST")
        return needed


# ---------------------------------------------------------------------------
# SMTP — send
# ---------------------------------------------------------------------------

def _smtp_send(cfg: MailConfig, to: str, subject: str, body: str,
               cc: str = "", from_addr: str = "") -> None:
    from_addr = from_addr or cfg.smtp_from
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to
    if cc:
        msg["Cc"]  = cc
    msg.attach(MIMEText(body, "plain", "utf-8"))

    recipients = [a.strip() for a in (to + ("," + cc if cc else "")).split(",") if a.strip()]

    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15) as s:
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.sendmail(from_addr, recipients, msg.as_string())
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.sendmail(from_addr, recipients, msg.as_string())


# ---------------------------------------------------------------------------
# IMAP — receive
# ---------------------------------------------------------------------------

def _imap_connect(cfg: MailConfig) -> imaplib.IMAP4:
    if cfg.imap_port == 993:
        imap = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    else:
        imap = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
        imap.starttls()
    imap.login(cfg.imap_user, cfg.imap_password)
    return imap


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
    """Return plain-text body; fall back to stripped HTML if no text/plain part."""
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
    if raw is None:
        return ""
    return raw.decode(charset, errors="replace")


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fmt_date(date_str: str) -> str:
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return date_str


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


def _imap_list(cfg: MailConfig, folder: str, unread_only: bool,
               page: int, per_page: int) -> tuple[int, list[dict]]:
    imap = _imap_connect(cfg)
    try:
        imap.select(f'"{folder}"', readonly=True)
        criteria = "UNSEEN" if unread_only else "ALL"
        _, data = imap.search(None, criteria)
        uids = data[0].split() if data[0] else []
        uids = list(reversed(uids))  # newest first
        total = len(uids)

        page_uids = uids[(page - 1) * per_page : page * per_page]
        if not page_uids:
            return total, []

        uid_set = b",".join(page_uids)
        _, fetch_data = imap.fetch(uid_set,
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] FLAGS)")

        msgs = []
        i = 0
        while i < len(fetch_data):
            item = fetch_data[i]
            if isinstance(item, tuple):
                meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                uid_match = re.search(r"(\d+) \(", meta)
                uid = uid_match.group(1) if uid_match else str(i)
                read = "\\Seen" in meta
                parsed = _email_module.message_from_bytes(item[1])
                msgs.append({
                    "uid": uid,
                    "from": _decode_str(parsed.get("From", "")),
                    "subject": _decode_str(parsed.get("Subject", "(no subject)")),
                    "date": _fmt_date(parsed.get("Date", "")),
                    "read": read,
                })
            i += 1
        return total, msgs
    finally:
        imap.logout()


def _imap_fetch(cfg: MailConfig, folder: str, uid: str,
                mark_seen: bool = True) -> dict | None:
    imap = _imap_connect(cfg)
    try:
        imap.select(f'"{folder}"')
        _, data = imap.fetch(uid, "(RFC822)")
        if not data or data[0] is None:
            return None
        raw = data[0][1] if isinstance(data[0], tuple) else data[0]
        if not isinstance(raw, bytes):
            return None
        if mark_seen:
            imap.store(uid, "+FLAGS", "\\Seen")
        msg = _email_module.message_from_bytes(raw)
        return {
            "uid": uid,
            "from": _decode_str(msg.get("From", "")),
            "to": _decode_str(msg.get("To", "")),
            "cc": _decode_str(msg.get("Cc", "")),
            "subject": _decode_str(msg.get("Subject", "(no subject)")),
            "date": _fmt_date(msg.get("Date", "")),
            "body": _extract_body(msg),
        }
    finally:
        imap.logout()


def _imap_delete(cfg: MailConfig, folder: str, uid: str) -> bool:
    imap = _imap_connect(cfg)
    try:
        imap.select(f'"{folder}"')
        imap.store(uid, "+FLAGS", "\\Deleted")
        imap.expunge()
        return True
    except Exception:
        return False
    finally:
        imap.logout()


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_mailbox(inact_app, prefix: str, cfg: MailConfig) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_mail_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _send():
        if not cfg.smtp_ok():
            missing = [v for v in ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"]
                       if not getattr(cfg, v.lower().replace("smtp_", "smtp_"), "")]
            return text_response(
                f"ERROR 503: SMTP not configured. Set: {', '.join(cfg.missing())}\n", 503
            )
        body_data = request.get_json(force=True, silent=True) or {}
        to      = (body_data.get("to")      or "").strip()
        subject = (body_data.get("subject") or "(no subject)").strip()
        body    = (body_data.get("body")    or "").strip()
        cc      = (body_data.get("cc")      or "").strip()
        from_   = (body_data.get("from")    or "").strip()
        if not to:
            return text_response(
                "ERROR 400: 'to' required\n"
                f"POST {prefix}/send\n"
                '  Body: {"to":"...","subject":"...","body":"...","cc":"optional"}\n',
                400,
            )
        if not body:
            return text_response("ERROR 400: 'body' required\n", 400)
        try:
            _smtp_send(cfg, to, subject, body, cc=cc, from_addr=from_)
        except smtplib.SMTPAuthenticationError:
            return text_response("ERROR 401: SMTP authentication failed\n", 401)
        except smtplib.SMTPRecipientsRefused as e:
            return text_response(f"ERROR 400: recipient refused: {e}\n", 400)
        except Exception as exc:
            return text_response(f"ERROR 502: {exc}\n", 502)
        return text_response(
            f"OK\nto      = {toml_str(to)}\nsubject = {toml_str(subject)}\n"
        )

    def _inbox():
        if not cfg.imap_ok():
            return text_response(
                f"ERROR 503: IMAP not configured. Set: {', '.join(cfg.missing())}\n", 503
            )
        unread_only = request.args.get("unread", "0") == "1"
        page, per_page = _parse_page_params()
        try:
            total, msgs = _imap_list(cfg, "INBOX", unread_only, page, per_page)
        except Exception as exc:
            return text_response(f"ERROR 502: {exc}\n", 502)
        total_pages = max(1, (total + per_page - 1) // per_page)
        lines = [
            "# Inbox\n",
            f"# page {page} of {total_pages} ({total} messages)\n",
        ]
        if page > 1:
            lines.append(f"# ?page={page-1}&per_page={per_page} for prev\n")
        if page < total_pages:
            lines.append(f"# ?page={page+1}&per_page={per_page} for next\n")
        lines.append("# tip: ?unread=1 to filter unread\n\n")
        for m in msgs:
            lines.append("[[messages]]\n")
            lines.append(f'uid     = {toml_str(m["uid"])}\n')
            lines.append(f'from    = {toml_str(m["from"])}\n')
            lines.append(f'subject = {toml_str(m["subject"])}\n')
            lines.append(f'date    = {toml_str(m["date"])}\n')
            lines.append(f'read    = {str(m["read"]).lower()}\n')
            lines.append(f'url     = {toml_str(prefix + "/inbox/" + m["uid"])}\n')
            lines.append("\n")
        return text_response("".join(lines))

    def _message(uid: str):
        if not cfg.imap_ok():
            return text_response(
                f"ERROR 503: IMAP not configured. Set: {', '.join(cfg.missing())}\n", 503
            )
        if request.method == "DELETE":
            try:
                ok = _imap_delete(cfg, "INBOX", uid)
            except Exception as exc:
                return text_response(f"ERROR 502: {exc}\n", 502)
            return text_response("OK\n" if ok else "ERROR 404: message not found\n", 200 if ok else 404)
        try:
            msg = _imap_fetch(cfg, "INBOX", uid, mark_seen=True)
        except Exception as exc:
            return text_response(f"ERROR 502: {exc}\n", 502)
        if not msg:
            return text_response("ERROR 404: message not found\n", 404)
        lines = [
            f"# {msg['subject']}\n\n",
            f"from    = {toml_str(msg['from'])}\n",
            f"to      = {toml_str(msg['to'])}\n",
        ]
        if msg["cc"]:
            lines.append(f"cc      = {toml_str(msg['cc'])}\n")
        lines.append(f"date    = {toml_str(msg['date'])}\n")
        lines.append(f"uid     = {toml_str(uid)}\n")
        lines.append("\n---\n\n")
        lines.append(msg["body"] + "\n")
        return text_response("".join(lines))

    def _sent():
        if not cfg.imap_ok():
            return text_response(
                f"ERROR 503: IMAP not configured. Set: {', '.join(cfg.missing())}\n", 503
            )
        page, per_page = _parse_page_params()
        try:
            total, msgs = _imap_list(cfg, cfg.imap_sent_folder, False, page, per_page)
        except Exception as exc:
            return text_response(f"ERROR 502: {exc}\n", 502)
        total_pages = max(1, (total + per_page - 1) // per_page)
        lines = [
            f"# Sent  ({cfg.imap_sent_folder})\n",
            f"# page {page} of {total_pages} ({total} messages)\n",
        ]
        if page < total_pages:
            lines.append(f"# ?page={page+1}&per_page={per_page} for next\n")
        lines.append("\n")
        for m in msgs:
            lines.append("[[messages]]\n")
            lines.append(f'uid     = {toml_str(m["uid"])}\n')
            lines.append(f'to      = {toml_str(m["from"])}\n')  # sent: from field = recipient
            lines.append(f'subject = {toml_str(m["subject"])}\n')
            lines.append(f'date    = {toml_str(m["date"])}\n')
            lines.append(f'url     = {toml_str(prefix + "/sent/" + m["uid"])}\n')
            lines.append("\n")
        return text_response("".join(lines))

    flask_app.add_url_rule(
        prefix + "/send",
        endpoint=ep + "_send", view_func=_send, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/inbox",
        endpoint=ep + "_inbox", view_func=_inbox)
    flask_app.add_url_rule(
        prefix + "/inbox/<uid>",
        endpoint=ep + "_message", view_func=_message, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/sent",
        endpoint=ep + "_sent", view_func=_sent)


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_mailbox(inact_app, prefix: str, **kwargs) -> None:
    """
    Mount an external email connector at *prefix*.

    Credentials are resolved from keyword arguments first, then environment
    variables.  See module docstring for the full list of env vars.

    Example::

        mount_mailbox(app, "/mail")  # reads SMTP_* and IMAP_* from env

        mount_mailbox(app, "/mail",
                      smtp_host="smtp.gmail.com", smtp_user="you@gmail.com",
                      smtp_password="app-pw",     imap_host="imap.gmail.com")
    """
    cfg = MailConfig(**kwargs)
    p = "/" + prefix.strip("/")
    attach_mailbox(inact_app, p, cfg)

    missing = cfg.missing()
    status = "⚠ missing: " + ", ".join(missing) if missing else f"smtp={cfg.smtp_host}  imap={cfg.imap_host}"
    help_text = (
        f"\nMailbox: {p}  ({status})\n"
        f"  POST   {p}/send           send email\n"
        f'                             body: {{"to":"...","subject":"...","body":"..."}}\n'
        f"  GET    {p}/inbox          list inbox  (?page=1  ?unread=1)\n"
        f"  GET    {p}/inbox/{{uid}}    read message (marks Seen)\n"
        f"  DELETE {p}/inbox/{{uid}}    delete message\n"
        f"  GET    {p}/sent           list sent folder\n"
        f"  # env: SMTP_HOST  SMTP_USER  SMTP_PASSWORD  IMAP_HOST\n"
    )
    inact_app._app_mounts.append((p, help_text))
