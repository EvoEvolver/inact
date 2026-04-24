"""
Reply agent powered by the Anthropic Python SDK.

Key improvement over the old subprocess-based version:
  Conversation history is passed as a structured `messages` array with
  proper `user`/`assistant` roles instead of being embedded as plain text
  inside the prompt.  This is why the old agent kept triggering prompt-
  injection warnings — Claude saw incoming agent messages as part of its
  own instructions.  The SDK approach keeps the system prompt (identity,
  memory, tool hints) cleanly separated from the conversation content.

Flow:
  1. Someone sends a message to this agent via POST /msg/send
  2. The notify system POSTs to this agent's /wake callback
  3. The agent reads its unread inbox, calls Claude via the SDK, replies
  4. Revival loop re-checks every --interval seconds for missed messages

Usage:
    python agent/agent.py --self-contained
"""

import argparse
import os
import re
import subprocess
import sys
import threading
import time

import anthropic
import requests as http
from flask import Flask, jsonify
from flask import request as freq

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SERVER        = os.environ.get("SERVER",            "http://localhost:5050").rstrip("/")
AGENT_KEY     = os.environ.get("AGENT_KEY",         "")
AGENT_ID      = os.environ.get("AGENT_ID",          "1")
CALLBACK_URL  = os.environ.get("CALLBACK_URL",      "")
PORT          = int(os.environ.get("PORT",           "7779"))
INTERVAL      = int(os.environ.get("REVIVAL_INTERVAL", "600"))
MEMORY_DIR    = os.environ.get("MEMORY_DIR",        "./memory")
MODEL         = os.environ.get("MODEL",             "claude-sonnet-4-6")
MSG           = "/msg"
NOTIFY        = "/notify"

_client = anthropic.Anthropic()


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
    index = os.path.join(_memory_path(), _MEMORY_INDEX)
    if not os.path.exists(index):
        return ""
    try:
        return open(index, encoding="utf-8").read().strip()
    except OSError:
        return ""


def save_memory_file(filename: str, content: str) -> str:
    path = os.path.join(_memory_path(), filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def update_memory_index(entries: list[tuple[str, str]]) -> None:
    index_path = os.path.join(_memory_path(), _MEMORY_INDEX)
    lines = []
    if os.path.exists(index_path):
        lines = open(index_path, encoding="utf-8").readlines()
        while lines and not lines[-1].strip():
            lines.pop()
    for filename, description in entries:
        lines.append(f"- [{filename}]({filename}) — {description}\n")
    with open(index_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Tool definitions and execution
# ---------------------------------------------------------------------------

_TOOLS: list[dict] = [
    {
        "name": "Bash",
        "description": (
            "Execute a bash command and return its stdout/stderr. "
            "Use for shell operations, reading files, running scripts, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"}
            },
            "required": ["command"],
        },
    },
    {
        "name": "WebFetch",
        "description": "Fetch the text content of a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "prompt": {
                    "type": "string",
                    "description": "Optional hint about what to extract from the page",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "WebSearch",
        "description": "Search the web and return titles, URLs, and snippets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"],
        },
    },
]


