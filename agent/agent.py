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

# Configure scrubbing so certain fields are not redacted in logs
def scrubbing_callback(m: logfire.ScrubMatch):
    if (
        m.path == ('attributes', 'tool_arguments', 'path')
        and m.pattern_match.group(0) == 'session'
    ):
        return m.value

    if (
        m.path == ('attributes', 'tool_response')
        and m.pattern_match.group(0) == 'Session'
    ):
        return m.value

logfire.configure(scrubbing=logfire.ScrubbingOptions(callback=scrubbing_callback))
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

# Archive hierarchy (finest → coarsest).
# Each tuple: (pattern that matches an entry name, key_fn that returns its parent bucket).
# Bucket names double as subdir names, so they must sort lexicographically by time.
#
#   YYYY-MM-DD-HH-MM[suffix].md  →  YYYY-MM-DD-HH/   (hour bucket)
#   YYYY-MM-DD-HH/               →  YYYY-MM-DD/       (day bucket)
#   YYYY-MM-DD/                  →  YYYY-MM/          (month bucket)
#   YYYY-MM/                     →  YYYY/             (year bucket)
_ARCHIVE_LEVELS: list[tuple[re.Pattern, object]] = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}.*\.md$"), lambda n: n[:13]),
    (re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}$"),             lambda n: n[:10]),
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"),                   lambda n: n[:7]),
    (re.compile(r"^\d{4}-\d{2}$"),                         lambda n: n[:4]),
]


def _memory_path() -> str:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return MEMORY_DIR


def _bucket(name: str) -> str | None:
    """Return the parent bucket dir name for a memory entry, or None if ungroupable."""
    for pat, key_fn in _ARCHIVE_LEVELS:
        if pat.match(name):
            return key_fn(name)
    return None


def _unique_path(base: str) -> str:
    """Return a unique path by appending -N before the extension if needed."""
    if not os.path.exists(base):
        return base
    root, ext = os.path.splitext(base)
    n = 1
    while True:
        cand = f"{root}-{n}{ext}"
        if not os.path.exists(cand):
            return cand
        n += 1


def _move_into(src: str, dest_dir: str) -> None:
    """Move a file or directory into dest_dir, merging if target exists.

    - For files: if a file with the same name exists, append -N before the
      extension to avoid collisions.
    - For directories: if the target directory exists, recursively merge the
      contents then remove the source directory.
    """
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(src.rstrip(os.sep))
    dest_path = os.path.join(dest_dir, name)
    try:
        if os.path.isdir(src):
            # If target exists, merge contents; otherwise rename atomically.
            if not os.path.exists(dest_path):
                os.rename(src, dest_path)
                return
            if os.path.isdir(dest_path):
                for child in os.listdir(src):
                    _move_into(os.path.join(src, child), dest_path)
                try:
                    os.rmdir(src)
                except OSError:
                    pass  # leave if not empty for some reason
                return
            # Target exists and is a file — rename the source dir under a unique name
            dest_path = _unique_path(dest_path)
            os.rename(src, dest_path)
            return
        else:
            # File: resolve collisions by uniquifying the destination filename
            dest_path = _unique_path(dest_path)
            os.rename(src, dest_path)
            return
    except OSError:
        # Last-resort fallback via shutil for cross-device or permission quirks
        import shutil
        if os.path.isdir(src):
            if not os.path.exists(dest_path):
                shutil.move(src, dest_path)
            else:
                # Merge directory contents
                for child in os.listdir(src):
                    _move_into(os.path.join(src, child), dest_path)
                try:
                    os.rmdir(src)
                except OSError:
                    pass
        else:
            shutil.move(src, dest_path)


