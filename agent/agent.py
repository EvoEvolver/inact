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
# Config — all values can be overridden by environment variables
# ---------------------------------------------------------------------------

SERVER        = os.environ.get("SERVER",           "http://localhost:5050").rstrip("/")
AGENT_KEY     = os.environ.get("AGENT_KEY",        "")
AGENT_ID      = os.environ.get("AGENT_ID",         "1")
CALLBACK_URL  = os.environ.get("CALLBACK_URL",     "")
PORT          = int(os.environ.get("PORT",          "7779"))
INTERVAL      = int(os.environ.get("REVIVAL_INTERVAL", "600"))
ALLOWED_TOOLS = os.environ.get("ALLOWED_TOOLS",    "WebFetch,WebSearch,Bash")
MEMORY_DIR    = os.environ.get("MEMORY_DIR",       "./memory")
MSG           = "/msg"
NOTIFY        = "/notify"


def _headers(extra: dict | None = None) -> dict:
    h = {"X-Agent-Id": AGENT_ID}
    if AGENT_KEY:
        h["X-Api-Key"] = AGENT_KEY
    if extra:
        h.update(extra)
    return h

# ---------------------------------------------------------------------------
# Memory system
# ---------------------------------------------------------------------------

_MEMORY_INDEX = "MEMORY.md"


def _memory_path() -> str:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return MEMORY_DIR


def load_memory() -> str:
    """Return the full contents of MEMORY.md, or empty string if it doesn't exist."""
    index = os.path.join(_memory_path(), _MEMORY_INDEX)
    if not os.path.exists(index):
        return ""
    try:
        return open(index, encoding="utf-8").read().strip()
    except OSError:
        return ""


def save_memory_file(filename: str, content: str) -> str:
    """Write a memory file to memory/ and return its path."""
    path = os.path.join(_memory_path(), filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def update_memory_index(entries: list[tuple[str, str]]) -> None:
    """
    Append new entries to MEMORY.md.
    entries: list of (filename, one-line description)
    """
    index_path = os.path.join(_memory_path(), _MEMORY_INDEX)
    lines = []
    if os.path.exists(index_path):
        lines = open(index_path, encoding="utf-8").readlines()
        # Remove trailing newline blanks
        while lines and not lines[-1].strip():
            lines.pop()

    for filename, description in entries:
        lines.append(f"- [{filename}]({filename}) — {description}\n")

    with open(index_path, "w", encoding="utf-8") as f:
        f.writelines(lines)




# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

# Track group messages already replied to (in-memory; re-populated on startup)
_processed_group_msgs: set[str] = set()


def list_my_groups() -> list[dict]:
    """Return all groups this agent is a member of."""
    try:
        r = requests.get(f"{SERVER}{MSG}/groups", headers=_headers(), timeout=10)
        groups = []
        for block in r.text.split("[[groups]]")[1:]:
            g_id   = (re.search(r'id\s*=\s*"([^"]+)"',   block) or ["", ""])[1]
            g_name = (re.search(r'name\s*=\s*"([^"]*)"', block) or ["", ""])[1]
            if g_id:
                groups.append({"id": g_id, "name": g_name})
        return groups
    except Exception:
        return []


def fetch_group_context(group_id: str) -> list[dict]:
    """Return recent messages in a group, oldest-first."""
    try:
        r = requests.get(
            f"{SERVER}{MSG}/groups/{group_id}/messages",
            params={"per_page": "50"},
            headers=_headers(),
            timeout=10,
        )
        msgs = []
        for block in r.text.split("[[messages]]")[1:]:
            msg_id  = (re.search(r'id\s*=\s*"([^"]+)"',   block) or ["", ""])[1]
            from_id = (re.search(r'from\s*=\s*"([^"]+)"', block) or ["", ""])[1]
            body    = (re.search(r'body\s*=\s*"([^"]*)"', block) or ["", ""])[1]
            date    = (re.search(r'date\s*=\s*"([^"]+)"', block) or ["", ""])[1]
            if msg_id and from_id:
                msgs.append({"id": msg_id, "from_id": from_id, "body": body, "date": date})
        return sorted(msgs, key=lambda m: m.get("date", ""))
    except Exception:
        return []


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
                         headers=_headers(), timeout=10)
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
                         headers=_headers(), timeout=10)
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


