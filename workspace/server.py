#!/usr/bin/env python3
"""
Inact Agent Workspace — multi-agent collaboration server.

Environment variables:
  PORT                  listen port          (default: 5050)
  DATA_DIR              data directory       (default: ./workspace_data)
  SMTP_PORT             inbound SMTP port    (default: 0 = disabled)
  SMTP_RELAY_HOST       outbound SMTP relay
  SMTP_RELAY_PORT       relay port           (default: 587)
  SMTP_RELAY_USER       relay auth user
  SMTP_RELAY_PASSWORD   relay auth password
  TAVILY_API_KEY        enables /search
  ADMIN_KEY             secret for /_human/admin
  SMTP2GO_API_KEY       use SMTP2GO HTTP API for outbound email
  FROM_EMAIL            default sender for system emails
  FRONTEND_URL          optional URL for human UI links in notifications
"""

import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

if os.path.exists(".env"):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        raise SystemExit(
            "ERROR: .env file found but python-dotenv is not installed.\n"
            "  pip install python-dotenv\n"
            "or remove the .env file if you don't need it."
        )

from inact import Inact, CSVHandler
from inact.utils import server_base
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
FILES_DIR    = f"{DATA_DIR}/files"  # on-disk folder; served at /documents

SMTP_PORT    = int(os.environ.get("SMTP_PORT", "0"))  # 0 = disable inbound SMTP
RELAY_HOST   = os.environ.get("SMTP_RELAY_HOST",   "")
RELAY_PORT   = int(os.environ.get("SMTP_RELAY_PORT", "587"))
RELAY_USER   = os.environ.get("SMTP_RELAY_USER",   "")
RELAY_PASS   = os.environ.get("SMTP_RELAY_PASSWORD", "")
TAVILY_KEY   = os.environ.get("TAVILY_API_KEY", "")
DOMAIN       = os.environ.get("DOMAIN", "")      # e.g. agents.example.com
ADMIN_KEY    = os.environ.get("ADMIN_KEY", "123456")
CODE_SERVER_PORT = int(os.environ.get("CODE_SERVER_PORT", "0")) or None  # 0 = disabled

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Inact("agent-workspace")

# Home page
@app.inact_md("/")
def home():
    base = server_base()
    optional = []
    if TAVILY_KEY:
        optional.append("| `GET  /search?q=...`  | web search (Tavily) |")
    if RELAY_HOST or SMTP_PORT:
        optional.append("| `POST /mail/send`     | send email to humans |")
        optional.append("| `GET  /mail/inbox`    | received emails |")
    optional_rows = "\n".join(optional)
    return f"""# Agent Workspace

`{base}`

## Services

| Endpoint | Description |
|---|---|
| `GET  /members/`        | list agents and humans |
| `GET  /msg/sessions`   | your message sessions |
| `POST /msg/sessions`   | start a session |
| `GET  /issues/`        | shared issue tracker |
| `POST /issues/`        | open an issue |
| `GET  /notify/inbox`   | notification inbox |
| `GET  /documents/`     | shared documents (add your docs here) |
| `POST /notify/register`| register push callback |
| `GET  /db/`            | raw SQL database |
| `POST /data/`          | typed tables |
{optional_rows}

## Help

`GET <endpoint>/.help` — full API reference for any service
"""

# ---------------------------------------------------------------------------
# Mount all apps
# ---------------------------------------------------------------------------

# Notifications (must come before workspace so callbacks can be registered)
mount_notify(app, "/notify",
             storage=NOTIFY_DB,
             revival_interval=600,
             registry=STORAGE,
             agents_prefix="/members")

# Core workspace: agents + messaging + issues + email (if explicitly configured)
_smtp_port = SMTP_PORT or (2525 if RELAY_HOST else None)
mount_workspace(app,
    storage=STORAGE,
    notify_storage=NOTIFY_DB,
    smtp_port=_smtp_port,
    relay_host=RELAY_HOST,
    relay_port=RELAY_PORT,
    relay_user=RELAY_USER,
    relay_password=RELAY_PASS,
    admin_key=ADMIN_KEY,
)

# Shared SQL database (agents can run arbitrary SQL, create tables, etc.)
# /data (typed tables) is already mounted by mount_workspace
mount_sql(app, "/db", SHARED_DB)

# Shared file storage (read + write for all file types, CSV paginated)
# Shared documents storage (read + write for all file types, CSV paginated)
mount_files(app, "/documents", FILES_DIR,
            handlers=[CSVHandler(rows_per_page=50)],
            editable=True,
            code_server_port=CODE_SERVER_PORT)

# Web search: always mount so /_human/search/ works even without a key
mount_search(app, "/search", api_key=TAVILY_KEY)

# Auth: require X-Api-Key on everything except discovery + UI + favicon
# Explicitly whitelist favicon to avoid 403s from browsers without/invalid auth
mount_auth(
    app,
    STORAGE,
    public=[
        "/",
        "/.help",
        "/favicon.ico",
        "/admin",          # standalone admin: own X-Admin-Key auth
        "/_human/admin",
    ],
)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    local = f"http://localhost:{PORT}"
    lines = [
        "",
        "  Inact Agent Workspace",
        f"  {local}/",
        "",
        "  /members/     registry     /msg/        messaging",
        "  /issues/     issues       /notify/     notifications",
        "  /documents/  documents    /db/         SQL",
    ]
    if TAVILY_KEY:
        lines.append("  /search    web search")
    if RELAY_HOST or _smtp_port:
        smtp_info = f"relay={RELAY_HOST}" if RELAY_HOST else f"inbound port {_smtp_port}"
        lines.append(f"  /mail/     email ({smtp_info})")
    lines.append(f"  data: {DATA_DIR}/")
    lines.append("")
    print("\n".join(lines))
    app.run(host="0.0.0.0", port=PORT, debug=False)

# gunicorn entry point: gunicorn "server:wsgi"
wsgi = app.app
