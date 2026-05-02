"""
Agent server — manages waking the agent, providing memory context, and
organising memory storage.

Endpoints:
    POST /wake              receive push notifications → queues agent wake
    GET  /health            liveness check
    GET  /memory            load recent memory (used by the agent's system prompt)
    POST /memory            save a memory entry (used by the agent after each run)

Usage:
    OPENROUTER_API_KEY=sk-... python agent/server.py
"""

import argparse
import json
import os
import queue
import re
import threading
import time

import logfire
import requests as http
from flask import Flask, Response, jsonify, request as freq

import agent as ag
import memory as mem


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_railway_domain = os.environ.get("RAILWAY_PRIVATE_DOMAIN", "")
CALLBACK_URL    = os.environ.get("CALLBACK_URL", f"https://{_railway_domain}/wake" if _railway_domain else "")
PORT            = int(os.environ.get("PORT",           "7779"))
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL", "60"))
NOTIFY_REGISTER = os.environ.get("NOTIFY_REGISTER_PATH", "/notify/register")
NOTIFY_INBOX    = os.environ.get("NOTIFY_INBOX_PATH",    "/notify/inbox")


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def _get_unread_notification_ids() -> list[str]:
    try:
        resp = http.get(
            f"{ag.WORKSPACE_HOST}{NOTIFY_INBOX}",
            headers=ag._headers(),
            params={"unread": "1"},
            timeout=5,
        )
        return re.findall(r'id\s*=\s*"([^"]+)"', resp.text)
    except Exception as exc:
        logfire.warning("could not fetch unread notifications: {exc}", exc=exc)
        return []


def _mark_notifications_read(ids: list[str]) -> None:
    for nid in ids:
        try:
            http.get(
                f"{ag.WORKSPACE_HOST}{NOTIFY_INBOX}/{nid}",
                headers=ag._headers(),
                timeout=5,
            )
        except Exception as exc:
            logfire.warning("could not mark notification {nid} as read: {exc}", nid=nid, exc=exc)
    if ids:
        logfire.info("marked {n} notification(s) as read", n=len(ids))


# ---------------------------------------------------------------------------
# Notification queue + agent loop
# ---------------------------------------------------------------------------

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

            if ag.SESSION_TIMEOUT > 0 and time.time() - ag._session_start > ag.SESSION_TIMEOUT:
                ag.reset_session()

            parts = []
            for p in batch:
                if p is None:
                    parts.append("Revival tick: check for any unread messages and reply to them.")
                else:
                    parts.append(f"Notification received:\n{json.dumps(p, indent=2)}")

            user_msg = "\n\n".join(parts)
            logfire.info("trigger: {msg}", msg=user_msg[:200])

            unread_before = _get_unread_notification_ids()

            try:
                output = ag.run_llm(user_msg)
            except Exception:
                logfire.exception("LLM error")
                continue

            logfire.info("reply: {reply}", reply=output.strip()[:300])

            try:
                mem.auto_save_output(output)
            except Exception:
                logfire.exception("memory auto-save error")

            _mark_notifications_read(unread_before)
        finally:
            _agent_busy.clear()


# ---------------------------------------------------------------------------
# Flask server
# ---------------------------------------------------------------------------

app = Flask("reply-agent")


@app.route("/wake", methods=["POST"])
def wake():
    payload = freq.get_json(force=True, silent=True) or {}
    logfire.info("wake: {type} — queued", type=payload.get("type", "notification"))
    _notification_queue.put(payload)
    return jsonify({"status": "ok"})


@app.route("/health")
def health():
    return jsonify({
        "status": "alive",
        "agent_id": ag.AGENT_ID,
        "workspace": ag.WORKSPACE_HOST,
    })


@app.route("/memory", methods=["GET"])
def get_memory():
    limit = freq.args.get("limit", 5, type=int)
    content = mem.load_memory(limit=limit)
    return Response(content, mimetype="text/plain")


@app.route("/memory", methods=["POST"])
def save_memory():
    content = freq.get_data(as_text=True)
    if not content:
        body = freq.get_json(silent=True) or {}
        content = body.get("content", "")
    if not content:
        return jsonify({"error": "empty body"}), 400
    mem.auto_save_output(content)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global CALLBACK_URL, PORT

    parser = argparse.ArgumentParser(description="Inact agent server")
    parser.add_argument("--workspace",  default=None, help="override WORKSPACE_HOST")
    parser.add_argument("--agent-id",   default=None, help="agent id (resolved from key if omitted)")
    parser.add_argument("--agent-key",  default=None, help="override AGENT_KEY")
    parser.add_argument("--port",       default=None, type=int)
    parser.add_argument("--callback",   default=None)
    args = parser.parse_args()

    if args.workspace:  ag.WORKSPACE_HOST = args.workspace.rstrip("/")
    if args.agent_id:   ag.AGENT_ID       = args.agent_id
    if args.agent_key:  ag.AGENT_KEY      = args.agent_key
    if args.port:       PORT              = args.port
    if args.callback:   CALLBACK_URL      = args.callback

    ag.resolve_agent_id()

    callback_url = CALLBACK_URL or f"http://localhost:{PORT}/wake"

    if NOTIFY_REGISTER:
        try:
            r = http.post(
                f"{ag.WORKSPACE_HOST}{NOTIFY_REGISTER}",
                json={"agent_id": ag.AGENT_ID, "callback": callback_url},
                headers=ag._headers({"Content-Type": "application/json"}),
                timeout=5,
            )
            logfire.info("registered callback: {resp}", resp=r.text.strip())
        except Exception as exc:
            logfire.warning("could not register callback: {exc}", exc=exc)

    # Start Flask in a background thread so we can wait for it to be ready
    # before queuing the first revival tick.  The agent calls back to /memory
    # in its system prompt, so Flask must be listening first.
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True,
    )
    flask_thread.start()

    # Poll until Flask is accepting connections (up to 10 s)
    for _ in range(100):
        try:
            http.get(f"http://localhost:{PORT}/health", timeout=0.2)
            break
        except Exception:
            time.sleep(0.1)

    logfire.info("callback server on :{port}", port=PORT)

    ag._session_start = time.time()
    threading.Thread(target=_agent_loop, daemon=True).start()

    if POLL_INTERVAL > 0:
        def _poll_loop() -> None:
            while True:
                time.sleep(POLL_INTERVAL)
                if _agent_busy.is_set():
                    continue
                try:
                    resp = http.get(
                        f"{ag.WORKSPACE_HOST}{NOTIFY_INBOX}",
                        headers=ag._headers(),
                        params={"unread": "1"},
                        timeout=5,
                    )
                    has_unread = "[[notifications]]" in resp.text
                except Exception:
                    has_unread = True
                if has_unread:
                    logfire.info("poll — unread notifications found, queuing check")
                    _notification_queue.put(None)

        threading.Thread(target=_poll_loop, daemon=True).start()

    logfire.info("checking for pending messages...")
    _notification_queue.put(None)

    logfire.info(
        "agent #{agent_id} ready — workspace={workspace} callback={callback} model={model}",
        agent_id=ag.AGENT_ID,
        workspace=ag.WORKSPACE_HOST,
        callback=callback_url,
        model=ag.MODEL,
    )
    flask_thread.join()


if __name__ == "__main__":
    main()
