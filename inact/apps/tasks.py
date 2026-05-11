"""
Agent task list — tasks with due dates, assignees, and arbitrary nesting.

mount_tasks(prefix, storage) registers:

  GET    {prefix}/                          list root tasks (no parent)
  GET    {prefix}/?status=todo|done         filter by status
  GET    {prefix}/?assignee=alice           filter by assignee
  POST   {prefix}/                          create task
                                            body: {"title":"...","description":"...",
                                                   "due":"YYYY-MM-DD",
                                                   "assignee":"...","parent_id":"optional-id"}
  GET    {prefix}/.today                    due today or overdue, not done (all levels)
  GET    {prefix}/.overdue                  past due, not done (all levels)
  GET    {prefix}/.unassigned               no assignee, not done (all levels)
  GET    {prefix}/{id}                      task detail + direct children
  POST   {prefix}/{id}                      update fields (title/description/status/due/assignee)
  DELETE {prefix}/{id}                      delete task and all descendants
  GET    {prefix}/{id}/children             list direct children
  POST   {prefix}/{id}/.done                mark done
  POST   {prefix}/{id}/.reopen              reopen (status → todo)
  POST   {prefix}/{id}/.assign              set assignee  body: {"assignee":"alice"}

Status values:   todo | done
Listings sorted by due date asc (no-due last), then created_at.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import Request

from ..storage import Storage
from ..utils import text_response, toml_str, _body, caller_id

_DDL = [
    """CREATE TABLE IF NOT EXISTS tasks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id   INTEGER,
        title       TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        status      TEXT    NOT NULL DEFAULT 'todo',
        due         TEXT,
        assignee    TEXT    NOT NULL DEFAULT '',
        created_at  BIGINT  NOT NULL,
        updated_at  BIGINT  NOT NULL,
        done_at     BIGINT
    )""",
]

_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN assignee TEXT NOT NULL DEFAULT ''",
]

_VALID_STATUS = frozenset({"todo", "done"})


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _sort_key(t: dict):
    return (
        t["due"] or "9999-99-99",
        t["created_at"],
    )


class TaskStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)
        self._migrate()

    def _migrate(self) -> None:
        try:
            cols = self._s.fetchall("PRAGMA table_info(tasks)")
            id_col = next((c for c in cols if c["name"] == "id"), None)
            if id_col and id_col["type"].upper() == "TEXT":
                self._s.execute("DROP TABLE IF EXISTS tasks")
                self._s.init(_DDL)
                return
        except Exception:
            pass
        for sql in _MIGRATIONS:
            try:
                self._s.execute(sql)
            except Exception:
                pass

    def create(self, title: str, description: str = "",
               due: str | None = None, assignee: str = "",
               parent_id: str | None = None) -> str:
        now = int(time.time())
        new_id = self._s.insert(
            "INSERT INTO tasks (parent_id, title, description, status, due, assignee,"
            " created_at, updated_at, done_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (parent_id, title, description, "todo", due, assignee, now, now, None),
        )
        return str(new_id)

    def list_tasks(self, parent_id: str | None = None,
                   status: str | None = None,
                   assignee: str | None = None) -> list[dict]:
        if parent_id is None:
            q, params = "SELECT * FROM tasks WHERE parent_id IS NULL", []
        else:
            q, params = "SELECT * FROM tasks WHERE parent_id=?", [parent_id]
        if status:
            q += " AND status=?"
            params.append(status)
        if assignee is not None:
            q += " AND assignee=?"
            params.append(assignee)
        return sorted(self._s.fetchall(q, tuple(params)), key=_sort_key)

    def get(self, task_id: str) -> dict | None:
        return self._s.fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))

    def update(self, task_id: str, fields: dict) -> bool:
        allowed = {"title", "description", "status", "due", "assignee"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        now = int(time.time())
        updates["updated_at"] = now
        if updates.get("status") == "done":
            updates["done_at"] = now
        elif "status" in updates:
            updates["done_at"] = None
        set_clause = ", ".join(f"{k}=?" for k in updates)
        return self._s.execute(
            f"UPDATE tasks SET {set_clause} WHERE id=?",
            tuple(updates.values()) + (task_id,),
        ) > 0

    def delete(self, task_id: str) -> bool:
        for child in self._s.fetchall("SELECT id FROM tasks WHERE parent_id=?", (task_id,)):
            self.delete(child["id"])
        return self._s.execute("DELETE FROM tasks WHERE id=?", (task_id,)) > 0

    def children(self, task_id: str) -> list[dict]:
        return sorted(
            self._s.fetchall("SELECT * FROM tasks WHERE parent_id=?", (task_id,)),
            key=_sort_key,
        )

    def child_counts(self, task_id: str) -> tuple[int, int]:
        total = self._s.fetchall(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE parent_id=?", (task_id,)
        )
        done = self._s.fetchall(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE parent_id=? AND status='done'", (task_id,)
        )
        return (total[0]["cnt"] if total else 0), (done[0]["cnt"] if done else 0)

    def today(self) -> list[dict]:
        td = _today_str()
        return sorted(
            self._s.fetchall(
                "SELECT * FROM tasks WHERE due <= ? AND status != 'done'", (td,)
            ),
            key=_sort_key,
        )

    def overdue(self) -> list[dict]:
        td = _today_str()
        return sorted(
            self._s.fetchall(
                "SELECT * FROM tasks WHERE due < ? AND status != 'done'", (td,)
            ),
            key=_sort_key,
        )

    def unassigned(self) -> list[dict]:
        return sorted(
            self._s.fetchall(
                "SELECT * FROM tasks WHERE (assignee IS NULL OR assignee='') AND status != 'done'"
            ),
            key=_sort_key,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _is_overdue(task: dict) -> bool:
    return bool(task["due"]) and task["due"] < _today_str() and task["status"] != "done"


def _task_row_toml(task: dict, prefix: str,
                   total_children: int = 0, done_children: int = 0) -> str:
    lines = [
        "[[tasks]]\n",
        f"id       = {toml_str(str(task['id']))}\n",
        f"title    = {toml_str(task['title'])}\n",
        f"status   = {toml_str(task['status'])}\n",
    ]
    if task.get("assignee"):
        lines.append(f"assignee = {toml_str(task['assignee'])}\n")
    if task["due"]:
        lines.append(f"due      = {toml_str(task['due'])}\n")
        if _is_overdue(task):
            lines.append("overdue  = true\n")
    if task["parent_id"]:
        lines.append(f"parent   = {toml_str(prefix + '/' + str(task['parent_id']))}\n")
    if total_children:
        lines.append(f"children = {total_children}\n")
        lines.append(f"done     = {done_children}\n")
    lines.append(f"url      = {toml_str(prefix + '/' + str(task['id']))}\n")
    lines.append("\n")
    return "".join(lines)


def _task_detail(task: dict, children: list[dict], prefix: str) -> str:
    tid = str(task["id"])
    lines = [f"# {task['title']}{'  [OVERDUE]' if _is_overdue(task) else ''}\n\n"]
    lines.append(f"id          = {toml_str(tid)}\n")
    lines.append(f"title       = {toml_str(task['title'])}\n")
    if task["description"]:
        lines.append(f"description = {toml_str(task['description'])}\n")
    lines.append(f"status      = {toml_str(task['status'])}\n")
    lines.append(f"assignee    = {toml_str(task.get('assignee') or '')}\n")
    if task["due"]:
        lines.append(f"due         = {toml_str(task['due'])}\n")
    if task["parent_id"]:
        lines.append(f"parent      = {toml_str(prefix + '/' + str(task['parent_id']))}\n")
    lines.append(f"created_at  = {toml_str(_fmt_ts(task['created_at']))}\n")
    lines.append(f"updated_at  = {toml_str(_fmt_ts(task['updated_at']))}\n")
    if task["done_at"]:
        lines.append(f"done_at     = {toml_str(_fmt_ts(task['done_at']))}\n")
    lines.append(f"children    = {toml_str(prefix + '/' + tid + '/children')}\n")
    lines.append("\n")

    if children:
        lines.append(f"# Children ({len(children)})\n\n")
        for child in children:
            cid = str(child["id"])
            lines.append("[[children]]\n")
            lines.append(f"id       = {toml_str(cid)}\n")
            lines.append(f"title    = {toml_str(child['title'])}\n")
            lines.append(f"status   = {toml_str(child['status'])}\n")
            if child.get("assignee"):
                lines.append(f"assignee = {toml_str(child['assignee'])}\n")
            if child["due"]:
                lines.append(f"due      = {toml_str(child['due'])}\n")
                if _is_overdue(child):
                    lines.append("overdue  = true\n")
            lines.append(f"url      = {toml_str(prefix + '/' + cid)}\n")
            lines.append(f"children = {toml_str(prefix + '/' + cid + '/children')}\n")
            lines.append("\n")

    return "".join(lines)


def _parse_create_body(body: dict,
                       lookup_agent=None) -> tuple[str, dict] | tuple[None, str]:
    title = (body.get("title") or "").strip()
    if not title:
        return None, "'title' required"
    due = (body.get("due") or "").strip() or None
    if due:
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            return None, "'due' must be YYYY-MM-DD"
    assignee = (body.get("assignee") or "").strip()
    if assignee and lookup_agent is not None:
        if lookup_agent(assignee) is None:
            return None, f"'assignee' {assignee!r} is not a registered agent id"
    return title, {
        "description": (body.get("description") or "").strip(),
        "due": due,
        "assignee": assignee,
    }


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_tasks(inact_app, prefix: str, store: TaskStore,
                 agents_prefix: str = "/agents",
                 lookup_agent=None,
                 notify_fn=None) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_tasks_" + prefix.replace("/", "__")
    fastapi_app = inact_app.app

    def _name(agent_id: str) -> str:
        if not agent_id or lookup_agent is None:
            return ""
        agent = lookup_agent(agent_id)
        return (agent.get("name") or f"Agent #{agent_id}") if agent else ""

    def _notify_assign(assignee_id: str, task_id: str, task_title: str) -> None:
        if notify_fn and assignee_id:
            notify_fn(assignee_id, "tasks", (
                f"[task:{task_id}] You have been assigned: \"{task_title}\"\n"
                f"  details : GET {prefix}/{task_id}\n"
                f"  done    : POST {prefix}/{task_id}/.done\n"
                f"  update  : POST {prefix}/{task_id}  body: {{\"description\":\"...\"}}"
            ))

    def _root(request: Request):
        if request.method == "POST":
            body = _body(request)
            title, result = _parse_create_body(body, lookup_agent=lookup_agent)
            if title is None:
                return text_response(
                    f"ERROR 400: {result}\n"
                    f"POST {prefix}/\n"
                    '  Body: {"title":"...","description":"...","due":"YYYY-MM-DD",'
                    '"assignee":"<agent_id>","parent_id":"optional-id"}\n'
                    f"\nassignee: integer agent id from {agents_prefix}/\n",
                    400,
                )
            if not result["assignee"]:
                result["assignee"] = caller_id(request)
            parent_id = (body.get("parent_id") or "").strip() or None
            if parent_id and not store.get(parent_id):
                return text_response(f"ERROR 404: parent task {parent_id!r} not found\n", 404)
            task_id = store.create(title, parent_id=parent_id, **result)
            _notify_assign(result["assignee"], task_id, title)
            return text_response(
                f"OK\nid  = {toml_str(task_id)}\nurl = {toml_str(prefix + '/' + task_id)}\n"
            )

        status_f   = request.query_params.get("status",   "").strip() or None
        assignee_f = request.query_params.get("assignee", None)
        if assignee_f is not None:
            assignee_f = assignee_f.strip()
        tasks = store.list_tasks(status=status_f, assignee=assignee_f)
        td = _today_str()
        n_overdue = sum(1 for t in tasks if t["due"] and t["due"] < td and t["status"] != "done")
        lines = [f"# Tasks\n# {len(tasks)} task(s)"]
        if n_overdue:
            lines.append(f", {n_overdue} overdue")
        lines.append("\n# tip: ?status=todo|done  ?assignee=<agent_id>\n\n")
        for t in tasks:
            total, done = store.child_counts(t["id"])
            lines.append(_task_row_toml(t, prefix, total, done))
        return text_response("".join(lines))

    def _today():
        tasks = store.today()
        lines = [f"# Due today or overdue ({_today_str()})\n# {len(tasks)} task(s)\n\n"]
        for t in tasks:
            lines.append(_task_row_toml(t, prefix))
        return text_response("".join(lines))

    def _overdue():
        tasks = store.overdue()
        lines = [f"# Overdue tasks\n# {len(tasks)} task(s)\n\n"]
        for t in tasks:
            lines.append(_task_row_toml(t, prefix))
        return text_response("".join(lines))

    def _unassigned():
        tasks = store.unassigned()
        lines = [f"# Unassigned tasks\n# {len(tasks)} task(s)\n\n"]
        for t in tasks:
            lines.append(_task_row_toml(t, prefix))
        return text_response("".join(lines))

    def _task(task_id: str, request: Request):
        if request.method == "DELETE":
            ok = store.delete(task_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)

        if request.method == "POST":
            task = store.get(task_id)
            if not task:
                return text_response("ERROR 404: task not found\n", 404)
            body = _body(request)
            fields: dict = {}
            if "title" in body:
                fields["title"] = (body["title"] or "").strip()
            if "description" in body:
                fields["description"] = body["description"] or ""
            if "status" in body:
                s = (body["status"] or "").strip()
                if s not in _VALID_STATUS:
                    return text_response(
                        f"ERROR 400: 'status' must be one of: {', '.join(sorted(_VALID_STATUS))}\n", 400
                    )
                fields["status"] = s
            if "due" in body:
                due = (body["due"] or "").strip() or None
                if due:
                    try:
                        datetime.strptime(due, "%Y-%m-%d")
                    except ValueError:
                        return text_response("ERROR 400: 'due' must be YYYY-MM-DD\n", 400)
                fields["due"] = due
            if "assignee" in body:
                new_assignee = (body["assignee"] or "").strip()
                if new_assignee and lookup_agent is not None:
                    if lookup_agent(new_assignee) is None:
                        return text_response(
                            f"ERROR 400: 'assignee' {new_assignee!r} is not a registered agent id\n",
                            400,
                        )
                fields["assignee"] = new_assignee
            old_assignee = task.get("assignee", "")
            store.update(task_id, fields)
            new_assignee = fields.get("assignee", "")
            if new_assignee and new_assignee != old_assignee:
                _notify_assign(new_assignee, task_id, task["title"])
            return text_response("OK\n")

        task = store.get(task_id)
        if not task:
            return text_response("ERROR 404: task not found\n", 404)
        return text_response(_task_detail(task, store.children(task_id), prefix))

    def _list_children(task_id: str):
        task = store.get(task_id)
        if not task:
            return text_response("ERROR 404: task not found\n", 404)
        kids = store.children(task_id)
        lines = [f"# Children: {task['title']}\n# {len(kids)} task(s)\n\n"]
        for k in kids:
            total, done = store.child_counts(k["id"])
            lines.append(_task_row_toml(k, prefix, total, done))
        return text_response("".join(lines))

    def _done(task_id: str):
        if not store.get(task_id):
            return text_response("ERROR 404: task not found\n", 404)
        store.update(task_id, {"status": "done"})
        return text_response("OK\n")

    def _reopen(task_id: str):
        if not store.get(task_id):
            return text_response("ERROR 404: task not found\n", 404)
        store.update(task_id, {"status": "todo"})
        return text_response("OK\n")

    def _assign(task_id: str, request: Request):
        task = store.get(task_id)
        if not task:
            return text_response("ERROR 404: task not found\n", 404)
        body = _body(request)
        assignee = (body.get("assignee") or "").strip()
        if not assignee:
            return text_response(
                "ERROR 400: 'assignee' required\n"
                f'Body: {{"assignee": "<agent_id>"}}\n'
                f"Agent ids: GET {agents_prefix}/\n", 400
            )
        if lookup_agent is not None:
            agent = lookup_agent(assignee)
            if agent is None:
                return text_response(
                    f"ERROR 400: {assignee!r} is not a registered agent id\n"
                    f"Agent ids: GET {agents_prefix}/\n", 400
                )
        old_assignee = task.get("assignee", "")
        store.update(task_id, {"assignee": assignee})
        if assignee != old_assignee:
            _notify_assign(assignee, task_id, task["title"])
        return text_response(f"OK\nassignee = {toml_str(assignee)}\n")

    fastapi_app.add_api_route(prefix + "/", _root, methods=["GET", "POST"])
    fastapi_app.add_api_route(prefix + "/.today", _today, methods=["GET"])
    fastapi_app.add_api_route(prefix + "/.overdue", _overdue, methods=["GET"])
    fastapi_app.add_api_route(prefix + "/.unassigned", _unassigned, methods=["GET"])
    fastapi_app.add_api_route(prefix + "/{task_id}", _task, methods=["GET", "POST", "DELETE"])
    fastapi_app.add_api_route(prefix + "/{task_id}/children", _list_children, methods=["GET"])
    fastapi_app.add_api_route(prefix + "/{task_id}/.done", _done, methods=["POST"])
    fastapi_app.add_api_route(prefix + "/{task_id}/.reopen", _reopen, methods=["POST"])
    fastapi_app.add_api_route(prefix + "/{task_id}/.assign", _assign, methods=["POST"])

    def _human(path: str):
        from ..render import render_template
        from ..utils import html_response
        from ..render import workspace_nav
        return html_response(render_template("todo_human.html",
            title="Tasks", prefix=prefix, agents_prefix=agents_prefix,
            nav="", pills=[],
            workspace_links=workspace_nav("/_human/tasks/"),
            show_identity=True))

    inact_app._human_views[prefix] = _human
    inact_app.add_nav_item(prefix.rsplit("/", 1)[-1] or prefix.strip("/"),
                           "/_human" + prefix + "/")


def mount_tasks(inact_app, prefix: str, storage,
                agents_prefix: str = "/agents",
                agents_storage=None,
                notify_storage=None) -> None:
    """
    Mount a task list at *prefix*.

    *storage*        — database URL/path or Storage instance for tasks.
    *agents_prefix*  — prefix where the agent registry is mounted (for assignee validation).
    *agents_storage* — if provided, assignee is validated as a registered agent id.
    *notify_storage* — if provided, agents receive a notification when assigned.

    Example::

        mount_tasks(app, "/tasks", "./tasks.db",
                    agents_storage="./agents.db",
                    notify_storage="./notify.db")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    store = TaskStore(backend)

    lookup_agent = None
    if agents_storage is not None:
        from .workspace.register import AgentRegistry
        ag_back = make_storage(agents_storage) if isinstance(agents_storage, str) else agents_storage
        ag_reg = AgentRegistry(ag_back)
        def lookup_agent(agent_id: str) -> dict | None:
            try:
                return ag_reg.get(int(agent_id))
            except (ValueError, TypeError):
                return None

    notify_fn = None
    if notify_storage is not None:
        from .notify import NotifyStore, _push
        ns_back = make_storage(notify_storage) if isinstance(notify_storage, str) else notify_storage
        nstore = NotifyStore(ns_back)
        def notify_fn(to_id: str, from_id: str, message: str) -> None:
            notif_id = nstore.send(to_id, message, from_id)
            _push(nstore, to_id, notif_id, message, from_id)

    attach_tasks(inact_app, p, store,
                 agents_prefix="/" + agents_prefix.strip("/"),
                 lookup_agent=lookup_agent,
                 notify_fn=notify_fn)
    inact_app._app_mounts.append((p, (
        f"\nTasks: {p}\n"
        f"  GET    {p}/                           list tasks  (?status=todo|done  ?assignee=name)\n"
        f"  POST   {p}/                           create task\n"
        f"  GET    {p}/.today                     due today or overdue\n"
        f"  GET    {p}/.overdue                   past due, not done\n"
        f"  GET    {p}/.unassigned                no assignee, not done\n"
        f"  GET    {p}/{{id}}                       task detail + children\n"
        f"  POST   {p}/{{id}}                       update fields\n"
        f"  DELETE {p}/{{id}}                       delete task\n"
        f"  POST   {p}/{{id}}/.done                 mark done\n"
        f"  POST   {p}/{{id}}/.reopen               reopen\n"
        f"  POST   {p}/{{id}}/.assign               set assignee\n"
        f"  GET    {p}/{{id}}/children              list children\n"
    )))
