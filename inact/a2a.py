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
