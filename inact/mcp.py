"""
MCP clients for inact.

Two transports are supported:

  McpClient       — Streamable HTTP (POST-based, spec 2024-11-05)
  StdioMcpClient  — Stdio (subprocess, newline-delimited JSON-RPC)
                    Used by servers launched with npx or uvx.

Both expose the same four methods: list_tools, call_tool,
list_resources, read_resource.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import queue
import subprocess
import threading

import httpx

_MCP_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

_http_id_counter = itertools.count(1)


class McpClient:
    """Stateful client for a URL-based MCP server (Streamable HTTP transport)."""

    def __init__(self, url: str):
        self.url = url
        self._session_id: str | None = None
        self._initialized = False
        self._lock = threading.Lock()

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _send(self, payload: dict) -> dict | None:
        resp = httpx.post(self.url, json=payload, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        if sid := resp.headers.get("Mcp-Session-Id"):
            self._session_id = sid
        # Notifications get 202 with no body; also guard against empty 200s.
        if resp.status_code == 202 or not resp.content.strip():
            return None
        return resp.json()

    def _request(self, method: str, params: dict | None = None) -> dict:
        data = self._send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": next(_http_id_counter),
        })
        if data is None:
            return {}
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']['message']}")
        return data.get("result", {})

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._request("initialize", {
                "protocolVersion": _MCP_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "inact", "version": "0.1.0"},
            })
            self._notify("notifications/initialized")
            self._initialized = True

    def list_tools(self) -> list[dict]:
        self.ensure_initialized()
        return self._request("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> list[dict]:
        self.ensure_initialized()
        return self._request("tools/call", {"name": name, "arguments": arguments}).get("content", [])

    def list_resources(self) -> list[dict]:
        self.ensure_initialized()
        return self._request("resources/list").get("resources", [])

    def read_resource(self, uri: str) -> list[dict]:
        self.ensure_initialized()
        return self._request("resources/read", {"uri": uri}).get("contents", [])


# ---------------------------------------------------------------------------
# Stdio transport
# ---------------------------------------------------------------------------

class StdioMcpClient:
    """
    Client for an MCP server launched as a subprocess (stdio transport).

    The server communicates via newline-delimited JSON-RPC 2.0 on
    stdin/stdout.  stderr is inherited so server logs reach the terminal.

    Typical usage via the Inact helpers::

        app.mount_mcp_npx("/fs", "@modelcontextprotocol/server-filesystem",
                          args=["--allowed-paths", "/tmp"])
        app.mount_mcp_uvx("/git", "mcp-server-git")

    Or directly::

        client = StdioMcpClient("uvx", ["mcp-server-git"])
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.command = command
        self.args = args or []
        self.env = env  # merged on top of os.environ at start time
        self._process: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._pending: dict[int, queue.Queue] = {}
        self._write_lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._id_counter = itertools.count(1)

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    def _start(self) -> None:
        merged_env = os.environ.copy()
        if self.env:
            merged_env.update(self.env)
        self._process = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,          # inherit — server logs go to terminal
            env=merged_env,
            encoding="utf-8",
            bufsize=1,            # line-buffered text mode
        )
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name=f"mcp-reader-{self.command}"
        )
        self._reader_thread.start()
        atexit.register(self._shutdown)

    def _shutdown(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()

    # ------------------------------------------------------------------
    # Reader loop (runs in background thread)
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        while True:
            line = self._process.stdout.readline()
            if not line:
                # Process exited; unblock all waiting requests with an error.
                err_msg = f"{self.command} process exited unexpectedly"
                for q in list(self._pending.values()):
                    q.put({"error": {"message": err_msg}})
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._pending:
                self._pending[msg_id].put(msg)
            # Notifications from server (no id) are silently ignored.

    # ------------------------------------------------------------------
    # JSON-RPC helpers
    # ------------------------------------------------------------------

    def _write(self, payload: dict) -> None:
        assert self._process and self._process.stdin
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._write_lock:
            self._process.stdin.write(line)
            self._process.stdin.flush()

    def _request(self, method: str, params: dict | None = None) -> dict:
        msg_id = next(self._id_counter)
        q: queue.Queue = queue.Queue()
        self._pending[msg_id] = q
        try:
            self._write({
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
                "id": msg_id,
            })
            try:
                msg = q.get(timeout=60)
            except queue.Empty:
                raise TimeoutError(f"MCP request timed out: {method}")
            if "error" in msg:
                raise RuntimeError(f"MCP error: {msg['error']['message']}")
            return msg.get("result", {})
        finally:
            self._pending.pop(msg_id, None)

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._start()
            self._request("initialize", {
                "protocolVersion": _MCP_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "inact", "version": "0.1.0"},
            })
            self._notify("notifications/initialized")
            self._initialized = True

    # ------------------------------------------------------------------
    # MCP operations
    # ------------------------------------------------------------------

    def list_tools(self) -> list[dict]:
        self.ensure_initialized()
        return self._request("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> list[dict]:
        self.ensure_initialized()
        return self._request("tools/call", {"name": name, "arguments": arguments}).get("content", [])

    def list_resources(self) -> list[dict]:
        self.ensure_initialized()
        return self._request("resources/list").get("resources", [])

    def read_resource(self, uri: str) -> list[dict]:
        self.ensure_initialized()
        return self._request("resources/read", {"uri": uri}).get("contents", [])
