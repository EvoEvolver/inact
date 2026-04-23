"""
Agent todo list — tasks and subtasks with priorities and due dates.

mount_todo(prefix, storage) registers:

  GET    {prefix}/                  list top-level tasks (TOML)
  GET    {prefix}/?status=todo      filter: status  (todo|doing|done)
  GET    {prefix}/?priority=high    filter: priority (low|normal|high|urgent)
  POST   {prefix}/                  create task
                                    body: {"title":"...","description":"...","priority":"normal","due":"YYYY-MM-DD"}
  GET    {prefix}/.today            due today or overdue, not yet done
  GET    {prefix}/.overdue          past due, not yet done
  GET    {prefix}/{id}              task detail + subtasks
  POST   {prefix}/{id}              update task fields (any subset of title/description/status/priority/due)
  DELETE {prefix}/{id}              delete task and all its subtasks
  POST   {prefix}/{id}/subtasks     add subtask (same body as create)
  GET    {prefix}/{id}/subtasks     list subtasks
  POST   {prefix}/{id}/.done        mark done
  POST   {prefix}/{id}/.reopen      reopen (status → todo)

Priority levels (low → normal → high → urgent).
Listing is sorted by priority desc, then due date asc (no-due last).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from flask import request

from .storage import Storage
from .utils import text_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS tasks (
        id          TEXT    PRIMARY KEY,
        parent_id   TEXT,
        title       TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        status      TEXT    NOT NULL DEFAULT 'todo',
        priority    TEXT    NOT NULL DEFAULT 'normal',
        due         TEXT,
        created_at  BIGINT  NOT NULL,
        updated_at  BIGINT  NOT NULL,
        done_at     BIGINT
    )""",
]

