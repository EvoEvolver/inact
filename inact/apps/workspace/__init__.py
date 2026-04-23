"""
Workspace — agent identity, messaging, tasks, and email in one mount.

mount_workspace(app, storage) mounts:

  /agents      agent registry      (mount_register)
  /msg         internal messaging  (mount_message)
  /tasks       task list           (mount_todo)
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
from .message  import MessageStore,  attach_message,  mount_message
from .mailbox  import MailStore,     attach_mailbox,  mount_mailbox
from .todo     import TodoStore,     attach_todo,     mount_todo


__all__ = [
    # stores
    "AgentRegistry", "MessageStore", "MailStore", "TodoStore",
    # attach functions
    "attach_register", "attach_message", "attach_mailbox", "attach_todo",
    # mount functions
    "mount_register", "mount_message", "mount_mailbox", "mount_todo",
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
    tasks_prefix  = f"{p}/tasks"
    mail_prefix   = f"{p}/mail"

    mount_register(inact_app, agents_prefix, storage)

    mount_message(inact_app, msg_prefix, storage,
                  agents_prefix=agents_prefix,
                  notify_storage=notify_storage)

    mount_todo(inact_app, tasks_prefix, storage)

    # Email: mount if SMTP is configured
    smtp_host_env = os.environ.get("SMTP_HOST", "")
    if smtp_port or smtp_host_env:
        mount_mailbox(inact_app, mail_prefix, storage,
                      registry=storage,
                      smtp_port=smtp_port or 2525,
                      relay_host=relay_host,
                      relay_port=relay_port,
                      relay_user=relay_user,
                      relay_password=relay_password)
