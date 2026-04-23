"""
Agent cron scheduler — register scheduled HTTP callbacks.

mount_cron(prefix, storage) registers:

  GET    {prefix}/              list all jobs (TOML)
  POST   {prefix}/              create job
                                body: {"url":"...","schedule":"* * * * *","label":"...","body":"..."}
  GET    {prefix}/{id}          job details + last run
  DELETE {prefix}/{id}          delete job
  POST   {prefix}/{id}/.run     fire immediately
  GET    {prefix}/{id}/runs     run history (last 50)

Schedule format — standard 5-field cron:

  *  *  *  *  *
  |  |  |  |  +-- day-of-week  (0=Sunday … 6=Saturday)
  |  |  |  +----- month        (1-12)
  |  |  +-------- day-of-month (1-31)
  |  +----------- hour         (0-23)
  +-------------- minute       (0-59)

Supports: *, ranges (1-5), lists (1,3,5), steps (*/15, 8-17/2).

At each scheduled time the scheduler sends an HTTP POST to the job's URL.
An optional body string is sent as the request payload.  The scheduler
adds X-Inact-Cron-Job and X-Inact-Cron-Label headers so the receiver can
identify the wake-up.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from flask import request

from ..storage import Storage
from ..utils import text_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS jobs (
        id         TEXT    PRIMARY KEY,
        url        TEXT    NOT NULL,
        schedule   TEXT    NOT NULL,
        label      TEXT    NOT NULL DEFAULT '',
        body       TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL,
        last_run   BIGINT,
        next_run   BIGINT  NOT NULL,
        enabled    INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        id      TEXT    PRIMARY KEY,
        job_id  TEXT    NOT NULL,
        ran_at  BIGINT  NOT NULL,
        status  INTEGER NOT NULL,
        output  TEXT    NOT NULL DEFAULT ''
    )""",
]

_POLL = 10  # seconds between scheduler ticks


# ---------------------------------------------------------------------------
# Cron expression parser (no external deps)
# ---------------------------------------------------------------------------

def _next_run(schedule: str, after: float) -> float:
    """
    Return the next Unix timestamp at which *schedule* fires after *after*.

    Implements standard 5-field cron semantics including the Unix OR rule:
    if both dom and dow are non-*, a day matches if *either* condition holds.
    """
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
    dows    = expand(dows_f,    0, 6)   # cron: 0 = Sunday

    dom_star = doms_f.strip() == "*"
    dow_star = dows_f.strip() == "*"

    dt = datetime.fromtimestamp(after, tz=timezone.utc).replace(second=0, microsecond=0)
    dt += timedelta(minutes=1)

    # Scan forward up to 4 years of minutes (~2 M iterations worst case)
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
# Scheduler
# ---------------------------------------------------------------------------

