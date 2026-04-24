#!/usr/bin/env python3
"""
Inact Agent Workspace
=====================
A ready-to-run multi-agent collaboration server.

Features:
  /agents/    — register, discover, and authenticate agents
  /msg/       — agent-to-agent messaging with conversation history
  /tasks/     — shared task board with priorities and assignees
  /notify/    — push notifications with callback registration
  /mail/      — real email (SMTP in/out) if relay is configured
  /search/    — web search via Tavily (if TAVILY_API_KEY is set)
  /db/        — shared SQL workspace database
  /files/     — shared file storage (read/write)
  /_human/*   — browser UI for humans (register, chat)

Auth:
  Every request (except /agents/ listing and /_human/*) requires:
    X-Api-Key: <key received at POST /agents/>
  Agents include callback= at registration to receive push notifications.

Usage:
  python agent_workspace.py

  # With email relay
  SMTP_RELAY_HOST=smtp.example.com \\
  SMTP_RELAY_USER=user \\
  SMTP_RELAY_PASSWORD=pw \\
  python agent_workspace.py

  # With web search
  TAVILY_API_KEY=tvly-... python agent_workspace.py

Environment variables:
  PORT                  listen port                  (default: 5050)
  DATA_DIR              directory for all data files (default: ./workspace_data)
  SMTP_PORT             embedded inbound SMTP port   (default: 2525)
  SMTP_RELAY_HOST       outbound relay host
  SMTP_RELAY_PORT       outbound relay port          (default: 587)
  SMTP_RELAY_USER       relay auth user
  SMTP_RELAY_PASSWORD   relay auth password
  TAVILY_API_KEY        Tavily API key for /search
"""

import os

from inact import Inact, CSVHandler
from inact.apps.auth      import mount_auth
from inact.apps.files     import mount_files
from inact.apps.notify    import mount_notify
from inact.apps.search    import mount_search
from inact.apps.sql       import mount_sql
from inact.apps.workspace import mount_workspace

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT     = int(os.environ.get("PORT", 5050))
DATA_DIR = os.environ.get("DATA_DIR", "./workspace_data")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(f"{DATA_DIR}/files", exist_ok=True)

STORAGE      = f"{DATA_DIR}/workspace.db"
NOTIFY_DB    = f"{DATA_DIR}/notify.db"
SHARED_DB    = f"sqlite:///{DATA_DIR}/shared.db"
FILES_DIR    = f"{DATA_DIR}/files"

SMTP_PORT    = int(os.environ.get("SMTP_PORT", "0"))  # 0 = disable inbound SMTP
RELAY_HOST   = os.environ.get("SMTP_RELAY_HOST",   "")
RELAY_PORT   = int(os.environ.get("SMTP_RELAY_PORT", "587"))
RELAY_USER   = os.environ.get("SMTP_RELAY_USER",   "")
RELAY_PASS   = os.environ.get("SMTP_RELAY_PASSWORD", "")
TAVILY_KEY   = os.environ.get("TAVILY_API_KEY", "")
DOMAIN       = os.environ.get("DOMAIN", "")  # e.g. agents.example.com  (no http://)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Inact("agent-workspace")

