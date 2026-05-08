"""
Jobs system — unified, extensible store for long-running tasks.

mount_jobs(inact_app, prefix, storage, notify_store=None) registers:

  POST {prefix}              create a new job
                             body: {"title":"...","notify_to":"agent_id",
                                    "metadata":{...},"kind":"generic",
                                    "backend":""}
  GET  {prefix}              list jobs  (X-Agent-Id or ?agent_id=)
                             ?status=  ?kind=  ?page=  ?per_page=
  GET  {prefix}/{id}         get job details
  POST {prefix}/{id}/update  update status / merge metadata / set result
                             body: {"status":"done","metadata":{...},
                                    "exit_code":0,"reason":"...","result":{...}}
  DELETE {prefix}/{id}       delete a job

  GET  {prefix}/{id}/logs    read job logs (?stream=stdout|stderr|...
                             &since_seq=N)
  POST {prefix}/{id}/logs    append a log chunk
                             body: {"stream":"stdout","content":"..."}

Status values: pending, running, done, failed, cancelled
Terminal: done, failed, cancelled

JobStore is the canonical store for any long-running task. Extra job kinds
(slurm, gpu, …) set ``kind`` and stash kind-specific spec/state under
``metadata``. Hot-path columns (worker_id, cancel_requested, exit_code,
…) are promoted out of metadata for indexed queries.

When a job transitions to a terminal status, a push notification fires to
the agent in notify_to (requires notify_store).
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from fastapi import Request

from ..storage import Storage
from ..utils import text_response, toml_str, _body

_DDL = [
    """CREATE TABLE IF NOT EXISTS jobs (
        id               INTEGER    PRIMARY KEY AUTOINCREMENT,
        kind             TEXT    NOT NULL DEFAULT 'generic',
        backend          TEXT    NOT NULL DEFAULT '',
        title            TEXT    NOT NULL DEFAULT '',
        status           TEXT    NOT NULL DEFAULT 'pending',
        notify_to        TEXT    NOT NULL DEFAULT '',
        worker_id        TEXT    NOT NULL DEFAULT '',
        metadata_json    TEXT    NOT NULL DEFAULT '{}',
        exit_code        INTEGER,
        reason           TEXT    NOT NULL DEFAULT '',
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        cancel_acked     INTEGER NOT NULL DEFAULT 0,
        created_at       BIGINT  NOT NULL,
        updated_at       BIGINT  NOT NULL,
        submitted_at     BIGINT,
        finished_at      BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS job_logs (
        id          TEXT    PRIMARY KEY,
        job_id      TEXT    NOT NULL,
        stream      TEXT    NOT NULL,
        seq         INTEGER NOT NULL,
        content     TEXT    NOT NULL,
        created_at  BIGINT  NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_kind_status   ON jobs(kind, status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_kind_backend  ON jobs(kind, backend, status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_notify_to     ON jobs(notify_to)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_worker        ON jobs(worker_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_job_logs_owner     ON job_logs(job_id, stream, seq)",
]

VALID_STATUSES   = {"pending", "running", "done", "failed", "cancelled"}
TERMINAL_STATUSES = {"done", "failed", "cancelled"}

_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100


def _now() -> int:
    return int(time.time())


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_page_params(request: Request) -> tuple[int, int]:
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(_MAX_PER_PAGE, max(1, int(request.query_params.get("per_page", _DEFAULT_PER_PAGE))))
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

    # ---- helpers ----

    @staticmethod
    def _hydrate(row: dict | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        try:
            out["metadata"] = json.loads(out.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            out["metadata"] = {}
        return out

    # ---- CRUD ----

    def create(self, *, title: str = "", kind: str = "generic",
               backend: str = "", notify_to: str = "",
               metadata: dict | None = None) -> dict:
        kind = (kind or "generic").strip() or "generic"
        backend = (backend or "").strip()
        meta_json = json.dumps(metadata or {})
        now = _now()
        job_id = self._s.insert(
            "INSERT INTO jobs (kind, backend, title, status, notify_to, "
            "metadata_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, backend, title, "pending", notify_to,
             meta_json, now, now),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        return self._hydrate(
            self._s.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        )

    def delete(self, job_id: str) -> bool:
        self._s.execute("DELETE FROM job_logs WHERE job_id = ?", (job_id,))
        return self._s.execute("DELETE FROM jobs WHERE id = ?", (job_id,)) > 0

    # ---- listing / counting ----

    def _where(self, *, kind=None, backend=None, status=None,
               notify_to=None, worker_id=None) -> tuple[str, list]:
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind = ?"); params.append(kind)
        if backend is not None:
            clauses.append("backend = ?"); params.append(backend)
        if status is not None:
            clauses.append("status = ?"); params.append(status)
        if notify_to is not None:
            clauses.append("notify_to = ?"); params.append(notify_to)
        if worker_id is not None:
            clauses.append("worker_id = ?"); params.append(worker_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def count(self, *, kind=None, backend=None, status=None, notify_to=None,
              worker_id=None) -> int:
        where, params = self._where(kind=kind, backend=backend, status=status,
                                    notify_to=notify_to, worker_id=worker_id)
        row = self._s.fetchone(
            f"SELECT COUNT(*) AS cnt FROM jobs {where}", tuple(params)
        )
        return row["cnt"] if row else 0

    def list(self, *, kind=None, backend=None, status=None, notify_to=None,
             worker_id=None, page: int = 1, per_page: int = 20
             ) -> tuple[list[dict], int]:
        where, params = self._where(kind=kind, backend=backend, status=status,
                                    notify_to=notify_to, worker_id=worker_id)
        total = self.count(kind=kind, backend=backend, status=status,
                           notify_to=notify_to, worker_id=worker_id)
        params2 = list(params) + [per_page, (page - 1) * per_page]
        rows = self._s.fetchall(
            f"SELECT * FROM jobs {where} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?", tuple(params2)
        )
        return [self._hydrate(r) for r in rows], total

    # ---- mutations ----

    def update_status(self, job_id: str, status: str, *,
                      exit_code: int | None = None,
                      reason: str | None = None,
                      result: dict | None = None) -> dict | None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}")
        job = self.get(job_id)
        if not job:
            return None
        sets = ["status = ?", "updated_at = ?"]
        params: list = [status, _now()]
        if exit_code is not None:
            sets.append("exit_code = ?"); params.append(int(exit_code))
        if reason is not None:
            sets.append("reason = ?"); params.append(reason)
        if status in TERMINAL_STATUSES and not job.get("finished_at"):
            sets.append("finished_at = ?"); params.append(_now())
        if result is not None:
            meta = dict(job["metadata"])
            meta_result = dict(meta.get("result") or {})
            meta_result.update(result)
            meta["result"] = meta_result
            sets.append("metadata_json = ?"); params.append(json.dumps(meta))
        params.append(job_id)
        self._s.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        return self.get(job_id)

    def merge_metadata(self, job_id: str, patch: dict) -> dict | None:
        job = self.get(job_id)
        if not job:
            return None
        meta = dict(job["metadata"])
        meta.update(patch or {})
        self._s.execute(
            "UPDATE jobs SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), _now(), job_id),
        )
        return self.get(job_id)

    def set_metadata(self, job_id: str, metadata: dict) -> dict | None:
        if not self.get(job_id):
            return None
        self._s.execute(
            "UPDATE jobs SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(metadata or {}), _now(), job_id),
        )
        return self.get(job_id)

    def set_worker(self, job_id: str, worker_id: str) -> dict | None:
        if not self.get(job_id):
            return None
        self._s.execute(
            "UPDATE jobs SET worker_id = ?, updated_at = ? WHERE id = ?",
            (worker_id, _now(), job_id),
        )
        return self.get(job_id)

    def set_submitted(self, job_id: str,
                      submitted_at: int | None = None) -> dict | None:
        if not self.get(job_id):
            return None
        ts = submitted_at if submitted_at is not None else _now()
        self._s.execute(
            "UPDATE jobs SET submitted_at = ?, updated_at = ? WHERE id = ?",
            (ts, _now(), job_id),
        )
        return self.get(job_id)

    def request_cancel(self, job_id: str) -> dict | None:
        if not self.get(job_id):
            return None
        self._s.execute(
            "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
            (_now(), job_id),
        )
        return self.get(job_id)

    def ack_cancel(self, job_id: str) -> dict | None:
        if not self.get(job_id):
            return None
        self._s.execute(
            "UPDATE jobs SET cancel_acked = 1, updated_at = ? WHERE id = ?",
            (_now(), job_id),
        )
        return self.get(job_id)

    # ---- worker helpers ----

    def claim_next(self, *, kind: str, worker_id: str,
                   backend: str | None = None) -> dict | None:
        sql = ("SELECT * FROM jobs WHERE kind = ? AND status = 'pending' "
               "AND worker_id = '' AND cancel_requested = 0")
        params: list = [kind]
        if backend is not None:
            sql += " AND backend = ?"
            params.append(backend)
        sql += " ORDER BY created_at ASC LIMIT 1"
        row = self._s.fetchone(sql, tuple(params))
        if not row:
            return None
        n = self._s.execute(
            "UPDATE jobs SET worker_id = ?, updated_at = ? "
            "WHERE id = ? AND worker_id = ''",
            (worker_id, _now(), row["id"]),
        )
        if n == 0:
            return None
        return self.get(row["id"])

    def list_active(self, *, kind: str, worker_id: str,
                    backend: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM jobs WHERE kind = ? AND worker_id = ? "
               "AND status NOT IN ('done','failed','cancelled')")
        params: list = [kind, worker_id]
        if backend is not None:
            sql += " AND backend = ?"
            params.append(backend)
        sql += " ORDER BY created_at ASC"
        rows = self._s.fetchall(sql, tuple(params))
        return [self._hydrate(r) for r in rows]

    def list_pending_cancels(self, *, kind: str, worker_id: str,
                             backend: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM jobs WHERE kind = ? AND worker_id = ? "
               "AND cancel_requested = 1 AND cancel_acked = 0")
        params: list = [kind, worker_id]
        if backend is not None:
            sql += " AND backend = ?"
            params.append(backend)
        rows = self._s.fetchall(sql, tuple(params))
        return [self._hydrate(r) for r in rows]

    # ---- logs ----

    def append_log(self, job_id: str, stream: str, content: str) -> int:
        seq_row = self._s.fetchone(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM job_logs "
            "WHERE job_id = ? AND stream = ?",
            (job_id, stream),
        )
        seq = (seq_row["m"] if seq_row else 0) + 1
        self._s.execute(
            "INSERT INTO job_logs (id, job_id, stream, seq, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, job_id, stream, seq, content, _now()),
        )
        return seq

    def read_logs(self, job_id: str, *, stream: str | None = None,
                  since_seq: int = 0,
                  limit: int | None = None) -> list[dict]:
        clauses = ["job_id = ?"]
        params: list = [job_id]
        if stream is not None:
            clauses.append("stream = ?"); params.append(stream)
        if since_seq:
            clauses.append("seq > ?"); params.append(int(since_seq))
        where = "WHERE " + " AND ".join(clauses)
        sql = (f"SELECT * FROM job_logs {where} "
               "ORDER BY stream ASC, seq ASC")
        if limit:
            sql += " LIMIT ?"; params.append(int(limit))
        return self._s.fetchall(sql, tuple(params))

    def tail_logs(self, job_id: str, *, stream: str | None = None,
                  n: int = 100) -> list[dict]:
        clauses = ["job_id = ?"]
        params: list = [job_id]
        if stream is not None:
            clauses.append("stream = ?"); params.append(stream)
        where = "WHERE " + " AND ".join(clauses)
        rows = self._s.fetchall(
            f"SELECT * FROM job_logs {where} "
            "ORDER BY seq DESC LIMIT ?",
            tuple(params + [int(n)]),
        )
        return list(reversed(rows))


# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------

def _notify_completion(job: dict, notify_store, prefix: str) -> None:
    from .notify import _push
    to_id = job["notify_to"]
    title = job.get("title") or job["id"]
    msg = (
        f'Job "{title}" finished — status: {job["status"]}\n'
        f'id: {job["id"]}\n'
        + (f'reason: {job["reason"]}\n' if job.get("reason") else "")
        + f"GET {prefix}/{job['id']} for full details"
    )
    notif_id = notify_store.send(to_id, msg, from_id="jobs")
    _push(notify_store, to_id, notif_id, msg, from_id="jobs")


def maybe_notify_terminal(job: dict | None, old_status: str | None,
                          notify_store, prefix: str) -> None:
    """Fire push if status crossed into terminal set. Public so other apps
    (slurm, etc.) can call after their own update_status."""
    if not (notify_store and job and job.get("notify_to")):
        return
    if (job["status"] in TERMINAL_STATUSES
            and old_status not in TERMINAL_STATUSES):
        threading.Thread(
            target=_notify_completion,
            args=(job, notify_store, prefix),
            daemon=True,
        ).start()


# ---------------------------------------------------------------------------
# TOML render
# ---------------------------------------------------------------------------

def _job_lines(prefix: str, j: dict, *, full: bool) -> list[str]:
    lines = [
        f"id           = {j['id']}\n",
        f"kind         = {toml_str(j['kind'])}\n",
    ]
    if j.get("backend"):
        lines.append(f"backend      = {toml_str(j['backend'])}\n")
    lines += [
        f"title        = {toml_str(j['title'])}\n",
        f"status       = {toml_str(j['status'])}\n",
    ]
    if j.get("notify_to"):
        lines.append(f"notify_to    = {toml_str(j['notify_to'])}\n")
    if j.get("worker_id"):
        lines.append(f"worker_id    = {toml_str(j['worker_id'])}\n")
    if j.get("exit_code") is not None:
        lines.append(f"exit_code    = {j['exit_code']}\n")
    if j.get("reason"):
        lines.append(f"reason       = {toml_str(j['reason'])}\n")
    lines.append(f"created_at   = {toml_str(_fmt_ts(j['created_at']))}\n")
    lines.append(f"updated_at   = {toml_str(_fmt_ts(j['updated_at']))}\n")
    if j.get("submitted_at"):
        lines.append(f"submitted_at = {toml_str(_fmt_ts(j['submitted_at']))}\n")
    if j.get("finished_at"):
        lines.append(f"finished_at  = {toml_str(_fmt_ts(j['finished_at']))}\n")
    lines.append(f"url          = {toml_str(prefix + '/' + str(j['id']))}\n")
    lines.append(f"logs         = {toml_str(prefix + '/' + str(j['id']) + '/logs')}\n")
    if full and j.get("metadata"):
        lines.append(f"metadata     = {toml_str(json.dumps(j['metadata']))}\n")
    return lines


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def attach_jobs(inact_app, prefix: str, store: JobStore,
                notify_store=None, registry=None) -> None:
    prefix = "/" + prefix.strip("/")
    fastapi_app = inact_app.app

    def _resolve_agent_id(request: Request) -> str | None:
        if registry is None:
            return None
        api_key = (
            request.headers.get("x-api-key", "")
            or request.cookies.get("_inact_key", "")
        ).strip()
        if not api_key:
            return None
        agent = registry.get_by_key(api_key)
        return str(agent["id"]) if agent else None

    def _jobs(request: Request):
        if request.method == "POST":
            body = _body(request)
            title     = (body.get("title") or "").strip()
            kind      = (body.get("kind")  or "generic").strip() or "generic"
            backend   = (body.get("backend") or "").strip()
            notify_to = str(body.get("notify_to") or "").strip()
            metadata  = body.get("metadata") if isinstance(body.get("metadata"), dict) else None
            if not title:
                return text_response(
                    "ERROR 400: 'title' required\n"
                    f"POST {prefix}\n"
                    '  body: {"title":"...","notify_to":"agent_id",'
                    '"metadata":{...},"kind":"generic","backend":""}\n',
                    400,
                )
            job = store.create(title=title, kind=kind, backend=backend,
                               notify_to=notify_to, metadata=metadata)
            return text_response(
                "OK\n" + "".join(_job_lines(prefix, job, full=False)),
                201,
            )
        agent_id = _resolve_agent_id(request)
        if not agent_id:
            agent_id = (
                request.query_params.get("agent_id", "")
                or request.headers.get("x-agent-id", "")
            ).strip()
        if not agent_id:
            return text_response(
                "ERROR 400: agent_id required\n"
                f"Usage: GET {prefix}?agent_id=<id>\n"
                "       or set X-Api-Key header\n",
                400,
            )
        page, per_page = _parse_page_params(request)
        status_filter = request.query_params.get("status", "").strip() or None
        if status_filter and status_filter not in VALID_STATUSES:
            return text_response(
                f"ERROR 400: invalid status {status_filter!r}\n"
                f"valid: {', '.join(sorted(VALID_STATUSES))}\n",
                400,
            )
        kind_filter = request.query_params.get("kind", "").strip() or None
        if kind_filter == "*":
            kind_filter = None
        rows, total = store.list(
            kind=kind_filter, status=status_filter, notify_to=agent_id,
            page=page, per_page=per_page,
        )
        lines = [f"# Jobs (agent {agent_id})\n", _page_header(page, per_page, total)]
        if status_filter:
            lines.append(f"# filter: status={status_filter}\n")
        if kind_filter:
            lines.append(f"# filter: kind={kind_filter}\n")
        lines.append("# tip: ?status=pending|running|done|failed|cancelled  ?kind=*  POST here to create\n\n")
        for j in rows:
            lines.append("[[jobs]]\n")
            lines += _job_lines(prefix, j, full=False)
            lines.append("\n")
        return text_response("".join(lines))

    def _job(job_id: str, request: Request):
        if request.method == "DELETE":
            ok = store.delete(job_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n",
                                 200 if ok else 404)
        j = store.get(job_id)
        if not j:
            return text_response("ERROR 404: job not found\n", 404)
        return text_response("".join(_job_lines(prefix, j, full=True)))

    def _update(job_id: str, request: Request):
        body    = _body(request)
        status  = (body.get("status") or "").strip() or None
        if status and status not in VALID_STATUSES:
            return text_response(
                f"ERROR 400: invalid status {status!r}\n"
                f"valid: {', '.join(sorted(VALID_STATUSES))}\n",
                400,
            )
        old_job = store.get(job_id)
        if not old_job:
            return text_response("ERROR 404: job not found\n", 404)

        metadata_patch = body.get("metadata")
        if isinstance(metadata_patch, dict) and metadata_patch:
            store.merge_metadata(job_id, metadata_patch)

        if status:
            exit_code = body.get("exit_code")
            if exit_code is not None:
                try:
                    exit_code = int(exit_code)
                except (TypeError, ValueError):
                    return text_response("ERROR 400: exit_code must be int\n", 400)
            reason = body.get("reason")
            result = body.get("result") if isinstance(body.get("result"), dict) else None
            job = store.update_status(job_id, status,
                                      exit_code=exit_code,
                                      reason=reason,
                                      result=result)
            maybe_notify_terminal(job, old_job["status"],
                                  notify_store, prefix)
        else:
            job = store.get(job_id)
        return text_response(
            "OK\n"
            f"id     = {job['id']}\n"
            f"status = {toml_str(job['status'])}\n"
        )

    def _logs(job_id: str, request: Request):
        if not store.get(job_id):
            return text_response("ERROR 404: job not found\n", 404)
        if request.method == "POST":
            body = _body(request)
            stream = (body.get("stream") or "").strip()
            content = body.get("content") or ""
            if not stream:
                return text_response("ERROR 400: 'stream' required\n", 400)
            seq = store.append_log(job_id, stream, content)
            return text_response(f"OK\nseq = {seq}\n")
        stream = request.query_params.get("stream", "").strip() or None
        try:
            since_seq = int(request.query_params.get("since_seq", 0))
        except (ValueError, TypeError):
            since_seq = 0
        rows = store.read_logs(job_id, stream=stream, since_seq=since_seq)
        out = [f"# logs for {job_id}\n"]
        cur = None
        for r in rows:
            if r["stream"] != cur:
                cur = r["stream"]
                out.append(f"\n# ---- {cur} ----\n")
            out.append(r["content"])
            if not r["content"].endswith("\n"):
                out.append("\n")
        return text_response("".join(out))

    fastapi_app.add_api_route(prefix, _jobs, methods=["GET", "POST"])
    fastapi_app.add_api_route(prefix + "/{job_id}", _job, methods=["GET", "DELETE"])
    fastapi_app.add_api_route(prefix + "/{job_id}/update", _update, methods=["POST"])
    fastapi_app.add_api_route(prefix + "/{job_id}/logs", _logs, methods=["GET", "POST"])

    def _human(_path: str):
        from ..render import render_template, workspace_nav
        from ..utils import html_response
        html = render_template(
            "jobs_human.html",
            title="Jobs",
            prefix=prefix,
            agents_prefix="/agents",
            workspace_links=workspace_nav("/_human" + prefix + "/"),
            show_identity=True,
        )
        return html_response(html)

    inact_app._human_views[prefix] = _human
    inact_app.add_nav_item("jobs", "/_human" + prefix + "/")


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_jobs(
    inact_app,
    prefix: str,
    storage,
    notify_store=None,
    registry=None,
) -> JobStore:
    """
    Mount the jobs system at *prefix* and return the JobStore (so other apps
    — slurm, etc. — can persist their own job kinds in the same store).

    *storage*      — database URL/path or Storage instance.
    *notify_store* — NotifyStore; on terminal status, agents in notify_to
                     get push notifications.

    Example::

        from inact import make_storage, NotifyStore, mount_notify, mount_jobs

        db = make_storage("./app.db")
        notify_store = NotifyStore(db)
        mount_notify(app, "/notify", db)
        jobs_store = mount_jobs(app, "/jobs", db, notify_store=notify_store)
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
        f"  POST   {p}            create job  body: {{\"title\":\"...\",\"notify_to\":\"agent_id\",\"metadata\":{{...}},\"kind\":\"generic\"}}\n"
        f"  GET    {p}            list jobs   (X-Agent-Id, ?status=, ?kind=, ?kind=*)\n"
        f"  GET    {p}/{{id}}       job details\n"
        f"  POST   {p}/{{id}}/update  update status/metadata/result\n"
        f"  GET    {p}/{{id}}/logs   read logs (?stream=, ?since_seq=)\n"
        f"  POST   {p}/{{id}}/logs   append log  body: {{\"stream\":\"...\",\"content\":\"...\"}}\n"
        f"  DELETE {p}/{{id}}       delete job\n"
        + (f"  # completion notifications via notify store\n" if notify_store else "")
    )))
    return store