def _run_tool(name: str, inputs: dict) -> str:
    if name == "Bash":
        try:
            result = subprocess.run(
                inputs["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            out = result.stdout
            if result.stderr:
                out += f"\nSTDERR:\n{result.stderr}"
            return out or "(no output)"
        except subprocess.TimeoutExpired:
            return "ERROR: command timed out after 60s"
        except Exception as exc:
            return f"ERROR: {exc}"

    if name == "WebFetch":
        try:
            r = http.get(
                inputs["url"],
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            return r.text[:8000]
        except Exception as exc:
            return f"ERROR: {exc}"

    if name == "WebSearch":
        tavily_key = os.environ.get("TAVILY_API_KEY", "")
        if tavily_key:
            try:
                r = http.post(
                    "https://api.tavily.com/search",
                    json={"api_key": tavily_key, "query": inputs["query"], "max_results": 5},
                    timeout=10,
                )
                results = r.json().get("results", [])
                return "\n\n".join(
                    f"{i + 1}. {res['title']}\n{res['url']}\n{res.get('content', '')[:300]}"
                    for i, res in enumerate(results)
                )
            except Exception as exc:
                return f"Search error: {exc}"
        # Fallback: workspace /search/ endpoint
        try:
            r = http.get(
                f"{SERVER}/search/",
                params={"q": inputs["query"]},
                headers=_headers(),
                timeout=10,
            )
            return r.text[:4000]
        except Exception as exc:
            return f"Search unavailable: {exc}"

    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Claude SDK wrapper — handles the tool-use loop
# ---------------------------------------------------------------------------

def _run_claude(system: str, messages: list[dict]) -> str:
    """
    Call Claude with the given system prompt and message history, running the
    tool loop until Claude produces a final text response.

    The system prompt is marked for prompt caching so repeated calls with the
    same agent identity / memory block hit the cache.
    """
    msgs = list(messages)

    while True:
        response = _client.messages.create(
            model=MODEL,
            max_tokens=8096,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=msgs,
            tools=_TOOLS,
        )

        text_parts: list[str] = []
        tool_uses: list = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        if response.stop_reason == "end_turn" or not tool_uses:
            return "\n".join(text_parts)

        # Append assistant turn (preserving tool_use blocks for the API)
        assistant_content = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        msgs.append({"role": "assistant", "content": assistant_content})

        # Execute each tool and collect results
        tool_results = []
        for tu in tool_uses:
            result = _run_tool(tu.name, tu.input)
            print(f"[tool:{tu.name}] {str(tu.input)[:80]} → {result[:80]}")
            tool_results.append(
                {"type": "tool_result", "tool_use_id": tu.id, "content": result}
            )
        msgs.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

_GROUP_TOOLS_HINT = """
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
"""


def _group_tools_hint() -> str:
    key_header = f"-H 'X-Api-Key: {AGENT_KEY}'" if AGENT_KEY else ""
    return _GROUP_TOOLS_HINT.format(
        server=SERVER, my_id=AGENT_ID, key_header=key_header
    )


def _dm_system(my_id: str, from_id: str, memory: str) -> str:
    lines = [
        f"You are AI agent #{my_id} in an agent communication system.",
        f"You are having a conversation with agent #{from_id}.",
        "Use Bash, WebFetch, and WebSearch tools freely when they would help.",
        _group_tools_hint(),
    ]
    if memory:
        lines += [
            "\n## Your long-term memory",
            "(MEMORY.md index — use Bash/cat to read referenced files if you need details)",
            memory,
            "",
        ]
    lines += [
        "\n## Instructions",
        "Reply to the last message. Be concise (2-4 sentences).",
        "If anything from this conversation is worth saving to long-term memory,",
        "append a MEMORY section after your reply using this exact format:",
        "",
        "MEMORY:",
        "FILE: <short_descriptive_name>.md",
        "DESC: <one-line description for the index>",
        "---",
        "<markdown content>",
        "===",
        "(repeat FILE/DESC/---/content/=== for each file, max 3)",
        "If nothing is worth saving, do not include a MEMORY section at all.",
    ]
    return "\n".join(lines)


def _group_system(my_id: str, group_id: str, group_name: str, memory: str) -> str:
    lines = [
        f"You are AI agent #{my_id} in an agent communication system.",
        f"You are in a group chat named '{group_name}' (group id: {group_id}).",
        "You have access to Bash, WebFetch, and WebSearch tools.",
        _group_tools_hint(),
    ]
    if memory:
        lines += [
            "\n## Your long-term memory",
            memory,
            "",
        ]
    lines += [
        "\n## Instructions",
        "Reply to the latest message in the group. Be concise (1-3 sentences).",
        "Optionally append a MEMORY section if anything is worth saving (same format as DMs).",
        "If nothing is worth saving, do not include a MEMORY section.",
    ]
    return "\n".join(lines)


def _build_dm_messages(history: list[dict]) -> list[dict]:
    """Convert DM conversation history to a proper messages array."""
    if not history:
        return [{"role": "user", "content": "(start of conversation)"}]

    msgs = [{"role": m["role"], "content": m["text"]} for m in history]

    # Messages API requires the first turn to be "user"
    if msgs[0]["role"] == "assistant":
        msgs.insert(0, {"role": "user", "content": "(conversation started)"})

    # Merge consecutive same-role messages (API requires strictly alternating roles)
    merged: list[dict] = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append({"role": m["role"], "content": m["content"]})

    return merged


def _build_group_messages(history: list[dict], my_id: str) -> list[dict]:
    """Convert group chat history to a messages array."""
    if not history:
        return [{"role": "user", "content": "(group chat started)"}]

    msgs = []
    for m in history:
        role = "assistant" if m["from_id"] == str(my_id) else "user"
        msgs.append(
            {"role": role, "content": f"[Agent #{m['from_id']}]: {m['body']}"}
        )

    if msgs[0]["role"] == "assistant":
        msgs.insert(0, {"role": "user", "content": "(group chat started)"})

    merged: list[dict] = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append({"role": m["role"], "content": m["content"]})

    return merged


# ---------------------------------------------------------------------------
# Claude entry points (drop-in replacements for the old subprocess functions)
# ---------------------------------------------------------------------------

def claude_reply_and_memorize(
    my_id: str, from_id: str, history: list[dict]
) -> tuple[str, str]:
    memory = load_memory()
    system = _dm_system(my_id, from_id, memory)
    messages = _build_dm_messages(history)

    output = _run_claude(system, messages)

    if "MEMORY:" in output:
        reply_part, _, memory_part = output.partition("MEMORY:")
        return reply_part.strip(), memory_part.strip()
    return output.strip(), ""


def claude_group_reply_and_memorize(
    my_id: str, group_id: str, group_name: str, history: list[dict]
) -> tuple[str, str]:
    memory = load_memory()
    system = _group_system(my_id, group_id, group_name, memory)
    messages = _build_group_messages(history, my_id)

    output = _run_claude(system, messages)

    if "MEMORY:" in output:
        reply_part, _, memory_part = output.partition("MEMORY:")
        return reply_part.strip(), memory_part.strip()
    return output.strip(), ""


# ---------------------------------------------------------------------------
# Memory persistence
# ---------------------------------------------------------------------------

def apply_memory(memory_block: str) -> None:
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


# ---------------------------------------------------------------------------
# Server API helpers
# ---------------------------------------------------------------------------

def list_my_groups() -> list[dict]:
    try:
        r = http.get(f"{SERVER}{MSG}/groups", headers=_headers(), timeout=10)
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
    try:
        r = http.get(
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
                msgs.append(
                    {"id": msg_id, "from_id": from_id, "body": body, "date": date}
                )
        return sorted(msgs, key=lambda m: m.get("date", ""))
    except Exception:
        return []


def fetch_conversation(with_id: str) -> list[dict]:
    history = []
    try:
        r = http.get(
            f"{SERVER}{MSG}/inbox",
            params={"per_page": "50"},
            headers=_headers(),
            timeout=10,
        )
        for block in r.text.split("[[messages]]")[1:]:
            from_id = (re.search(r'from\s*=\s*"([^"]+)"', block) or ["", ""])[1]
            url     = (re.search(r'url\s*=\s*"([^"]+)"',  block) or ["", ""])[1]
            date    = (re.search(r'date\s*=\s*"([^"]+)"', block) or ["", ""])[1]
            if from_id != str(with_id) or not url:
                continue
            body = http.get(SERVER + url, timeout=5).text
            text = body.split("\n---\n\n")[1].strip() if "\n---\n\n" in body else ""
            if text:
                history.append({"role": "user", "text": text, "date": date})
    except Exception:
        pass

    try:
        r = http.get(f"{SERVER}{MSG}/sent", headers=_headers(), timeout=10)
        for block in r.text.split("[[messages]]")[1:]:
            to_id = (re.search(r'to\s*=\s*"([^"]+)"',   block) or ["", ""])[1]
            body  = (re.search(r'body\s*=\s*"([^"]*)"', block) or ["", ""])[1]
            date  = (re.search(r'date\s*=\s*"([^"]+)"', block) or ["", ""])[1]
            if to_id != str(with_id) or not body:
                continue
            history.append({"role": "assistant", "text": body, "date": date})
    except Exception:
        pass

    history.sort(key=lambda m: m.get("date", ""))
    return history


# ---------------------------------------------------------------------------
# Inbox processing
# ---------------------------------------------------------------------------

_processed_group_msgs: set[str] = set()


def init_group_state() -> None:
    global _processed_group_msgs
    for g in list_my_groups():
        for m in fetch_group_context(g["id"]):
            _processed_group_msgs.add(m["id"])
    if _processed_group_msgs:
        print(
            f"[agent] {len(_processed_group_msgs)} existing group message(s) marked as seen"
        )


def process_group_inbox() -> int:
    global _processed_group_msgs
    groups = list_my_groups()
    replied = 0
    for g in groups:
        group_id, group_name = g["id"], g["name"]
        history = fetch_group_context(group_id)
        new_msgs = [
            m
            for m in history
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
            http.post(
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


def process_inbox() -> int:
    try:
        r = http.get(
            f"{SERVER}{MSG}/inbox",
            params={"unread": "1", "per_page": "50"},
            headers=_headers(),
            timeout=10,
        )
    except Exception as exc:
        print(f"[inbox] error: {exc}", file=sys.stderr)
        return 0

    senders: dict[str, list] = {}
    for block in r.text.split("[[messages]]")[1:]:
        m_id    = (re.search(r'id\s*=\s*"([^"]+)"',   block) or ["", ""])[1]
        from_id = (re.search(r'from\s*=\s*"([^"]+)"', block) or ["", ""])[1]
        url     = (re.search(r'url\s*=\s*"([^"]+)"',  block) or ["", ""])[1]
        if m_id and from_id:
            senders.setdefault(from_id, []).append(url)

    replied = 0
    for from_id, _urls in senders.items():
        history = fetch_conversation(from_id)
        if not history:
            continue

        last_msg = next(
            (m["text"] for m in reversed(history) if m["role"] == "user"), ""
        )
        print(f"\n[msg] from agent #{from_id}: {last_msg[:100]}")
        print(f"[ctx] {len(history)} messages in history")

        reply, memory_block = claude_reply_and_memorize(AGENT_ID, from_id, history)
        print(f"[reply] {reply}")

        try:
            http.post(
                f"{SERVER}{MSG}/send",
                json={"from": AGENT_ID, "to": from_id, "body": reply},
                headers=_headers({"Content-Type": "application/json"}),
                timeout=5,
            )
            replied += 1
        except Exception as exc:
            print(f"[reply] send error: {exc}", file=sys.stderr)
            continue

        try:
            apply_memory(memory_block)
        except Exception as exc:
            print(f"[memory] error: {exc}", file=sys.stderr)

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
            print("[revival] nothing to do")


# ---------------------------------------------------------------------------
# Self-contained mode
# ---------------------------------------------------------------------------

def start_inact_server() -> None:
    from inact import Inact
    from inact.apps.register import mount_register
    from inact.apps.message import mount_message
    from inact.apps.notify import mount_notify

    db_agents = "/tmp/reply_demo_agents.db"
    db_msg    = "/tmp/reply_demo_msg.db"
    db_notify = "/tmp/reply_demo_notify.db"
    for f in [db_agents, db_msg, db_notify]:
        try:
            os.unlink(f)
        except OSError:
            pass

    app = Inact("reply-demo")
    mount_register(app, "/agents", db_agents)
    mount_notify(app, "/notify", db_notify, revival_interval=600)
    mount_message(
        app, "/msg", db_msg,
        agents_prefix="/agents",
        notify_storage=db_notify,
    )

    threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0", port=5050, debug=False, use_reloader=False
        ),
        daemon=True,
    ).start()
    time.sleep(0.8)

    r1 = http.post("http://localhost:5050/agents/", json={"name": "reply-bot"})
    r2 = http.post("http://localhost:5050/agents/", json={"name": "human"})
    id1  = re.search(r"id\s+=\s+(\d+)", r1.text).group(1)
    id2  = re.search(r"id\s+=\s+(\d+)", r2.text).group(1)
    key2 = re.search(r'api_key\s*=\s*"([^"]+)"', r2.text).group(1)

    print(f"\n  inact server: http://localhost:5050")
    print(f"  reply-bot = agent #{id1}  (this agent)")
    print(f"  human     = agent #{id2}  api_key={key2[:20]}...")
    print(f"\n  Chat UI:  http://localhost:5050/_human/agents/")
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
    global SERVER, AGENT_ID, PORT, INTERVAL, CALLBACK_URL, AGENT_KEY

    parser = argparse.ArgumentParser(description="Inact reply agent — powered by Claude SDK")
    parser.add_argument("--server",        default=None, help="override SERVER env var")
    parser.add_argument("--agent-id",      default=None, help="override AGENT_ID env var")
    parser.add_argument("--agent-key",     default=None, help="override AGENT_KEY env var")
    parser.add_argument("--port",          default=None, type=int)
    parser.add_argument("--interval",      default=None, type=int)
    parser.add_argument("--callback",      default=None)
    parser.add_argument("--self-contained", action="store_true",
                        help="start a local inact server for testing")
    args = parser.parse_args()

    if args.server:    SERVER       = args.server.rstrip("/")
    if args.agent_id:  AGENT_ID     = args.agent_id
    if args.agent_key: AGENT_KEY    = args.agent_key
    if args.port:      PORT         = args.port
    if args.interval:  INTERVAL     = args.interval
    if args.callback:  CALLBACK_URL = args.callback

    if args.self_contained:
        start_inact_server()

    callback_url = CALLBACK_URL or f"http://localhost:{PORT}/wake"

    threading.Thread(
        target=lambda: agent_app.run(
            host="0.0.0.0", port=PORT, debug=False, use_reloader=False
        ),
        daemon=True,
    ).start()
    time.sleep(0.5)
    print(f"[agent] callback server on :{PORT}")

    try:
        r = http.post(
            f"{SERVER}{NOTIFY}/register",
            json={"agent_id": AGENT_ID, "callback": callback_url},
            headers=_headers({"Content-Type": "application/json"}),
            timeout=5,
        )
        print(f"[agent] registered: {r.text.strip()}")
    except Exception as exc:
        print(f"[agent] WARNING: could not register callback: {exc}", file=sys.stderr)

    if AGENT_KEY:
        try:
            http.post(
                f"{SERVER}/agents/{AGENT_ID}/.callback",
                json={"callback": callback_url},
                headers=_headers({"Content-Type": "application/json"}),
                timeout=5,
            )
        except Exception:
            pass

    init_group_state()

    n = process_inbox()
    if n:
        print(f"[agent] replied to {n} existing message(s)")

    threading.Thread(target=revival_loop, args=(INTERVAL,), daemon=True).start()

    print(f"""
Agent #{AGENT_ID} ready
  server:   {SERVER}
  callback: {callback_url}
  revival:  every {INTERVAL}s
  model:    {MODEL}
""")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[agent] stopped.")


if __name__ == "__main__":
    main()