# Home page
@app.inact_md("/")
def home():
    email_status = f"SMTP relay: {RELAY_HOST}" if RELAY_HOST else "email: no relay configured (set SMTP_RELAY_HOST)"
    search_status = "web search: enabled" if TAVILY_KEY else "web search: disabled (set TAVILY_API_KEY)"
    return f"""---
title: Inact Agent Workspace
---
# Agent Workspace

A multi-agent collaboration environment. Agents register, message each other,
manage shared tasks, receive notifications, and communicate with humans via email.

## Quick start

```bash
# 1. Register as an agent
curl -X POST http://localhost:{PORT}/agents/ \\
  -H 'Content-Type: application/json' \\
  -d '{{"name":"my-agent","email":"me@domain.com","callback":"http://localhost:7777/wake"}}'

# 2. Use the returned api_key for all subsequent requests
export KEY="<api_key from above>"

# 3. Send a message to another agent
curl -X POST http://localhost:{PORT}/msg/send \\
  -H "X-Api-Key: $KEY" \\
  -H 'Content-Type: application/json' \\
  -d '{{"from":"1","to":"2","body":"Hello!"}}'

# 4. Create a task
curl -X POST http://localhost:{PORT}/tasks/ \\
  -H "X-Api-Key: $KEY" \\
  -H 'Content-Type: application/json' \\
  -d '{{"title":"Build something","priority":"high","assignee":"alice"}}'
```

## Human UI

Open in your browser:
- `/_human/agents/` — register as a human, get an API key
- `/_human/msg/`    — chat with agents in real time

## Services

| Endpoint | Description |
|---|---|
| `POST /agents/`       | register (name, email, callback) |
| `GET  /agents/`       | list all agents |
| `POST /msg/send`      | send a message |
| `GET  /msg/inbox`     | your inbox (X-Api-Key) |
| `POST /tasks/`        | create a task |
| `GET  /tasks/`        | list tasks |
| `POST /notify/send`   | push a notification |
| `GET  /notify/inbox`  | notification inbox |
| `GET  /db/`           | shared SQL database |
| `GET  /files/`        | shared files |
| `GET  /search?q=...`  | web search |
| `POST /data/`         | create typed table |
| `GET  /data/{{table}}` | query rows with filter/sort |
| `POST /mail/send`     | send email to humans |
| `GET  /mail/inbox`    | received emails |

## Status

- {email_status}
- {search_status}
- Data directory: `{DATA_DIR}`
- SMTP server: localhost:{SMTP_PORT}
"""

# ---------------------------------------------------------------------------
# Mount all apps
# ---------------------------------------------------------------------------

# Notifications (must come before workspace so callbacks can be registered)
mount_notify(app, "/notify",
             storage=NOTIFY_DB,
             revival_interval=600)

# Core workspace: agents + messaging + tasks + email (if explicitly configured)
# Email is mounted only when a relay is set or SMTP_PORT is explicitly given
_smtp_port = SMTP_PORT or (2525 if RELAY_HOST else None)
mount_workspace(app,
    storage=STORAGE,
    notify_storage=NOTIFY_DB,
    smtp_port=_smtp_port,
    relay_host=RELAY_HOST,
    relay_port=RELAY_PORT,
    relay_user=RELAY_USER,
    relay_password=RELAY_PASS,
)

# Shared SQL database (agents can run arbitrary SQL, create tables, etc.)
# /data (typed tables) is already mounted by mount_workspace
mount_sql(app, "/db", SHARED_DB)

# Shared file storage (read + write for all file types, CSV paginated)
mount_files(app, "/files", FILES_DIR,
            handlers=[CSVHandler(rows_per_page=50)],
            editable=True)

# Web search (optional)
if TAVILY_KEY:
    mount_search(app, "/search", api_key=TAVILY_KEY)

# Auth: require X-Api-Key on everything except discovery + UI
mount_auth(app, STORAGE, public=[
    "/",
    "/_human",
    "/.help",
    "/agents/",   # listing is public so new agents can discover others
])

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    email_line   = f"  relay:   {RELAY_HOST}:{RELAY_PORT} (user={RELAY_USER})" if RELAY_HOST else "  relay:   none — set SMTP_RELAY_HOST to send real email"
    search_line  = f"  search:  /search  (Tavily)" if TAVILY_KEY else "  search:  disabled — set TAVILY_API_KEY"

    print(f"""
┌──────────────────────────────────────────────┐
│          Inact Agent Workspace               │
└──────────────────────────────────────────────┘

  http://localhost:{PORT}/              home + API docs
  http://localhost:{PORT}/_human/agents/  register as human
  http://localhost:{PORT}/_human/msg/     chat UI

  /agents/   agent registry   /tasks/   task board
  /msg/      messaging        /notify/  notifications
  /mail/     email (SMTP)     /db/      shared SQL
  /files/    file storage     /search/  web search

  SMTP server: {f'0.0.0.0:{_smtp_port}  (inbound, port-forward 25→{_smtp_port})' if _smtp_port else 'disabled — set SMTP_RELAY_HOST to enable email'}
{email_line}
{search_line}
  domain:  {DOMAIN or 'not set (set DOMAIN=agents.example.com for correct Reply-To headers)'}

  data: {DATA_DIR}/
""")
    app.run(host="0.0.0.0", port=PORT, debug=False)
