"""
Git proxy — server-configured GitHub token, agent-transparent access.

mount_git_proxy(inact_app, prefix, storage, admin_key="")

Admin (X-Admin-Key):
  POST {prefix}/repos            register a repo + token
                                 body: {"owner":"EvoEvolver","repo":"inact","token":"ghp_xxx"}
  GET  {prefix}/repos            list configured repos
  DELETE {prefix}/repos/{owner}/{repo}  remove repo

Agent (X-Api-Key — existing auth middleware validates it):
  GET|POST|PUT|PATCH|DELETE
    {prefix}/{owner}/{repo}/{rest:path}
    → https://api.github.com/repos/{owner}/{repo}/{rest:path}

The agent never sees the GitHub token.  The proxy response body is
forwarded verbatim (JSON or plain text) so agents can parse it directly.
"""

from __future__ import annotations

import json
import time
import logging

import httpx
from fastapi import Request
from fastapi.responses import Response

from ..storage import Storage
from ..utils import text_response, toml_str, _body

_log = logging.getLogger(__name__)

# We reuse a single async client for connection-pooling.
_gh_client: httpx.AsyncClient | None = None

_DDL = [
    """CREATE TABLE IF NOT EXISTS git_repos (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        owner      TEXT    NOT NULL,
        repo       TEXT    NOT NULL,
        token      TEXT    NOT NULL,
        created_at BIGINT  NOT NULL,
        UNIQUE (owner, repo)
    )""",
]


class GitProxyStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)

    def add(self, owner: str, repo: str, token: str) -> None:
        ts = int(time.time())
        self._s.execute(
            "INSERT OR REPLACE INTO git_repos (owner, repo, token, created_at) VALUES (?, ?, ?, ?)",
            (owner, repo, token, ts),
        )

    def remove(self, owner: str, repo: str) -> bool:
        return self._s.execute(
            "DELETE FROM git_repos WHERE owner = ? AND repo = ?",
            (owner, repo),
        ) > 0

    def get(self, owner: str, repo: str) -> dict | None:
        return self._s.fetchone(
            "SELECT id, owner, repo, token, created_at FROM git_repos WHERE owner = ? AND repo = ?",
            (owner, repo),
        )

    def list_all(self) -> list[dict]:
        return self._s.fetchall(
            "SELECT id, owner, repo, created_at FROM git_repos ORDER BY created_at DESC"
        )


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_git_proxy(inact_app, prefix: str, store: GitProxyStore, admin_key: str = "") -> None:
    prefix = "/" + prefix.strip("/")
    fastapi_app = inact_app.app
    global _gh_client

    if _gh_client is None:
        _gh_client = httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=httpx.Timeout(30.0),
        )

    # --- helpers -------------------------------------------------------------

    def _require_admin(request: Request):
        if not admin_key:
            return text_response("ERROR 503: admin_key not configured\n", 503)
        if request.headers.get("x-admin-key", "").strip() != admin_key:
            return text_response("ERROR 401: X-Admin-Key required\n", 401)
        return True

    # --- admin routes --------------------------------------------------------

    def _admin_list(request: Request):
        auth = _require_admin(request)
        if auth is not True:
            return auth
        rows = store.list_all()
        lines = [f"# Configured repos ({len(rows)})\n\n"]
        for r in rows:
            lines += [
                "[[repos]]\n",
                f"id         = {r['id']}\n",
                f"owner      = {toml_str(r['owner'])}\n",
                f"repo       = {toml_str(r['repo'])}\n",
                f"created_at = {toml_str(_fmt_ts(r['created_at']))}\n",
                f"proxy      = {toml_str(prefix + '/' + r['owner'] + '/' + r['repo'] + '/')}\n",
                "\n",
            ]
        return text_response("".join(lines))

    def _admin_create(request: Request):
        auth = _require_admin(request)
        if auth is not True:
            return auth
        body = _body(request)
        owner = (body.get("owner") or "").strip()
        repo  = (body.get("repo")  or "").strip()
        token = (body.get("token") or "").strip()
        if not owner or not repo or not token:
            return text_response(
                "ERROR 400: owner, repo, token required\n"
                'body: {"owner":"...","repo":"...","token":"ghp_xxx"}\n',
                400,
            )
        store.add(owner, repo, token)
        return text_response(
            f"OK\nowner = {toml_str(owner)}\nrepo  = {toml_str(repo)}\n"
            f"proxy = {toml_str(prefix + '/' + owner + '/' + repo + '/')}\n"
        )

    def _admin_delete(owner: str, repo: str, request: Request):
        auth = _require_admin(request)
        if auth is not True:
            return auth
        ok = store.remove(owner, repo)
        return text_response("OK\n" if ok else "ERROR 404: repo not found\n",
                             200 if ok else 404)

    # --- agent proxy ---------------------------------------------------------

    async def _proxy(owner: str, repo: str, rest: str, request: Request):
        """Forward any HTTP method to GitHub REST API using the stored token."""
        cfg = store.get(owner, repo)
        if not cfg:
            return text_response(
                f"ERROR 404: repo {owner}/{repo} not configured for proxy\n"
                f"  Admin: POST {prefix}/repos  body: {{'owner':'{owner}','repo':'{repo}','token':'...'}}\n",
                404,
            )
        token = cfg["token"]

        # Build forward URL
        # Remove leading / from rest so we don't double-slash
        rest_clean = rest.lstrip("/")
        url = f"https://api.github.com/repos/{owner}/{repo}/{rest_clean}"
        if request.query_params:
            url = f"{url}?{request.query_params}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "inact-git-proxy/0.1",
        }
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]

        body = await request.body()

        # Only forward these headers back to the agent (drop GH connection-layer ones)
        _DROP_HEADERS = {
            "transfer-encoding", "content-encoding", "connection",
            "keep-alive", "server", "date",
        }

        try:
            resp = await _gh_client.request(
                request.method,
                url,
                headers=headers,
                content=body or None,
                timeout=30.0,
            )
        except httpx.RequestError as e:
            _log.warning("GitHub upstream error: %s", e)
            return text_response(f"ERROR 502: upstream error: {e}\n", 502)

        out_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in _DROP_HEADERS
        }
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=out_headers,
        )

    # --- register ------------------------------------------------------------

    fastapi_app.add_api_route(prefix + "/.admin/repos", _admin_list, methods=["GET"])
    fastapi_app.add_api_route(prefix + "/.admin/repos", _admin_create, methods=["POST"])
    fastapi_app.add_api_route(prefix + "/.admin/repos/{owner}/{repo}", _admin_delete, methods=["DELETE"])

    fastapi_app.add_api_route(
        prefix + "/{owner}/{repo}/{rest:path}", _proxy,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )

    # --- human view ----------------------------------------------------------

    def _human(path: str = ""):
        from ..render import render_template, workspace_nav
        from ..utils import html_response

        rows = store.list_all()
        items = []
        for r in rows:
            proxy_url = prefix + "/" + r["owner"] + "/" + r["repo"] + "/"
            items.append(
                f"<li><code>{r['owner']}/{r['repo']}</code> — "
                f"<a href='{proxy_url}'>{proxy_url}</a></li>"
            )
        body = (
            "<h2>Git Proxy</h2>\n"
            "<p>Configured repos (token hidden):</p>\n"
            f"<ul>\n{''.join(items) if items else '<li>None</li>'}\n</ul>\n"
        )
        nav = workspace_nav("/_human" + prefix)
        html = render_template(
            "human.html",
            title="Git Proxy",
            prefix=prefix,
            nav=nav,
            pills=[],
            show_identity=False,
            content=body,
        )
        return html_response(html)

    inact_app._human_views[prefix] = _human
    inact_app.add_nav_item("git", "/_human" + prefix + "/")

    # --- help text -----------------------------------------------------------

    inact_app._app_mounts.append((prefix, (
        f"\nGit proxy: {prefix}\n"
        f"  Admin (X-Admin-Key):\n"
        f"    GET    {prefix}/repos                list configured repos\n"
        f"    POST   {prefix}/repos                register repo + token\n"
        f"    DELETE {prefix}/repos/{{owner}}/{{repo}}  remove repo\n"
        f"\n"
        f"  Agent (X-Api-Key):\n"
        f"    GET|POST|PUT|PATCH|DELETE\n"
        f"      {prefix}/{{owner}}/{{repo}}/{{rest}}  proxy to GitHub API\n"
        f"      e.g. GET {prefix}/EvoEvolver/inact/issues/\n"
    )))


# ---------------------------------------------------------------------------
# Mount helper
# ---------------------------------------------------------------------------

def mount_git_proxy(inact_app, prefix: str, storage, admin_key: str = "") -> None:
    """
    Mount the git proxy app.

    *storage*    — database URL/path or Storage instance.
    *admin_key*  — required for admin routes (POST/DELETE /repos).
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    attach_git_proxy(inact_app, p, GitProxyStore(backend), admin_key=admin_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
