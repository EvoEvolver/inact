"""
Minimal A2A chatbot server.

Always replies "Hello. This is a hard problem" — after a 20-second delay
to simulate a slow-thinking agent.

Run this before the main example app:

    python example/chatbot.py        # listens on :5001

Then exercise it through the inact proxy:

    curl -s http://localhost:5000/chatbot/.card
    curl -s -X POST http://localhost:5000/chatbot/chat \
         -H "Content-Type: application/json" \
         -d '{"message": "what is 2+2?"}'
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import itertools
import threading
import time

from flask import Flask, jsonify, request

_id_lock = threading.Lock()
_id_counter = itertools.count(1)


def _next_id() -> str:
    with _id_lock:
        return str(next(_id_counter))

PORT = 5001
BASE_URL = f"http://localhost:{PORT}"

app = Flask(__name__)

AGENT_CARD = {
    "name": "Slow Chatbot",
    "description": "A deeply thoughtful agent. Always replies after 20 seconds.",
    "url": BASE_URL + "/",
    "version": "1.0.0",
    "capabilities": {},
    "skills": [
        {
            "id": "chat",
            "name": "Chat",
            "description": "Responds to any message after careful consideration.",
            "tags": ["chat"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
        }
    ],
}


@app.get("/.well-known/agent.json")
def agent_card():
    return jsonify(AGENT_CARD)


@app.post("/")
def rpc():
    body = request.get_json(force=True, silent=True) or {}
    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    if method == "message/send":
        msg = params.get("message", {})
        context_id = msg.get("contextId") or _next_id()

        time.sleep(20)

        return jsonify({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "id": _next_id(),
                "contextId": context_id,
                "status": {"state": "completed"},
                "artifacts": [
                    {
                        "artifactId": _next_id(),
                        "parts": [{"kind": "text", "text": "Hello. This is a hard problem"}],
                    }
                ],
            },
        })

    return jsonify({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }), 404


if __name__ == "__main__":
    print(f"Slow Chatbot A2A server → {BASE_URL}")
    print(f"  card : {BASE_URL}/.well-known/agent.json")
    print(f"  rpc  : POST {BASE_URL}/")
    app.run(port=PORT)
