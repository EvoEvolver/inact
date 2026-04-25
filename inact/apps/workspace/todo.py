"""
Agent todo list — tasks with priorities, due dates, assignees, arbitrary nesting,
and per-task cron reminders.

mount_todo(prefix, storage) registers:

  GET    {prefix}/                          list root tasks (no parent)
  GET    {prefix}/?status=todo|done         filter by status
  GET    {prefix}/?priority=high            filter by priority
  GET    {prefix}/?assignee=alice           filter by assignee
  POST   {prefix}/                          create task
                                            body: {"title":"...","description":"...",
                                                   "priority":"normal","due":"YYYY-MM-DD",
                                                   "assignee":"...","parent_id":"optional-uuid"}
  GET    {prefix}/.today                    due today or overdue, not done (all levels)
  GET    {prefix}/.overdue                  past due, not done (all levels)
  GET    {prefix}/.unassigned               no assignee, not done (all levels)
  GET    {prefix}/{id}                      task detail + direct children
  POST   {prefix}/{id}                      update fields (title/description/status/priority/due/assignee)
  DELETE {prefix}/{id}                      delete task, all descendants, and all reminders
  GET    {prefix}/{id}/children             list direct children
  POST   {prefix}/{id}/.done                mark done
  POST   {prefix}/{id}/.reopen              reopen (status → todo)
  POST   {prefix}/{id}/.assign              set assignee  body: {"assignee":"alice"}

  POST   {prefix}/{id}/reminders            add a cron reminder
                                            body: {"url":"https://...","schedule":"* * * * *",
                                                   "label":"...","body":"..."}
  GET    {prefix}/{id}/reminders            list reminders for task
  DELETE {prefix}/{id}/reminders/{rid}      delete a reminder
  POST   {prefix}/{id}/reminders/{rid}/.run fire reminder immediately
  GET    {prefix}/{id}/reminders/{rid}/runs run history (last 50)

Reminder schedule — standard 5-field cron:
  *  *  *  *  *
  |  |  |  |  +-- day-of-week  (0=Sunday … 6=Saturday)
  |  |  |  +----- month        (1-12)
  |  |  +-------- day-of-month (1-31)
  |  +----------- hour         (0-23)
  +-------------- minute       (0-59)

  Supports: *, ranges (1-5), lists (1,3,5), steps (*/15, 8-17/2).

  When a reminder fires the scheduler sends an HTTP POST to the reminder URL.
  Headers: X-Inact-Task-Id, X-Inact-Reminder-Id, X-Inact-Reminder-Label.
  Body: the custom body if set, otherwise a TOML summary of the task.

Priority levels: low | normal | high | urgent
Status values:   todo | done
Listings sorted by priority desc, then due date asc (no-due last).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from flask import request

from ...storage import Storage
from ...utils import text_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS tasks (
        id          INTEGER PRIMARY KEY,
        parent_id   INTEGER,
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
    """CREATE TABLE IF NOT EXISTS reminders (
        id         INTEGER PRIMARY KEY,
        task_id    INTEGER NOT NULL,
        url        TEXT    NOT NULL,
        schedule   TEXT    NOT NULL,
        label      TEXT    NOT NULL DEFAULT '',
        body       TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL,
        last_run   BIGINT,
        next_run   BIGINT  NOT NULL,
        enabled    INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS reminder_runs (
        id          INTEGER PRIMARY KEY,
        reminder_id INTEGER NOT NULL,
        ran_at      BIGINT  NOT NULL,
        status      INTEGER NOT NULL,
        output      TEXT    NOT NULL DEFAULT ''
    )""",
]

_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN assignee TEXT NOT NULL DEFAULT ''",
]

_VALID_STATUS   = frozenset({"todo", "done"})
_VALID_PRIORITY = frozenset({"low", "normal", "high", "urgent"})
_PRIORITY_RANK  = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
_POLL = 10  # seconds between scheduler ticks


# ---------------------------------------------------------------------------
# Cron expression parser
# ---------------------------------------------------------------------------

def _next_run(schedule: str, after: float) -> float:
    parts = schedule.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"Expected 5-field cron expression (min hr dom mon dow), got: {schedule!r}"
        )
    minutes_f, hours_f, doms_f, months_f, dows_f = parts

    def expand(field: str, lo: int, hi: int) -> frozenset[int]:
        result: set[int] = set()
        for part in field.split(","):
            if part == "*":
                result.update(range(lo, hi + 1))
            elif "/" in part:
                base, step_s = part.split("/", 1)
                step = int(step_s)
                start = lo if base == "*" else int(base)
                result.update(range(start, hi + 1, step))
            elif "-" in part:
                a, b = part.split("-", 1)
                result.update(range(int(a), int(b) + 1))
            else:
                result.add(int(part))
        return frozenset(result)

    minutes = expand(minutes_f, 0, 59)
    hours   = expand(hours_f,   0, 23)
    doms    = expand(doms_f,    1, 31)
    months  = expand(months_f,  1, 12)
    dows    = expand(dows_f,    0, 6)

    dom_star = doms_f.strip() == "*"
    dow_star = dows_f.strip() == "*"

    dt = datetime.fromtimestamp(after, tz=timezone.utc).replace(second=0, microsecond=0)
    dt += timedelta(minutes=1)

    for _ in range(366 * 24 * 60 * 4):
        cron_dow = (dt.weekday() + 1) % 7  # Python Mon=0 → cron Sun=0
        dom_ok = dt.day   in doms
        dow_ok = cron_dow in dows

        if dom_star and dow_star:
            day_ok = True
        elif dom_star:
            day_ok = dow_ok
        elif dow_star:
            day_ok = dom_ok
        else:
            day_ok = dom_ok or dow_ok  # Unix OR semantics

        if dt.month in months and day_ok and dt.hour in hours and dt.minute in minutes:
            return dt.timestamp()

        dt += timedelta(minutes=1)

    raise ValueError(f"No next occurrence found for schedule {schedule!r}")


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _sort_key(t: dict):
    return (
        _PRIORITY_RANK.get(t["priority"], 2),
        t["due"] or "9999-99-99",
        t["created_at"],
    )


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
                pass

    def create(self, title: str, description: str = "",
               priority: str = "normal", due: str | None = None,
               assignee: str = "", parent_id: str | None = None) -> str:
        now = int(time.time())
        new_id = self._s.insert(
            "INSERT INTO tasks (parent_id, title, description, status, priority, due, assignee,"
            " created_at, updated_at, done_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (parent_id, title, description, "todo", priority, due, assignee, now, now, None),
        )
        return str(new_id)

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
        rids = [r["id"] for r in self._s.fetchall(
            "SELECT id FROM reminders WHERE task_id=?", (task_id,)
        )]
        for rid in rids:
            self._s.execute("DELETE FROM reminder_runs WHERE reminder_id=?", (rid,))
        self._s.execute("DELETE FROM reminders WHERE task_id=?", (task_id,))
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

    # Reminder CRUD (scheduler calls these too)

    def add_reminder(self, task_id: str, url: str, schedule: str,
                     label: str = "", body: str = "") -> str:
        first_next = int(_next_run(schedule, time.time()))
        new_id = self._s.insert(
            "INSERT INTO reminders (task_id, url, schedule, label, body, created_at, last_run,"
            " next_run, enabled) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, url, schedule, label, body, int(time.time()), None, first_next, 1),
        )
        return str(new_id)

    def list_reminders(self, task_id: str) -> list[dict]:
        return self._s.fetchall(
            "SELECT * FROM reminders WHERE task_id=? ORDER BY next_run ASC", (task_id,)
        )

    def get_reminder(self, rid: str) -> dict | None:
        return self._s.fetchone("SELECT * FROM reminders WHERE id=?", (rid,))

    def delete_reminder(self, rid: str) -> bool:
        self._s.execute("DELETE FROM reminder_runs WHERE reminder_id=?", (rid,))
        return self._s.execute("DELETE FROM reminders WHERE id=?", (rid,)) > 0

    def reminder_runs(self, rid: str) -> list[dict]:
        return self._s.fetchall(
            "SELECT * FROM reminder_runs WHERE reminder_id=? ORDER BY ran_at DESC LIMIT 50",
            (rid,),
        )

    def last_reminder_run(self, rid: str) -> dict | None:
        rows = self._s.fetchall(
            "SELECT * FROM reminder_runs WHERE reminder_id=? ORDER BY ran_at DESC LIMIT 1",
            (rid,),
        )
        return rows[0] if rows else None

    def record_reminder_run(self, rid: str, schedule: str, ran_at: int,
                            status: int, output: str) -> None:
        next_t = int(_next_run(schedule, ran_at))
        self._s.batch([
            ("INSERT INTO reminder_runs (reminder_id, ran_at, status, output) VALUES (?,?,?,?)",
             (rid, ran_at, status, output)),
            ("UPDATE reminders SET last_run=?, next_run=? WHERE id=?",
             (ran_at, next_t, rid)),
        ])


# ---------------------------------------------------------------------------
# Reminder scheduler (background thread)
# ---------------------------------------------------------------------------

class ReminderScheduler:
    def __init__(self, store: TodoStore):
        self._store = store
        self._s = store._s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="inact-reminders"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(timeout=_POLL):
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self) -> None:
        now = int(time.time())
        due = self._s.fetchall(
            "SELECT * FROM reminders WHERE enabled=1 AND next_run <= ?", (now,)
        )
        for rem in due:
            self.fire(rem)

    def fire(self, reminder: dict) -> dict:
        task = self._store.get(reminder["task_id"])
        ran_at = int(time.time())
        payload = reminder["body"] or (_task_toml_brief(task) if task else "")
        try:
            resp = httpx.post(
                reminder["url"],
                content=payload.encode(),
                headers={
                    "Content-Type": "text/plain",
                    "X-Inact-Task-Id":        reminder["task_id"],
                    "X-Inact-Reminder-Id":    reminder["id"],
                    "X-Inact-Reminder-Label": reminder["label"] or "",
                },
                timeout=30,
            )
            status = resp.status_code
            output = resp.text[:500]
        except Exception as exc:
            status = 0
            output = str(exc)[:500]

        self._store.record_reminder_run(
            reminder["id"], reminder["schedule"], ran_at, status, output
        )
        return {"status": status, "output": output}

    def fire_by_id(self, rid: str) -> dict | None:
        rem = self._store.get_reminder(rid)
        if not rem:
            return None
        return self.fire(rem)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _fmt_ts_or_never(ts: int | None) -> str:
    if ts is None:
        return "never"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _is_overdue(task: dict) -> bool:
    return bool(task["due"]) and task["due"] < _today_str() and task["status"] != "done"


def _task_toml_brief(task: dict) -> str:
    lines = [
        f"id       = {toml_str(str(task['id']))}\n",
        f"title    = {toml_str(task['title'])}\n",
        f"status   = {toml_str(task['status'])}\n",
        f"priority = {toml_str(task['priority'])}\n",
    ]
    if task.get("due"):
        lines.append(f"due      = {toml_str(task['due'])}\n")
    if task.get("assignee"):
        lines.append(f"assignee = {toml_str(task['assignee'])}\n")
    return "".join(lines)


def _task_row_toml(task: dict, prefix: str,
                   total_children: int = 0, done_children: int = 0,
                   assignee_name: str = "") -> str:
    lines = [
        "[[tasks]]\n",
        f"id       = {toml_str(str(task['id']))}\n",
        f"title    = {toml_str(task['title'])}\n",
        f"status   = {toml_str(task['status'])}\n",
        f"priority = {toml_str(task['priority'])}\n",
    ]
    if task.get("assignee"):
        lines.append(f"assignee      = {toml_str(task['assignee'])}\n")
        if assignee_name:
            lines.append(f"assignee_name = {toml_str(assignee_name)}\n")
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


def _task_detail(task: dict, children: list[dict], prefix: str,
                 reminders: list[dict], assignee_name: str = "") -> str:
    tid = str(task["id"])
    lines = [f"# {task['title']}{'  [OVERDUE]' if _is_overdue(task) else ''}\n\n"]
    lines.append(f"id          = {toml_str(tid)}\n")
    lines.append(f"title       = {toml_str(task['title'])}\n")
    if task["description"]:
        lines.append(f"description = {toml_str(task['description'])}\n")
    lines.append(f"status      = {toml_str(task['status'])}\n")
    lines.append(f"priority    = {toml_str(task['priority'])}\n")
    lines.append(f"assignee    = {toml_str(task.get('assignee') or '')}\n")
    if task.get("assignee") and assignee_name:
        lines.append(f"assignee_name = {toml_str(assignee_name)}\n")
    if task["due"]:
        lines.append(f"due         = {toml_str(task['due'])}\n")
    if task["parent_id"]:
        lines.append(f"parent      = {toml_str(prefix + '/' + str(task['parent_id']))}\n")
    lines.append(f"created_at  = {toml_str(_fmt_ts(task['created_at']))}\n")
    lines.append(f"updated_at  = {toml_str(_fmt_ts(task['updated_at']))}\n")
    if task["done_at"]:
        lines.append(f"done_at     = {toml_str(_fmt_ts(task['done_at']))}\n")
    lines.append(f"children    = {toml_str(prefix + '/' + tid + '/children')}\n")
    lines.append(f"reminders   = {toml_str(prefix + '/' + tid + '/reminders')}\n")
    lines.append("\n")

    if children:
        lines.append(f"# Children ({len(children)})\n\n")
        for child in children:
            cid = str(child["id"])
            lines.append("[[children]]\n")
            lines.append(f"id       = {toml_str(cid)}\n")
            lines.append(f"title    = {toml_str(child['title'])}\n")
            lines.append(f"status   = {toml_str(child['status'])}\n")
            lines.append(f"priority = {toml_str(child['priority'])}\n")
            if child.get("assignee"):
                lines.append(f"assignee = {toml_str(child['assignee'])}\n")
            if child["due"]:
                lines.append(f"due      = {toml_str(child['due'])}\n")
                if _is_overdue(child):
                    lines.append("overdue  = true\n")
            lines.append(f"url      = {toml_str(prefix + '/' + cid)}\n")
            lines.append(f"children = {toml_str(prefix + '/' + cid + '/children')}\n")
            lines.append("\n")

    if reminders:
        lines.append(f"# Reminders ({len(reminders)})\n\n")
        for r in reminders:
            rid_str = str(r["id"])
            lines.append("[[reminders]]\n")
            lines.append(f"id       = {toml_str(rid_str)}\n")
            lines.append(f"label    = {toml_str(r['label'])}\n")
            lines.append(f"schedule = {toml_str(r['schedule'])}\n")
            lines.append(f"next_run = {toml_str(_fmt_ts_or_never(r['next_run']))}\n")
            lines.append(f"last_run = {toml_str(_fmt_ts_or_never(r['last_run']))}\n")
            lines.append(f"url      = {toml_str(r['url'])}\n")
            lines.append(f"fire     = {toml_str(prefix + '/' + tid + '/reminders/' + rid_str + '/.run')}\n")
            lines.append("\n")

    return "".join(lines)


def _reminder_toml(r: dict, prefix: str, task_id: str) -> str:
    base = f"{prefix}/{task_id}/reminders/{r['id']}"
    return (
        "[[reminders]]\n"
        f"id       = {toml_str(str(r['id']))}\n"
        f"label    = {toml_str(r['label'])}\n"
        f"schedule = {toml_str(r['schedule'])}\n"
        f"next_run = {toml_str(_fmt_ts_or_never(r['next_run']))}\n"
        f"last_run = {toml_str(_fmt_ts_or_never(r['last_run']))}\n"
        f"url      = {toml_str(r['url'])}\n"
        f"enabled  = {str(bool(r['enabled'])).lower()}\n"
        f"fire     = {toml_str(base + '/.run')}\n"
        f"runs     = {toml_str(base + '/runs')}\n"
        "\n"
    )


def _run_toml(run: dict) -> str:
    ok = 200 <= run["status"] < 300
    return (
        "[[runs]]\n"
        f"id     = {toml_str(str(run['id']))}\n"
        f"ran_at = {toml_str(_fmt_ts_or_never(run['ran_at']))}\n"
        f"status = {run['status']}\n"
        f"ok     = {str(ok).lower()}\n"
        f"output = {toml_str(run['output'])}\n"
        "\n"
    )


def _parse_create_body(body: dict,
                       lookup_agent=None) -> tuple[str, dict] | tuple[None, str]:
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
    assignee = (body.get("assignee") or "").strip()
    if assignee and lookup_agent is not None:
        if lookup_agent(assignee) is None:
            return None, f"'assignee' {assignee!r} is not a registered agent id"
    return title, {
        "description": (body.get("description") or "").strip(),
        "priority": priority,
        "due": due,
        "assignee": assignee,
    }


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_todo(inact_app, prefix: str, store: TodoStore,
                scheduler: ReminderScheduler,
                agents_prefix: str = "/agents",
                lookup_agent=None,
                notify_fn=None) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_todo_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _name(agent_id: str) -> str:
        """Resolve agent ID → display name, empty string if unknown."""
        if not agent_id or lookup_agent is None:
            return ""
        agent = lookup_agent(agent_id)
        return (agent.get("name") or f"Agent #{agent_id}") if agent else ""

    def _notify_assign(assignee_id: str, task_id: str, task_title: str) -> None:
        if notify_fn and assignee_id:
            notify_fn(assignee_id, "tasks",
                      f"[task:{task_id}] Assigned to you: {task_title}")

    def _root():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            title, result = _parse_create_body(body, lookup_agent=lookup_agent)
            if title is None:
                return text_response(
                    f"ERROR 400: {result}\n"
                    f"POST {prefix}/\n"
                    '  Body: {"title":"...","description":"...","priority":"normal",\n'
                    '         "due":"YYYY-MM-DD","assignee":"<agent_id>","parent_id":"optional-uuid"}\n'
                    f"\nPriority: low | normal | high | urgent\n"
                    f"assignee: integer agent id from {agents_prefix}/\n",
                    400,
                )
            parent_id = (body.get("parent_id") or "").strip() or None
            if parent_id and not store.get(parent_id):
                return text_response(f"ERROR 404: parent task {parent_id!r} not found\n", 404)
            task_id = store.create(title, parent_id=parent_id, **result)
            _notify_assign(result["assignee"], task_id, title)
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
            "\n# tip: ?status=todo|done  ?priority=low|normal|high|urgent  ?assignee=<agent_id>\n\n"
        )
        for t in tasks:
            total, done = store.child_counts(t["id"])
            lines.append(_task_row_toml(t, prefix, total, done,
                                        assignee_name=_name(t.get("assignee", ""))))
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
        reminders = store.list_reminders(task_id)
        aname = _name(task.get("assignee", ""))
        return text_response(
            _task_detail(task, store.children(task_id), prefix, reminders, assignee_name=aname)
        )

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
        task = store.get(task_id)
        if not task:
            return text_response("ERROR 404: task not found\n", 404)
        body = request.get_json(force=True, silent=True) or {}
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
            aname = agent.get("name") or f"Agent #{assignee}"
        else:
            aname = assignee
        old_assignee = task.get("assignee", "")
        store.update(task_id, {"assignee": assignee})
        if assignee != old_assignee:
            _notify_assign(assignee, task_id, task["title"])
        return text_response(
            f"OK\nassignee      = {toml_str(assignee)}\n"
            f"assignee_name = {toml_str(aname)}\n"
        )

    def _reminders(task_id: str):
        task = store.get(task_id)
        if not task:
            return text_response("ERROR 404: task not found\n", 404)

        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            url      = (body.get("url")      or "").strip()
            schedule = (body.get("schedule") or "").strip()
            label    = (body.get("label")    or "").strip()
            payload  = (body.get("body")     or "").strip()
            if not url:
                return text_response(
                    "ERROR 400: 'url' required\n"
                    f"POST {prefix}/{task_id}/reminders\n"
                    '  Body: {"url":"https://...","schedule":"* * * * *","label":"...","body":"..."}\n'
                    "\nSchedule: 5-field cron  min hr dom mon dow\n"
                    'Examples: "*/30 * * * *"  every 30 min\n'
                    '          "0 9 * * 1"    every Monday 09:00 UTC\n',
                    400,
                )
            if not schedule:
                return text_response("ERROR 400: 'schedule' required\n", 400)
            try:
                rid = store.add_reminder(task_id, url, schedule, label, payload)
            except ValueError as exc:
                return text_response(f"ERROR 400: invalid schedule — {exc}\n", 400)
            base = f"{prefix}/{task_id}/reminders/{rid}"
            return text_response(
                f"OK\n"
                f"id   = {toml_str(rid)}\n"
                f"fire = {toml_str(base + '/.run')}\n"
                f"runs = {toml_str(base + '/runs')}\n"
            )

        rems = store.list_reminders(task_id)
        lines = [f"# Reminders: {task['title']}\n# {len(rems)} reminder(s)\n\n"]
        for r in rems:
            lines.append(_reminder_toml(r, prefix, task_id))
        return text_response("".join(lines))

    def _reminder(task_id: str, rid: str):
        if not store.get(task_id):
            return text_response("ERROR 404: task not found\n", 404)
        if request.method == "DELETE":
            ok = store.delete_reminder(rid)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)
        rem = store.get_reminder(rid)
        if not rem:
            return text_response("ERROR 404: reminder not found\n", 404)
        return text_response(_reminder_toml(rem, prefix, task_id))

    def _fire(task_id: str, rid: str):
        if not store.get(task_id):
            return text_response("ERROR 404: task not found\n", 404)
        result = scheduler.fire_by_id(rid)
        if result is None:
            return text_response("ERROR 404: reminder not found\n", 404)
        return text_response(
            f"OK\nstatus = {result['status']}\noutput = {toml_str(result['output'])}\n"
        )

    def _runs(task_id: str, rid: str):
        if not store.get(task_id):
            return text_response("ERROR 404: task not found\n", 404)
        rem = store.get_reminder(rid)
        if not rem:
            return text_response("ERROR 404: reminder not found\n", 404)
        runs = store.reminder_runs(rid)
        lines = [
            f"# Reminder runs: {rem['label'] or rid}\n",
            f"# {len(runs)} run(s) (last 50)\n\n",
        ]
        for r in runs:
            lines.append(_run_toml(r))
        return text_response("".join(lines))

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
    flask_app.add_url_rule(
        prefix + "/<task_id>/reminders",
        endpoint=ep + "_reminders", view_func=_reminders, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/<task_id>/reminders/<rid>",
        endpoint=ep + "_reminder", view_func=_reminder, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<task_id>/reminders/<rid>/.run",
        endpoint=ep + "_fire", view_func=_fire, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<task_id>/reminders/<rid>/runs",
        endpoint=ep + "_runs", view_func=_runs)

    def _human(path: str):
        from inact.render import render_template
        from inact.utils import html_response
        from inact.render import workspace_nav
        return html_response(render_template("todo_human.html",
            title="Tasks", prefix=prefix, agents_prefix=agents_prefix,
            nav="", pills=[],
            workspace_links=workspace_nav("/_human/tasks/"),
            show_identity=True))

    inact_app._human_views[prefix] = _human


def mount_todo(inact_app, prefix: str, storage,
               agents_prefix: str = "/agents",
               agents_storage=None,
               notify_storage=None) -> None:
    """
    Mount a todo list at *prefix* with cron-based per-task reminders.

    *storage*        — database URL/path or Storage instance for tasks.
    *agents_prefix*  — prefix where the agent registry is mounted (for assignee validation).
    *agents_storage* — if provided, assignee is validated as a registered agent id.
    *notify_storage* — if provided, agents receive a notification when assigned.

    Example::

        mount_todo(app, "/tasks", "./tasks.db",
                   agents_storage="./agents.db",
                   notify_storage="./notify.db")
    """
    from ...storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    store = TodoStore(backend)
    scheduler = ReminderScheduler(store)
    scheduler.start()

    lookup_agent = None
    if agents_storage is not None:
        from .register import AgentRegistry
        ag_back = make_storage(agents_storage) if isinstance(agents_storage, str) else agents_storage
        ag_reg = AgentRegistry(ag_back)
        def lookup_agent(agent_id: str) -> dict | None:
            try:
                return ag_reg.get(int(agent_id))
            except (ValueError, TypeError):
                return None

    notify_fn = None
    if notify_storage is not None:
        from ..notify import NotifyStore, _push
        ns_back = make_storage(notify_storage) if isinstance(notify_storage, str) else notify_storage
        nstore = NotifyStore(ns_back)
        def notify_fn(to_id: str, from_id: str, message: str) -> None:
            notif_id = nstore.send(to_id, message, from_id)
            _push(nstore, to_id, notif_id, message, from_id)

    attach_todo(inact_app, p, store, scheduler,
                agents_prefix="/" + agents_prefix.strip("/"),
                lookup_agent=lookup_agent,
                notify_fn=notify_fn)
    inact_app._app_mounts.append((p, (
        f"\nTodo: {p}\n"
        f"  GET    {p}/                           list tasks  (?status=todo|done  ?priority=high  ?assignee=name)\n"
        f"  POST   {p}/                           create task\n"
        f"  GET    {p}/.today                     due today or overdue\n"
        f"  GET    {p}/.overdue                   past due, not done\n"
        f"  GET    {p}/.unassigned                no assignee, not done\n"
        f"  GET    {p}/{{id}}                       task detail + children + reminders\n"
        f"  POST   {p}/{{id}}                       update fields\n"
        f"  DELETE {p}/{{id}}                       delete task + reminders\n"
        f"  POST   {p}/{{id}}/.done                 mark done\n"
        f"  POST   {p}/{{id}}/.reopen               reopen\n"
        f"  POST   {p}/{{id}}/.assign               set assignee\n"
        f"  GET    {p}/{{id}}/reminders             list reminders\n"
        f"  POST   {p}/{{id}}/reminders             add reminder\n"
        f"  DELETE {p}/{{id}}/reminders/{{rid}}       delete reminder\n"
        f"  POST   {p}/{{id}}/reminders/{{rid}}/.run  fire reminder now\n"
        f"  GET    {p}/{{id}}/reminders/{{rid}}/runs  run history\n"
    )))
