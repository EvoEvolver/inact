"""
Workspace-agnostic reply agent powered by PydanticAI + OpenRouter.

The agent makes no assumptions about the workspace API structure.
On each wake the raw notification payload is handed to the LLM, which
uses curl_workspace to figure out what to do.

Usage:
    OPENROUTER_API_KEY=sk-... python agent/agent.py
"""

import argparse
import json
import os
import queue
import re
import subprocess
import threading
import time

import logfire
import requests as http
from flask import Flask, jsonify
from flask import request as freq
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

logfire.configure()
logfire.instrument_pydantic_ai()

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
SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT",  "600"))
# Periodic self-check interval in seconds — safety net for missed push notifications (0 = off)
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL",    "60"))
NOTIFY_REGISTER = os.environ.get("NOTIFY_REGISTER_PATH", "/notify/register")
NOTIFY_INBOX    = os.environ.get("NOTIFY_INBOX_PATH",    "/notify/inbox")


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
        logfire.info("resolved id={agent_id} from api key", agent_id=AGENT_ID)
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
            logfire.info("memory: saved {filename} — {desc}", filename=filename, desc=desc)

    if new_entries:
        update_memory_index(new_entries)
        logfire.info("memory: MEMORY.md updated ({n} new entries)", n=len(new_entries))


# ---------------------------------------------------------------------------
# PydanticAI agent
# ---------------------------------------------------------------------------

_model = OpenAIChatModel(
    MODEL,
    provider=OpenAIProvider(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    ),
)
_agent: Agent[None, str] = Agent(_model)


