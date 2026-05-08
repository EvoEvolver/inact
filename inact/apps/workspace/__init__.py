"""
Workspace — agent identity, messaging, issues, and email in one mount.

mount_workspace(app, storage) mounts:

  /members     agent registry      (mount_register)
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

from .register import AgentRegistry, attach_register, mount_register, attach_admin, mount_admin
from .message  import SessionStore, MessageStore, attach_message, mount_message
from ..issues  import IssueStore,    attach_issues,   mount_issues


__all__ = [
    # stores
    "AgentRegistry", "SessionStore", "MessageStore", "IssueStore",
    # attach functions
    "attach_register", "attach_message", "attach_issues",
    # mount functions
    "mount_register", "mount_message", "mount_issues",
    "mount_admin",
    # unified
    "mount_workspace",
]


def mount_workspace(
    inact_app,
    storage: str,
    prefix: str = "",
    notify_storage: str | None = None,
    admin_key: str = "",
) -> None:
    """
    Mount the full agent workspace at *prefix*.

    Always mounts:
      {prefix}/members  — agent registry
      {prefix}/msg      — agent-to-agent messaging
      {prefix}/issues   — issue tracker

    All apps share *storage* (a single SQLite file or Postgres URL).

    Example::

        mount_workspace(app, "./workspace.db")

        # Everything under /ws
        mount_workspace(app, "./workspace.db", prefix="/ws")
    """
    p = prefix.rstrip("/")
    agents_prefix = f"{p}/members"
    msg_prefix    = f"{p}/msg"
    issues_prefix = f"{p}/issues"

    mount_register(inact_app, agents_prefix, storage,
                   notify_storage=notify_storage)
    if admin_key:
        mount_admin(inact_app, f"{p}/admin", storage,
                    admin_key=admin_key, notify_storage=notify_storage)

    mount_message(inact_app, msg_prefix, storage,
                  agents_prefix=agents_prefix,
                  notify_storage=notify_storage,
                  registry=storage)

    mount_issues(inact_app, issues_prefix, storage,
                 agents_prefix=agents_prefix,
                 agents_storage=storage,
                 notify_storage=notify_storage)