_GROUP_TOOLS_PROMPT = """
## Group chat tools (use Bash to call these)

Create a new group chat:
  curl -s -X POST {server}/msg/groups \\
    -H 'Content-Type: application/json' \\
    -H 'X-Agent-Id: {my_id}' \\
    {key_header} \\
    -d '{{"name":"<name>","created_by":"{my_id}","members":["<id1>","<id2>"]}}'

List all available agents:
  curl -s {server}/agents/ -H 'X-Agent-Id: {my_id}' {key_header}

Send a message to a group (after creating it):
  curl -s -X POST {server}/msg/groups/<group_id>/send \\
    -H 'Content-Type: application/json' \\
    -H 'X-Agent-Id: {my_id}' \\
    {key_header} \\
    -d '{{"from":"{my_id}","body":"<message>"}}'

When asked to create a group chat: list agents first to find the right IDs, then
create the group with those IDs as members (always include your own ID), then send
a welcome message to the group.
"""


def _group_tools_prompt() -> str:
    key_header = f"-H 'X-Api-Key: {AGENT_KEY}'" if AGENT_KEY else ""
    return _GROUP_TOOLS_PROMPT.format(server=SERVER, my_id=AGENT_ID, key_header=key_header)


def claude_reply_and_memorize(my_id: str, from_id: str,
                              history: list[dict]) -> tuple[str, str]:
    """
    Single claude -p session: reply to the conversation AND optionally update memory.

    Returns (reply_text, raw_memory_block).
    The raw_memory_block is empty if claude decided nothing is worth saving.
    """
    memory = load_memory()
    lines = [
        f"You are AI agent #{my_id} in an agent communication system.",
        f"You are having a conversation with agent #{from_id}.",
        "You have access to WebFetch, WebSearch, and Bash tools.",
        "Use them freely: run shell commands, fetch URLs, search the web,",
        "read files — whatever the task requires.",
        _group_tools_prompt(),
    ]
    if memory:
        lines += [
            "## Your long-term memory",
            "(MEMORY.md index — read referenced files with Bash/cat if you need details)",
            memory,
            "",
        ]
    lines += [
        "## Conversation history (oldest first)",
        "",
    ]
    for m in history:
        speaker = f"Agent #{from_id}" if m["role"] == "user" else f"You (agent #{my_id})"
        lines.append(f"{speaker}: {m['text']}")
    lines += [
        "",
        "## Your task",
        "1. Reply to the last message. Use tools if helpful. Be concise (2-4 sentences).",
        "2. After replying, decide if anything from this conversation is worth saving to",
        "   long-term memory for future sessions.",
        "",
        "## Output format — use EXACTLY this structure:",
        "",
        "REPLY:",
        "<your reply here>",
        "",
        "MEMORY:",
        "# if nothing to save, write just: NO_MEMORY",
        "# otherwise write one or more blocks:",
        "FILE: <short_descriptive_name>.md",
        "DESC: <one-line description for the index>",
        "---",
        "<markdown content>",
        "===",
        "# (repeat FILE/DESC/---/content/=== for each file, max 3)",
    ]

    result = subprocess.run(
        ["claude", "-p", "\n".join(lines), "--allowedTools", ALLOWED_TOOLS],
        capture_output=True, text=True, timeout=180,
    )
    output = result.stdout.strip()

    # Split on the MEMORY: marker
    if "MEMORY:" in output:
        reply_part, _, memory_part = output.partition("MEMORY:")
        # Strip the REPLY: prefix if present
        reply = reply_part.replace("REPLY:", "").strip()
        return reply, memory_part.strip()

    # Fallback: no marker found — treat everything as the reply
    reply = output.replace("REPLY:", "").strip()
    return reply, ""


