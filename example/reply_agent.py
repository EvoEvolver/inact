"""
An agent that automatically replies to messages using claude -p.

Flow:
  1. Someone sends a message to this agent via POST /msg/send
  2. The message system fires a notification (if connected via notify_storage)
  3. The notify system POSTs to this agent's /wake callback
  4. The agent reads its unread inbox, uses claude -p to reply to each message
  5. Revival loop re-checks every --interval seconds for missed messages

Usage:
    # Start the inact server first (see below), then:
    python example/reply_agent.py --agent-id 1 --port 7779

Quick start — run everything in one go:
    python example/reply_agent.py --self-contained
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

import requests
from flask import Flask, jsonify
from flask import request as freq

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

SERVER   = "http://localhost:5050"
MSG      = "/msg"
NOTIFY   = "/notify"
AGENT_ID = "1"
PORT     = 7779
INTERVAL = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def fetch_conversation(with_id: str) -> list[dict]:
    """
    Return the full message history between this agent and *with_id*,
    sorted oldest-first: [{"role": "user"|"assistant", "text": str}, ...]
    """
    history = []
    try:
        # Messages we received from with_id
        r = requests.get(f"{SERVER}{MSG}/inbox",
                         params={"per_page": "50"},
                         headers={"X-Agent-Id": AGENT_ID}, timeout=10)
        for block in r.text.split("[[messages]]")[1:]:
            from_id = (re.search(r'from\s*=\s*"([^"]+)"', block) or ["",""])[1]
            url     = (re.search(r'url\s*=\s*"([^"]+)"',  block) or ["",""])[1]
            date    = (re.search(r'date\s*=\s*"([^"]+)"', block) or ["",""])[1]
            if from_id != str(with_id) or not url:
                continue
            body = requests.get(SERVER + url, timeout=5).text
            text = body.split("\n---\n\n")[1].strip() if "\n---\n\n" in body else ""
            if text:
                history.append({"role": "user", "text": text, "date": date})
    except Exception:
        pass

    try:
        # Messages we sent to with_id
        r = requests.get(f"{SERVER}{MSG}/sent",
                         headers={"X-Agent-Id": AGENT_ID}, timeout=10)
        for block in r.text.split("[[messages]]")[1:]:
            to_id = (re.search(r'to\s*=\s*"([^"]+)"',   block) or ["",""])[1]
            body  = (re.search(r'body\s*=\s*"([^"]*)"', block) or ["",""])[1]
            date  = (re.search(r'date\s*=\s*"([^"]+)"', block) or ["",""])[1]
            if to_id != str(with_id) or not body:
                continue
            history.append({"role": "assistant", "text": body, "date": date})
    except Exception:
        pass

    history.sort(key=lambda m: m.get("date", ""))
    return history


def claude_reply(my_id: str, from_id: str, history: list[dict]) -> str:
    lines = [
        f"You are AI agent #{my_id}. You are having a conversation with agent #{from_id}.",
        "Conversation history (oldest first):",
        "",
    ]
    for m in history:
        speaker = f"Agent #{from_id}" if m["role"] == "user" else f"You (agent #{my_id})"
        lines.append(f"{speaker}: {m['text']}")
    lines += ["", "Reply to the last message. Be helpful and concise (2-3 sentences)."]
    result = subprocess.run(
        ["claude", "-p", "\n".join(lines)],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Inbox processing
# ---------------------------------------------------------------------------

def process_inbox() -> int:
    """Read all unread messages, reply with full context. Returns reply count."""
    try:
        r = requests.get(
            f"{SERVER}{MSG}/inbox",
            params={"unread": "1", "per_page": "50"},
            headers={"X-Agent-Id": AGENT_ID},
            timeout=10,
        )
    except Exception as exc:
        print(f"[inbox] error: {exc}", file=sys.stderr)
        return 0

    # Group by sender so we fetch history once per conversation, not per message
    senders: dict[str, list] = {}
    for block in r.text.split("[[messages]]")[1:]:
        m_id    = (re.search(r'id\s*=\s*"([^"]+)"',   block) or ["", ""])[1]
        from_id = (re.search(r'from\s*=\s*"([^"]+)"', block) or ["", ""])[1]
        url     = (re.search(r'url\s*=\s*"([^"]+)"',  block) or ["", ""])[1]
        if m_id and from_id:
            senders.setdefault(from_id, []).append(url)

    replied = 0
    for from_id, urls in senders.items():
        # Fetch full conversation context
        history = fetch_conversation(from_id)
        if not history:
            continue

        last_msg = next((m["text"] for m in reversed(history) if m["role"] == "user"), "")
        print(f"\n[msg] from agent #{from_id}: {last_msg[:100]}")
        print(f"[ctx] {len(history)} messages in history")

        reply = claude_reply(AGENT_ID, from_id, history)
        print(f"[reply] {reply}")

        try:
            requests.post(
                f"{SERVER}{MSG}/send",
                json={"from": AGENT_ID, "to": from_id, "body": reply},
                timeout=5,
            )
            replied += 1
        except Exception as exc:
            print(f"[reply] send error: {exc}", file=sys.stderr)

    return replied


# ---------------------------------------------------------------------------
# Callback server
# ---------------------------------------------------------------------------

agent_app = Flask("reply-agent")


@agent_app.route("/wake", methods=["POST"])
def wake():
    payload = freq.get_json(force=True, silent=True) or {}
    notif_type = payload.get("type", "notification")
    print(f"\n[wake] {notif_type} — checking inbox...")
    threading.Thread(target=process_inbox, daemon=True).start()
    return jsonify({"status": "ok"})


@agent_app.route("/health")
def health():
    return jsonify({"status": "alive", "agent_id": AGENT_ID, "server": SERVER})


# ---------------------------------------------------------------------------
# Revival loop
# ---------------------------------------------------------------------------

def revival_loop(interval: int) -> None:
    while True:
        time.sleep(interval)
        ts = time.strftime("%H:%M:%S")
        print(f"\n[revival] {ts} — checking inbox...")
        n = process_inbox()
        if not n:
            print(f"[revival] nothing to do")


# ---------------------------------------------------------------------------
# Self-contained mode: start the inact server too
# ---------------------------------------------------------------------------

def start_inact_server() -> None:
    import importlib.util
    for dep in ["inact", "flask", "aiosmtpd"]:
        if importlib.util.find_spec(dep) is None and dep == "aiosmtpd":
            pass  # optional

    from inact import Inact
    from inact.apps.register import mount_register
    from inact.apps.message import mount_message
    from inact.apps.notify import mount_notify

    db_agents = "/tmp/reply_demo_agents.db"
    db_msg    = "/tmp/reply_demo_msg.db"
    db_notify = "/tmp/reply_demo_notify.db"
    for f in [db_agents, db_msg, db_notify]:
        try: os.unlink(f)
        except: pass

    app = Inact("reply-demo")
    mount_register(app, "/agents", db_agents)
    mount_notify(app, "/notify", db_notify, revival_interval=600)
    mount_message(app, "/msg", db_msg,
                  agents_prefix="/agents",
                  notify_storage=db_notify)

    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    time.sleep(0.8)

    # Register the reply agent (agent #1) and a human test account (agent #2)
    r1 = requests.post("http://localhost:5050/agents/", json={"name": "reply-bot"})
    r2 = requests.post("http://localhost:5050/agents/", json={"name": "human"})
    id1 = re.search(r"id\s+=\s+(\d+)", r1.text).group(1)
    id2 = re.search(r"id\s+=\s+(\d+)", r2.text).group(1)
    key2 = re.search(r'api_key\s*=\s*"([^"]+)"', r2.text).group(1)

    print(f"\n  inact server: http://localhost:5050")
    print(f"  reply-bot = agent #{id1}  (this agent)")
    print(f"  human     = agent #{id2}  api_key={key2[:20]}...")
    print(f"\n  Chat UI:  http://localhost:5050/_human/agents/  (register with name=human, then chat)")
    print(f"\n  Or send directly:")
    print(f'    curl -X POST localhost:5050/msg/send \\')
    print(f'      -H "Content-Type: application/json" \\')
    print(f'      -d \'{{"from":"{id2}","to":"{id1}","body":"Hello bot!"}}\'\n')

    global AGENT_ID
    AGENT_ID = id1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global SERVER, AGENT_ID, PORT, INTERVAL

    parser = argparse.ArgumentParser(description="Self-replying agent using claude -p")
    parser.add_argument("--server",         default=SERVER,   help="inact server base URL")
    parser.add_argument("--agent-id",       default=None,     help="agent ID (assigned by register app)")
    parser.add_argument("--port",           default=PORT,     type=int)
    parser.add_argument("--interval",       default=INTERVAL, type=int, help="revival interval in seconds")
    parser.add_argument("--self-contained", action="store_true",
                        help="also start an inact server on :5050 with all apps pre-wired")
    args = parser.parse_args()

    SERVER   = args.server.rstrip("/")
    PORT     = args.port
    INTERVAL = args.interval

    if args.self_contained:
        start_inact_server()

    if args.agent_id:
        AGENT_ID = args.agent_id

    callback_url = f"http://localhost:{PORT}/wake"

    # Start callback server
    threading.Thread(
        target=lambda: agent_app.run(port=PORT, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    time.sleep(0.5)
    print(f"[agent] callback server on :{PORT}")

    # Register callback with notify system
    try:
        r = requests.post(
            f"{SERVER}{NOTIFY}/register",
            json={"agent_id": AGENT_ID, "callback": callback_url},
            timeout=5,
        )
        print(f"[agent] {r.text.strip()}")
    except Exception as exc:
        print(f"[agent] WARNING: could not register: {exc}", file=sys.stderr)

    # Process any messages already waiting
    n = process_inbox()
    if n:
        print(f"[agent] replied to {n} existing message(s)")

    # Start revival loop
    threading.Thread(target=revival_loop, args=(INTERVAL,), daemon=True).start()

    print(f"\nAgent #{AGENT_ID} ready — waiting for messages")
    print(f"  callback: {callback_url}")
    print(f"  revival:  every {INTERVAL}s\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[agent] stopped.")


if __name__ == "__main__":
    main()
