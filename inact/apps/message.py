"""
Agent messaging — send and receive messages between agents.

mount_message(prefix, storage) registers:

  POST   {prefix}/send                   send a message
                                         body: {"from": "1", "to": "2", "body": "..."}
  GET    {prefix}/inbox                  list received messages (paginated)
                                         ?agent_id=<id>  or  X-Agent-Id header
                                         ?page=1&per_page=20  ?unread=1
  GET    {prefix}/inbox/{id}             read message (auto-marks read)
  DELETE {prefix}/inbox/{id}             delete message
  GET    {prefix}/sent                   list sent messages (paginated)
                                         ?agent_id=<id>  or  X-Agent-Id header
                                         ?page=1&per_page=20
  GET    {prefix}/agents                 list known agents (paginated)
                                         agents who have sent at least one message
                                         ?page=1&per_page=20

Agent identity is passed via X-Agent-Id header or ?agent_id= query param.

*storage* accepts a :class:`~inact.storage.Storage` object or any URL/path
accepted by :func:`~inact.storage.make_storage`.
"""

from __future__ import annotations

import time
import uuid

from flask import request

from ..storage import Storage
from ..utils import text_response, html_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS messages (
        id         TEXT    PRIMARY KEY,
        from_id    TEXT    NOT NULL,
        to_id      TEXT    NOT NULL,
        body       TEXT    NOT NULL DEFAULT '',
        read       INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT  NOT NULL
    )""",
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


class MessageStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)

    def send(self, from_id: str, to_id: str, body: str) -> str:
        msg_id = str(uuid.uuid4())
        self._s.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, from_id, to_id, body, 0, int(time.time())),
        )
        return msg_id

    def count_inbox(self, to_id: str, unread_only: bool = False) -> int:
        q = "SELECT COUNT(*) AS cnt FROM messages WHERE to_id = ?"
        params: list = [to_id]
        if unread_only:
            q += " AND read = 0"
        row = self._s.fetchone(q, tuple(params))
        return row["cnt"] if row else 0

    def inbox(self, to_id: str, page: int = 1, per_page: int = _DEFAULT_PER_PAGE,
              unread_only: bool = False) -> list[dict]:
        offset = (page - 1) * per_page
        q = "SELECT * FROM messages WHERE to_id = ?"
        params: list = [to_id]
        if unread_only:
            q += " AND read = 0"
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [per_page, offset]
        return self._s.fetchall(q, tuple(params))

    def count_sent(self, from_id: str) -> int:
        row = self._s.fetchone(
            "SELECT COUNT(*) AS cnt FROM messages WHERE from_id = ?", (from_id,)
        )
        return row["cnt"] if row else 0

    def sent(self, from_id: str, page: int = 1,
             per_page: int = _DEFAULT_PER_PAGE) -> list[dict]:
        offset = (page - 1) * per_page
        return self._s.fetchall(
            "SELECT * FROM messages WHERE from_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (from_id, per_page, offset),
        )

    def get(self, msg_id: str) -> dict | None:
        m = self._s.fetchone("SELECT * FROM messages WHERE id = ?", (msg_id,))
        if m:
            self._s.execute("UPDATE messages SET read = 1 WHERE id = ?", (msg_id,))
        return m

    def delete(self, msg_id: str) -> bool:
        return self._s.execute("DELETE FROM messages WHERE id = ?", (msg_id,)) > 0

    def count_agents(self) -> int:
        row = self._s.fetchone(
            "SELECT COUNT(DISTINCT from_id) AS cnt FROM messages"
        )
        return row["cnt"] if row else 0

    def list_agents(self, page: int = 1,
                    per_page: int = _DEFAULT_PER_PAGE) -> list[dict]:
        offset = (page - 1) * per_page
        return self._s.fetchall(
            "SELECT from_id, COUNT(*) AS sent_count, MAX(created_at) AS last_seen "
            "FROM messages GROUP BY from_id ORDER BY last_seen DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        )


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_message(inact_app, prefix: str, store: MessageStore) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_msg_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _agent_id() -> str:
        return (
            request.args.get("agent_id", "")
            or request.headers.get("X-Agent-Id", "")
        ).strip()

    def _send():
        body = request.get_json(force=True, silent=True) or {}
        from_id = str(body.get("from") or "").strip()
        to_id   = str(body.get("to")   or "").strip()
        text    = (body.get("body")    or "").strip()
        if not from_id:
            return text_response(
                "ERROR 400: 'from' required\n"
                f"POST {prefix}/send\n"
                '  Body: {"from": "1", "to": "2", "body": "..."}\n',
                400,
            )
        if not to_id:
            return text_response("ERROR 400: 'to' required\n", 400)
        if not text:
            return text_response("ERROR 400: 'body' required\n", 400)
        msg_id = store.send(from_id, to_id, text)
        return text_response(f"OK\nid = {toml_str(msg_id)}\n")

    def _inbox():
        to_id = _agent_id()
        if not to_id:
            return text_response(
                "ERROR 400: agent_id required\n"
                f"Usage: GET {prefix}/inbox?agent_id=<id>\n"
                "       or set X-Agent-Id header\n",
                400,
            )
        unread_only = request.args.get("unread", "0") == "1"
        page, per_page = _parse_page_params()
        total = store.count_inbox(to_id, unread_only)
        msgs = store.inbox(to_id, page, per_page, unread_only)
        lines = [
            f"# Inbox (agent {to_id})\n",
            _page_header(page, per_page, total),
            "# tip: ?unread=1 to filter unread\n\n",
        ]
        for m in msgs:
            lines.append("[[messages]]\n")
            lines.append(f"id   = {toml_str(m['id'])}\n")
            lines.append(f"from = {toml_str(m['from_id'])}\n")
            lines.append(f"read = {str(bool(m['read'])).lower()}\n")
            lines.append(f"date = {toml_str(_fmt_ts(m['created_at']))}\n")
            lines.append(f"url  = {toml_str(prefix + '/inbox/' + m['id'])}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _msg(msg_id: str):
        if request.method == "DELETE":
            ok = store.delete(msg_id)
            return text_response(
                "OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404
            )
        m = store.get(msg_id)
        if not m:
            return text_response("ERROR 404: message not found\n", 404)
        return text_response(
            f"id   = {toml_str(m['id'])}\n"
            f"from = {toml_str(m['from_id'])}\n"
            f"to   = {toml_str(m['to_id'])}\n"
            f"date = {toml_str(_fmt_ts(m['created_at']))}\n"
            "\n---\n\n"
            + m["body"] + "\n"
        )

    def _sent():
        from_id = _agent_id()
        if not from_id:
            return text_response(
                "ERROR 400: agent_id required\n"
                f"Usage: GET {prefix}/sent?agent_id=<id>\n"
                "       or set X-Agent-Id header\n",
                400,
            )
        page, per_page = _parse_page_params()
        total = store.count_sent(from_id)
        msgs = store.sent(from_id, page, per_page)
        lines = [
            f"# Sent (agent {from_id})\n",
            _page_header(page, per_page, total),
            "\n",
        ]
        for m in msgs:
            lines.append("[[messages]]\n")
            lines.append(f"id   = {toml_str(m['id'])}\n")
            lines.append(f"to   = {toml_str(m['to_id'])}\n")
            lines.append(f"date = {toml_str(_fmt_ts(m['created_at']))}\n")
            lines.append(f"url  = {toml_str(prefix + '/inbox/' + m['id'])}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _agents():
        page, per_page = _parse_page_params()
        total = store.count_agents()
        agents = store.list_agents(page, per_page)
        lines = [
            "# Known agents\n",
            "# agents who have sent at least one message\n",
            _page_header(page, per_page, total),
            "\n",
        ]
        for a in agents:
            lines.append("[[agents]]\n")
            lines.append(f"id        = {toml_str(a['from_id'])}\n")
            lines.append(f"sent      = {a['sent_count']}\n")
            lines.append(f"last_seen = {toml_str(_fmt_ts(a['last_seen']))}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _human():
        p = prefix
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chat</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#f5f5f5;color:#222;height:100vh;display:flex;flex-direction:column;overflow:hidden}}
header{{background:#fff;border-bottom:1px solid #e0e0e0;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}}
header strong{{font-size:15px}}
header small{{color:#888;font-size:13px}}
a{{color:#0066cc;text-decoration:none}}
.layout{{display:flex;flex:1;overflow:hidden}}
aside{{width:200px;background:#fff;border-right:1px solid #e0e0e0;overflow-y:auto;flex-shrink:0}}
.aside-title{{padding:12px 14px 6px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#888}}
.agent-btn{{display:block;width:100%;text-align:left;padding:10px 14px;border:none;background:none;cursor:pointer;font-size:14px;border-left:3px solid transparent}}
.agent-btn:hover{{background:#f5f5f5}}
.agent-btn.active{{background:#e8f0fe;border-left-color:#0066cc;font-weight:600}}
main{{flex:1;display:flex;flex-direction:column;overflow:hidden;background:#fafafa}}
.chat-title{{padding:12px 20px;background:#fff;border-bottom:1px solid #e0e0e0;font-weight:600;font-size:14px;flex-shrink:0}}
#messages{{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:10px}}
.wrap{{display:flex;flex-direction:column;max-width:72%}}
.wrap.sent{{align-self:flex-end;align-items:flex-end}}
.wrap.recv{{align-self:flex-start}}
.bubble{{padding:10px 14px;border-radius:16px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-break:break-word}}
.sent .bubble{{background:#0066cc;color:#fff;border-bottom-right-radius:4px}}
.recv .bubble{{background:#e9e9eb;color:#222;border-bottom-left-radius:4px}}
.meta{{font-size:11px;color:#aaa;margin-top:3px;padding:0 4px}}
.placeholder{{flex:1;display:flex;align-items:center;justify-content:center;color:#bbb;font-size:14px}}
.compose{{background:#fff;border-top:1px solid #e0e0e0;padding:14px 20px;flex-shrink:0}}
.compose-row{{display:flex;gap:10px;align-items:flex-end}}
textarea{{flex:1;border:1px solid #ddd;border-radius:8px;padding:10px 12px;font-size:14px;font-family:inherit;resize:none;height:48px;max-height:120px}}
textarea:focus{{outline:none;border-color:#0066cc}}
button.send{{background:#0066cc;color:#fff;border:none;border-radius:8px;padding:0 20px;height:48px;cursor:pointer;font-size:14px;white-space:nowrap;flex-shrink:0}}
button.send:hover{{background:#0052a3}}
button.send:disabled{{background:#aaa;cursor:default}}
</style>
</head>
<body>
<header>
  <strong>Chat</strong>
  <small>Agent&nbsp;#<span id="my-id">…</span>&nbsp;·&nbsp;<a href="{p}/_human" style="color:#aaa">switch</a></small>
</header>
<div class="layout">
  <aside>
    <div class="aside-title">Agents</div>
    <div id="agent-list"></div>
  </aside>
  <main>
    <div class="chat-title" id="chat-title">Select an agent</div>
    <div id="messages"><div class="placeholder">← choose an agent to start</div></div>
    <div class="compose">
      <div class="compose-row">
        <textarea id="inp" placeholder="Type a message… (Enter to send)" disabled></textarea>
        <button class="send" id="send-btn" disabled>Send</button>
      </div>
    </div>
  </main>
</div>
<script>
const P = {p!r};
let myId = localStorage.getItem('inact_agent_id');
let myName = localStorage.getItem('inact_name') || ('Agent #'+myId);
if (!myId) {{ location.href = P+'/_human'; }}
document.getElementById('my-id').textContent = myId;

let selId=null, selName='', timer=null, shownIds=new Set();

function parseBlocks(text,tag){{
  return text.split('[['+tag+']]').slice(1).map(b=>{{
    const o={{}};
    b.split('\\n').forEach(l=>{{const m=l.match(/^(\\w+)\\s*=\\s*"([^"]*)"/)|| l.match(/^(\\w+)\\s*=\\s*(\\S+)/);if(m)o[m[1]]=m[2];}});
    return o;
  }}).filter(o=>Object.keys(o).length>0);
}}

async function loadAgents(){{
  const text=await fetch(P+'/agents').then(r=>r.text()).catch(()=>'');
  const agents=parseBlocks(text,'agents');
  const list=document.getElementById('agent-list');
  if(!agents.length){{list.innerHTML='<div style="padding:12px 14px;font-size:13px;color:#bbb">No agents yet.</div>';return;}}
  list.innerHTML='';
  agents.forEach(a=>{{
    const btn=document.createElement('button');
    btn.className='agent-btn'; btn.dataset.id=a.id; btn.dataset.name=a.id;
    btn.textContent='Agent #'+a.id+(a.id==myId?' (you)':'');
    btn.addEventListener('click',()=>select(a.id,'Agent #'+a.id));
    list.appendChild(btn);
  }});
}}

function select(id,name){{
  selId=id; selName=name;
  document.querySelectorAll('.agent-btn').forEach(b=>b.classList.remove('active'));
  const b=document.querySelector(`.agent-btn[data-id="${{id}}"]`);
  if(b)b.classList.add('active');
  document.getElementById('chat-title').textContent='Chat with '+name;
  document.getElementById('inp').disabled=false;
  document.getElementById('send-btn').disabled=false;
  shownIds.clear();
  document.getElementById('messages').innerHTML='';
  if(timer)clearInterval(timer);
  poll(); timer=setInterval(poll,3000);
}}

async function poll(){{
  if(!selId)return;
  const [inbTxt,sntTxt]=await Promise.all([
    fetch(P+'/inbox',{{headers:{{'X-Agent-Id':myId}}}}).then(r=>r.text()).catch(()=>''),
    fetch(P+'/sent', {{headers:{{'X-Agent-Id':myId}}}}).then(r=>r.text()).catch(()=>''),
  ]);
  const recv=parseBlocks(inbTxt,'messages').filter(m=>m.from===String(selId)).map(m=>{{m._dir='recv';return m;}});
  const sent=parseBlocks(sntTxt,'messages').filter(m=>m.to  ===String(selId)).map(m=>{{m._dir='sent';return m;}});
  const all=[...recv,...sent].filter(m=>m.id&&!shownIds.has(m.id)).sort((a,b)=>a.date>b.date?1:-1);
  const box=document.getElementById('messages');
  let added=0;
  for(const m of all){{
    shownIds.add(m.id);
    const body=await fetch(P+'/inbox/'+m.id).then(r=>r.text()).then(t=>(t.split('\\n---\\n\\n')[1]||'').trim()).catch(()=>'');
    const wrap=document.createElement('div');
    wrap.className='wrap '+m._dir;
    const time=m.date?new Date(m.date).toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit'}}):'';
    wrap.innerHTML='<div class="bubble">'+esc(body)+'</div><div class="meta">'+(m._dir==='recv'?selName:'You')+(time?' · '+time:'')+'</div>';
    box.appendChild(wrap); added++;
  }}
  if(added)box.scrollTop=box.scrollHeight;
}}

async function send(){{
  const inp=document.getElementById('inp');
  const body=inp.value.trim();
  if(!body||!selId)return;
  inp.value=''; inp.disabled=true; document.getElementById('send-btn').disabled=true;
  await fetch(P+'/send',{{method:'POST',headers:{{'Content-Type':'application/json','X-Agent-Id':myId}},body:JSON.stringify({{from:myId,to:selId,body}})}}).catch(()=>{{}});
  await poll();
  inp.disabled=false; document.getElementById('send-btn').disabled=false; inp.focus();
}}

document.getElementById('send-btn').addEventListener('click',send);
document.getElementById('inp').addEventListener('keydown',e=>{{if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();send();}}}});
function esc(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}

loadAgents();
</script>
</body></html>"""
        return html_response(html)

    flask_app.add_url_rule(
        prefix + "/send",
        endpoint=ep + "_send", view_func=_send, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/inbox",
        endpoint=ep + "_inbox", view_func=_inbox)
    flask_app.add_url_rule(
        prefix + "/inbox/<msg_id>",
        endpoint=ep + "_msg", view_func=_msg, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/sent",
        endpoint=ep + "_sent", view_func=_sent)
    flask_app.add_url_rule(
        prefix + "/agents",
        endpoint=ep + "_agents", view_func=_agents)
    inact_app._human_views[prefix] = lambda path: _human()


def mount_message(inact_app, prefix: str, storage) -> None:
    """
    Mount an agent messaging service at *prefix*.

    Agents send plain-text messages to each other by ID. Inbox and sent folders
    are paginated. ``/agents`` lists agents who have sent at least one message.

    *storage* — a database URL/path or a :class:`~inact.storage.Storage` instance.

    Example::

        app.mount_message("/msg", "./data/messages.db")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    attach_message(inact_app, p, MessageStore(backend))
    inact_app._app_mounts.append((p, (
        f"\nAgent messaging: {p}\n"
        f'  POST   {p}/send          send  body: {{"from":"1","to":"2","body":"..."}}\n'
        f"  GET    {p}/inbox         received messages  (?agent_id=<id>  ?unread=1  ?page=1)\n"
        f"  GET    {p}/inbox/{{id}}    read message (marks read)\n"
        f"  DELETE {p}/inbox/{{id}}    delete message\n"
        f"  GET    {p}/sent          sent messages  (?agent_id=<id>  ?page=1)\n"
        f"  GET    {p}/agents        known agents  (?page=1)\n"
        f"  # identity: X-Agent-Id header or ?agent_id= param\n"
    )))
