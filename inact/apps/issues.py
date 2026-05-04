"""
GitHub-style issue tracker.

mount_issues(inact_app, prefix, storage) registers:

  GET    {prefix}/                         list issues  (?state=open|closed|all
                                                         ?label=bug  ?assignee=id
                                                         ?author=id  ?page=1&per_page=20)
  POST   {prefix}/                         create issue
                                           body: {"title":"...","body":"...",
                                                  "labels":["bug"],"assignee":"agent_id",
                                                  "author":"agent_id"}
  GET    {prefix}/.open                    open issues (shortcut)
  GET    {prefix}/.closed                  closed issues (shortcut)

  GET    {prefix}/{number}                 issue detail + labels + recent comments
  POST   {prefix}/{number}                 update fields (title/body/state/assignee)
  DELETE {prefix}/{number}                 delete issue and all comments

  POST   {prefix}/{number}/.close          close issue
  POST   {prefix}/{number}/.reopen         reopen issue
  POST   {prefix}/{number}/.assign         set assignee  body: {"assignee":"agent_id"}
  POST   {prefix}/{number}/.label          add label     body: {"label":"bug"}
  DELETE {prefix}/{number}/labels/{name}   remove label

  GET    {prefix}/{number}/comments        list all comments
  POST   {prefix}/{number}/comments        add comment  body: {"body":"...","author":"..."}
  DELETE {prefix}/{number}/comments/{cid}  delete comment

  GET    {prefix}/labels/                  list defined labels
  POST   {prefix}/labels/                  create label  body: {"name":"bug","description":"..."}
  DELETE {prefix}/labels/{name}            delete label (also removes from issues)

State values: open | closed
Listings sorted by updated_at DESC.
"""

from __future__ import annotations

import time
from flask import request

from ..storage import Storage
from ..utils import text_response, toml_str

_DDL = [
    """CREATE TABLE IF NOT EXISTS issues (
        number     INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT    NOT NULL,
        body       TEXT    NOT NULL DEFAULT '',
        state      TEXT    NOT NULL DEFAULT 'open',
        author     TEXT    NOT NULL DEFAULT '',
        assignee   TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL,
        updated_at BIGINT  NOT NULL,
        closed_at  BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS issue_labels (
        issue_number INTEGER NOT NULL,
        label        TEXT    NOT NULL,
        PRIMARY KEY (issue_number, label)
    )""",
    """CREATE TABLE IF NOT EXISTS labels (
        name        TEXT   PRIMARY KEY,
        description TEXT   NOT NULL DEFAULT '',
        created_at  BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS issue_comments (
        id           INTEGER PRIMARY KEY,
        issue_number INTEGER NOT NULL,
        author       TEXT    NOT NULL DEFAULT '',
        body         TEXT    NOT NULL,
        created_at   BIGINT  NOT NULL
    )""",
]

_VALID_STATES = frozenset({"open", "closed"})

_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return ""
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

class IssueStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)
        # Seed sqlite_sequence so existing databases (created before AUTOINCREMENT was added)
        # never reuse a previously-assigned issue number.
        try:
            self._s.execute(
                "INSERT OR IGNORE INTO sqlite_sequence (name, seq)"
                " SELECT 'issues', COALESCE(MAX(number), 0) FROM issues"
            )
        except Exception:
            pass  # Non-SQLite backends or sqlite_sequence not yet created

    # --- issues ---

    def create(self, title: str, body: str = "", author: str = "",
               assignee: str = "", labels: list[str] | None = None) -> dict:
        now = int(time.time())
        number = self._s.insert(
            "INSERT INTO issues (title, body, state, author, assignee, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (title, body, "open", author, assignee, now, now),
        )
        for lbl in (labels or []):
            self._attach_label(number, lbl)
        return self.get(number)

    def get(self, number: int) -> dict | None:
        row = self._s.fetchone("SELECT * FROM issues WHERE number=?", (number,))
        if row:
            row["labels"] = self._labels_for(number)
        return row

    def _build_where(self, state: str | None, label: str | None,
                     assignee: str | None, author: str | None) -> tuple[str, list]:
        clauses, params = [], []
        if state and state != "all":
            clauses.append("i.state=?")
            params.append(state)
        if assignee is not None:
            clauses.append("i.assignee=?")
            params.append(assignee)
        if author is not None:
            clauses.append("i.author=?")
            params.append(author)
        if label:
            clauses.append(
                "EXISTS (SELECT 1 FROM issue_labels il WHERE il.issue_number=i.number AND il.label=?)"
            )
            params.append(label)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def count(self, state: str | None = "open", label: str | None = None,
              assignee: str | None = None, author: str | None = None) -> int:
        where, params = self._build_where(state, label, assignee, author)
        row = self._s.fetchone(
            f"SELECT COUNT(*) AS cnt FROM issues i {where}", tuple(params)
        )
        return row["cnt"] if row else 0

    def list_issues(self, page: int, per_page: int,
                    state: str | None = "open", label: str | None = None,
                    assignee: str | None = None, author: str | None = None) -> list[dict]:
        where, params = self._build_where(state, label, assignee, author)
        offset = (page - 1) * per_page
        rows = self._s.fetchall(
            f"SELECT * FROM issues i {where} ORDER BY i.updated_at DESC LIMIT ? OFFSET ?",
            tuple(params) + (per_page, offset),
        )
        for row in rows:
            row["labels"] = self._labels_for(row["number"])
        return rows

    def update(self, number: int, fields: dict) -> bool:
        allowed = {"title", "body", "state", "assignee"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        now = int(time.time())
        updates["updated_at"] = now
        if updates.get("state") == "closed":
            updates["closed_at"] = now
        elif "state" in updates:
            updates["closed_at"] = None
        set_clause = ", ".join(f"{k}=?" for k in updates)
        return self._s.execute(
            f"UPDATE issues SET {set_clause} WHERE number=?",
            tuple(updates.values()) + (number,),
        ) > 0

    def delete(self, number: int) -> bool:
        self._s.execute("DELETE FROM issue_labels WHERE issue_number=?", (number,))
        self._s.execute("DELETE FROM issue_comments WHERE issue_number=?", (number,))
        return self._s.execute("DELETE FROM issues WHERE number=?", (number,)) > 0

    # --- labels ---

    def _labels_for(self, number: int) -> list[str]:
        rows = self._s.fetchall(
            "SELECT label FROM issue_labels WHERE issue_number=? ORDER BY label", (number,)
        )
        return [r["label"] for r in rows]

    def _attach_label(self, number: int, label: str) -> None:
        existing = self._s.fetchone(
            "SELECT 1 FROM issue_labels WHERE issue_number=? AND label=?", (number, label)
        )
        if not existing:
            self._s.execute(
                "INSERT INTO issue_labels (issue_number, label) VALUES (?,?)", (number, label)
            )

    def add_label(self, number: int, label: str) -> None:
        self._s.execute(
            "UPDATE issues SET updated_at=? WHERE number=?", (int(time.time()), number)
        )
        self._attach_label(number, label)

    def remove_label(self, number: int, label: str) -> bool:
        ok = self._s.execute(
            "DELETE FROM issue_labels WHERE issue_number=? AND label=?", (number, label)
        ) > 0
        if ok:
            self._s.execute(
                "UPDATE issues SET updated_at=? WHERE number=?", (int(time.time()), number)
            )
        return ok

    def create_label(self, name: str, description: str = "") -> bool:
        existing = self._s.fetchone("SELECT 1 FROM labels WHERE name=?", (name,))
        if existing:
            return False
        self._s.execute(
            "INSERT INTO labels (name, description, created_at) VALUES (?,?,?)",
            (name, description, int(time.time())),
        )
        return True

    def list_labels(self) -> list[dict]:
        return self._s.fetchall("SELECT * FROM labels ORDER BY name")

    def delete_label(self, name: str) -> bool:
        self._s.execute("DELETE FROM issue_labels WHERE label=?", (name,))
        return self._s.execute("DELETE FROM labels WHERE name=?", (name,)) > 0

    # --- comments ---

    def add_comment(self, number: int, body: str, author: str = "") -> dict:
        now = int(time.time())
        cid = self._s.insert(
            "INSERT INTO issue_comments (issue_number, author, body, created_at) VALUES (?,?,?,?)",
            (number, author, body, now),
        )
        self._s.execute(
            "UPDATE issues SET updated_at=? WHERE number=?", (now, number)
        )
        return {"id": cid, "issue_number": number, "author": author,
                "body": body, "created_at": now}

    def list_comments(self, number: int) -> list[dict]:
        return self._s.fetchall(
            "SELECT * FROM issue_comments WHERE issue_number=? ORDER BY created_at ASC",
            (number,),
        )

    def delete_comment(self, cid: int) -> bool:
        return self._s.execute(
            "DELETE FROM issue_comments WHERE id=?", (cid,)
        ) > 0


# ---------------------------------------------------------------------------
# TOML formatting helpers
# ---------------------------------------------------------------------------

def _issue_row_toml(issue: dict, prefix: str, name_for=None) -> str:
    nf = name_for or (lambda x: x)
    lines = [
        "[[issues]]\n",
        f"id         = {issue['number']}\n",
        f"title      = {toml_str(issue['title'])}\n",
        f"state      = {toml_str(issue['state'])}\n",
    ]
    if issue.get("author"):
        lines.append(f"author     = {toml_str(nf(issue['author']))}\n")
    if issue.get("assignee"):
        lines.append(f"assignee   = {toml_str(nf(issue['assignee']))}\n")
    if issue.get("labels"):
        labels_toml = "[" + ", ".join(toml_str(l) for l in issue["labels"]) + "]"
        lines.append(f"labels     = {labels_toml}\n")
    lines += [
        f"updated_at = {toml_str(_fmt_ts(issue['updated_at']))}\n",
        f"url        = {toml_str(prefix + '/' + str(issue['number']))}\n",
        "\n",
    ]
    return "".join(lines)


def _issue_detail_toml(issue: dict, comments: list[dict], prefix: str, name_for=None) -> str:
    nf = name_for or (lambda x: x)
    n = str(issue["number"])
    lines = [
        f"# #{n} {issue['title']}\n\n",
        f"id         = {issue['number']}\n",
        f"title      = {toml_str(issue['title'])}\n",
        f"state      = {toml_str(issue['state'])}\n",
    ]
    if issue.get("body"):
        lines.append(f"body       = {toml_str(issue['body'])}\n")
    if issue.get("author"):
        lines.append(f"author     = {toml_str(nf(issue['author']))}\n")
    if issue.get("assignee"):
        lines.append(f"assignee   = {toml_str(nf(issue['assignee']))}\n")
    if issue.get("labels"):
        labels_toml = "[" + ", ".join(toml_str(l) for l in issue["labels"]) + "]"
        lines.append(f"labels     = {labels_toml}\n")
    lines += [
        f"created_at = {toml_str(_fmt_ts(issue['created_at']))}\n",
        f"updated_at = {toml_str(_fmt_ts(issue['updated_at']))}\n",
    ]
    if issue.get("closed_at"):
        lines.append(f"closed_at  = {toml_str(_fmt_ts(issue['closed_at']))}\n")
    lines.append(f"comments_url = {toml_str(prefix + '/' + n + '/comments')}\n")
    lines.append("\n")
    if comments:
        lines.append(f"# Comments ({len(comments)})\n\n")
        for c in comments:
            lines += [
                "[[comments]]\n",
                f"id         = {c['id']}\n",
                f"author     = {toml_str(nf(c['author']))}\n",
                f"body       = {toml_str(c['body'])}\n",
                f"created_at = {toml_str(_fmt_ts(c['created_at']))}\n",
                f"delete     = {toml_str(prefix + '/' + n + '/comments/' + str(c['id']))}\n",
                "\n",
            ]
    return "".join(lines)


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_issues(inact_app, prefix: str, store: IssueStore,
                  notify_fn=None, lookup_agent=None,
                  lookup_agent_by_key=None,
                  agents_prefix: str = "/agents") -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_issues_" + prefix.replace("/", "__")
    flask_app = inact_app.app

    def _agent_display(agent_id: str) -> str:
        if not agent_id or lookup_agent is None:
            return agent_id
        agent = lookup_agent(agent_id)
        if agent and agent.get("name"):
            return f"{agent['name']}#{agent_id}"
        return agent_id

    def _caller_id() -> str:
        """Resolve the calling agent's id from X-Api-Key / cookie."""
        if lookup_agent_by_key is None:
            return ""
        api_key = (
            request.headers.get("X-Api-Key", "")
            or request.cookies.get("_inact_key", "")
        ).strip()
        if not api_key:
            return ""
        agent = lookup_agent_by_key(api_key)
        return str(agent["id"]) if agent else ""

    def _notify_assign(assignee_id: str, number: int, title: str) -> None:
        if notify_fn and assignee_id:
            notify_fn(assignee_id, "issues", (
                f'[issue:#{number}] You have been assigned: "{title}"\n'
                f"  details : GET {prefix}/{number}\n"
                f"  close   : POST {prefix}/{number}/.close\n"
                f"  comment : POST {prefix}/{number}/comments  body: {{\"body\":\"...\"}}"
            ))

    def _root():
        if request.method == "POST":
            body   = request.get_json(force=True, silent=True) or {}
            title  = (body.get("title") or "").strip()
            if not title:
                return text_response(
                    "ERROR 400: 'title' required\n"
                    f"POST {prefix}/\n"
                    '  body: {"title":"...","body":"...","labels":["bug"],'
                    '"assignee":"agent_id","author":"agent_id"}\n',
                    400,
                )
            issue_body = (body.get("body") or "").strip()
            author     = str(body.get("author") or "").strip() or _caller_id()
            assignee   = str(body.get("assignee") or "").strip()
            raw_labels = body.get("labels") or []
            labels = [str(l).strip() for l in raw_labels if str(l).strip()]
            if assignee and lookup_agent is not None:
                if lookup_agent(assignee) is None:
                    return text_response(
                        f"ERROR 400: 'assignee' {assignee!r} is not a registered agent id\n"
                        f"Agent ids: GET {agents_prefix}/\n",
                        400,
                    )
            issue = store.create(title, issue_body, author, assignee, labels)
            _notify_assign(assignee, issue["number"], title)
            return text_response(
                f"OK\n"
                f"id  = {issue['number']}\n"
                f"url = {toml_str(prefix + '/' + str(issue['number']))}\n",
                201,
            )

        # GET — list
        state_f    = request.args.get("state",    "open").strip() or "open"
        label_f    = request.args.get("label",    "").strip() or None
        assignee_f = request.args.get("assignee", None)
        author_f   = request.args.get("author",   None)
        if assignee_f is not None:
            assignee_f = assignee_f.strip()
        if author_f is not None:
            author_f = author_f.strip()
        if state_f not in _VALID_STATES and state_f != "all":
            return text_response(
                f"ERROR 400: invalid state {state_f!r}\n"
                "valid: open | closed | all\n",
                400,
            )
        page, per_page = _parse_page_params()
        total  = store.count(state_f, label_f, assignee_f, author_f)
        issues = store.list_issues(page, per_page, state_f, label_f, assignee_f, author_f)
        lines  = [f"# Issues ({state_f})\n", _page_header(page, per_page, total)]
        lines.append("# tip: ?state=open|closed|all  ?label=bug  ?assignee=id  ?author=id\n\n")
        for iss in issues:
            lines.append(_issue_row_toml(iss, prefix, _agent_display))
        return text_response("".join(lines))

    def _open_issues():
        issues = store.list_issues(1, _MAX_PER_PAGE, state="open")
        lines  = [f"# Open issues ({len(issues)})\n\n"]
        for iss in issues:
            lines.append(_issue_row_toml(iss, prefix, _agent_display))
        return text_response("".join(lines))

    def _closed_issues():
        issues = store.list_issues(1, _MAX_PER_PAGE, state="closed")
        lines  = [f"# Closed issues ({len(issues)})\n\n"]
        for iss in issues:
            lines.append(_issue_row_toml(iss, prefix, _agent_display))
        return text_response("".join(lines))

    def _issue(number: int):
        if request.method == "DELETE":
            ok = store.delete(number)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)

        if request.method == "POST":
            issue = store.get(number)
            if not issue:
                return text_response("ERROR 404: issue not found\n", 404)
            body   = request.get_json(force=True, silent=True) or {}
            fields: dict = {}
            if "title" in body:
                t = (body["title"] or "").strip()
                if not t:
                    return text_response("ERROR 400: 'title' cannot be empty\n", 400)
                fields["title"] = t
            if "body" in body:
                fields["body"] = body["body"] or ""
            if "state" in body:
                s = (body["state"] or "").strip()
                if s not in _VALID_STATES:
                    return text_response(
                        f"ERROR 400: 'state' must be one of: {', '.join(sorted(_VALID_STATES))}\n",
                        400,
                    )
                fields["state"] = s
            if "assignee" in body:
                new_a = str(body["assignee"] or "").strip()
                if new_a and lookup_agent is not None:
                    if lookup_agent(new_a) is None:
                        return text_response(
                            f"ERROR 400: 'assignee' {new_a!r} is not a registered agent id\n", 400
                        )
                old_a = issue.get("assignee", "")
                fields["assignee"] = new_a
                if new_a and new_a != old_a:
                    _notify_assign(new_a, number, issue["title"])
            store.update(number, fields)
            return text_response("OK\n")

        issue = store.get(number)
        if not issue:
            return text_response("ERROR 404: issue not found\n", 404)
        comments = store.list_comments(number)
        return text_response(_issue_detail_toml(issue, comments, prefix, _agent_display))

    def _close(number: int):
        if not store.get(number):
            return text_response("ERROR 404: issue not found\n", 404)
        store.update(number, {"state": "closed"})
        return text_response("OK\n")

    def _reopen(number: int):
        if not store.get(number):
            return text_response("ERROR 404: issue not found\n", 404)
        store.update(number, {"state": "open"})
        return text_response("OK\n")

    def _assign(number: int):
        issue = store.get(number)
        if not issue:
            return text_response("ERROR 404: issue not found\n", 404)
        body     = request.get_json(force=True, silent=True) or {}
        assignee = str(body.get("assignee") or "").strip()
        if not assignee:
            return text_response(
                "ERROR 400: 'assignee' required\n"
                f'body: {{"assignee": "<agent_id>"}}\n'
                f"Agent ids: GET {agents_prefix}/\n",
                400,
            )
        if lookup_agent is not None and lookup_agent(assignee) is None:
            return text_response(
                f"ERROR 400: {assignee!r} is not a registered agent id\n"
                f"Agent ids: GET {agents_prefix}/\n",
                400,
            )
        old_a = issue.get("assignee", "")
        store.update(number, {"assignee": assignee})
        if assignee != old_a:
            _notify_assign(assignee, number, issue["title"])
        return text_response(f"OK\nassignee = {toml_str(assignee)}\n")

    def _add_label_route(number: int):
        if not store.get(number):
            return text_response("ERROR 404: issue not found\n", 404)
        body  = request.get_json(force=True, silent=True) or {}
        label = (body.get("label") or "").strip()
        if not label:
            return text_response(
                "ERROR 400: 'label' required\n"
                f'body: {{"label": "bug"}}\n'
                f"Defined labels: GET {prefix}/labels/\n",
                400,
            )
        store.add_label(number, label)
        return text_response(f"OK\nlabel = {toml_str(label)}\n")

    def _remove_label_route(number: int, label_name: str):
        if not store.get(number):
            return text_response("ERROR 404: issue not found\n", 404)
        ok = store.remove_label(number, label_name)
        return text_response("OK\n" if ok else "ERROR 404: label not on this issue\n",
                             200 if ok else 404)

    def _comments(number: int):
        issue = store.get(number)
        if not issue:
            return text_response("ERROR 404: issue not found\n", 404)

        if request.method == "POST":
            body   = request.get_json(force=True, silent=True) or {}
            cbody  = (body.get("body") or "").strip()
            author = str(body.get("author") or "").strip() or _caller_id()
            if not cbody:
                return text_response(
                    "ERROR 400: 'body' required\n"
                    f"POST {prefix}/{number}/comments\n"
                    '  body: {"body":"...","author":"agent_id"}\n',
                    400,
                )
            c = store.add_comment(number, cbody, author)

            # Notify assignee and creator on new replies, skipping the commenter themself
            if notify_fn:
                title = issue.get("title", "")
                from_id = author or ""
                who = _agent_display(from_id) if from_id else "someone"
                msg = (
                    f"[issue:#{number}] New reply from {who} on \"{title}\"\n"
                    f"  details : GET {prefix}/{number}\n"
                    f"  comment : {cbody[:200]}{'…' if len(cbody) > 200 else ''}"
                )
                assignee_id = str(issue.get("assignee") or "").strip()
                author_id   = str(issue.get("author")   or "").strip()
                if assignee_id and assignee_id != from_id:
                    try:
                        notify_fn(assignee_id, from_id, msg)
                    except Exception:
                        pass
                if author_id and author_id not in (from_id, assignee_id):
                    try:
                        notify_fn(author_id, from_id, msg)
                    except Exception:
                        pass
            return text_response(
                f"OK\nid  = {c['id']}\nurl = {toml_str(prefix + '/' + str(number) + '/comments/' + str(c['id']))}\n",
                201,
            )

        comments = store.list_comments(number)
        lines = [
            f"# Comments on #{number}: {issue['title']}\n",
            f"# {len(comments)} comment(s)\n\n",
        ]
        for c in comments:
            lines += [
                "[[comments]]\n",
                f"id         = {c['id']}\n",
                f"author     = {toml_str(_agent_display(c['author']))}\n",
                f"body       = {toml_str(c['body'])}\n",
                f"created_at = {toml_str(_fmt_ts(c['created_at']))}\n",
                f"delete     = {toml_str(prefix + '/' + str(number) + '/comments/' + str(c['id']))}\n",
                "\n",
            ]
        return text_response("".join(lines))

    def _comment(number: int, cid: int):
        if request.method == "DELETE":
            ok = store.delete_comment(cid)
            return text_response("OK\n" if ok else "ERROR 404: not found\n", 200 if ok else 404)

    def _labels_root():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            name = (body.get("name") or "").strip()
            if not name:
                return text_response(
                    "ERROR 400: 'name' required\n"
                    f"POST {prefix}/labels/\n"
                    '  body: {"name":"bug","description":"..."}\n',
                    400,
                )
            desc = (body.get("description") or "").strip()
            created = store.create_label(name, desc)
            if not created:
                return text_response(f"ERROR 409: label {name!r} already exists\n", 409)
            return text_response(f"OK\nname = {toml_str(name)}\n", 201)

        labels = store.list_labels()
        lines  = [f"# Labels ({len(labels)})\n\n"]
        for lbl in labels:
            lines += [
                "[[labels]]\n",
                f"name        = {toml_str(lbl['name'])}\n",
            ]
            if lbl["description"]:
                lines.append(f"description = {toml_str(lbl['description'])}\n")
            lines += [
                f"created_at  = {toml_str(_fmt_ts(lbl['created_at']))}\n",
                "\n",
            ]
        return text_response("".join(lines))

    def _label(label_name: str):
        if request.method == "DELETE":
            ok = store.delete_label(label_name)
            return text_response("OK\n" if ok else "ERROR 404: label not found\n",
                                 200 if ok else 404)

    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_root", view_func=_root, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/.open",
        endpoint=ep + "_open", view_func=_open_issues)
    flask_app.add_url_rule(
        prefix + "/.closed",
        endpoint=ep + "_closed", view_func=_closed_issues)
    flask_app.add_url_rule(
        prefix + "/labels/",
        endpoint=ep + "_labels", view_func=_labels_root, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/labels/<label_name>",
        endpoint=ep + "_label", view_func=_label, methods=["DELETE"])
    flask_app.add_url_rule(
        prefix + "/<int:number>",
        endpoint=ep + "_issue", view_func=_issue, methods=["GET", "POST", "DELETE"])
    flask_app.add_url_rule(
        prefix + "/<int:number>/.close",
        endpoint=ep + "_close", view_func=_close, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<int:number>/.reopen",
        endpoint=ep + "_reopen", view_func=_reopen, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<int:number>/.assign",
        endpoint=ep + "_assign", view_func=_assign, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<int:number>/.label",
        endpoint=ep + "_add_label", view_func=_add_label_route, methods=["POST"])
    flask_app.add_url_rule(
        prefix + "/<int:number>/labels/<label_name>",
        endpoint=ep + "_remove_label", view_func=_remove_label_route, methods=["DELETE"])
    flask_app.add_url_rule(
        prefix + "/<int:number>/comments",
        endpoint=ep + "_comments", view_func=_comments, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/<int:number>/comments/<int:cid>",
        endpoint=ep + "_comment", view_func=_comment, methods=["DELETE"])

    def _human(path: str):
        from ..render import render_template, workspace_nav
        from ..utils import html_response
        import re as _re
        sub = path[len(prefix):].lstrip("/")
        m = _re.match(r"^(\d+)$", sub)
        initial_issue = int(m.group(1)) if m else None
        html = render_template(
            "issues_human.html",
            title="Issues",
            prefix=prefix,
            agents_prefix=agents_prefix,
            workspace_links=workspace_nav("/_human" + prefix + "/"),
            show_identity=True,
            initial_issue=initial_issue,
        )
        return html_response(html)

    inact_app._human_views[prefix] = _human
    inact_app.add_nav_item(prefix.rsplit("/", 1)[-1] or prefix.strip("/"),
                           "/_human" + prefix + "/")


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_issues(
    inact_app,
    prefix: str,
    storage,
    agents_prefix: str = "/agents",
    agents_storage=None,
    notify_storage=None,
) -> None:
    """
    Mount a GitHub-style issue tracker at *prefix*.

    *storage*        — database URL/path or Storage instance.
    *agents_prefix*  — prefix where the agent registry is mounted.
    *agents_storage* — if provided, assignee is validated as a registered agent id.
    *notify_storage* — if provided, agents receive a notification when assigned to an issue.

    Example::

        mount_issues(app, "/issues", "./app.db",
                     agents_storage="./app.db",
                     notify_storage="./app.db")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    store = IssueStore(backend)

    lookup_agent = None
    lookup_agent_by_key = None
    if agents_storage is not None:
        from .workspace.register import AgentRegistry
        ag_back = make_storage(agents_storage) if isinstance(agents_storage, str) else agents_storage
        ag_reg  = AgentRegistry(ag_back)
        def lookup_agent(agent_id: str) -> dict | None:
            try:
                return ag_reg.get(int(agent_id))
            except (ValueError, TypeError):
                return None
        def lookup_agent_by_key(api_key: str) -> dict | None:
            return ag_reg.get_by_key(api_key)

    notify_fn = None
    if notify_storage is not None:
        from .notify import NotifyStore, _push
        ns_back = make_storage(notify_storage) if isinstance(notify_storage, str) else notify_storage
        nstore  = NotifyStore(ns_back)
        def notify_fn(to_id: str, from_id: str, message: str) -> None:
            notif_id = nstore.send(to_id, message, from_id)
            name = ""
            if agents_storage is not None and from_id:
                try:
                    row = ag_reg.get(int(from_id))
                    name = (row.get("name") or "") if row else ""
                except Exception:
                    pass
            _push(nstore, to_id, notif_id, message, from_id, name=name)
            # Also email human assignees even if they haven't registered a callback.
            if agents_storage is not None:
                try:
                    to_agent = ag_reg.get(int(to_id))
                    if to_agent and to_agent.get("kind") == "human" and to_agent.get("email"):
                        to_email = to_agent["email"].strip()
                        if to_email:
                            import os
                            from ..apps.workspace.mailbox import _send_email
                            r_host = os.environ.get("SMTP_RELAY_HOST", "")
                            r_port = int(os.environ.get("SMTP_RELAY_PORT", "587") or 587)
                            r_user = os.environ.get("SMTP_RELAY_USER", "")
                            r_pass = os.environ.get("SMTP_RELAY_PASSWORD", "")
                            s_port = int(os.environ.get("SMTP_PORT", "2525") or 2525)
                            from_email = (
                                os.environ.get("FROM_EMAIL", "")
                                or os.environ.get("SMTP_FROM", "")
                                or f"notify@{os.environ.get('DOMAIN', 'localhost')}"
                            )
                            # Extract title from the standard assignment message
                            subject = "Inact Issue Notification"
                            if 'You have been assigned: "' in message:
                                title = message.split('You have been assigned: "', 1)[1].split('"', 1)[0]
                                subject = f'[Issue] "{title}" assigned to you'
                            elif 'New reply from' in message:
                                subject = "Inact Issue Reply"
                            _send_email(
                                from_email, to_email, subject, message,
                                relay_host=r_host, relay_port=r_port,
                                relay_user=r_user, relay_password=r_pass,
                                smtp_port=s_port,
                            )
                except Exception:
                    pass

    ap = "/" + agents_prefix.strip("/")
    attach_issues(inact_app, p, store,
                  notify_fn=notify_fn,
                  lookup_agent=lookup_agent,
                  lookup_agent_by_key=lookup_agent_by_key,
                  agents_prefix=ap)
    inact_app._app_mounts.append((p, (
        f"\nIssues: {p}\n"
        f"  GET    {p}/                           list issues (?state=open|closed|all ?label=bug ?assignee=id ?author=id)\n"
        f"  POST   {p}/                           create issue\n"
        f"  GET    {p}/.open                      open issues\n"
        f"  GET    {p}/.closed                    closed issues\n"
        f"  GET    {p}/{{number}}                   issue detail + comments\n"
        f"  POST   {p}/{{number}}                   update (title/body/state/assignee)\n"
        f"  DELETE {p}/{{number}}                   delete issue\n"
        f"  POST   {p}/{{number}}/.close             close\n"
        f"  POST   {p}/{{number}}/.reopen            reopen\n"
        f"  POST   {p}/{{number}}/.assign            set assignee\n"
        f"  POST   {p}/{{number}}/.label             add label   body: {{\"label\":\"bug\"}}\n"
        f"  DELETE {p}/{{number}}/labels/{{name}}     remove label\n"
        f"  GET    {p}/{{number}}/comments           list comments\n"
        f"  POST   {p}/{{number}}/comments           add comment\n"
        f"  DELETE {p}/{{number}}/comments/{{cid}}    delete comment\n"
        f"  GET    {p}/labels/                    list labels\n"
        f"  POST   {p}/labels/                    create label\n"
        f"  DELETE {p}/labels/{{name}}              delete label\n"
    )))