def apply_memory(memory_block: str) -> None:
    """Parse and persist the MEMORY: section produced by claude."""
    if not memory_block or memory_block.startswith("NO_MEMORY"):
        print("[memory] nothing to save")
        return

    new_entries: list[tuple[str, str]] = []
    for block in memory_block.split("==="):
        block = block.strip()
        if not block:
            continue
        filename = desc = content_start = None
        block_lines = block.split("\n")
        for i, line in enumerate(block_lines):
            if line.startswith("FILE:"):
                filename = line[5:].strip()
            elif line.startswith("DESC:"):
                desc = line[5:].strip()
            elif line.strip() == "---":
                content_start = i + 1
                break
        if filename and desc and content_start is not None:
            content = "\n".join(block_lines[content_start:]).strip()
            save_memory_file(filename, content)
            new_entries.append((filename, desc))
            print(f"[memory] saved {filename}: {desc}")

    if new_entries:
        update_memory_index(new_entries)
        print(f"[memory] MEMORY.md updated ({len(new_entries)} new entries)")


def claude_group_reply_and_memorize(my_id: str, group_id: str, group_name: str,
                                    history: list[dict]) -> tuple[str, str]:
    """Generate a reply for a group conversation. Returns (reply_text, memory_block)."""
    memory = load_memory()
    lines = [
        f"You are AI agent #{my_id} in an agent communication system.",
        f"You are in a group chat named '{group_name}' (group id: {group_id}).",
        "You have access to WebFetch, WebSearch, and Bash tools.",
        _group_tools_prompt(),
    ]
    if memory:
        lines += [
            "## Your long-term memory",
            "(MEMORY.md index — read referenced files with Bash/cat if you need details)",
            memory,
            "",
        ]
    lines += ["## Group chat history (oldest first)", ""]
    for m in history:
        speaker = f"You (#{my_id})" if m["from_id"] == str(my_id) else f"Agent #{m['from_id']}"
        lines.append(f"{speaker}: {m['body']}")
    lines += [
        "",
        "## Your task",
        "1. Reply to the latest message in the group chat. Be concise (1-3 sentences).",
        "2. After replying, decide if anything is worth saving to long-term memory.",
        "",
        "## Output format — use EXACTLY this structure:",
        "",
        "REPLY:",
        "<your reply here>",
        "",
        "MEMORY:",
        "# if nothing to save, write just: NO_MEMORY",
        "# otherwise: FILE: <name>.md / DESC: <desc> / --- / <content> / ===",
    ]

    result = subprocess.run(
        ["claude", "-p", "\n".join(lines), "--allowedTools", ALLOWED_TOOLS],
        capture_output=True, text=True, timeout=180,
    )
    output = result.stdout.strip()
    if "MEMORY:" in output:
        reply_part, _, memory_part = output.partition("MEMORY:")
        return reply_part.replace("REPLY:", "").strip(), memory_part.strip()
    return output.replace("REPLY:", "").strip(), ""


# ---------------------------------------------------------------------------
# Inbox processing
# ---------------------------------------------------------------------------

def process_group_inbox() -> int:
    """Check all groups for new messages and reply. Returns reply count."""
    global _processed_group_msgs
    groups = list_my_groups()
    replied = 0
    for g in groups:
        group_id, group_name = g["id"], g["name"]
        history = fetch_group_context(group_id)
        new_msgs = [
            m for m in history
            if m["from_id"] != str(AGENT_ID) and m["id"] not in _processed_group_msgs
        ]
        if not new_msgs:
            continue
        for m in new_msgs:
            _processed_group_msgs.add(m["id"])

        last_msg = new_msgs[-1]["body"]
        print(f"\n[group:{group_name}] new message: {last_msg[:80]}")
        print(f"[ctx] {len(history)} messages in group history")

        reply, memory_block = claude_group_reply_and_memorize(
            AGENT_ID, group_id, group_name, history
        )
        print(f"[group reply] {reply}")

        try:
            requests.post(
                f"{SERVER}{MSG}/groups/{group_id}/send",
                json={"from": AGENT_ID, "body": reply},
                headers=_headers({"Content-Type": "application/json"}),
                timeout=5,
            )
            replied += 1
        except Exception as exc:
            print(f"[group reply] send error: {exc}", file=sys.stderr)
            continue

        try:
            apply_memory(memory_block)
        except Exception as exc:
            print(f"[memory] error: {exc}", file=sys.stderr)

    return replied


def init_group_state() -> None:
    """On startup, mark all existing group messages as seen (avoid replying to history)."""
    global _processed_group_msgs
    for g in list_my_groups():
        for m in fetch_group_context(g["id"]):
            _processed_group_msgs.add(m["id"])
    if _processed_group_msgs:
        print(f"[agent] {len(_processed_group_msgs)} existing group message(s) marked as seen")