class CronScheduler:
    """
    Background scheduler: fires HTTP POST to each job's URL when due.

    Call :meth:`start` once to launch the daemon thread.  The thread checks
    for due jobs every ``_POLL`` seconds (default 10 s).
    """

    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="inact-cron"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(timeout=_POLL):
            try:
                self._tick()
            except Exception:
                pass  # never crash the scheduler thread

    def _tick(self) -> None:
        now = int(time.time())
        due = self._s.fetchall(
            "SELECT * FROM jobs WHERE enabled=1 AND next_run <= ?", (now,)
        )
        for job in due:
            self._fire(job)

    def _fire(self, job: dict) -> None:
        ran_at = int(time.time())
        try:
            resp = httpx.post(
                job["url"],
                content=(job["body"] or "").encode(),
                headers={
                    "Content-Type": "text/plain",
                    "X-Inact-Cron-Job": job["id"],
                    "X-Inact-Cron-Label": job["label"] or "",
                },
                timeout=30,
            )
            status = resp.status_code
            output = resp.text[:500]
        except Exception as exc:
            status = 0
            output = str(exc)[:500]

        next_t = int(_next_run(job["schedule"], ran_at))
        self._s.batch([
            ("INSERT INTO runs VALUES (?,?,?,?,?)",
             (str(uuid.uuid4()), job["id"], ran_at, status, output)),
            ("UPDATE jobs SET last_run=?, next_run=? WHERE id=?",
             (ran_at, next_t, job["id"])),
        ])

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, url: str, schedule: str, label: str = "", body: str = "") -> str:
        first_next = int(_next_run(schedule, time.time()))
        job_id = str(uuid.uuid4())
        self._s.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, url, schedule, label, body,
             int(time.time()), None, first_next, 1),
        )
        return job_id

    def list_all(self) -> list[dict]:
        return self._s.fetchall(
            "SELECT * FROM jobs ORDER BY next_run ASC"
        )

    def get(self, job_id: str) -> dict | None:
        return self._s.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))

    def delete(self, job_id: str) -> bool:
        n = self._s.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        self._s.execute("DELETE FROM runs WHERE job_id=?", (job_id,))
        return n > 0

    def fire_now(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        self._fire(job)
        return True

    def runs(self, job_id: str) -> list[dict]:
        return self._s.fetchall(
            "SELECT * FROM runs WHERE job_id=? ORDER BY ran_at DESC LIMIT 50",
            (job_id,),
        )

    def last_run(self, job_id: str) -> dict | None:
        rows = self._s.fetchall(
            "SELECT * FROM runs WHERE job_id=? ORDER BY ran_at DESC LIMIT 1",
            (job_id,),
        )
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "never"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _job_toml(job: dict, prefix: str) -> str:
    lines = [
        "[[jobs]]\n",
        f"id       = {toml_str(job['id'])}\n",
        f"label    = {toml_str(job['label'])}\n",
        f"url      = {toml_str(job['url'])}\n",
        f"schedule = {toml_str(job['schedule'])}\n",
        f"next_run = {toml_str(_fmt_ts(job['next_run']))}\n",
        f"last_run = {toml_str(_fmt_ts(job['last_run']))}\n",
        f"enabled  = {str(bool(job['enabled'])).lower()}\n",
        f"detail   = {toml_str(prefix + '/' + job['id'])}\n",
        f"runs     = {toml_str(prefix + '/' + job['id'] + '/runs')}\n",
        "\n",
    ]
    return "".join(lines)


def _run_toml(run: dict) -> str:
    ok = 200 <= run["status"] < 300
    return (
        "[[runs]]\n"
        f"id     = {toml_str(run['id'])}\n"
        f"ran_at = {toml_str(_fmt_ts(run['ran_at']))}\n"
        f"status = {run['status']}\n"
        f"ok     = {str(ok).lower()}\n"
        f"output = {toml_str(run['output'])}\n"
        "\n"
    )


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_cron(inact_app, prefix: str, scheduler: CronScheduler) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_cron_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _root():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            url      = (body.get("url")      or "").strip()
            schedule = (body.get("schedule") or "").strip()
            label    = (body.get("label")    or "").strip()
            payload  = (body.get("body")     or "").strip()
            if not url:
                return text_response(
                    "ERROR 400: 'url' required\n"
                    f"POST {prefix}/\n"
                    '  Body: {"url":"https://...","schedule":"* * * * *","label":"...","body":"..."}\n'
                    "\nSchedule: 5-field cron  min hr dom mon dow\n"
                    "Examples: \"*/5 * * * *\"  every 5 min\n"
                    "          \"0 9 * * 1\"    every Monday 09:00 UTC\n",
                    400,
                )
            if not schedule:
                return text_response("ERROR 400: 'schedule' required\n", 400)
            try:
                job_id = scheduler.create(url, schedule, label, payload)
            except ValueError as exc:
                return text_response(f"ERROR 400: invalid schedule — {exc}\n", 400)
            return text_response(
                f"OK\n"
                f"id     = {toml_str(job_id)}\n"
                f"detail = {toml_str(prefix + '/' + job_id)}\n"
            )

        jobs = scheduler.list_all()
        lines = [f"# Cron jobs\n# {len(jobs)} job(s)\n\n"]
        for j in jobs:
            lines.append(_job_toml(j, prefix))
        return text_response("".join(lines))

    def _job(job_id: str):
        if request.method == "DELETE":
            ok = scheduler.delete(job_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)
        job = scheduler.get(job_id)
        if not job:
            return text_response("ERROR 404: job not found\n", 404)
        last = scheduler.last_run(job_id)
        lines = [
            f"# Job: {job['label'] or job['id']}\n\n",
            f"id       = {toml_str(job['id'])}\n",
            f"label    = {toml_str(job['label'])}\n",
            f"url      = {toml_str(job['url'])}\n",
            f"schedule = {toml_str(job['schedule'])}\n",
            f"next_run = {toml_str(_fmt_ts(job['next_run']))}\n",
            f"last_run = {toml_str(_fmt_ts(job['last_run']))}\n",
            f"enabled  = {str(bool(job['enabled'])).lower()}\n",
            f"runs     = {toml_str(prefix + '/' + job_id + '/runs')}\n",
        ]
        if job["body"]:
            lines.append(f"body     = {toml_str(job['body'])}\n")
        if last:
            lines.append("\n# Last run\n\n")
            lines.append(_run_toml(last))
        return text_response("".join(lines))

    def _fire(job_id: str):
        ok = scheduler.fire_now(job_id)
        if not ok:
            return text_response("ERROR 404: job not found\n", 404)
        last = scheduler.last_run(job_id)
        if last:
            return text_response(
                f"OK\nstatus = {last['status']}\noutput = {toml_str(last['output'])}\n"
            )
        return text_response("OK\n")

    def _runs(job_id: str):
        job = scheduler.get(job_id)
        if not job:
            return text_response("ERROR 404: job not found\n", 404)
        runs = scheduler.runs(job_id)
        lines = [
            f"# Run history: {job['label'] or job_id}\n",
            f"# {len(runs)} run(s) (last 50)\n\n",
        ]
        for r in runs:
            lines.append(_run_toml(r))
        return text_response("".join(lines))

    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_root", view_func=_root, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/<job_id>",
        endpoint=ep + "_job", view_func=_job, methods=["GET", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<job_id>/.run",
        endpoint=ep + "_fire", view_func=_fire, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<job_id>/runs",
        endpoint=ep + "_runs", view_func=_runs)


def mount_cron(inact_app, prefix: str, storage) -> None:
    """
    Mount a cron scheduler at *prefix* and start the background thread.

    Agents register jobs by POSTing a URL and a 5-field cron expression.

    *storage* — a database URL/path or a :class:`~inact.storage.Storage` instance.

    Example::

        app.mount_cron("/cron", "./data/cron.db")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    scheduler = CronScheduler(backend)
    scheduler.start()
    attach_cron(inact_app, p, scheduler)
    inact_app._app_mounts.append((p, (
        f"\nCron: {p}\n"
        f"  GET    {p}/           list jobs\n"
        f'  POST   {p}/           create job  body: {{"url":"...","schedule":"* * * * *","label":"..."}}\n'
        f"  GET    {p}/{{id}}       job details\n"
        f"  DELETE {p}/{{id}}       delete job\n"
        f"  POST   {p}/{{id}}/.run  fire now\n"
        f"  GET    {p}/{{id}}/runs  run history\n"
    )))
