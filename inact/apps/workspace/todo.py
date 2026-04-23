"""
Agent todo list — tasks with priorities, due dates, assignees, and arbitrary nesting.

A task is just a task.  Nesting is an optional relationship: set
``parent_id`` when creating to attach a task under another.  Any task can
have children; children can have children.

mount_todo(prefix, storage) registers:

  GET    {prefix}/                     list root tasks (no parent)
  GET    {prefix}/?status=todo|done    filter by status
  GET    {prefix}/?priority=high       filter by priority
  GET    {prefix}/?assignee=alice      filter by assignee
  POST   {prefix}/                     create task
                                       body: {"title":"...","description":"...",
                                              "priority":"normal","due":"YYYY-MM-DD",
                                              "assignee":"...","parent_id":"optional-uuid"}
  GET    {prefix}/.today               due today or overdue, not done (all levels)
  GET    {prefix}/.overdue             past due, not done (all levels)
  GET    {prefix}/.unassigned          no assignee, not done (all levels)
  GET    {prefix}/{id}                 task detail + direct children
  POST   {prefix}/{id}                 update fields (title/description/status/priority/due/assignee)
  DELETE {prefix}/{id}                 delete task and all descendants
  GET    {prefix}/{id}/children        list direct children
  POST   {prefix}/{id}/.done           mark done
  POST   {prefix}/{id}/.reopen         reopen (status → todo)
  POST   {prefix}/{id}/.assign         set assignee  body: {"assignee":"alice"}

Priority levels: low | normal | high | urgent
Status values:   todo | done
Listings sorted by priority desc, then due date asc (no-due last).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from flask import request

from ...storage import Storage
from ...utils import text_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS tasks (
        id          TEXT    PRIMARY KEY,
        parent_id   TEXT,
        title       TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        status      TEXT    NOT NULL DEFAULT 'todo',
        priority    TEXT    NOT NULL DEFAULT 'normal',
        due         TEXT,
        assignee    TEXT    NOT NULL DEFAULT '',
        created_at  BIGINT  NOT NULL,
        updated_at  BIGINT  NOT NULL,
        done_at     BIGINT
    )""",
]

# Run on existing tables that predate the assignee column.
_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN assignee TEXT NOT NULL DEFAULT ''",
]

_VALID_STATUS   = frozenset({"todo", "done"})
_VALID_PRIORITY = frozenset({"low", "normal", "high", "urgent"})
_PRIORITY_RANK  = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def _today_str() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _sort_key(t: dict):
    return (
        _PRIORITY_RANK.get(t["priority"], 2),
        t["due"] or "9999-99-99",
        t["created_at"],
    )


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

class TodoStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)
        self._migrate()

    def _migrate(self) -> None:
        for sql in _MIGRATIONS:
            try:
                self._s.execute(sql)
            except Exception:
                pass  # column already exists

    def create(self, title: str, description: str = "",
               priority: str = "normal", due: str | None = None,
               assignee: str = "", parent_id: str | None = None) -> str:
        task_id = str(uuid.uuid4())
        now = int(time.time())
        self._s.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, parent_id, title, description,
             "todo", priority, due, assignee, now, now, None),
        )
        return task_id

    def list_tasks(self, parent_id: str | None = None,
                   status: str | None = None,
                   priority: str | None = None,
                   assignee: str | None = None) -> list[dict]:
        if parent_id is None:
            q, params = "SELECT * FROM tasks WHERE parent_id IS NULL", []
        else:
            q, params = "SELECT * FROM tasks WHERE parent_id=?", [parent_id]
        if status:
            q += " AND status=?"
            params.append(status)
        if priority:
            q += " AND priority=?"
            params.append(priority)
        if assignee is not None:
            q += " AND assignee=?"
            params.append(assignee)
        return sorted(self._s.fetchall(q, tuple(params)), key=_sort_key)

    def get(self, task_id: str) -> dict | None:
        return self._s.fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))

    def update(self, task_id: str, fields: dict) -> bool:
        allowed = {"title", "description", "status", "priority", "due", "assignee"}
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
        f"id       = {toml_str(task['id'])}\n",
        f"title    = {toml_str(task['title'])}\n",
        f"status   = {toml_str(task['status'])}\n",
        f"priority = {toml_str(task['priority'])}\n",
    ]
    if task.get("assignee"):
        lines.append(f"assignee = {toml_str(task['assignee'])}\n")
    if task["due"]:
        lines.append(f"due      = {toml_str(task['due'])}\n")
        if _is_overdue(task):
            lines.append("overdue  = true\n")
    if task["parent_id"]:
        lines.append(f"parent   = {toml_str(prefix + '/' + task['parent_id'])}\n")
    if total_children:
        lines.append(f"children = {total_children}\n")
        lines.append(f"done     = {done_children}\n")
    lines.append(f"url      = {toml_str(prefix + '/' + task['id'])}\n")
    lines.append("\n")
    return "".join(lines)


