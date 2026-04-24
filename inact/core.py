"""
Inact — AI-oriented Flask toolkit.

Provides:
  - inact_md / inact_toml   : content-typed route decorators
  - help()                  : register /.help for a path
  - Auto /_human/<path>     : HTML rendering for any registered route
  - Inherited /.help        : walks up the path tree automatically
"""

from __future__ import annotations

import os
from typing import Callable

from flask import Flask, request

from .apps.files import PAGE_RE
from .pages import normalize_md, normalize_toml
from .render import render_markdown, render_toml, render_plain, render_ls
from .utils import text_response, toml_str


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------

_RouteEntry = tuple[str, Callable[[], str]]  # ("md" | "toml", fn)


class Inact:
    def __init__(self, import_name_or_app: str | Flask = __name__):
        if isinstance(import_name_or_app, Flask):
            self.app = import_name_or_app
        else:
            self.app = Flask(import_name_or_app)

        self._routes: dict[str, _RouteEntry] = {}
        self._help: dict[str, Callable[[], str] | str] = {}
        self._mounts: dict[str, str] = {}  # prefix -> abs folder
        self._mount_handlers: dict[str, dict[str, FileHandler]] = {}  # prefix -> {ext -> handler}
        self._mount_editable: dict[str, bool | list[str]] = {}  # prefix -> editable spec
        self._website_mounts: dict[str, str] = {}  # prefix -> base URL (used by _render_human)
        self._app_mounts: list[tuple[str, str]] = []  # (prefix, help_text) injected by mount_*
        self._human_views: dict[str, Callable] = {}   # prefix -> fn(path) -> HTML response

        self._register_builtins()

    # -----------------------------------------------------------------------
    # Public decorators / methods
    # -----------------------------------------------------------------------

    def inact_md(self, path: str):
        """Register a Markdown + YAML-frontmatter route."""
        def decorator(fn: Callable[[], str]):
            self._add_route(path, "md", fn)
            return fn
        return decorator

    def inact_toml(self, path: str):
        """Register a TOML route (plain text for agents, HTML for humans)."""
        def decorator(fn: Callable[[], str]):
            self._add_route(path, "toml", fn)
            return fn
        return decorator

    def help(self, path: str):
        """Register help text for a path (used by /.help)."""
        def decorator(fn: Callable[[], str] | str):
            self._help[path.rstrip("/") or "/"] = fn
            return fn
        return decorator

    def route(self, path: str, **kwargs):
        """Pass-through to Flask's @app.route."""
        return self.app.route(path, **kwargs)

    def run(self, **kwargs):
        self.app.run(**kwargs)

    def to_mcp(self, base_url: str, name: str | None = None):
        """
        Export this inact app as an MCP server with a single ``fetch`` tool.

        The server description auto-lists every route and mount so the agent
        knows what paths exist before making any requests.

        Requires: pip install mcp httpx

        Usage::

            mcp = app.to_mcp("http://localhost:5000")
            mcp.run()                          # stdio (Claude Desktop, etc.)
            mcp.run(transport="streamable-http", port=8080)
        """
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError:
            raise RuntimeError("mcp package is required: pip install mcp")

        import httpx

        base = base_url.rstrip("/")
        mcp_server = FastMCP(name or self.app.name, description=self._mcp_description(base))

        @mcp_server.tool()
        def fetch(path: str, method: str = "GET", body: str = "") -> str:
            """
            Access any route on this inact server.

            path: URL path starting with / — may include query string.
                  Examples: '/docs/.ls', '/docs/.grep?q=python',
                  '/docs/README.md', '/docs/README.md/.info'
            method: HTTP method. Use 'GET' to read, 'POST' to write
                    (e.g. /.replace endpoints).
            body: Request body for POST (plain text content to write).
            """
            url = base + "/" + path.lstrip("/")
            with httpx.Client(timeout=30) as client:
                if method.upper() == "POST":
                    resp = client.post(url, content=body.encode())
                else:
                    resp = client.get(url)
                return resp.text

        return mcp_server

    def _mcp_description(self, base_url: str) -> str:
        lines = [
            f"Inact server at {base_url} — an AI-first HTTP API.\n",
            "All responses are plain text, designed for direct AI consumption.\n\n",
            f"  curl {base_url}/.help\n\n",
        ]
        if self._routes:
            lines.append("## Routes\n\n")
            for path in sorted(self._routes):
                kind, _ = self._routes[path]
                lines.append(f"  GET {path}  [{kind}]\n")
            lines.append("\n")
        if self._app_mounts:
            lines.append("## Mounted apps\n\n")
            for _, help_text in sorted(self._app_mounts):
                lines.append(help_text)
        return "".join(lines)

    # -----------------------------------------------------------------------
    # Built-in global routes
    # -----------------------------------------------------------------------

    def _register_builtins(self):
        app = self.app

        @app.route("/_human/", defaults={"subpath": ""})
        @app.route("/_human/<path:subpath>")
        def _human(subpath: str):
            return self._render_human("/" + subpath)

        @app.route("/.help")
        def _root_help():
            return self._serve_help("/")

        @app.route("/<path:subpath>/.help")
        def _path_help(subpath: str):
            return self._serve_help("/" + subpath)

    # -----------------------------------------------------------------------
    # Route registration helpers
    # -----------------------------------------------------------------------

    def _add_route(self, path: str, kind: str, fn: Callable[[], str]):
        self._routes[path] = (kind, fn)

        app = self.app
        ep = "_inact_" + path.replace("/", "__")

        @app.route(path, endpoint=ep)
        def _handler(_kind=kind, _fn=fn, _path=path):
            return self._serve_plain(_kind, _fn)

    def _serve_plain(self, kind: str, fn: Callable) -> tuple:
        value = fn()
        if kind == "md":
            _, body = normalize_md(value)
            return text_response(body)
        if kind == "toml":
            _, toml_text = normalize_toml(value)
            return text_response(toml_text)
        return text_response(str(value))

    # -----------------------------------------------------------------------
    # /_human/ rendering
    # -----------------------------------------------------------------------

    def _render_human(self, path: str) -> tuple:
        # App human views (checked first — they override the generic file/route rendering)
        for prefix, view_fn in self._human_views.items():
            if path == prefix or path.startswith(prefix + "/"):
                return view_fn(path)

        # Registered md / toml routes
        if path in self._routes:
            kind, fn = self._routes[path]
            value = fn()
            if kind == "md":
                return render_markdown(value, path)
            if kind == "toml":
                return render_toml(value, path)
            return render_plain(str(value), path)

        # Mounted file/directory
        for prefix, folder in self._mounts.items():
            if path == prefix or path.startswith(prefix + "/"):
                subpath = path[len(prefix):].lstrip("/")

                # Resolve pagination in human view: /<file>/p/<N>
                page = 1
                display_subpath = subpath
                m = PAGE_RE.match(subpath)
                if m:
                    file_sub, pg = m.group(1), int(m.group(2))
                    _, ext = os.path.splitext(file_sub.lower())
                    if ext in self._mount_handlers.get(prefix, {}):
                        display_subpath = file_sub
                        page = pg

                full = os.path.normpath(os.path.join(folder, display_subpath)) if display_subpath else folder
                if not full.startswith(folder):
                    return text_response("ERROR 403: Forbidden\n", 403)

                if os.path.isdir(full):
                    entries = _list_dir_local(folder, full, prefix, display_subpath)
                    return render_ls(entries, path, prefix + ("/" + display_subpath if display_subpath else ""))

                if os.path.isfile(full):
                    _, ext = os.path.splitext(display_subpath.lower())
                    handler = self._mount_handlers.get(prefix, {}).get(ext)
                    if handler is not None:
                        content, status = handler.serve(full, prefix + "/" + display_subpath, page)
                        if status != 200:
                            return text_response(f"ERROR {status}: {content}\n", status)
                        return render_plain(content, path)
                    try:
                        content = open(full, encoding="utf-8").read()
                    except Exception as e:
                        return text_response(f"ERROR 500: {e}\n", 500)
                    if full.endswith(".md"):
                        return render_markdown(content, path)
                    if full.endswith(".toml"):
                        return render_toml(content, path)
                    return render_plain(content, path)

        # Proxied website — return the raw HTML so the browser renders it natively
        for prefix, base_url in self._website_mounts.items():
            if path == prefix or path.startswith(prefix + "/"):
                subpath = path[len(prefix):].lstrip("/")
                # /.links in human view → show the links page as plain text
                if subpath == ".links" or subpath.endswith("/.links"):
                    page_sub = subpath[:-6].rstrip("/") if subpath.endswith("/.links") else ""
                    from .apps.website import WebsiteProxy, serve_links
                    return serve_links(WebsiteProxy(base_url), prefix, base_url, page_sub)
                params = dict(request.args) or None
                try:
                    from .apps.website import WebsiteProxy
                    content_type, body = WebsiteProxy(base_url).fetch_raw(subpath, params)
                except Exception as exc:
                    return text_response(f"ERROR 502: {exc}\n", 502)
                if "text/html" in content_type:
                    return body, 200, {"Content-Type": "text/html; charset=utf-8"}
                return render_plain(body, path)

        return text_response(f"ERROR 404: No human view for {path}\n", 404)

    # -----------------------------------------------------------------------
    # /.help
    # -----------------------------------------------------------------------

    def _serve_help(self, path: str) -> tuple:
        text = self._find_help(path)
        if text:
            return text_response(text)

        # Auto-generate a stub listing child routes under this path
        stub = self._auto_help(path)
        return text_response(stub)

    def _find_help(self, path: str) -> str | None:
        parts = [p for p in path.rstrip("/").split("/") if p]
        for depth in range(len(parts), -1, -1):
            candidate = "/" + "/".join(parts[:depth])
            if candidate in self._help:
                entry = self._help[candidate]
                return entry() if callable(entry) else entry
            # Pull help from frontmatter of md routes
            if candidate in self._routes:
                kind, fn = self._routes[candidate]
                if kind == "md":
                    meta, _ = normalize_md(fn())
                    if "help" in meta:
                        return str(meta["help"])
        return None

    def _auto_help(self, path: str) -> str:
        prefix = path.rstrip("/")
        lines = [f"# Help: {prefix or '/'}\n\n"]

        children = sorted(r for r in self._routes if r.startswith(prefix + "/") or r == prefix)
        if children:
            lines.append("Routes:\n")
            for r in children:
                kind, _ = self._routes[r]
                lines.append(f"  {r}  [{kind}]\n")

        app_found = False
        for app_prefix, help_text in sorted(self._app_mounts):
            if not prefix or app_prefix == prefix or app_prefix.startswith(prefix + "/"):
                lines.append(help_text)
                app_found = True

        if not any([children, app_found]):
            lines.append("No help registered for this path.\n")
        return "".join(lines)


def _list_dir_local(folder: str, dir_path: str, prefix: str, subpath: str) -> list[dict]:
    """Build entry list for _render_human's file-mount HTML view."""
    entries = []
    try:
        names = sorted(os.listdir(dir_path))
    except OSError:
        return entries
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(dir_path, name)
        rel = os.path.relpath(full, folder)
        stat = os.stat(full)
        entries.append({
            "name": name,
            "type": "dir" if os.path.isdir(full) else "file",
            "size": stat.st_size if os.path.isfile(full) else None,
            "path": prefix + "/" + rel,
        })
    return entries