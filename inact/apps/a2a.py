"""
A2A (Agent-to-Agent) client for inact.

Implements the synchronous `message/send` JSON-RPC call defined in the
A2A protocol specification.  The agent is discovered via its card at
`/.well-known/agent.json`; the RPC endpoint is taken from `card["url"]`.
"""

from __future__ import annotations

import threading
import uuid

import httpx


class A2AClient:
    """HTTP client for a remote A2A-compatible agent."""

    def __init__(self, agent_url: str):
        self.agent_url = agent_url.rstrip("/")
        self._card: dict | None = None
        self._rpc_url: str | None = None
        self._lock = threading.Lock()
        self._id = 0

    # ------------------------------------------------------------------
    # Card discovery
    # ------------------------------------------------------------------

    def ensure_card(self) -> dict:
        if self._card is not None:
            return self._card
        with self._lock:
            if self._card is not None:
                return self._card
            resp = httpx.get(
                f"{self.agent_url}/.well-known/agent.json",
                timeout=10,
                follow_redirects=True,
            )
            resp.raise_for_status()
            self._card = resp.json()
            self._rpc_url = self._card.get("url") or self.agent_url
            return self._card

    def card(self) -> dict:
        return self.ensure_card()

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def send(self, message: str, context_id: str) -> tuple[str, str]:
        """
        Send *message* to the agent and return (reply_text, context_id).

        Uses the synchronous ``message/send`` JSON-RPC method.
        Raises ``RuntimeError`` on transport or protocol errors.
        """
        try:
            self.ensure_card()
        except Exception:
            self._rpc_url = self.agent_url  # best-effort fallback

        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": message}],
                    "messageId": str(uuid.uuid4()),
                    "contextId": context_id,
                }
            },
        }
        rpc_url = self._rpc_url or self.agent_url
        resp = httpx.post(
            rpc_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise RuntimeError(f"A2A error {err.get('code', '')}: {err.get('message', err)}")
        return _extract_reply(data.get("result", {})), context_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_reply(result: dict) -> str:
    """Return the concatenated text parts from a task result."""
    state = result.get("status", {}).get("state", "")
    if state == "failed":
        raise RuntimeError("agent task failed")
    if state in ("submitted", "working"):
        raise RuntimeError(f"unexpected async task state: {state!r}")

    parts: list[str] = []

    # Primary source: artifacts
    for artifact in result.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "text":
                text = part.get("text", "")
                if text:
                    parts.append(text)

    # Fallback: history messages from the agent
    if not parts:
        for msg in result.get("history", []):
            if msg.get("role") in ("agent", "assistant"):
                for part in msg.get("parts", []):
                    if part.get("kind") == "text":
                        text = part.get("text", "")
                        if text:
                            parts.append(text)

    prefix = "[input_required] " if state == "input_required" else ""
    return prefix + "\n".join(parts)


def _strip_none(obj):
    """Recursively remove None values so tomli_w can serialise the result."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(i) for i in obj if i is not None]
    return obj


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_a2a(inact_app, prefix: str, client: A2AClient, label: str) -> None:
    import json
    from flask import request
    from ..pages import dict_to_toml
    from ..utils import text_response, toml_str

    prefix = "/" + prefix.strip("/")
    ep = "_inact_a2a_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    _history: dict[str, list[dict]] = {}

    def _help():
        try:
            card = client.card()
        except Exception:
            card = {}
        name = card.get("name", label)
        desc = card.get("description", "")
        skills = card.get("skills", [])
        lines = [f"# {name}\n"]
        if desc:
            lines.append(f"\n{desc}\n")
        lines.append("\n## Endpoints\n\n")
        lines.append(f"  GET  {prefix}/              this page\n")
        lines.append(f"  GET  {prefix}/.card         agent card (TOML)\n")
        lines.append(f"  GET  {prefix}/chat          list conversations\n")
        lines.append(f"  GET  {prefix}/chat?context_id=<uuid>  read a conversation\n")
        lines.append(f"  POST {prefix}/chat          send a message\n")
        lines.append("\n## Sending a message\n\n")
        lines.append(f"  POST {prefix}/chat\n")
        lines.append(f'  Body: {{"message": "hello", "context_id": "<optional>"}}\n\n')
        lines.append("The response includes a # context_id comment. Pass it back\n")
        lines.append("in subsequent requests to continue the same conversation.\n")
        if skills:
            lines.append("\n## Skills\n\n")
            for s in skills:
                lines.append(f"  {s.get('name', s.get('id', ''))}")
                if s.get("description"):
                    lines.append(f" — {s['description']}")
                lines.append("\n")
        return text_response("".join(lines))

    def _card():
        try:
            raw = client.card()
        except Exception as exc:
            return text_response(f"ERROR 502: {exc}\n", 502)
        safe = _strip_none(raw)
        try:
            body = f"# Agent card: {label}\n\n" + dict_to_toml(safe)
        except Exception:
            body = f"# Agent card: {label}\n\n" + json.dumps(safe, indent=2)
        return text_response(body)

    def _chat():
        if request.method == "GET":
            context_id = request.args.get("context_id", "").strip()
            if context_id:
                msgs = _history.get(context_id, [])
                lines = [f"# Conversation: {context_id}\n",
                         f"# {len(msgs)} message(s)\n\n"]
                for m in msgs:
                    lines.append(f"[{m['role']}] {m['text']}\n\n")
                return text_response("".join(lines))
            lines = [f"# Conversations at {prefix}/chat\n",
                     f"# {len(_history)} conversation(s)\n\n"]
            for ctx_id, msgs in _history.items():
                lines.append("[[conversations]]\n")
                lines.append(f"context_id = {toml_str(ctx_id)}\n")
                lines.append(f"messages   = {len(msgs)}\n")
                lines.append(f"url        = {toml_str(f'{prefix}/chat?context_id={ctx_id}')}\n\n")
            return text_response("".join(lines))

        body = request.get_json(force=True, silent=True) or {}
        message = (body.get("message") or "").strip()
        if not message:
            return text_response(
                "ERROR 400: 'message' field required\n"
                f"Usage: POST {prefix}/chat\n"
                f'Body: {{"message": "your text", "context_id": "<optional-uuid>"}}\n',
                400,
            )
        context_id = (body.get("context_id") or body.get("contextId") or "").strip()
        if not context_id:
            context_id = str(uuid.uuid4())

        _history.setdefault(context_id, []).append({"role": "user", "text": message})
        try:
            reply, context_id = client.send(message, context_id)
        except Exception as exc:
            _history[context_id].pop()
            return text_response(f"ERROR 502: {exc}\n", 502)

        _history[context_id].append({"role": "agent", "text": reply})
        status = 202 if reply.startswith("[input_required]") else 200
        return text_response(f"# context_id: {context_id}\n\n{reply}\n", status)

    flask_app.add_url_rule(prefix + "/",      endpoint=ep + "_help", view_func=_help)
    flask_app.add_url_rule(prefix + "/.card", endpoint=ep + "_card", view_func=_card)
    flask_app.add_url_rule(
        prefix + "/chat", endpoint=ep + "_chat",
        view_func=_chat, methods=["GET", "POST"])


def mount_a2a(inact_app, prefix: str, agent_url: str) -> None:
    """
    Mount a remote A2A agent at *prefix*.

    The agent card is fetched lazily from ``{agent_url}/.well-known/agent.json``.
    ``/chat`` (POST) sends a message; pass the returned ``context_id`` back to
    continue the conversation.

    Example::

        app.mount_a2a("/assistant", "https://agent.example.com")
    """
    p = "/" + prefix.strip("/")
    attach_a2a(inact_app, prefix, A2AClient(agent_url), label=agent_url)
    inact_app._app_mounts.append((p, (
        f"\nA2A agent: {p}  ({agent_url})\n"
        f"  GET  {p}/              info & overview\n"
        f"  GET  {p}/.card         agent card (TOML)\n"
        f"  GET  {p}/chat          list conversations\n"
        f'  POST {p}/chat          send  body: {{"message":"...","context_id":"<opt>"}}\n'
    )))