def _compact(dirpath: str) -> None:
    """
    Recursively compact dirpath to <= 7 date-named children by moving them
    into coarser date-bucket subdirs.  Non-date files are permanent residents
    and are excluded from the count so they never block archiving.
    The `key == dir_name` guard prevents self-referential moves.
    """
    dir_name = os.path.basename(dirpath)
    while True:
        all_entries = [
            e for e in os.listdir(dirpath)
            if not e.startswith(".") and e != _MEMORY_INDEX
        ]
        # Only date-named entries count toward the threshold.
        date_entries = [e for e in all_entries if _bucket(e) is not None]
        if len(date_entries) <= 7:
            break
        moved = False
        # Process files before directories to reduce intermediate dir churn
        date_entries_sorted = sorted(
            date_entries,
            key=lambda n: (0 if os.path.isfile(os.path.join(dirpath, n)) else 1, n),
        )
        for name in list(date_entries_sorted):
            key = _bucket(name)
            if key == dir_name:
                continue
            subdir = os.path.join(dirpath, key)
            _move_into(os.path.join(dirpath, name), subdir)
            logfire.info("memory: {name} → {key}/", name=name, key=key)
            moved = True
        if not moved:
            break
    for entry in os.listdir(dirpath):
        full = os.path.join(dirpath, entry)
        if os.path.isdir(full) and not entry.startswith("."):
            _compact(full)


def _rebuild_memory_index() -> None:
    """Walk the memory tree and regenerate MEMORY.md (newest first)."""
    mem_dir = _memory_path()
    all_files: list[str] = []
    for root, dirs, files in os.walk(mem_dir):
        dirs.sort()
        for fname in sorted(files):
            if fname.endswith(".md") and fname != _MEMORY_INDEX:
                rel = os.path.relpath(os.path.join(root, fname), mem_dir)
                all_files.append(rel)
    all_files.sort(reverse=True)
    with open(os.path.join(mem_dir, _MEMORY_INDEX), "w", encoding="utf-8") as fp:
        for rel in all_files:
            fp.write(f"- [{rel}]({rel})\n")


def archive_if_needed() -> None:
    """Compact the memory tree if any dir has > 7 entries; rebuild index."""
    _compact(_memory_path())
    _rebuild_memory_index()


def auto_save_output(output: str) -> None:
    """Save the agent's final output as a minute-precision dated log, then archive."""
    from datetime import datetime as _dt

    mem_dir = _memory_path()
    base = _dt.now().strftime("%Y-%m-%d-%H-%M")   # YYYY-MM-DD-HH-MM
    path = os.path.join(mem_dir, f"{base}.md")
    counter = 1
    while os.path.exists(path):
        path = os.path.join(mem_dir, f"{base}-{counter}.md")
        counter += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(output)
    logfire.info("memory: saved output → {path}", path=path)
    archive_if_needed()


def load_memory() -> str:
    """Return the MEMORY.md index plus the content of the most recent log."""
    mem_dir = _memory_path()
    index_path = os.path.join(mem_dir, _MEMORY_INDEX)
    if not os.path.exists(index_path):
        return ""
    try:
        index = open(index_path, encoding="utf-8").read().strip()
    except OSError:
        return ""
    if not index:
        return ""

    # Inline the most recent file for immediate context
    m = re.search(r"\(([^)]+)\)", index.split("\n")[0])
    recent = ""
    if m:
        try:
            recent = open(os.path.join(mem_dir, m.group(1)), encoding="utf-8").read().strip()
        except OSError:
            pass

    if recent:
        return f"{index}\n\n### Most recent log\n{recent[:3000]}"
    return index


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
        "Your complete final response is automatically saved as a dated memory log.",
        "Do NOT output any special MEMORY block — just respond naturally.",
        "Use bash to cat recent memory files when you need past context.",
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
    logfire.info("session timeout — resetting conversation")
    try:
        output = _run_llm(
            "Session timeout reached. Write a concise summary of what you've done and any "
            "important context to carry forward. This will be saved as your memory log."
        )
        auto_save_output(output)
    except Exception:
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

            logfire.info("reply: {reply}", reply=output.strip()[:300])

            try:
                auto_save_output(output)
            except Exception:
                logfire.exception("memory auto-save error")

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
