"""
Agent registry — auto-incrementing integer IDs and API keys for agents.

mount_register(prefix, storage) registers:

  POST   {prefix}/                register a new agent
                                  body: {"name": "optional", "email": "optional"}
                                  returns: id, api_key, url
  GET    {prefix}/                list all agents (paginated)
                                  ?page=1&per_page=20
  GET    {prefix}/{id}            agent public profile (id, name, email, created_at)
  POST   {prefix}/{id}/.email     set / update email address
                                  requires X-Api-Key header
                                  body: {"email": "agent@example.com"}
  DELETE {prefix}/{id}            deregister agent
                                  requires X-Api-Key header

*storage* accepts a :class:`~inact.storage.Storage` object or any URL/path
accepted by :func:`~inact.storage.make_storage`.
"""

from __future__ import annotations

import secrets
import time

from flask import request

from ..storage import Storage
from ..utils import text_response, html_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS agents (
        id         INTEGER PRIMARY KEY,
        api_key    TEXT    NOT NULL UNIQUE,
        name       TEXT    NOT NULL DEFAULT '',
        email      TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL
    )""",
]

# Run on existing tables that predate the email column.
_MIGRATIONS = [
    "ALTER TABLE agents ADD COLUMN email TEXT NOT NULL DEFAULT ''",
]

_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100


def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_page_params() -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(_MAX_PER_PAGE, max(1, int(request.args.get("per_page", _DEFAULT_PER_PAGE))))
    except (ValueError, TypeError):
        per_page = _DEFAULT_PER_PAGE
    return page, per_page


def _page_header(page: int, per_page: int, total: int) -> str:
    total_pages = max(1, (total + per_page - 1) // per_page)
    lines = [f"# page {page} of {total_pages} ({total} total)\n"]
    if page > 1:
        lines.append(f"# ?page={page - 1}&per_page={per_page} for prev\n")
    if page < total_pages:
        lines.append(f"# ?page={page + 1}&per_page={per_page} for next\n")
    return "".join(lines)


class AgentRegistry:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)
        for sql in _MIGRATIONS:
            try:
                self._s.execute(sql)
            except Exception:
                pass  # column already exists

    def register(self, name: str = "", email: str = "") -> tuple[int, str]:
        api_key = secrets.token_urlsafe(32)
        ts = int(time.time())
        self._s.execute(
            "INSERT INTO agents (id, api_key, name, email, created_at) "
            "SELECT COALESCE(MAX(id), 0) + 1, ?, ?, ?, ? FROM agents",
            (api_key, name, email, ts),
        )
        row = self._s.fetchone("SELECT id FROM agents WHERE api_key = ?", (api_key,))
        return row["id"], api_key

    def count(self) -> int:
        row = self._s.fetchone("SELECT COUNT(*) AS cnt FROM agents")
        return row["cnt"] if row else 0

    def list_agents(self, page: int = 1, per_page: int = _DEFAULT_PER_PAGE) -> list[dict]:
        offset = (page - 1) * per_page
        return self._s.fetchall(
            "SELECT id, name, email, created_at FROM agents ORDER BY id ASC LIMIT ? OFFSET ?",
            (per_page, offset),
        )

    def get(self, agent_id: int) -> dict | None:
        return self._s.fetchone(
            "SELECT id, name, email, created_at FROM agents WHERE id = ?", (agent_id,)
        )

    def get_by_key(self, api_key: str) -> dict | None:
        """Look up a full agent record by API key (for auth in other apps)."""
        return self._s.fetchone(
            "SELECT id, name, email, created_at FROM agents WHERE api_key = ?", (api_key,)
        )

    def set_email(self, agent_id: int, api_key: str, email: str) -> bool:
        return self._s.execute(
            "UPDATE agents SET email = ? WHERE id = ? AND api_key = ?",
            (email, agent_id, api_key),
        ) > 0

    def delete(self, agent_id: int, api_key: str) -> bool:
        return self._s.execute(
            "DELETE FROM agents WHERE id = ? AND api_key = ?", (agent_id, api_key)
        ) > 0


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_register(inact_app, prefix: str, registry: AgentRegistry) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_register_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _root():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            name  = (body.get("name")  or "").strip()
            email = (body.get("email") or "").strip()
            agent_id, api_key = registry.register(name, email)
            lines = [
                f"OK\n",
                f"id      = {agent_id}\n",
                f"api_key = {toml_str(api_key)}\n",
                f"url     = {toml_str(prefix + '/' + str(agent_id))}\n",
            ]
            if email:
                lines.append(f"email   = {toml_str(email)}\n")
            return text_response("".join(lines))
        page, per_page = _parse_page_params()
        total = registry.count()
        agents = registry.list_agents(page, per_page)
        lines = ["# Agents\n", _page_header(page, per_page, total), "\n"]
        for a in agents:
            lines.append("[[agents]]\n")
            lines.append(f"id         = {a['id']}\n")
            if a["name"]:
                lines.append(f"name       = {toml_str(a['name'])}\n")
            if a["email"]:
                lines.append(f"email      = {toml_str(a['email'])}\n")
            lines.append(f"created_at = {toml_str(_fmt_ts(a['created_at']))}\n")
            lines.append(f"url        = {toml_str(prefix + '/' + str(a['id']))}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _agent(agent_id: str):
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: agent id must be an integer\n", 400)
        if request.method == "DELETE":
            api_key = request.headers.get("X-Api-Key", "").strip()
            if not api_key:
                return text_response(
                    "ERROR 401: X-Api-Key header required\n"
                    f"Usage: DELETE {prefix}/{{id}}\n"
                    "  Header: X-Api-Key: <your api_key>\n",
                    401,
                )
            ok = registry.delete(aid, api_key)
            if not ok:
                return text_response(
                    "ERROR 404: agent not found or api_key mismatch\n", 404
                )
            return text_response("OK\n")
        agent = registry.get(aid)
        if not agent:
            return text_response("ERROR 404: agent not found\n", 404)
        display = agent["name"] or f"agent {aid}"
        lines = [
            f"# {display}\n\n",
            f"id         = {agent['id']}\n",
        ]
        if agent["name"]:
            lines.append(f"name       = {toml_str(agent['name'])}\n")
        if agent["email"]:
            lines.append(f"email      = {toml_str(agent['email'])}\n")
        lines.append(f"created_at = {toml_str(_fmt_ts(agent['created_at']))}\n")
        lines.append(f"url        = {toml_str(prefix + '/' + str(agent['id']))}\n")
        if not agent["email"]:
            lines.append(f"\n# no email set — POST {prefix}/{aid}/.email to configure\n")
        return text_response("".join(lines))

    def _set_email(agent_id: str):
        try:
            aid = int(agent_id)
        except ValueError:
            return text_response("ERROR 400: agent id must be an integer\n", 400)
        api_key = request.headers.get("X-Api-Key", "").strip()
        if not api_key:
            return text_response(
                "ERROR 401: X-Api-Key header required\n"
                f"Usage: POST {prefix}/{{id}}/.email\n"
                "  Header: X-Api-Key: <your api_key>\n"
                '  Body:   {"email": "you@example.com"}\n',
                401,
            )
        body = request.get_json(force=True, silent=True) or {}
        email = (body.get("email") or "").strip()
        if not email:
            return text_response("ERROR 400: 'email' required\n", 400)
        ok = registry.set_email(aid, api_key, email)
        if not ok:
            return text_response("ERROR 404: agent not found or api_key mismatch\n", 404)
        return text_response(f"OK\nemail = {toml_str(email)}\n")

    def _human():
        p = prefix
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Register</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#f5f5f5;color:#222;min-height:100vh;display:flex;align-items:flex-start;justify-content:center;padding:40px 16px}}
.card{{background:#fff;border-radius:12px;padding:36px;width:100%;max-width:420px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
h1{{font-size:20px;margin-bottom:6px}}
p{{color:#666;font-size:14px;margin-bottom:20px}}
input{{width:100%;border:1px solid #ddd;border-radius:8px;padding:10px 12px;font-size:14px;font-family:inherit}}
input:focus{{outline:none;border-color:#0066cc}}
button{{background:#0066cc;color:#fff;border:none;border-radius:8px;padding:10px 18px;cursor:pointer;font-size:14px;white-space:nowrap}}
button:hover{{background:#0052a3}}
.row{{display:flex;gap:8px;margin-top:12px}}
.row input{{flex:1}}
.badge{{background:#e8f0fe;color:#1a56db;border-radius:8px;padding:12px 14px;font-size:13px;margin:16px 0;word-break:break-all;line-height:1.6}}
.link{{display:inline-block;margin-top:12px;color:#0066cc;font-size:14px;text-decoration:none;font-weight:500}}
.err{{color:#cc0000;font-size:13px;margin-top:8px}}
hr{{border:none;border-top:1px solid #eee;margin:24px 0}}
.agents{{margin-top:8px}}
.agent-row{{padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:14px;display:flex;justify-content:space-between;align-items:center}}
.agent-row:last-child{{border:none}}
.agent-id{{color:#888;font-size:12px}}
</style>
</head>
<body>
<div class="card">
  <h1>Inact</h1>
  <p>Register to get an agent ID and API key.</p>

  <div id="logged" hidden>
    <div class="badge">
      Signed in as <strong id="li-name"></strong> &nbsp;·&nbsp; Agent&nbsp;#<span id="li-id"></span>
    </div>
    <a id="chat-link" href="#" class="link">Open chat →</a>
    <br><a href="#" id="logout" style="font-size:12px;color:#aaa;margin-top:8px;display:inline-block">Sign out</a>
  </div>

  <div id="form-section">
    <div class="row">
      <input id="name" placeholder="Your name (optional)" autocomplete="off">
      <button id="reg-btn">Register</button>
    </div>
    <div id="err" class="err" hidden></div>
  </div>

  <div id="ok" hidden>
    <div class="badge">
      ✓ Registered as agent&nbsp;#<strong id="new-id"></strong><br>
      <span id="new-key" style="font-size:12px"></span>
    </div>
    <a id="ok-chat-link" href="#" class="link">Start chatting →</a>
  </div>

  <hr>
  <strong style="font-size:13px">All agents</strong>
  <div class="agents" id="agents-list"><div style="color:#bbb;font-size:13px;padding:8px 0">Loading…</div></div>
</div>

<script>
const PREFIX = {p!r};
function load() {{
  const id = localStorage.getItem('inact_agent_id');
  const key = localStorage.getItem('inact_api_key');
  const nm  = localStorage.getItem('inact_name') || '';
  if (id && key) {{
    document.getElementById('logged').hidden = false;
    document.getElementById('form-section').hidden = true;
    document.getElementById('li-id').textContent = id;
    document.getElementById('li-name').textContent = nm || ('Agent #' + id);
  }}
  loadAgents();
}}
function parseBlocks(text, tag) {{
  return text.split('[[' + tag + ']]').slice(1).map(b => {{
    const o = {{}};
    b.split('\\n').forEach(l => {{ const m = l.match(/^(\\w+)\\s*=\\s*"([^"]*)"/) || l.match(/^(\\w+)\\s*=\\s*(\\S+)/); if(m) o[m[1]]=m[2]; }});
    return o;
  }}).filter(o => o.id);
}}
async function loadAgents() {{
  const text = await fetch(PREFIX + '/').then(r => r.text()).catch(()=>'');
  const agents = parseBlocks(text, 'agents');
  const list = document.getElementById('agents-list');
  list.innerHTML = agents.map(a =>
    `<div class="agent-row"><span>${{a.name || ('Agent #'+a.id)}}</span><span class="agent-id">#${{a.id}}</span></div>`
  ).join('') || '<div style="color:#bbb;font-size:13px;padding:8px 0">No agents yet.</div>';
}}
document.getElementById('reg-btn').addEventListener('click', async () => {{
  const name = document.getElementById('name').value.trim();
  const errEl = document.getElementById('err');
  errEl.hidden = true;
  try {{
    const text = await fetch(PREFIX + '/', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name}})}}).then(r=>r.text());
    const id  = (text.match(/^id\\s*=\\s*(\\d+)/m)||[])[1];
    const key = (text.match(/api_key\\s*=\\s*"([^"]+)"/m)||[])[1];
    if (!id||!key) throw new Error(text);
    localStorage.setItem('inact_agent_id', id);
    localStorage.setItem('inact_api_key',  key);
    localStorage.setItem('inact_name',     name);
    document.getElementById('form-section').hidden = true;
    document.getElementById('ok').hidden = false;
    document.getElementById('new-id').textContent = id;
    document.getElementById('new-key').textContent = key;
    loadAgents();
  }} catch(e) {{ errEl.textContent = String(e); errEl.hidden = false; }}
}});
document.getElementById('name').addEventListener('keydown', e => {{ if(e.key==='Enter') document.getElementById('reg-btn').click(); }});
document.getElementById('logout').addEventListener('click', e => {{
  e.preventDefault();
  ['inact_agent_id','inact_api_key','inact_name'].forEach(k=>localStorage.removeItem(k));
  location.reload();
}});
load();
</script>
</body></html>"""
        return html_response(html)

    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_root", view_func=_root, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/<agent_id>",
        endpoint=ep + "_agent", view_func=_agent, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<agent_id>/.email",
        endpoint=ep + "_email", view_func=_set_email, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/_human",
        endpoint=ep + "_human", view_func=_human)


def mount_register(inact_app, prefix: str, storage) -> None:
    """
    Mount an agent registry at *prefix*.

    Agents POST to register and receive an auto-incrementing integer ID and API key.
    The registry is publicly listable for agent discovery.

    *storage* — a database URL/path or a :class:`~inact.storage.Storage` instance.

    Example::

        app.mount_register("/agents", "./data/agents.db")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    attach_register(inact_app, p, AgentRegistry(backend))
    inact_app._app_mounts.append((p, (
        f"\nAgent registry: {p}\n"
        f'  POST   {p}/             register  body: {{"name":"...","email":"..."}}  → id, api_key\n'
        f"  GET    {p}/             list agents  (?page=1&per_page=20)\n"
        f"  GET    {p}/{{id}}          agent profile (id, name, email, url)\n"
        f"  POST   {p}/{{id}}/.email   set email  (X-Api-Key required)\n"
        f"  DELETE {p}/{{id}}          deregister  (X-Api-Key required)\n"
    )))
