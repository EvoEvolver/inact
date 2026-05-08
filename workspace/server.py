#!/usr/bin/env python3
"""
Inact Agent Workspace — multi-agent collaboration server.

Environment variables:
  PORT       listen port   (default: 5050)
  DATA_DIR   data directory (default: ./workspace_data)
  ADMIN_KEY  secret for /_human/admin
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

from fastapi import Request

from inact import Inact
from inact.utils import server_base
from inact.apps.auth      import mount_auth
from inact.apps.notify    import mount_notify
from inact.apps.workspace import mount_workspace

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT     = int(os.environ.get("PORT", 5050))
DATA_DIR = os.environ.get("DATA_DIR", "./workspace_data")

os.makedirs(DATA_DIR, exist_ok=True)

STORAGE   = f"{DATA_DIR}/workspace.db"
NOTIFY_DB = f"{DATA_DIR}/notify.db"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "123456")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Inact("agent-workspace")

# Home page
@app.inact_md("/")
def home(request: Request):
    base = server_base(request)
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
| `POST /notify/register`| register push callback |

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

mount_workspace(app,
    storage=STORAGE,
    notify_storage=NOTIFY_DB,
    admin_key=ADMIN_KEY,
)

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
    print(
        f"\n  Inact Agent Workspace\n"
        f"  {local}/\n\n"
        f"  /members/  registry     /msg/     messaging\n"
        f"  /issues/   issues       /notify/  notifications\n"
        f"  data: {DATA_DIR}/\n"
    )
    app.run(host="0.0.0.0", port=PORT, debug=False)

# gunicorn entry point: gunicorn "server:wsgi"
wsgi = app.app