_VALID_STATUS   = frozenset({"todo", "doing", "done"})
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

    def create(self, title: str, description: str = "",
               priority: str = "normal", due: str | None = None,
               parent_id: str | None = None) -> str:
        task_id = str(uuid.uuid4())
        now = int(time.time())
        self._s.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, parent_id, title, description,
             "todo", priority, due, now, now, None),
        )
        return task_id

    def list_tasks(self, parent_id: str | None = None,
                   status: str | None = None,
                   priority: str | None = None) -> list[dict]:
        if parent_id is None:
            q = "SELECT * FROM tasks WHERE parent_id IS NULL"
        else:
            q = "SELECT * FROM tasks WHERE parent_id=?"
        params: list = [] if parent_id is None else [parent_id]
        if status:
            q += " AND status=?"
            params.append(status)
        if priority:
            q += " AND priority=?"
            params.append(priority)
        rows = self._s.fetchall(q, tuple(params))
        return sorted(rows, key=_sort_key)

    def get(self, task_id: str) -> dict | None:
        return self._s.fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))

    def update(self, task_id: str, fields: dict) -> bool:
        allowed = {"title", "description", "status", "priority", "due"}
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
        params = list(updates.values()) + [task_id]
        return self._s.execute(
            f"UPDATE tasks SET {set_clause} WHERE id=?", tuple(params)
        ) > 0

    def delete(self, task_id: str) -> bool:
        self._s.execute("DELETE FROM tasks WHERE parent_id=?", (task_id,))
        return self._s.execute("DELETE FROM tasks WHERE id=?", (task_id,)) > 0

    def subtasks(self, task_id: str) -> list[dict]:
        rows = self._s.fetchall(
            "SELECT * FROM tasks WHERE parent_id=?", (task_id,)
        )
        return sorted(rows, key=_sort_key)

    def subtask_counts(self, task_id: str) -> tuple[int, int]:
        """Return (total, done) subtask counts."""
        total_rows = self._s.fetchall(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE parent_id=?", (task_id,)
        )
        done_rows = self._s.fetchall(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE parent_id=? AND status='done'",
            (task_id,),
        )
        return (
            total_rows[0]["cnt"] if total_rows else 0,
            done_rows[0]["cnt"] if done_rows else 0,
        )

    def today(self) -> list[dict]:
        td = _today_str()
        rows = self._s.fetchall(
            "SELECT * FROM tasks WHERE parent_id IS NULL AND due <= ? AND status != 'done'",
            (td,),
        )
        return sorted(rows, key=_sort_key)

    def overdue(self) -> list[dict]:
        td = _today_str()
        rows = self._s.fetchall(
            "SELECT * FROM tasks WHERE parent_id IS NULL AND due < ? AND status != 'done'",
            (td,),
        )
        return sorted(rows, key=_sort_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _is_overdue(task: dict) -> bool:
    return bool(task["due"]) and task["due"] < _today_str() and task["status"] != "done"


def _task_row_toml(task: dict, prefix: str,
                   total_sub: int = 0, done_sub: int = 0) -> str:
    lines = [
        "[[tasks]]\n",
        f"id       = {toml_str(task['id'])}\n",
        f"title    = {toml_str(task['title'])}\n",
        f"status   = {toml_str(task['status'])}\n",
        f"priority = {toml_str(task['priority'])}\n",
    ]
    if task["due"]:
        lines.append(f"due      = {toml_str(task['due'])}\n")
        if _is_overdue(task):
            lines.append("overdue  = true\n")
    if total_sub:
        lines.append(f"subtasks = {total_sub}\n")
        lines.append(f"done     = {done_sub}\n")
    lines.append(f"url      = {toml_str(prefix + '/' + task['id'])}\n")
    lines.append("\n")
    return "".join(lines)


def _task_detail(task: dict, subtasks: list[dict], prefix: str) -> str:
    overdue = _is_overdue(task)
    heading = f"# {task['title']}"
    if overdue:
        heading += "  [OVERDUE]"
    lines = [heading + "\n\n"]
    lines.append(f"id          = {toml_str(task['id'])}\n")
    lines.append(f"title       = {toml_str(task['title'])}\n")
    if task["description"]:
        lines.append(f"description = {toml_str(task['description'])}\n")
    lines.append(f"status      = {toml_str(task['status'])}\n")
    lines.append(f"priority    = {toml_str(task['priority'])}\n")
    if task["due"]:
        lines.append(f"due         = {toml_str(task['due'])}\n")
    if task["parent_id"]:
        lines.append(f"parent      = {toml_str(prefix + '/' + task['parent_id'])}\n")
    lines.append(f"created_at  = {toml_str(_fmt_ts(task['created_at']))}\n")
    lines.append(f"updated_at  = {toml_str(_fmt_ts(task['updated_at']))}\n")
    if task["done_at"]:
        lines.append(f"done_at     = {toml_str(_fmt_ts(task['done_at']))}\n")
    lines.append(f"add_subtask = {toml_str(prefix + '/' + task['id'] + '/subtasks')}\n")
    lines.append("\n")

    if subtasks:
        lines.append(f"# Subtasks ({len(subtasks)})\n\n")
        for sub in subtasks:
            lines.append("[[subtasks]]\n")
            lines.append(f"id       = {toml_str(sub['id'])}\n")
            lines.append(f"title    = {toml_str(sub['title'])}\n")
            lines.append(f"status   = {toml_str(sub['status'])}\n")
            lines.append(f"priority = {toml_str(sub['priority'])}\n")
            if sub["due"]:
                lines.append(f"due      = {toml_str(sub['due'])}\n")
                if _is_overdue(sub):
                    lines.append("overdue  = true\n")
            lines.append(f"url      = {toml_str(prefix + '/' + sub['id'])}\n")
            lines.append("\n")

    return "".join(lines)


def _parse_create_body(body: dict) -> tuple[str, dict] | tuple[None, str]:
    """Return (title, fields) or (None, error_message)."""
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
                    '  Body: {"title":"...","description":"...","priority":"normal","due":"YYYY-MM-DD"}\n'
                    f"\nPriority values: low | normal | high | urgent\n",
                    400,
                )
            task_id = store.create(title, **result)
            return text_response(
                f"OK\nid  = {toml_str(task_id)}\nurl = {toml_str(prefix + '/' + task_id)}\n"
            )

        status_f   = request.args.get("status", "").strip() or None
        priority_f = request.args.get("priority", "").strip() or None
        tasks = store.list_tasks(status=status_f, priority=priority_f)
        td = _today_str()
        n_overdue = sum(1 for t in tasks if t["due"] and t["due"] < td and t["status"] != "done")
        lines = [
            f"# Tasks\n",
            f"# {len(tasks)} task(s)",
        ]
        if n_overdue:
            lines.append(f", {n_overdue} overdue")
        lines.append(f"\n# tip: ?status=todo|doing|done  ?priority=low|normal|high|urgent\n\n")
        for t in tasks:
            total, done = store.subtask_counts(t["id"])
            lines.append(_task_row_toml(t, prefix, total, done))
        return text_response("".join(lines))

    def _today():
        tasks = store.today()
        td = _today_str()
        lines = [f"# Due today or overdue ({td})\n# {len(tasks)} task(s)\n\n"]
        for t in tasks:
            total, done = store.subtask_counts(t["id"])
            lines.append(_task_row_toml(t, prefix, total, done))
        return text_response("".join(lines))

    def _overdue():
        tasks = store.overdue()
        lines = [f"# Overdue tasks\n# {len(tasks)} task(s)\n\n"]
        for t in tasks:
            total, done = store.subtask_counts(t["id"])
            lines.append(_task_row_toml(t, prefix, total, done))
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
            store.update(task_id, fields)
            return text_response("OK\n")

        # GET
        task = store.get(task_id)
        if not task:
            return text_response("ERROR 404: task not found\n", 404)
        subs = store.subtasks(task_id)
        return text_response(_task_detail(task, subs, prefix))

    def _add_subtask(task_id: str):
        parent = store.get(task_id)
        if not parent:
            return text_response("ERROR 404: task not found\n", 404)
        body = request.get_json(force=True, silent=True) or {}
        title, result = _parse_create_body(body)
        if title is None:
            return text_response(f"ERROR 400: {result}\n", 400)
        sub_id = store.create(title, parent_id=task_id, **result)
        return text_response(
            f"OK\nid  = {toml_str(sub_id)}\nurl = {toml_str(prefix + '/' + sub_id)}\n"
        )

    def _list_subtasks(task_id: str):
        parent = store.get(task_id)
        if not parent:
            return text_response("ERROR 404: task not found\n", 404)
        subs = store.subtasks(task_id)
        lines = [
            f"# Subtasks: {parent['title']}\n",
            f"# {len(subs)} subtask(s)\n\n",
        ]
        for s in subs:
            lines.append(_task_row_toml(s, prefix))
        return text_response("".join(lines))

    def _done(task_id: str):
        task = store.get(task_id)
        if not task:
            return text_response("ERROR 404: task not found\n", 404)
        store.update(task_id, {"status": "done"})
        return text_response("OK\n")

    def _reopen(task_id: str):
        task = store.get(task_id)
        if not task:
            return text_response("ERROR 404: task not found\n", 404)
        store.update(task_id, {"status": "todo"})
        return text_response("OK\n")

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
        prefix + "/<task_id>",
        endpoint=ep + "_task", view_func=_task, methods=["GET", "POST", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<task_id>/subtasks",
        endpoint=ep + "_subtasks", view_func=_list_subtasks, methods=["GET"])
    flask_app.add_url_rule(
        prefix + "/<task_id>/subtasks",
        endpoint=ep + "_add_subtask", view_func=_add_subtask, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<task_id>/.done",
        endpoint=ep + "_done", view_func=_done, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<task_id>/.reopen",
        endpoint=ep + "_reopen", view_func=_reopen, methods=["POST"])
