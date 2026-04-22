"""
Example inact application — demonstrates the dict-first API.
"""

import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inact import Inact, MdContent, TomlContent, text_response

app = Inact(__name__)


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

@app.help("/")
def root_help():
    return """\
INACT EXAMPLE SERVER

Routes:
  GET  /                  Homepage (markdown)
  GET  /status            Service status (TOML)
  GET  /api               API overview (markdown)
  GET  /docs/.ls          List documentation files
  GET  /docs/.grep        Search documentation

A2A chatbot (requires chatbot.py running on :5001):
  GET  /chatbot/.card     Agent card
  POST /chatbot/chat      Chat — body: {"message": "..."}

Append /_human/ prefix for HTML, /.help suffix for help.
"""


@app.help("/api")
def api_help():
    return "GET /api — API overview. All endpoints return text/plain.\n"


# ---------------------------------------------------------------------------
# Markdown pages — return (metadata_dict, body_str)
# ---------------------------------------------------------------------------

@app.inact_md("/")
def index():
    return MdContent(
        """\
# Welcome

This is an **inact** demo server.

## What is inact?

A Flask toolkit for building AI-oriented websites. Every page is:

- Plain text by default (great for `curl` and agents)
- HTML-rendered at `/_human/<path>` (great for humans)
- Self-documenting via `/.help`

## Quick navigation

    GET /              This page
    GET /status        Service status (TOML)
    GET /api           API overview
    GET /docs/.ls      Browse documentation
    GET /_human/       HTML version of this page

## A2A Chatbot

Start `chatbot.py` on port 5001, then:

    GET  /chatbot/.card       Agent card
    POST /chatbot/chat        {"message": "hello"}
""",
        title="Inact Example",
        description="AI-friendly web server demo",
    )


@app.inact_md("/api")
def api():
    return MdContent(
        """\
# API Overview

## Conventions

- All responses are `text/plain; charset=utf-8`
- Lists use TOML `[[item]]` syntax for easy grep/awk parsing
- Append `/.help` to any path for contextual documentation
- Prefix with `/_human/` for browser-friendly HTML

## Endpoints

| Path | Description |
|------|-------------|
| `/` | Homepage |
| `/status` | Service status (TOML) |
| `/api` | This page |
| `/docs/.ls` | List documentation files |
| `/docs/.grep?q=` | Search documentation |
| `/_human/<path>` | HTML rendering |
| `<path>/.help` | Contextual help |
""",
        title="API Overview",
        help="This page documents the API surface. All endpoints return text/plain.",
    )


# ---------------------------------------------------------------------------
# TOML pages — return InactToml
# ---------------------------------------------------------------------------

@app.inact_toml("/status")
def status():
    return TomlContent(
        {
            "title": "Service Status",
            "service": {
                "name": "inact-example",
                "status": "running",
                "version": "0.1.0",
                "timestamp": int(time.time()),
            },
            "endpoints": {
                "homepage": "/",
                "status": "/status",
                "api": "/api",
                "docs": "/docs/.ls",
            },
        },
        annotation="GET /status — service status\nRefresh any time; timestamp updates on each request.",
    )


# ---------------------------------------------------------------------------
# Mounted documentation folder
# ---------------------------------------------------------------------------

app.mount("/docs", os.path.join(os.path.dirname(__file__), "docs"))


# ---------------------------------------------------------------------------
# A2A chatbot (run example/chatbot.py first on port 5001)
# ---------------------------------------------------------------------------

app.mount_a2a("/chatbot", "http://localhost:5001")


# ---------------------------------------------------------------------------
# Plain Flask pass-through
# ---------------------------------------------------------------------------

@app.route("/ping")
def ping():
    return text_response("pong\n")


if __name__ == "__main__":
    app.run(debug=True)