def process_inbox() -> int:
    """Read all unread messages, reply with full context. Returns reply count."""
    try:
        r = requests.get(
            f"{SERVER}{MSG}/inbox",
            params={"unread": "1", "per_page": "50"},
            headers=_headers(),
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

        # Single claude session: reply + memory update
        reply, memory_block = claude_reply_and_memorize(AGENT_ID, from_id, history)
        print(f"[reply] {reply}")

        try:
            requests.post(
                f"{SERVER}{MSG}/send",
                json={"from": AGENT_ID, "to": from_id, "body": reply},
                headers=_headers({"Content-Type": "application/json"}),
                timeout=5,
            )
            replied += 1
        except Exception as exc:
            print(f"[reply] send error: {exc}", file=sys.stderr)
            continue

        # Persist any memory updates from this session
        try:
            apply_memory(memory_block)
        except Exception as exc:
            print(f"[memory] error: {exc}", file=sys.stderr)

    # Also check group chats for new messages
    replied += process_group_inbox()

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
    # All config comes from environment variables (see .env.example).
    # CLI args are optional overrides for local testing.
    global SERVER, AGENT_ID, PORT, INTERVAL, CALLBACK_URL, AGENT_KEY

    parser = argparse.ArgumentParser(description="Inact reply agent — powered by claude")
    parser.add_argument("--server",      default=None, help="override SERVER env var")
    parser.add_argument("--agent-id",    default=None, help="override AGENT_ID env var")
    parser.add_argument("--agent-key",   default=None, help="override AGENT_KEY env var")
    parser.add_argument("--port",        default=None, type=int, help="override PORT env var")
    parser.add_argument("--interval",    default=None, type=int, help="override REVIVAL_INTERVAL env var")
    parser.add_argument("--callback",    default=None, help="override CALLBACK_URL env var")
    parser.add_argument("--self-contained", action="store_true",
                        help="start a local inact server for testing")
    args = parser.parse_args()

    if args.server:   SERVER       = args.server.rstrip("/")
    if args.agent_id: AGENT_ID     = args.agent_id
    if args.agent_key:AGENT_KEY    = args.agent_key
    if args.port:     PORT         = args.port
    if args.interval: INTERVAL     = args.interval
    if args.callback: CALLBACK_URL = args.callback

    if args.self_contained:
        start_inact_server()

    # Use CALLBACK_URL if provided, otherwise fall back to localhost (dev only)
    callback_url = CALLBACK_URL or f"http://localhost:{PORT}/wake"

    # Start the Flask callback server (gunicorn in production via Dockerfile CMD)
    threading.Thread(
        target=lambda: agent_app.run(host="0.0.0.0", port=PORT,
                                      debug=False, use_reloader=False),
        daemon=True,
    ).start()
    time.sleep(0.5)
    print(f"[agent] callback server on :{PORT}")

    # Register callback with notify system
    try:
        r = requests.post(
            f"{SERVER}{NOTIFY}/register",
            json={"agent_id": AGENT_ID, "callback": callback_url},
            headers=_headers({"Content-Type": "application/json"}),
            timeout=5,
        )
        print(f"[agent] registered: {r.text.strip()}")
    except Exception as exc:
        print(f"[agent] WARNING: could not register callback: {exc}", file=sys.stderr)

    # Also update the callback stored in the agents table
    if AGENT_KEY:
        try:
            requests.post(
                f"{SERVER}/agents/{AGENT_ID}/.callback",
                json={"callback": callback_url},
                headers=_headers({"Content-Type": "application/json"}),
                timeout=5,
            )
        except Exception:
            pass

    # Mark existing group messages as seen (don't reply to history on startup)
    init_group_state()

    # Process any messages already waiting
    n = process_inbox()
    if n:
        print(f"[agent] replied to {n} existing message(s)")

    # Start revival loop
    threading.Thread(target=revival_loop, args=(INTERVAL,), daemon=True).start()

    print(f"""
Agent #{AGENT_ID} ready
  server:   {SERVER}
  callback: {callback_url}
  revival:  every {INTERVAL}s
  tools:    {ALLOWED_TOOLS}
""")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[agent] stopped.")


if __name__ == "__main__":
    main()
