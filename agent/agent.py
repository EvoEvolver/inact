"""
Workspace-agnostic AI agent powered by PydanticAI + OpenRouter.

Memory is managed by the server and accessed via HTTP endpoints:
    GET  {AGENT_SERVER_URL}/memory       — load recent memory into system prompt
    POST {AGENT_SERVER_URL}/memory       — save a memory entry (plain-text body)

Public API used by server.py:
    run_llm(user_msg) -> str
    reset_session()
    AGENT_ID, AGENT_KEY, WORKSPACE_HOST, MODEL, SESSION_TIMEOUT, _session_start
    resolve_agent_id(), _headers()
"""

import os
import re
import subprocess
import time

import logfire
import requests as http
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


def _scrubbing_callback(m: logfire.ScrubMatch):
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


logfire.configure(scrubbing=logfire.ScrubbingOptions(callback=_scrubbing_callback))
logfire.instrument_pydantic_ai()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WORKSPACE_HOST    = os.environ.get("WORKSPACE_HOST",    "http://localhost:5050").rstrip("/")
AGENT_KEY         = os.environ.get("AGENT_KEY",         "")
AGENT_ID          = os.environ.get("AGENT_ID",          "")
AGENT_ME_PATH     = os.environ.get("AGENT_ME_PATH",     "/agents/.me")
AGENT_SERVER_URL  = os.environ.get("AGENT_SERVER_URL",  "http://localhost:7779")
MODEL             = os.environ.get("MODEL",              "moonshotai/kimi-k2.6")
SESSION_TIMEOUT   = int(os.environ.get("SESSION_TIMEOUT", "600"))


def _headers(extra: dict | None = None) -> dict:
    h = {}
    if AGENT_ID:
        h["X-Agent-Id"] = AGENT_ID
    if AGENT_KEY:
        h["X-Api-Key"] = AGENT_KEY
    if extra:
        h.update(extra)
    return h


def _fetch_workspace_index(max_chars: int = 1800) -> str:
    try:
        resp = http.get(f"{WORKSPACE_HOST}/", headers=_headers(), timeout=3)
        return (resp.text or "").strip()[:max_chars]
    except Exception:
        return ""


def resolve_agent_id() -> None:
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
# Memory access (via server endpoints)
# ---------------------------------------------------------------------------

def _fetch_memory() -> str:
    """Load recent memory from the server's memory endpoint."""
    try:
        resp = http.get(f"{AGENT_SERVER_URL}/memory", timeout=3)
        return resp.text.strip() if resp.ok else ""
    except Exception:
        return ""


def _save_memory(output: str) -> None:
    """Persist output to memory via the server's memory endpoint."""
    try:
        http.post(
            f"{AGENT_SERVER_URL}/memory",
            data=output.encode(),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            timeout=10,
        )
    except Exception as exc:
        logfire.warning("could not save memory: {exc}", exc=exc)


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
    memory = _fetch_memory()
    home   = _fetch_workspace_index()
    lines = [
        f"You are AI agent #{AGENT_ID} connected to a workspace at {WORKSPACE_HOST}.",
        "Use curl_workspace to interact with the workspace API.",
        "Use bash for local shell tasks.",
        "",
        "## Workspace Home",
        (home or "(workspace index unavailable)").strip(),
        "",
        "## Acting on notifications",
        "Every notification contains the context and exact API calls you need — read it and act.",
        "from_kind tells you who sent it: 'human' = real person, 'agent' = another bot.",
        "",
        "On a revival tick:",
        "  1. GET /notify/inbox   — act on every unread notification",
        "",
        "## Context search (recommended)",
        "Before acting, consider a quick grep for relevant keywords in local folders.",
        "  bash(\"grep -Rin \\\"<keyword>\\\" . | head -n 50\")",
    ]
    if memory:
        lines += [
            "",
            "## Your long-term memory",
            "(5 most recent entries shown; full index on disk)",
            memory,
        ]
    lines += [
        "",
        "## Instructions",
        "You MUST take action on every unread notification and pending notification — no exceptions.",
        "Do NOT skip, defer, or summarize notifications without acting on them.",
        "For every unread item: read it, respond or join as appropriate, then move on to the next.",
        "Your complete final response is automatically saved as a dated memory log.",
        "Do NOT output any special MEMORY block — just respond naturally.",
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
def run_codex_agent(task: str, working_dir: str = ".") -> str:
    """Delegate a hard programming task to the Codex AI coding subagent.

    Codex autonomously reads and writes files and runs shell commands to
    implement the task end-to-end.  Use it for complex multi-file features,
    large refactors, or anything that requires iterative code editing.

    working_dir: directory to scope the work (default: current directory).
    Requires OPENAI_API_KEY in the environment; optionally set OPENAI_BASE_URL
    to route through OpenRouter or another provider.
    """
    try:
        result = subprocess.run(
            ["codex", "--approval-mode", "full-auto", "-q", task],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=working_dir,
        )
        out = result.stdout
        if result.stderr:
            out += f"\nSTDERR:\n{result.stderr}"
        return out or "(no output)"
    except FileNotFoundError:
        return "ERROR: codex CLI not found; install with: npm install -g @openai/codex"
    except subprocess.TimeoutExpired:
        return "ERROR: codex timed out after 5 minutes"
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


def run_llm(user_msg: str) -> str:
    global _history
    result = _agent.run_sync(user_msg, message_history=_history)
    _history = list(result.all_messages())
    return result.output


def reset_session() -> None:
    global _history, _session_start
    logfire.info("session timeout — resetting conversation")
    try:
        output = run_llm(
            "Session timeout reached. Write a concise summary of what you've done and any "
            "important context to carry forward. This will be saved as your memory log."
        )
        _save_memory(output)
    except Exception:
        logfire.exception("consolidation error")
    _history = []
    _session_start = time.time()
    logfire.info("conversation reset")
