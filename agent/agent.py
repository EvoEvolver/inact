"""
Workspace-agnostic reply agent powered by OpenRouter.

The agent makes no assumptions about the workspace API structure.
On each wake or revival tick the raw notification payload is handed
to the LLM, which uses curl_workspace to figure out what to do.

Usage:
    OPENROUTER_API_KEY=sk-... python agent/agent.py
"""

import argparse
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time

import openai
import requests as http
from flask import Flask, jsonify
from flask import request as freq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WORKSPACE_HOST  = os.environ.get("WORKSPACE_HOST",       "http://localhost:5050").rstrip("/")
AGENT_KEY       = os.environ.get("AGENT_KEY",            "")
AGENT_ID        = os.environ.get("AGENT_ID",             "")   # resolved from key if blank
AGENT_ME_PATH   = os.environ.get("AGENT_ME_PATH",        "/agents/.me")
_railway_domain = os.environ.get("RAILWAY_PRIVATE_DOMAIN", "")
CALLBACK_URL    = os.environ.get("CALLBACK_URL", f"https://{_railway_domain}/wake" if _railway_domain else "")
PORT            = int(os.environ.get("PORT",              "7779"))
MEMORY_DIR      = os.environ.get("MEMORY_DIR",           "./memory")
MODEL           = os.environ.get("MODEL",                "openai/gpt-4o-mini")
# Seconds before the agent consolidates memory and resets the conversation (0 = never)
SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT",  "600"))
# Path the workspace exposes for callback registration (empty = skip registration)
NOTIFY_REGISTER = os.environ.get("NOTIFY_REGISTER_PATH", "/notify/register")

_client = openai.OpenAI(
    api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    base_url="https://openrouter.ai/api/v1",
)


def _headers(extra: dict | None = None) -> dict:
    h = {}
    if AGENT_ID:
        h["X-Agent-Id"] = AGENT_ID
    if AGENT_KEY:
        h["X-Api-Key"] = AGENT_KEY
    if extra:
        h.update(extra)
    return h


def _resolve_agent_id() -> None:
    global AGENT_ID
    if AGENT_ID:
        return
    if not AGENT_KEY:
        raise RuntimeError("Set AGENT_ID or AGENT_KEY so the agent can identify itself")
    try:
        r = http.get(
            f"{WORKSPACE_HOST}{AGENT_ME_PATH}",
            headers={"X-Api-Key": AGENT_KEY},
            timeout=5,
        )
        r.raise_for_status()
        m = re.search(r"^id\s*=\s*(\S+)", r.text, re.MULTILINE)
        if not m:
            raise ValueError(f"no id field in response: {r.text[:200]}")
        AGENT_ID = m.group(1).strip('"')
        log.info("resolved id=%s from api key", AGENT_ID)
    except Exception as exc:
        raise RuntimeError(f"could not resolve agent id: {exc}") from exc


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


def apply_memory(memory_block: str) -> None:
    if not memory_block or memory_block.startswith("NO_MEMORY"):
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
            log.info("memory: saved %s — %s", filename, desc)

    if new_entries:
        update_memory_index(new_entries)
        log.info("memory: MEMORY.md updated (%d new entries)", len(new_entries))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Execute a bash command and return its stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "curl_workspace",
            "description": (
                "Make an HTTP request to the workspace server. "
                "The base URL, X-Agent-Id, and X-Api-Key headers are added automatically. "
                "Use this for all workspace API calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PATCH", "DELETE"],
                        "description": "HTTP method",
                    },
                    "path": {
                        "type": "string",
                        "description": "API path, e.g. /msg/inbox or /agents/",
                    },
                    "body": {
                        "type": "object",
                        "description": "Optional JSON request body (for POST/PATCH)",
                    },
                },
                "required": ["method", "path"],
            },
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

    if name == "curl_workspace":
        method = inputs["method"].upper()
        path = inputs.get("path", "/")
        body = inputs.get("body")
        try:
            r = http.request(
                method,
                WORKSPACE_HOST + path,
                headers=_headers({"Content-Type": "application/json"} if body else None),
                json=body,
                timeout=10,
            )
            return r.text[:4000]
        except Exception as exc:
            return f"ERROR: {exc}"

    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# LLM loop + persistent conversation
# ---------------------------------------------------------------------------

# Single persistent conversation shared across all notifications.
# Initialised in main() once AGENT_ID is resolved.
_conversation: list[dict] = []
_notification_queue: queue.Queue = queue.Queue()


def _run_llm() -> str:
    """Advance _conversation by one LLM turn (including any tool calls). Returns final text."""
    call = 0
    while True:
        call += 1
        log.info("thinking (call #%d)...", call)
        response = _client.chat.completions.create(
            model=MODEL,
            max_tokens=8096,
            messages=_conversation,
            tools=_TOOLS,
        )

        choice = response.choices[0]
        message = choice.message
        _conversation.append(message)

        if choice.finish_reason != "tool_calls" or not message.tool_calls:
            return message.content or ""

        for tc in message.tool_calls:
            inputs = json.loads(tc.function.arguments)
            args_preview = ", ".join(f"{k}={json.dumps(v)[:60]}" for k, v in inputs.items())
            log.info("tool: %s(%s)", tc.function.name, args_preview)
            result = _run_tool(tc.function.name, inputs)
            log.info("  ↳ %s", result.replace("\n", " ")[:120])
            _conversation.append({"role": "tool", "tool_call_id": tc.id, "content": result})