def _task_detail(task: dict, children: list[dict], prefix: str) -> str:
    lines = [f"# {task['title']}{'  [OVERDUE]' if _is_overdue(task) else ''}\n\n"]
    lines.append(f"id          = {toml_str(task['id'])}\n")
    lines.append(f"title       = {toml_str(task['title'])}\n")
    if task["description"]:
        lines.append(f"description = {toml_str(task['description'])}\n")
    lines.append(f"status      = {toml_str(task['status'])}\n")
    lines.append(f"priority    = {toml_str(task['priority'])}\n")
    lines.append(f"assignee    = {toml_str(task.get('assignee') or '')}\n")
    if task["due"]:
        lines.append(f"due         = {toml_str(task['due'])}\n")
    if task["parent_id"]:
        lines.append(f"parent      = {toml_str(prefix + '/' + task['parent_id'])}\n")
    lines.append(f"created_at  = {toml_str(_fmt_ts(task['created_at']))}\n")
    lines.append(f"updated_at  = {toml_str(_fmt_ts(task['updated_at']))}\n")
    if task["done_at"]:
        lines.append(f"done_at     = {toml_str(_fmt_ts(task['done_at']))}\n")
    lines.append(f"children    = {toml_str(prefix + '/' + task['id'] + '/children')}\n")
    lines.append("\n")

    if children:
        lines.append(f"# Children ({len(children)})\n\n")
        for child in children:
            lines.append("[[children]]\n")
            lines.append(f"id       = {toml_str(child['id'])}\n")
            lines.append(f"title    = {toml_str(child['title'])}\n")
            lines.append(f"status   = {toml_str(child['status'])}\n")
            lines.append(f"priority = {toml_str(child['priority'])}\n")
            if child.get("assignee"):
                lines.append(f"assignee = {toml_str(child['assignee'])}\n")
            if child["due"]:
                lines.append(f"due      = {toml_str(child['due'])}\n")
                if _is_overdue(child):
                    lines.append("overdue  = true\n")
            lines.append(f"url      = {toml_str(prefix + '/' + child['id'])}\n")
            lines.append(f"children = {toml_str(prefix + '/' + child['id'] + '/children')}\n")
            lines.append("\n")

    return "".join(lines)


