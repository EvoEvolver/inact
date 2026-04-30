"""
Workspace — agent identity, messaging, issues, and email in one mount.

mount_workspace(app, storage) mounts:

  /agents      agent registry      (mount_register)
  /msg         internal messaging  (mount_message)
  /issues      issue tracker       (mount_issues)
  /mail        external email      (mount_mailbox, optional — needs SMTP_HOST)

All apps share a single *storage* database.

Example::

    from inact import Inact
    from inact.apps.workspace import mount_workspace

    app = Inact(__name__)
    mount_workspace(app, "./workspace.db")           # no email
    mount_workspace(app, "./workspace.db",           # with email
                    smtp_port=2525)

    # Custom prefix
    mount_workspace(app, "./workspace.db", prefix="/workspace")

Environment variables for email (see mailbox module docstring):
    SMTP_HOST, SMTP_PORT, SMTP_RELAY_HOST, SMTP_RELAY_USER, SMTP_RELAY_PASSWORD
"""

from __future__ import annotations

import os

from .register import AgentRegistry, attach_register, mount_register
from .message  import SessionStore, MessageStore, attach_message, mount_message
from .mailbox  import MailStore,     attach_mailbox,  mount_mailbox
from .database import DbStore,       attach_db,       mount_db
from ..issues  import IssueStore,    attach_issues,   mount_issues


__all__ = [
    # stores
    "AgentRegistry", "SessionStore", "MessageStore", "MailStore", "IssueStore", "DbStore",
    # attach functions
    "attach_register", "attach_message", "attach_mailbox", "attach_issues", "attach_db",
    # mount functions
    "mount_register", "mount_message", "mount_mailbox", "mount_issues", "mount_db",
    # unified
    "mount_workspace",
]


def mount_workspace(
    inact_app,
    storage: str,
    prefix: str = "",
    smtp_port: int | None = None,
    relay_host: str = "",
    relay_port: int = 587,
    relay_user: str = "",
    relay_password: str = "",
    notify_storage: str | None = None,
    admin_key: str = "",
) -> None:
    """
    Mount the full agent workspace at *prefix*.

    Always mounts:
      {prefix}/agents   — agent registry (register + api_key)
      {prefix}/msg      — agent-to-agent messaging
      {prefix}/tasks    — task list with priorities and assignees

    Mounts email if ``SMTP_HOST`` is set or *smtp_port* is provided:
      {prefix}/mail     — self-hosted SMTP inbox + outbound relay

    All apps share *storage* (a single SQLite file or Postgres URL).

    Example::

        mount_workspace(app, "./workspace.db")

        # With self-hosted email server on port 2525
        mount_workspace(app, "./workspace.db", smtp_port=2525)

        # Everything under /ws
        mount_workspace(app, "./workspace.db", prefix="/ws")
    """
    p = prefix.rstrip("/")
    agents_prefix = f"{p}/agents"
    msg_prefix    = f"{p}/msg"
    issues_prefix = f"{p}/issues"
    mail_prefix   = f"{p}/mail"

    mount_register(inact_app, agents_prefix, storage,
                   notify_storage=notify_storage, admin_key=admin_key)

    mount_message(inact_app, msg_prefix, storage,
                  agents_prefix=agents_prefix,
                  notify_storage=notify_storage,
                  registry=storage)

    mount_issues(inact_app, issues_prefix, storage,
                 agents_prefix=agents_prefix,
                 agents_storage=storage,
                 notify_storage=notify_storage)
    mount_db(inact_app, f"{p}/data", storage)

    # Email: always mount routes (for human UI); SMTP server only if configured
    smtp_host_env = os.environ.get("SMTP_HOST", "")
    mount_mailbox(inact_app, mail_prefix, storage,
                  registry=storage,
                  notify_storage=notify_storage,
                  smtp_port=smtp_port or (2525 if smtp_host_env else None),
                  relay_host=relay_host,
                  relay_port=relay_port,
                  relay_user=relay_user,
                  relay_password=relay_password)