_session_start: float = 0.0


def _reset_session() -> None:
    global _session_start
    log.info("session timeout — consolidating memory and resetting conversation")
    _conversation.append({"role": "user", "content": (
        "Session timeout reached. Save everything worth keeping from this conversation "
        "to long-term memory using the MEMORY section format, then confirm with one sentence."
    )})
    try:
        output = _run_llm()
        if "MEMORY:" in output:
            _, _, mem = output.partition("MEMORY:")
            apply_memory(mem.strip())
    except Exception as exc:
        log.error("consolidation error: %s", exc, exc_info=True)

    _conversation.clear()
    _conversation.append({"role": "system", "content": _system_prompt()})
    _session_start = time.time()
    log.info("conversation reset")


def _agent_loop() -> None:
    """Single worker thread: dequeues notifications and runs the LLM sequentially."""
    while True:
        # Block until at least one notification arrives
        payload = _notification_queue.get()

        if SESSION_TIMEOUT > 0 and time.time() - _session_start > SESSION_TIMEOUT:
            _reset_session()

        # Drain any that piled up while we were busy so they land in one user turn
        batch = [payload]
        while not _notification_queue.empty():
            try:
                batch.append(_notification_queue.get_nowait())
            except queue.Empty:
                break

        parts = []
        for p in batch:
            if p is None:
                parts.append("Revival tick: check for any unread messages and reply to them.")
            else:
                parts.append(f"Notification received:\n{json.dumps(p, indent=2)}")

        user_msg = "\n\n".join(parts)
        log.info("trigger: %s", user_msg[:200])
        _conversation.append({"role": "user", "content": user_msg})

        try:
            output = _run_llm()
        except Exception as exc:
            log.error("LLM error: %s", exc, exc_info=True)
            # Remove the user message we just appended so the conversation stays consistent
            _conversation.pop()
            continue

        reply = output.partition("MEMORY:")[0].strip() if "MEMORY:" in output else output.strip()
        log.info("reply: %s", reply)

        if "MEMORY:" in output:
            _, _, mem = output.partition("MEMORY:")
            try:
                apply_memory(mem.strip())
            except Exception as exc:
                log.error("memory error: %s", exc)


def _system_prompt() -> str:
    memory = load_memory()
    lines = [
        f"You are AI agent #{AGENT_ID} connected to a workspace at {WORKSPACE_HOST}.",
        "Use curl_workspace to interact with the workspace API.",
        "Use Bash for local shell tasks.",
    ]
    if memory:
        lines += [
            "",
            "## Your long-term memory",
            "(MEMORY.md index — use Bash to cat referenced files for details)",
            memory,
        ]
    lines += [
        "",
        "## Instructions",
        "Each user message is a notification or revival tick. Handle it appropriately.",
        "If anything is worth saving to long-term memory, append a MEMORY section:",
        "",
        "MEMORY:",
        "FILE: <short_name>.md",
        "DESC: <one-line description>",
        "---",
        "<content>",
        "===",
        "(repeat for each file, max 3; omit the section entirely if nothing is worth saving)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Callback server
# ---------------------------------------------------------------------------

agent_app = Flask("reply-agent")


@agent_app.route("/wake", methods=["POST"])
def wake():
    payload = freq.get_json(force=True, silent=True) or {}
    log.info("wake: %s — queued", payload.get("type", "notification"))
    _notification_queue.put(payload)
    return jsonify({"status": "ok"})


@agent_app.route("/health")
def health():
    return jsonify({"status": "alive", "agent_id": AGENT_ID, "workspace": WORKSPACE_HOST})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global WORKSPACE_HOST, AGENT_ID, PORT, INTERVAL, CALLBACK_URL, AGENT_KEY

    parser = argparse.ArgumentParser(description="Inact reply agent")
    parser.add_argument("--workspace",  default=None, help="override WORKSPACE_HOST env var")
    parser.add_argument("--agent-id",  default=None, help="agent id (resolved from key if omitted)")
    parser.add_argument("--agent-key", default=None, help="override AGENT_KEY env var")
    parser.add_argument("--port",      default=None, type=int)
    parser.add_argument("--callback",  default=None)
    args = parser.parse_args()

    if args.workspace:  WORKSPACE_HOST = args.workspace.rstrip("/")
    if args.agent_id:   AGENT_ID       = args.agent_id
    if args.agent_key:  AGENT_KEY      = args.agent_key
    if args.port:       PORT           = args.port
    if args.callback:   CALLBACK_URL   = args.callback

    _resolve_agent_id()

    callback_url = CALLBACK_URL or f"http://localhost:{PORT}/wake"

    if NOTIFY_REGISTER:
        try:
            r = http.post(
                f"{WORKSPACE_HOST}{NOTIFY_REGISTER}",
                json={"agent_id": AGENT_ID, "callback": callback_url},
                headers=_headers({"Content-Type": "application/json"}),
                timeout=5,
            )
            log.info("registered callback: %s", r.text.strip())
        except Exception as exc:
            log.warning("could not register callback: %s", exc)

    _conversation.append({"role": "system", "content": _system_prompt()})
    _session_start = time.time()
    threading.Thread(target=_agent_loop, daemon=True).start()

    log.info("checking for pending messages...")
    _notification_queue.put(None)

    log.info(
        "agent #%s ready — workspace=%s callback=%s model=%s",
        AGENT_ID, WORKSPACE_HOST, callback_url, MODEL,
    )

    log.info("callback server on :%d", PORT)
    agent_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