def _parse_create_body(body: dict) -> tuple[str, dict] | tuple[None, str]:
    title = (body.get("title") or "").strip()
    if not title:
        return None, "'title' required"
    priority = (body.get("priority") or "normal").strip()
    if priority not in _VALID_PRIORITY:
        return None, f"'priority' must be one of: {', '.join(sorted(_VALID_PRIORITY))}"
    due = (body.get("due") or "").strip() or None
    if due:
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            return None, "'due' must be YYYY-MM-DD"
    return title, {
        "description": (body.get("description") or "").strip(),
        "priority": priority,
        "due": due,
        "assignee": (body.get("assignee") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_todo(inact_app, prefix: str, store: TodoStore) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_todo_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _root():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            title, result = _parse_create_body(body)
            if title is None:
                return text_response(
                    f"ERROR 400: {result}\n"
                    f"POST {prefix}/\n"
                    '  Body: {"title":"...","description":"...","priority":"normal",\n'
                    '         "due":"YYYY-MM-DD","assignee":"...","parent_id":"optional-uuid"}\n'
                    f"\nPriority: low | normal | high | urgent\n",
                    400,
                )
            parent_id = (body.get("parent_id") or "").strip() or None
            if parent_id and not store.get(parent_id):
                return text_response(f"ERROR 404: parent task {parent_id!r} not found\n", 404)
            task_id = store.create(title, parent_id=parent_id, **result)
            return text_response(
                f"OK\nid  = {toml_str(task_id)}\nurl = {toml_str(prefix + '/' + task_id)}\n"
            )

        status_f   = request.args.get("status",   "").strip() or None
        priority_f = request.args.get("priority", "").strip() or None
        assignee_f = request.args.get("assignee", None)
        if assignee_f is not None:
            assignee_f = assignee_f.strip()
        tasks = store.list_tasks(status=status_f, priority=priority_f, assignee=assignee_f)
        td = _today_str()
        n_overdue = sum(1 for t in tasks if t["due"] and t["due"] < td and t["status"] != "done")
        lines = [f"# Tasks\n# {len(tasks)} task(s)"]
        if n_overdue:
            lines.append(f", {n_overdue} overdue")
        lines.append(
            "\n# tip: ?status=todo|done  ?priority=low|normal|high|urgent  ?assignee=name\n\n"
        )
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

    def _task(task_id: str):
        if request.method == "DELETE":
            ok = store.delete(task_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)

        if request.method == "POST":
            task = store.get(task_id)
            if not task:
                return text_response("ERROR 404: task not found\n", 404)
            body = request.get_json(force=True, silent=True) or {}
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
            if "priority" in body:
                p = (body["priority"] or "").strip()
                if p not in _VALID_PRIORITY:
                    return text_response(
                        f"ERROR 400: 'priority' must be one of: {', '.join(sorted(_VALID_PRIORITY))}\n", 400
                    )
                fields["priority"] = p
            if "due" in body:
                due = (body["due"] or "").strip() or None
                if due:
                    try:
                        datetime.strptime(due, "%Y-%m-%d")
                    except ValueError:
                        return text_response("ERROR 400: 'due' must be YYYY-MM-DD\n", 400)
                fields["due"] = due
            if "assignee" in body:
                fields["assignee"] = (body["assignee"] or "").strip()
            store.update(task_id, fields)
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

    def _assign(task_id: str):
        if not store.get(task_id):
            return text_response("ERROR 404: task not found\n", 404)
        body = request.get_json(force=True, silent=True) or {}
        assignee = (body.get("assignee") or "").strip()
        if not assignee:
            return text_response(
                "ERROR 400: 'assignee' required\n"
                f'Body: {{"assignee": "alice"}}\n', 400
            )
        store.update(task_id, {"assignee": assignee})
        return text_response(f"OK\nassignee = {toml_str(assignee)}\n")

    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_root", view_func=_root, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/.today",
        endpoint=ep + "_today", view_func=_today)
    flask_app.add_url_rule(
        prefix + "/.overdue",
        endpoint=ep + "_overdue", view_func=_overdue)
    flask_app.add_url_rule(
        prefix + "/.unassigned",
        endpoint=ep + "_unassigned", view_func=_unassigned)
    flask_app.add_url_rule(
        prefix + "/<task_id>",
        endpoint=ep + "_task", view_func=_task, methods=["GET", "POST", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<task_id>/children",
        endpoint=ep + "_children", view_func=_list_children)
    flask_app.add_url_rule(
        prefix + "/<task_id>/.done",
        endpoint=ep + "_done", view_func=_done, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<task_id>/.reopen",
        endpoint=ep + "_reopen", view_func=_reopen, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<task_id>/.assign",
        endpoint=ep + "_assign", view_func=_assign, methods=["POST"])


def mount_todo(inact_app, prefix: str, storage) -> None:
    """
    Mount a todo list at *prefix*.

    Agents create tasks and subtasks with priorities, due dates, and assignees.

    *storage* — a database URL/path or a :class:`~inact.storage.Storage` instance.

    Example::

        app.mount_todo("/tasks", "./data/tasks.db")
    """
    from ...storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    attach_todo(inact_app, p, TodoStore(backend))
    inact_app._app_mounts.append((p, (
        f"\nTodo: {p}\n"
        f"  GET    {p}/             list tasks  (?status=todo|done  ?priority=high  ?assignee=name)\n"
        f"  POST   {p}/             create task\n"
        f"  GET    {p}/.today       due today or overdue\n"
        f"  GET    {p}/.overdue     past due, not done\n"
        f"  GET    {p}/.unassigned  no assignee, not done\n"
        f"  GET    {p}/{{id}}         task detail + children\n"
        f"  POST   {p}/{{id}}         update fields\n"
        f"  DELETE {p}/{{id}}         delete\n"
        f"  POST   {p}/{{id}}/.done   mark done\n"
        f"  POST   {p}/{{id}}/.reopen reopen\n"
        f"  POST   {p}/{{id}}/.assign set assignee\n"
    )))
