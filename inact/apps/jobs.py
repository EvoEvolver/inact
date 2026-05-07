"""
Jobs system — track long-running background tasks and notify agents on completion.

mount_jobs(inact_app, prefix, storage, notify_store=None) registers:

  POST {prefix}              create a new job
                             body: {"title":"...","details":"...","notify_to":"agent_id"}
  GET  {prefix}              list all jobs  (?page=1&per_page=20 ?status=running)
  GET  {prefix}/{id}         get job details
  POST {prefix}/{id}/update  update status or details
                             body: {"status":"done","details":"..."}
  DELETE {prefix}/{id}       delete a job

Status values: pending, running, done, failed

When a job transitions to "done" or "failed" a push notification is sent to the
agent specified in notify_to (requires notify_store).
"""

from __future__ import annotations

import threading
import time

from flask import request

from ..storage import Storage
from ..utils import text_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS jobs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT    NOT NULL,
        status     TEXT    NOT NULL DEFAULT 'pending',
        details    TEXT    NOT NULL DEFAULT '',
        notify_to  TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL,
        updated_at BIGINT  NOT NULL
    )""",
]

_VALID_STATUSES = {"pending", "running", "done", "failed"}
_TERMINAL_STATUSES = {"done", "failed"}

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


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

class JobStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)

    def create(self, title: str, details: str = "", notify_to: str = "") -> dict:
        now = int(time.time())
        job_id = self._s.insert(
            "INSERT INTO jobs (title, status, details, notify_to, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (title, "pending", details, notify_to, now, now),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        return self._s.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def count(self, agent_id: str, status: str | None = None) -> int:
        where = "WHERE notify_to = ?"
        params: list = [agent_id]
        if status:
            where += " AND status = ?"
            params.append(status)
        row = self._s.fetchone(
            f"SELECT COUNT(*) AS cnt FROM jobs {where}", tuple(params)
        )
        return row["cnt"] if row else 0

    def list_jobs(self, agent_id: str, page: int, per_page: int,
                  status: str | None = None) -> list[dict]:
        offset = (page - 1) * per_page
        where = "WHERE notify_to = ?"
        params: list = [agent_id]
        if status:
            where += " AND status = ?"
            params.append(status)
        params += [per_page, offset]
        return self._s.fetchall(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params),
        )

    def update(self, job_id: str,
               status: str | None = None,
               details: str | None = None) -> dict | None:
        job = self.get(job_id)
        if not job:
            return None
        new_status  = status  if status  is not None else job["status"]
        new_details = details if details is not None else job["details"]
        self._s.execute(
            "UPDATE jobs SET status = ?, details = ?, updated_at = ? WHERE id = ?",
            (new_status, new_details, int(time.time()), job_id),
        )
        return self.get(job_id)

    def delete(self, job_id: str) -> bool:
        return self._s.execute("DELETE FROM jobs WHERE id = ?", (job_id,)) > 0


# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------

def _notify_completion(job: dict, notify_store, prefix: str) -> None:
    from .notify import _push
    to_id = job["notify_to"]
    msg = (
        f'Job "{job["title"]}" finished — status: {job["status"]}\n'
        f'id: {job["id"]}\n'
        + (f'details: {job["details"]}\n' if job["details"] else "")
        + f"GET {prefix}/{job['id']} for full details"
    )
    notif_id = notify_store.send(to_id, msg, from_id="jobs")
    _push(notify_store, to_id, notif_id, msg, from_id="jobs")


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_jobs(inact_app, prefix: str, store: JobStore,
                notify_store=None, registry=None) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_jobs_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _resolve_agent_id() -> str | None:
        """Infer agent_id from X-Api-Key / ?api_key= / _inact_key cookie via registry."""
        if registry is None:
            return None
        api_key = (
            request.headers.get("X-Api-Key", "")
            or request.args.get("api_key", "")
            or request.cookies.get("_inact_key", "")
        ).strip()
        if not api_key:
            return None
        agent = registry.get_by_key(api_key)
        return str(agent["id"]) if agent else None

    def _jobs():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            title     = (body.get("title")     or "").strip()
            details   = (body.get("details")   or "").strip()
            notify_to = str(body.get("notify_to") or "").strip()
            if not title:
                return text_response(
                    "ERROR 400: 'title' required\n"
                    f"POST {prefix}\n"
                    '  body: {"title":"...","details":"...","notify_to":"agent_id"}\n',
                    400,
                )
            job = store.create(title, details, notify_to)
            return text_response(
                f"OK\n"
                f"id         = {job['id']}\n"
                f"title      = {toml_str(job['title'])}\n"
                f"status     = {toml_str(job['status'])}\n"
                f"url        = {toml_str(prefix + '/' + str(job['id']))}\n",
                201,
            )
        # GET — list (scoped to the requesting agent)
        agent_id = _resolve_agent_id()
        if not agent_id:
            agent_id = (
                request.args.get("agent_id", "")
                or request.headers.get("X-Agent-Id", "")
            ).strip()
        if not agent_id:
            return text_response(
                "ERROR 400: agent_id required\n"
                f"Usage: GET {prefix}?agent_id=<id>\n"
                "       or set X-Api-Key header\n",
                400,
            )
        page, per_page = _parse_page_params()
        status_filter = request.args.get("status", "").strip() or None
        if status_filter and status_filter not in _VALID_STATUSES:
            return text_response(
                f"ERROR 400: invalid status {status_filter!r}\n"
                f"valid: {', '.join(sorted(_VALID_STATUSES))}\n",
                400,
            )
        total = store.count(agent_id, status_filter)
        jobs  = store.list_jobs(agent_id, page, per_page, status_filter)
        lines = [f"# Jobs (agent {agent_id})\n", _page_header(page, per_page, total)]
        if status_filter:
            lines.append(f"# filter: status={status_filter}\n")
        lines.append("# tip: ?status=pending|running|done|failed  POST here to create\n\n")
        for j in jobs:
            lines += [
                "[[jobs]]\n",
                f"id         = {j['id']}\n",
                f"title      = {toml_str(j['title'])}\n",
                f"status     = {toml_str(j['status'])}\n",
            ]
            if j["details"]:
                lines.append(f"details    = {toml_str(j['details'])}\n")
            lines += [
                f"created_at = {toml_str(_fmt_ts(j['created_at']))}\n",
                f"updated_at = {toml_str(_fmt_ts(j['updated_at']))}\n",
                f"url        = {toml_str(prefix + '/' + str(j['id']))}\n",
                "\n",
            ]
        return text_response("".join(lines))

    def _job(job_id: str):
        if request.method == "DELETE":
            ok = store.delete(job_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n",
                                 200 if ok else 404)
        j = store.get(job_id)
        if not j:
            return text_response("ERROR 404: job not found\n", 404)
        lines = [
            f"id         = {j['id']}\n",
            f"title      = {toml_str(j['title'])}\n",
            f"status     = {toml_str(j['status'])}\n",
        ]
        if j["details"]:
            lines.append(f"details    = {toml_str(j['details'])}\n")
        if j["notify_to"]:
            lines.append(f"notify_to  = {toml_str(j['notify_to'])}\n")
        lines += [
            f"created_at = {toml_str(_fmt_ts(j['created_at']))}\n",
            f"updated_at = {toml_str(_fmt_ts(j['updated_at']))}\n",
        ]
        return text_response("".join(lines))

    def _update(job_id: str):
        body    = request.get_json(force=True, silent=True) or {}
        status  = (body.get("status") or "").strip() or None
        details = body.get("details")
        if details is not None:
            details = str(details).strip()
        if status and status not in _VALID_STATUSES:
            return text_response(
                f"ERROR 400: invalid status {status!r}\n"
                f"valid: {', '.join(sorted(_VALID_STATUSES))}\n",
                400,
            )
        old_job = store.get(job_id)
        if not old_job:
            return text_response("ERROR 404: job not found\n", 404)
        job = store.update(job_id, status, details)
        # Push notification on first transition into a terminal status
        if (notify_store and job["notify_to"]
                and job["status"] in _TERMINAL_STATUSES
                and old_job["status"] not in _TERMINAL_STATUSES):
            threading.Thread(
                target=_notify_completion,
                args=(job, notify_store, prefix),
                daemon=True,
            ).start()
        return text_response(
            f"OK\n"
            f"id     = {job['id']}\n"
            f"status = {toml_str(job['status'])}\n"
        )

    flask_app.add_url_rule(
        prefix, endpoint=ep + "_jobs",
        view_func=_jobs, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/<job_id>", endpoint=ep + "_job",
        view_func=_job, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<job_id>/update", endpoint=ep + "_update",
        view_func=_update, methods=["POST"])


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_jobs(
    inact_app,
    prefix: str,
    storage,
    notify_store=None,
    registry=None,
) -> None:
    """
    Mount the jobs system at *prefix*.

    *storage*      — database URL/path or Storage instance.
    *notify_store* — NotifyStore instance; when supplied, agents listed in
                     a job's notify_to field are notified on completion.

    Example::

        from inact import make_storage, NotifyStore, mount_notify, mount_jobs

        db = make_storage("./app.db")
        notify_store = NotifyStore(db)
        mount_notify(app, "/notify", db)
        mount_jobs(app, "/jobs", db, notify_store=notify_store)
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    store = JobStore(backend)

    ns = None
    if notify_store is not None:
        from ..apps.notify import NotifyStore
        ns = notify_store if isinstance(notify_store, NotifyStore) \
             else NotifyStore(make_storage(notify_store) if isinstance(notify_store, str) else notify_store)

    _reg = None
    if registry is not None:
        from .workspace.register import AgentRegistry
        _reg = registry if isinstance(registry, AgentRegistry) \
               else AgentRegistry(make_storage(registry) if isinstance(registry, str) else registry)

    attach_jobs(inact_app, p, store, notify_store=ns, registry=_reg)
    inact_app._app_mounts.append((p, (
        f"\nJobs: {p}\n"
        f"  POST   {p}            create job  body: {{\"title\":\"...\",\"notify_to\":\"agent_id\"}}\n"
        f"  GET    {p}            list jobs   (?status=pending|running|done|failed)\n"
        f"  GET    {p}/{{id}}       job details\n"
        f"  POST   {p}/{{id}}/update  update status/details\n"
        f"  DELETE {p}/{{id}}       delete job\n"
        + (f"  # completion notifications via notify store\n" if notify_store else "")
    )))