@_agent.system_prompt
def _system_prompt() -> str:
    memory = load_memory()
    lines = [
        f"You are AI agent #{AGENT_ID} connected to a workspace at {WORKSPACE_HOST}.",
        "Use curl_workspace to interact with the workspace API.",
        "Use bash for local shell tasks.",
        "",
        "## Acting on notifications",
        "Every notification contains the context and exact API calls you need — read it and act.",
        "from_kind tells you who sent it: 'human' = real person, 'agent' = another bot.",
        "",
        "On a revival tick:",
        "  1. GET /notify/inbox   — act on every unread notification",
        "  2. GET /msg/sessions   — reply to any session with unread > 0",
        "",
        "## File access",
        "  read        : curl_workspace GET    /files/path/to/file",
        "  list dir    : curl_workspace GET    /files/",
        "  overwrite   : curl_workspace POST   /files/path/to/file/.replace  text_body='...'",
        "  append      : curl_workspace POST   /files/path/to/file/.append   text_body='...'",
        "  patch       : curl_workspace POST   /files/path/to/file/.patch",
        "                  body={\"old\":\"exact string to replace\",\"new\":\"replacement\"}",
        "  delete      : curl_workspace DELETE /files/dav/path/to/file",
        "Use .patch for code edits — it replaces exactly one occurrence, returns 409 if not found.",
    ]
    if memory:
        lines += [
            "",
            "## Your long-term memory",
            "(MEMORY.md index — use bash to cat referenced files for details)",
            memory,
        ]
    lines += [
        "",
        "## Instructions",
        "Each user message is a notification or revival tick.",
        "You MUST take action on every unread message and pending notification — no exceptions.",
        "Do NOT skip, defer, or summarize notifications without acting on them.",
        "For every unread item: read it, respond or join as appropriate, then move on to the next.",
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


@_agent.tool_plain
def bash(command: str) -> str:
    """Execute a bash command and return its stdout/stderr."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60
        )
        out = result.stdout
        if result.stderr:
            out += f"\nSTDERR:\n{result.stderr}"
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 60s"
    except Exception as exc:
        return f"ERROR: {exc}"


@_agent.tool_plain
def curl_workspace(method: str, path: str, body: dict | None = None,
                   text_body: str | None = None) -> str:
    """Make an HTTP request to the workspace server. Base URL and auth headers are added automatically.
    Use body for JSON payloads, text_body for raw text/file content (e.g. WebDAV PUT)."""
    try:
        kwargs: dict = {}
        if text_body is not None:
            kwargs["data"] = text_body.encode()
            kwargs["headers"] = _headers({"Content-Type": "application/octet-stream"})
        elif body is not None:
            kwargs["json"] = body
            kwargs["headers"] = _headers({"Content-Type": "application/json"})
        else:
            kwargs["headers"] = _headers()
        r = http.request(
            method.upper(),
            WORKSPACE_HOST + path,
            timeout=10,
            **kwargs,
        )
        return r.text[:4000]
    except Exception as exc:
        return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
# Conversation state + LLM runner
# ---------------------------------------------------------------------------

_history: list = []
_session_start: float = 0.0


def _run_llm(user_msg: str) -> str:
    global _history
    result = _agent.run_sync(user_msg, message_history=_history)
    _history = list(result.all_messages())
    return result.output


def _reset_session() -> None:
    global _history, _session_start
    logfire.info("session timeout — consolidating memory and resetting conversation")
    try:
        output = _run_llm(
            "Session timeout reached. Save everything worth keeping from this conversation "
            "to long-term memory using the MEMORY section format, then confirm with one sentence."
        )
        if "MEMORY:" in output:
            _, _, mem = output.partition("MEMORY:")
            apply_memory(mem.strip())
    except Exception as exc:
        logfire.exception("consolidation error")
    _history = []
    _session_start = time.time()
    logfire.info("conversation reset")


# ---------------------------------------------------------------------------
# Notification queue + agent loop
# ---------------------------------------------------------------------------

def _get_unread_notification_ids() -> list[str]:
    """Return the IDs of all currently unread notifications."""
    try:
        resp = http.get(
            f"{WORKSPACE_HOST}{NOTIFY_INBOX}",
            headers=_headers(),
            params={"unread": "1"},
            timeout=5,
        )
        return re.findall(r'id\s*=\s*"([^"]+)"', resp.text)
    except Exception as exc:
        logfire.warning("could not fetch unread notifications: {exc}", exc=exc)
        return []


def _mark_notifications_read(ids: list[str]) -> None:
    """Mark a specific set of notification IDs as read."""
    for nid in ids:
        try:
            http.get(f"{WORKSPACE_HOST}{NOTIFY_INBOX}/{nid}", headers=_headers(), timeout=5)
        except Exception as exc:
            logfire.warning("could not mark notification {nid} as read: {exc}", nid=nid, exc=exc)
    if ids:
        logfire.info("marked {n} notification(s) as read", n=len(ids))


_notification_queue: queue.Queue = queue.Queue()
_agent_busy = threading.Event()


def _agent_loop() -> None:
    while True:
        payload = _notification_queue.get()
        _agent_busy.set()

        try:
            batch = [payload]
            while not _notification_queue.empty():
                try:
                    batch.append(_notification_queue.get_nowait())
                except queue.Empty:
                    break

            if SESSION_TIMEOUT > 0 and time.time() - _session_start > SESSION_TIMEOUT:
                _reset_session()

            parts = []
            for p in batch:
                if p is None:
                    parts.append("Revival tick: check for any unread messages and reply to them.")
                else:
                    parts.append(f"Notification received:\n{json.dumps(p, indent=2)}")

            user_msg = "\n\n".join(parts)
            logfire.info("trigger: {msg}", msg=user_msg[:200])

            # Snapshot unread IDs before the LLM runs — new ones arriving
            # during the call must stay unread so they get processed next.
            unread_before = _get_unread_notification_ids()

            try:
                output = _run_llm(user_msg)
            except Exception as exc:
                logfire.exception("LLM error")
                continue

            reply = output.partition("MEMORY:")[0].strip() if "MEMORY:" in output else output.strip()
            logfire.info("reply: {reply}", reply=reply)

            if "MEMORY:" in output:
                _, _, mem = output.partition("MEMORY:")
                try:
                    apply_memory(mem.strip())
                except Exception:
                    logfire.exception("memory error")

            _mark_notifications_read(unread_before)
        finally:
            _agent_busy.clear()


# ---------------------------------------------------------------------------
# Callback server
# ---------------------------------------------------------------------------

agent_app = Flask("reply-agent")


@agent_app.route("/wake", methods=["POST"])
def wake():
    payload = freq.get_json(force=True, silent=True) or {}
    logfire.info("wake: {type} — queued", type=payload.get("type", "notification"))
    _notification_queue.put(payload)
    return jsonify({"status": "ok"})


@agent_app.route("/health")
def health():
    return jsonify({"status": "alive", "agent_id": AGENT_ID, "workspace": WORKSPACE_HOST})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global WORKSPACE_HOST, AGENT_ID, PORT, CALLBACK_URL, AGENT_KEY, _session_start

    parser = argparse.ArgumentParser(description="Inact reply agent")
    parser.add_argument("--workspace",  default=None, help="override WORKSPACE_HOST env var")
    parser.add_argument("--agent-id",   default=None, help="agent id (resolved from key if omitted)")
    parser.add_argument("--agent-key",  default=None, help="override AGENT_KEY env var")
    parser.add_argument("--port",       default=None, type=int)
    parser.add_argument("--callback",   default=None)
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
            logfire.info("registered callback: {resp}", resp=r.text.strip())
        except Exception as exc:
            logfire.warning("could not register callback: {exc}", exc=exc)

    _session_start = time.time()
    threading.Thread(target=_agent_loop, daemon=True).start()

    if POLL_INTERVAL > 0:
        def _poll_loop() -> None:
            while True:
                time.sleep(POLL_INTERVAL)
                if _agent_busy.is_set():
                    continue  # agent is mid-run; don't pile up revival ticks
                try:
                    resp = http.get(
                        f"{WORKSPACE_HOST}{NOTIFY_INBOX}",
                        headers=_headers(),
                        params={"unread": "1"},
                        timeout=5,
                    )
                    has_unread = "[[notifications]]" in resp.text
                except Exception:
                    has_unread = True  # wake on error so we don't miss messages
                if has_unread:
                    logfire.info("poll — unread notifications found, queuing check")
                    _notification_queue.put(None)
        threading.Thread(target=_poll_loop, daemon=True).start()

    logfire.info("checking for pending messages...")
    _notification_queue.put(None)

    logfire.info(
        "agent #{agent_id} ready — workspace={workspace} callback={callback} model={model}",
        agent_id=AGENT_ID, workspace=WORKSPACE_HOST, callback=callback_url, model=MODEL,
    )

    logfire.info("callback server on :{port}", port=PORT)
    agent_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
