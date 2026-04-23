"""
A self-reviving agent that responds to notifications via the inact notify app.

Architecture:
  - Runs a local Flask server to receive push callbacks from the notify system
  - Registers its callback URL at startup
  - On each callback: runs claude -p to process the notification and react
  - Revival loop: every 10 min, polls the notify inbox and processes any unread
    notifications (catches messages missed while offline)

Usage:
    # 1. Start the inact server (see example/server.py or below)
    # 2. python example/notify_agent.py --server http://localhost:5050 --agent-id 1 --port 7777

    python example/notify_agent.py
"""

import argparse
import subprocess
import sys
import threading
import time

import requests
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Defaults (override via CLI args)
# ---------------------------------------------------------------------------

SERVER    = "http://localhost:5050"
NOTIFY    = "/notify"
AGENT_ID  = "1"
PORT      = 7777
INTERVAL  = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Claude helper
# ---------------------------------------------------------------------------

def think(context: str) -> str:
    """Ask claude -p to process a notification and return a response."""
    prompt = (
        "You are an AI agent registered in the Inact system. "
        "You just received a notification:\n\n"
        + context
        + "\n\nAcknowledge it and decide what (if anything) to do. "
        "Reply in 2-3 sentences."
    )
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Notification handler
# ---------------------------------------------------------------------------

def handle_notification(notif_type: str, payload: dict) -> None:
    if notif_type == "revival":
        unread = payload.get("unread", 0)
        print(f"\n[revival] {unread} unread notification(s) — checking inbox...")
        poll_inbox()
        return

    from_id  = payload.get("from",    "system")
    message  = payload.get("message", "")
    notif_id = payload.get("id",      "")

    print(f"\n[push] from={from_id}  message={message!r}")
    context = f"From: {from_id}\nMessage: {message}"
    reply = think(context)
    print(f"[agent] {reply}")

    # Mark notification as read
    if notif_id:
        requests.get(f"{SERVER}{NOTIFY}/inbox/{notif_id}",
                     headers={"X-Agent-Id": AGENT_ID})


def poll_inbox() -> None:
    """Fetch unread notifications and process each one."""
    try:
        r = requests.get(
            f"{SERVER}{NOTIFY}/inbox",
            params={"unread": "1"},
            headers={"X-Agent-Id": AGENT_ID},
            timeout=10,
        )
    except Exception as exc:
        print(f"[poll] error: {exc}")
        return

    import re
    blocks = r.text.split("[[notifications]]")[1:]
    for block in blocks:
        notif_id = (re.search(r'id\s*=\s*"([^"]+)"',      block) or ["", ""])[1]
        from_id  = (re.search(r'from\s*=\s*"([^"]*)"',    block) or ["", ""])[1]
        message  = (re.search(r'message\s*=\s*"([^"]*)"', block) or ["", ""])[1]
        if notif_id:
            handle_notification("push", {
                "id": notif_id, "from": from_id, "message": message,
            })


# ---------------------------------------------------------------------------
# Revival loop
# ---------------------------------------------------------------------------

def revival_loop(interval: int) -> None:
    print(f"[revival] polling every {interval}s for missed notifications")
    while True:
        time.sleep(interval)
        print(f"[revival] checking inbox at {time.strftime('%H:%M:%S')}...")
        poll_inbox()


# ---------------------------------------------------------------------------
# Callback server
# ---------------------------------------------------------------------------

callback_app = Flask("notify-agent")


@callback_app.route("/wake", methods=["POST"])
def wake():
    payload = request.get_json(force=True, silent=True) or {}
    notif_type = payload.get("type", "notification")
    threading.Thread(
        target=handle_notification, args=(notif_type, payload), daemon=True
    ).start()
    return jsonify({"status": "ok"})


@callback_app.route("/health")
def health():
    return jsonify({"status": "alive", "agent_id": AGENT_ID})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global SERVER, AGENT_ID, PORT, INTERVAL

    parser = argparse.ArgumentParser()
    parser.add_argument("--server",   default=SERVER,   help="inact server base URL")
    parser.add_argument("--agent-id", default=AGENT_ID, help="agent ID from register app")
    parser.add_argument("--port",     default=PORT,     type=int, help="local callback port")
    parser.add_argument("--interval", default=INTERVAL, type=int, help="revival interval (seconds)")
    args = parser.parse_args()

    SERVER   = args.server.rstrip("/")
    AGENT_ID = args.agent_id
    PORT     = args.port
    INTERVAL = args.interval

    callback_url = f"http://localhost:{PORT}/wake"

    # Start callback server
    threading.Thread(
        target=lambda: callback_app.run(port=PORT, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    time.sleep(0.5)
    print(f"[agent] callback server on :{PORT}")

    # Register with notify system
    try:
        r = requests.post(
            f"{SERVER}{NOTIFY}/register",
            json={"agent_id": AGENT_ID, "callback": callback_url},
            timeout=5,
        )
        print(f"[agent] registered callback: {r.text.strip()}")
    except Exception as exc:
        print(f"[agent] WARNING: could not register callback: {exc}", file=sys.stderr)

    # Process any notifications already waiting
    print(f"[agent] checking inbox for existing notifications...")
    poll_inbox()

    # Start revival loop in background
    threading.Thread(target=revival_loop, args=(INTERVAL,), daemon=True).start()

    print(f"\nAgent #{AGENT_ID} is running.")
    print(f"  Send a notification: POST {SERVER}{NOTIFY}/send")
    print(f'  Body: {{"to":"{AGENT_ID}","message":"hello","from":"system"}}')
    print(f"  Callback: {callback_url}")
    print(f"  Revival:  every {INTERVAL}s\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[agent] stopped.")


if __name__ == "__main__":
    main()
