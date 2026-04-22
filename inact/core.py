"""
Inact — AI-oriented Flask toolkit.

Provides:
  - inact_md / inact_toml   : content-typed route decorators
  - help()                  : register /.help for a path
  - mount()                 : serve a folder with /.ls and /.grep
  - Auto /_human/<path>     : HTML rendering for any registered route
  - Inherited /.help        : walks up the path tree automatically
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from typing import Callable

from flask import Flask, request, send_file

from .handlers import FileHandler
from .pages import normalize_md, normalize_toml
from .render import render_markdown, render_toml, render_plain, render_ls
from .utils import text_response, toml_str

# Matches pagination suffix: <file_subpath>/p/<page_number>
_PAGE_RE = re.compile(r"^(.+)/p/(\d+)$")


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
        self._mcp_mounts: dict[str, str] = {}     # prefix -> MCP server label
        self._a2a_mounts: dict[str, str] = {}     # prefix -> A2A agent URL
        self._website_mounts: dict[str, str] = {} # prefix -> base URL

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

    def mount(
        self,
        prefix: str,
        folder: str,
        handlers: list[FileHandler] | None = None,
        editable: bool | list[str] = False,
    ):
        """
        Mount *folder* under *prefix*.

        Provides:
          GET  {prefix}/.ls                   list root
          GET  {prefix}/<subpath>/.ls         list subdirectory
          GET  {prefix}/.grep?q=<term>        grep root
          GET  {prefix}/<subpath>/.grep?q=…   grep subdirectory
          GET  {prefix}/<file>                serve file (plain text / handler output)
          GET  {prefix}/<file>/.info          file metadata + page count
          GET  {prefix}/<file>/.download      download original raw file
          GET  {prefix}/<file>/p/<N>          page N of a paginated file (handler required)
          GET  {prefix}/<file>/.replace       show replace info (editable files only)
          POST {prefix}/<file>/.replace       overwrite file content (editable files only)

        *handlers* — list of :class:`FileHandler` instances for custom file rendering.
        *editable* — ``True`` makes all files writable; a list of glob patterns
        (e.g. ``["*.md", "notes/"]``) restricts editability to matching paths.
        Directory patterns must end with ``/`` and match recursively.
        """
        prefix = prefix.rstrip("/")
        abs_folder = os.path.abspath(folder)
        self._mounts[prefix] = abs_folder

        if handlers:
            h_dict: dict[str, FileHandler] = {}
            for handler in handlers:
                for ext in handler.extensions:
                    h_dict[ext.lower() if ext.startswith(".") else "." + ext.lower()] = handler
            self._mount_handlers[prefix] = h_dict

        if editable is not False:
            self._mount_editable[prefix] = editable

        app = self.app
        ep = "_inact_mount_" + prefix.replace("/", "__")

        @app.route(prefix + "/", defaults={"subpath": ""}, endpoint=ep + "_root", methods=["GET", "POST"])
        @app.route(prefix + "/<path:subpath>", endpoint=ep, methods=["GET", "POST"])
        def _mount_handler(subpath: str, _prefix=prefix, _folder=abs_folder):
            return self._handle_mount(_prefix, _folder, subpath)

    def mount_mcp(self, prefix: str, url: str) -> None:
        """Mount a URL-based MCP server (Streamable HTTP transport) at *prefix*."""
        from .mcp import McpClient
        self._attach_mcp(prefix, McpClient(url), label=url)

    def mount_mcp_npx(
        self,
        prefix: str,
        package: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """
        Mount an MCP server launched via ``npx`` at *prefix*.

        The server process is spawned lazily on the first request.
        ``-y`` is passed so npx auto-installs the package without prompting.

        Example::

            app.mount_mcp_npx("/fs", "@modelcontextprotocol/server-filesystem",
                              args=["--allowed-paths", "/tmp"])
        """
        from .mcp import StdioMcpClient
        client = StdioMcpClient("npx", ["-y", package, *(args or [])], env)
        self._attach_mcp(prefix, client, label=f"npx:{package}")

    def mount_mcp_uvx(
        self,
        prefix: str,
        package: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """
        Mount an MCP server launched via ``uvx`` at *prefix*.

        The server process is spawned lazily on the first request.

        Example::

            app.mount_mcp_uvx("/git", "mcp-server-git")
        """
        from .mcp import StdioMcpClient
        client = StdioMcpClient("uvx", [package, *(args or [])], env)
        self._attach_mcp(prefix, client, label=f"uvx:{package}")

    def mount_a2a(self, prefix: str, agent_url: str) -> None:
        """
        Mount a remote A2A agent at *prefix*.

        The agent's card is fetched lazily from
        ``{agent_url}/.well-known/agent.json`` on the first request.

        Registers:
          GET  {prefix}/.card    — agent card as TOML
          POST {prefix}/chat     — chat with the agent

        ``/chat`` accepts a JSON body::

            {"message": "hello", "context_id": "<optional-uuid>"}

        and returns plain text::

            # context_id: <uuid>

            Agent's reply here...

        Pass the returned ``context_id`` back in subsequent requests to
        continue the same conversation.

        Example::

            app.mount_a2a("/assistant", "https://agent.example.com")
        """
        from .a2a import A2AClient
        self._attach_a2a(prefix, A2AClient(agent_url), label=agent_url)

    def mount_website(self, prefix: str, base_url: str) -> None:
        """
        Mount a remote website at *prefix*.

        All GET requests are proxied to the remote site and returned as
        plain text (HTML is stripped) so AI agents can read them directly.
        Appending ``/.links`` to any path lists the hyperlinks on that page.

        The ``/_human/<prefix>/…`` view returns the original HTML so
        browsers can render the page normally.

        Registers:
          GET  {prefix}/                 — fetch base URL as plain text
          GET  {prefix}/<path>           — fetch sub-page as plain text
          GET  {prefix}/<path>/.links    — list hyperlinks on a page (TOML)

        Example::

            app.mount_website("/docs", "https://docs.example.com")
        """
        from .website import WebsiteProxy
        proxy = WebsiteProxy(base_url)
        prefix = "/" + prefix.strip("/")
        self._website_mounts[prefix] = base_url
        ep = "_inact_web_" + prefix.replace("/", "__")

        def _handler(subpath: str = ""):
            if subpath == ".links" or subpath.endswith("/.links"):
                page_sub = subpath[:-6].rstrip("/") if subpath.endswith("/.links") else ""
                return _serve_links(proxy, prefix, base_url, page_sub)
            params = dict(request.args) or None
            try:
                title, text, _ = proxy.fetch_text(subpath, params)
            except Exception as exc:
                return text_response(f"ERROR 502: {exc}\n", 502)
            page_url = base_url.rstrip("/") + ("/" + subpath.lstrip("/") if subpath else "/")
            header = f"# {title}\n# url: {page_url}\n\n" if title else f"# url: {page_url}\n\n"
            return text_response(header + text)

        self.app.add_url_rule(
            prefix + "/", endpoint=ep + "_root",
            view_func=lambda: _handler(""), defaults={},
        )
        self.app.add_url_rule(
            prefix + "/<path:subpath>", endpoint=ep, view_func=_handler,
        )

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
            "Use the `fetch` tool to access any route. You can also call curl directly:\n\n",
            f"  curl {base_url}/.help\n\n",
        ]

        if self._routes:
            lines.append("## Routes\n\n")
            for path in sorted(self._routes):
                kind, _ = self._routes[path]
                lines.append(f"  GET {path}  [{kind}]\n")
            lines.append("\n")

        if self._mounts:
            lines.append("## Mounted folders\n\n")
            for prefix in sorted(self._mounts):
                lines.append(f"  {prefix}/\n")
                lines.append(f"    GET  {prefix}/.ls                list files\n")
                lines.append(f"    GET  {prefix}/.grep?q=<term>     search content\n")
                lines.append(f"    GET  {prefix}/<file>             read (plain text)\n")
                lines.append(f"    GET  {prefix}/<file>/.info       metadata + page count\n")
                lines.append(f"    GET  {prefix}/<file>/.download   download raw bytes\n")
                if prefix in self._mount_handlers:
                    exts = ", ".join(sorted(self._mount_handlers[prefix]))
                    lines.append(f"    GET  {prefix}/<file>/p/<N>      paginate ({exts})\n")
                if prefix in self._mount_editable:
                    lines.append(f"    POST {prefix}/<file>/.replace   overwrite file\n")
                lines.append("\n")

        if self._routes or self._mounts:
            lines.append("## curl examples\n\n")
            for path in sorted(self._routes)[:2]:
                lines.append(f"  curl {base_url}{path}\n")
            for prefix in sorted(self._mounts)[:1]:
                lines.append(f"  curl {base_url}{prefix}/.ls\n")
                lines.append(f"  curl '{base_url}{prefix}/.grep?q=keyword'\n")
                lines.append(f"  curl {base_url}{prefix}/somefile.md\n")

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
                m = _PAGE_RE.match(subpath)
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
                    entries = _list_dir(folder, full, prefix, display_subpath)
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
                    from .website import WebsiteProxy
                    return _serve_links(WebsiteProxy(base_url), prefix, base_url, page_sub)
                params = dict(request.args) or None
                try:
                    from .website import WebsiteProxy
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
        children = sorted(
            r for r in self._routes if r.startswith(prefix + "/") or r == prefix
        )
        mount_children = sorted(
            p for p in self._mounts if p.startswith(prefix + "/") or p == prefix
        )
        mcp_children = sorted(
            p for p in self._mcp_mounts if p.startswith(prefix + "/") or p == prefix
        )
        a2a_children = sorted(
            p for p in self._a2a_mounts if p.startswith(prefix + "/") or p == prefix
        )
        web_children = sorted(
            p for p in self._website_mounts if p.startswith(prefix + "/") or p == prefix
        )
        lines = [f"# Help: {prefix or '/'}\n\n"]
        if children:
            lines.append("Routes:\n")
            for r in children:
                kind, _ = self._routes[r]
                lines.append(f"  {r}  [{kind}]\n")
        if mount_children:
            lines.append("\nMounted directories:\n")
            for p in mount_children:
                lines.append(f"  {p}/  (folder: {self._mounts[p]})\n")
                lines.append(f"    {p}/.ls               list files\n")
                lines.append(f"    {p}/.grep?q=…         search files\n")
                lines.append(f"    {p}/<file>/.download  download raw file\n")
                if p in self._mount_editable:
                    lines.append(f"    {p}/<file>/.replace   overwrite file (POST)\n")
        if mcp_children:
            lines.append("\nMounted MCP servers:\n")
            for p in mcp_children:
                lines.append(f"  {p}/  (url: {self._mcp_mounts[p]})\n")
                lines.append(f"    {p}/.tools              list tools\n")
                lines.append(f"    {p}/.resources          list resources\n")
                lines.append(f"    {p}/call/<name>         call a tool (POST, JSON body)\n")
                lines.append(f"    {p}/resource?uri=…      read a resource\n")
        if a2a_children:
            lines.append("\nMounted A2A agents:\n")
            for p in a2a_children:
                lines.append(f"  {p}/  (url: {self._a2a_mounts[p]})\n")
                lines.append(f"    {p}/               help & overview\n")
                lines.append(f"    {p}/.card          agent card (TOML)\n")
                lines.append(f"    {p}/chat           list conversations (GET) / send message (POST)\n")
        if web_children:
            lines.append("\nMounted websites:\n")
            for p in web_children:
                lines.append(f"  {p}/  (url: {self._website_mounts[p]})\n")
                lines.append(f"    {p}/<path>           fetch page as plain text\n")
                lines.append(f"    {p}/<path>/.links    list hyperlinks on page (TOML)\n")
        if not children and not mount_children and not mcp_children and not a2a_children and not web_children:
            lines.append("No help registered for this path.\n")
        return "".join(lines)

    # -----------------------------------------------------------------------
    # MCP route factory (shared by all three mount_mcp* methods)
    # -----------------------------------------------------------------------

    def _attach_mcp(self, prefix: str, client, label: str) -> None:
        """Register the four MCP proxy routes for *client* under *prefix*."""
        prefix = "/" + prefix.strip("/")
        self._mcp_mounts[prefix] = label
        ep = "_inact_mcp_" + prefix.replace("/", "__")

        def _tools():
            try:
                tools = client.list_tools()
            except Exception as exc:
                return text_response(f"ERROR 502: {exc}\n", 502)
            lines = [f"# {len(tools)} tool(s) from {label}\n\n"]
            for t in tools:
                lines.append("[[tools]]\n")
                lines.append(f"name = {toml_str(t['name'])}\n")
                lines.append(f"description = {toml_str(t.get('description', ''))}\n")
                call_url = prefix + "/call/" + t["name"]
                lines.append(f"call = {toml_str(call_url)}\n")
                schema = t.get("inputSchema")
                if schema:
                    lines.append(f"inputSchema = {toml_str(json.dumps(schema))}\n")
                lines.append("\n")
            return text_response("".join(lines))

        def _call(tool_name):
            try:
                arguments = request.get_json(force=True, silent=True) or {}
                content = client.call_tool(tool_name, arguments)
            except Exception as exc:
                return text_response(f"ERROR 502: {exc}\n", 502)
            parts = []
            for item in content:
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "image":
                    parts.append(f"[image/{item.get('mimeType', 'unknown')}]")
                else:
                    parts.append(json.dumps(item))
            return text_response("\n".join(parts))

        def _resources():
            try:
                resources = client.list_resources()
            except Exception as exc:
                return text_response(f"ERROR 502: {exc}\n", 502)
            lines = [f"# {len(resources)} resource(s) from {label}\n\n"]
            for r in resources:
                lines.append("[[resources]]\n")
                lines.append(f"uri = {toml_str(r['uri'])}\n")
                lines.append(f"name = {toml_str(r.get('name', r['uri']))}\n")
                if "description" in r:
                    lines.append(f"description = {toml_str(r['description'])}\n")
                if "mimeType" in r:
                    lines.append(f"mimeType = {toml_str(r['mimeType'])}\n")
                read_url = prefix + "/resource?uri=" + r["uri"]
                lines.append(f"read = {toml_str(read_url)}\n")
                lines.append("\n")
            return text_response("".join(lines))

        def _resource():
            uri = request.args.get("uri", "").strip()
            if not uri:
                return text_response(
                    f"ERROR 400: ?uri= required\nUsage: GET {prefix}/resource?uri=<uri>\n", 400
                )
            try:
                contents = client.read_resource(uri)
            except Exception as exc:
                return text_response(f"ERROR 502: {exc}\n", 502)
            parts = []
            for item in contents:
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "blob":
                    parts.append(f"[binary/{item.get('mimeType', 'application/octet-stream')}]")
                else:
                    parts.append(json.dumps(item))
            return text_response("\n".join(parts))

        self.app.add_url_rule(prefix + "/.tools", endpoint=ep + "_tools", view_func=_tools)
        self.app.add_url_rule(
            prefix + "/call/<tool_name>", endpoint=ep + "_call",
            view_func=_call, methods=["POST"],
        )
        self.app.add_url_rule(
            prefix + "/.resources", endpoint=ep + "_resources", view_func=_resources
        )
        self.app.add_url_rule(
            prefix + "/resource", endpoint=ep + "_resource", view_func=_resource
        )

    # -----------------------------------------------------------------------
    # A2A route factory
    # -----------------------------------------------------------------------

    def _attach_a2a(self, prefix: str, client, label: str) -> None:
        """Register routes for an A2A agent."""
        import uuid as _uuid
        from .a2a import _strip_none
        from .pages import dict_to_toml

        prefix = "/" + prefix.strip("/")
        self._a2a_mounts[prefix] = label
        ep = "_inact_a2a_" + prefix.replace("/", "__")

        # In-memory conversation log: context_id -> [{"role": ..., "text": ...}, ...]
        _history: dict[str, list[dict]] = {}

        def _help():
            try:
                card = client.card()
            except Exception:
                card = {}
            name = card.get("name", label)
            desc = card.get("description", "")
            skills = card.get("skills", [])
            lines = [f"# {name}\n"]
            if desc:
                lines.append(f"\n{desc}\n")
            lines.append(f"\n## Endpoints\n\n")
            lines.append(f"  GET  {prefix}/              this page\n")
            lines.append(f"  GET  {prefix}/.card         agent card (TOML)\n")
            lines.append(f"  GET  {prefix}/chat          list conversations\n")
            lines.append(f"  GET  {prefix}/chat?context_id=<uuid>  read a conversation\n")
            lines.append(f"  POST {prefix}/chat          send a message\n")
            lines.append(f"\n## Sending a message\n\n")
            lines.append(f"  POST {prefix}/chat\n")
            lines.append(f'  Body: {{"message": "hello", "context_id": "<optional>"}}\n\n')
            lines.append("The response includes a # context_id comment. Pass it back\n")
            lines.append("in subsequent requests to continue the same conversation.\n")
            if skills:
                lines.append("\n## Skills\n\n")
                for s in skills:
                    lines.append(f"  {s.get('name', s.get('id', ''))}")
                    if s.get("description"):
                        lines.append(f" — {s['description']}")
                    lines.append("\n")
            return text_response("".join(lines))

        def _card():
            try:
                raw = client.card()
            except Exception as exc:
                return text_response(f"ERROR 502: {exc}\n", 502)
            safe = _strip_none(raw)
            try:
                body = f"# Agent card: {label}\n\n" + dict_to_toml(safe)
            except Exception:
                body = f"# Agent card: {label}\n\n" + json.dumps(safe, indent=2)
            return text_response(body)

        def _chat():
            if request.method == "GET":
                context_id = request.args.get("context_id", "").strip()
                if context_id:
                    msgs = _history.get(context_id, [])
                    lines = [f"# Conversation: {context_id}\n",
                             f"# {len(msgs)} message(s)\n\n"]
                    for m in msgs:
                        lines.append(f"[{m['role']}] {m['text']}\n\n")
                    return text_response("".join(lines))
                # List all conversations
                lines = [f"# Conversations at {prefix}/chat\n",
                         f"# {len(_history)} conversation(s)\n\n"]
                for ctx_id, msgs in _history.items():
                    lines.append(f"[[conversations]]\n")
                    lines.append(f"context_id = {toml_str(ctx_id)}\n")
                    lines.append(f"messages = {len(msgs)}\n")
                    lines.append(f"url = {toml_str(f'{prefix}/chat?context_id={ctx_id}')}\n\n")
                return text_response("".join(lines))

            # POST — send a message
            body = request.get_json(force=True, silent=True) or {}
            message = (body.get("message") or "").strip()
            if not message:
                return text_response(
                    f"ERROR 400: 'message' field required\n"
                    f"Usage: POST {prefix}/chat\n"
                    f'Body: {{"message": "your text", "context_id": "<optional-uuid>"}}\n',
                    400,
                )
            context_id = (body.get("context_id") or body.get("contextId") or "").strip()
            if not context_id:
                context_id = str(_uuid.uuid4())

            _history.setdefault(context_id, []).append({"role": "user", "text": message})

            try:
                reply, context_id = client.send(message, context_id)
            except Exception as exc:
                _history[context_id].pop()  # remove the user msg we just logged
                return text_response(f"ERROR 502: {exc}\n", 502)

            _history[context_id].append({"role": "agent", "text": reply})

            status = 202 if reply.startswith("[input_required]") else 200
            return text_response(f"# context_id: {context_id}\n\n{reply}\n", status)

        self.app.add_url_rule(prefix + "/",      endpoint=ep + "_help", view_func=_help)
        self.app.add_url_rule(prefix + "/.card", endpoint=ep + "_card", view_func=_card)
        self.app.add_url_rule(
            prefix + "/chat", endpoint=ep + "_chat",
            view_func=_chat, methods=["GET", "POST"],
        )

    # -----------------------------------------------------------------------
    # Mount handler
    # -----------------------------------------------------------------------

    def _handle_mount(self, prefix: str, folder: str, subpath: str) -> tuple:
        # Delegate .help to the help system (mount route takes precedence over
        # the global /<path>/.help pattern when prefix is more specific)
        if subpath == ".help" or subpath.endswith("/.help"):
            file_part = subpath[:-5].rstrip("/") if subpath.endswith("/.help") else ""
            full_path = prefix + ("/" + file_part if file_part else "")
            return self._serve_help(full_path)

        # Route based on subpath suffix
        if subpath == "" or subpath == ".ls" or subpath.endswith("/.ls"):
            dir_sub = ""
            if subpath.endswith("/.ls"):
                dir_sub = subpath[:-3].rstrip("/")
            elif subpath == ".ls":
                dir_sub = ""
            else:
                dir_sub = subpath
            return self._serve_ls(prefix, folder, dir_sub)

        if subpath == ".grep" or subpath.endswith("/.grep"):
            q = request.args.get("q", "").strip()
            dir_sub = ""
            if subpath.endswith("/.grep"):
                dir_sub = subpath[:-6].rstrip("/")
            return self._serve_grep(prefix, folder, dir_sub, q)

        # File metadata (page count, handler type, etc.)
        if subpath.endswith("/.info"):
            file_sub = subpath[:-6].rstrip("/")
            return self._serve_info(prefix, folder, file_sub)

        # Raw file download
        if subpath.endswith("/.download"):
            file_sub = subpath[:-10].rstrip("/")
            return self._serve_download(prefix, folder, file_sub)

        # Editable file replace: GET shows info, POST writes new content
        if subpath.endswith("/.replace"):
            file_sub = subpath[:-9].rstrip("/")
            if request.method == "POST":
                return self._serve_replace(prefix, folder, file_sub)
            return self._serve_replace_info(prefix, folder, file_sub)

        # Pagination: <file>/p/<N> — only activates when a handler is registered
        m = _PAGE_RE.match(subpath)
        if m:
            file_sub, page = m.group(1), int(m.group(2))
            _, ext = os.path.splitext(file_sub.lower())
            if ext in self._mount_handlers.get(prefix, {}):
                return self._serve_file(prefix, folder, file_sub, page)

        return self._serve_file(prefix, folder, subpath)

    def _serve_ls(self, prefix: str, folder: str, subpath: str) -> tuple:
        dir_path = os.path.normpath(os.path.join(folder, subpath)) if subpath else folder
        if not dir_path.startswith(folder):
            return text_response("ERROR 403: Forbidden\n", 403)
        if not os.path.isdir(dir_path):
            return text_response(f"ERROR 404: Not a directory: {subpath}\n", 404)

        entries = _list_dir(folder, dir_path, prefix, subpath)
        handlers = self._mount_handlers.get(prefix, {})
        url_base = prefix + ("/" + subpath if subpath else "")
        lines = [
            f"# Directory listing: {url_base}\n",
            f"# {len(entries)} entries\n",
            f"# tip: append /.download to any file path to get the raw file\n\n",
        ]
        for e in entries:
            lines.append("[[entries]]\n")
            lines.append(f'name = {toml_str(e["name"])}\n')
            lines.append(f'type = {toml_str(e["type"])}\n')
            lines.append(f'path = {toml_str(e["path"])}\n')
            if e.get("size") is not None:
                lines.append(f'size = {e["size"]}\n')
            if e["type"] == "file":
                _, ext = os.path.splitext(e["name"].lower())
                if ext in handlers:
                    lines.append(f'handler = {toml_str(type(handlers[ext]).__name__)}\n')
                    lines.append(f'info = {toml_str(e["path"] + "/.info")}\n')
                rel_file = os.path.relpath(
                    os.path.join(dir_path, e["name"]), folder
                )
                if self._is_editable(prefix, rel_file):
                    lines.append(f'editable = true\n')
                    lines.append(f'replace = {toml_str(e["path"] + "/.replace")}\n')
            lines.append("\n")
        return text_response("".join(lines))

    def _serve_grep(self, prefix: str, folder: str, subpath: str, query: str) -> tuple:
        if not query:
            base = prefix + ("/" + subpath if subpath else "")
            return text_response(
                f"ERROR 400: Missing query parameter.\n\nUsage: GET {base}/.grep?q=keyword\n",
                400,
            )
        search_dir = os.path.normpath(os.path.join(folder, subpath)) if subpath else folder
        if not search_dir.startswith(folder):
            return text_response("ERROR 403: Forbidden\n", 403)

        matches = []
        q_lower = query.lower()
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
            for fname in sorted(files):
                if fname.startswith("."):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if q_lower in line.lower():
                                rel = os.path.relpath(fpath, folder)
                                matches.append((rel, lineno, line.rstrip()))
                                if len(matches) >= 200:
                                    break
                except OSError:
                    pass
                if len(matches) >= 200:
                    break
            if len(matches) >= 200:
                break

        url_base = prefix + ("/" + subpath if subpath else "")
        lines = [
            f"# Grep: {toml_str(query)} in {url_base}\n",
            f"# {len(matches)} match(es)\n\n",
        ]
        for rel_path, lineno, line_text in matches:
            lines.append("[[matches]]\n")
            lines.append(f'file = {toml_str(prefix + "/" + rel_path)}\n')
            lines.append(f"line = {lineno}\n")
            lines.append(f"text = {toml_str(line_text)}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _serve_file(self, prefix: str, folder: str, subpath: str, page: int = 1) -> tuple:
        safe = os.path.normpath(os.path.join(folder, subpath))
        if not safe.startswith(folder):
            return text_response("ERROR 403: Path traversal denied\n", 403)
        if not os.path.isfile(safe):
            return text_response(f"ERROR 404: File not found: {subpath}\n", 404)

        _, ext = os.path.splitext(subpath.lower())
        handler = self._mount_handlers.get(prefix, {}).get(ext)
        if handler is not None:
            content, status = handler.serve(safe, prefix + "/" + subpath, page)
            return text_response(content, status)

        try:
            content = open(safe, encoding="utf-8").read()
        except Exception as e:
            return text_response(f"ERROR 500: {e}\n", 500)
        return text_response(content)

    def _serve_info(self, prefix: str, folder: str, subpath: str) -> tuple:
        if not subpath:
            return text_response("ERROR 400: .info requires a file path\n", 400)
        safe = os.path.normpath(os.path.join(folder, subpath))
        if not safe.startswith(folder):
            return text_response("ERROR 403: Path traversal denied\n", 403)
        if not os.path.isfile(safe):
            return text_response(f"ERROR 404: File not found: {subpath}\n", 404)

        virtual_path = prefix + "/" + subpath
        stat = os.stat(safe)
        _, ext = os.path.splitext(subpath.lower())
        handler = self._mount_handlers.get(prefix, {}).get(ext)

        lines = [
            f"# File info: {virtual_path}\n\n",
            f"path = {toml_str(virtual_path)}\n",
            f"size = {stat.st_size}\n",
            f"download = {toml_str(virtual_path + '/.download')}\n",
        ]
        if handler is not None:
            lines.append(f"handler = {toml_str(type(handler).__name__)}\n")
            try:
                pages = handler.page_count(safe)
                if pages is not None:
                    lines.append(f"pages = {pages}\n")
                    lines.append(f"page_url_pattern = {toml_str(virtual_path + '/p/{{N}}')}\n")
                    lines.append(f"first_page = {toml_str(virtual_path + '/p/1')}\n")
            except Exception:
                pass
        if self._is_editable(prefix, subpath):
            lines.append(f"editable = true\n")
            lines.append(f"replace = {toml_str(virtual_path + '/.replace')}\n")
        return text_response("".join(lines))

    def _is_editable(self, prefix: str, subpath: str) -> bool:
        spec = self._mount_editable.get(prefix, False)
        if spec is False:
            return False
        if spec is True:
            return True
        for pattern in spec:
            if pattern.endswith("/"):
                # directory prefix — match recursively
                if subpath == pattern.rstrip("/") or subpath.startswith(pattern):
                    return True
            elif fnmatch.fnmatch(subpath, pattern):
                return True
        return False

    def _serve_download(self, prefix: str, folder: str, subpath: str):
        if not subpath:
            return text_response("ERROR 400: /.download requires a file path\n", 400)
        safe = os.path.normpath(os.path.join(folder, subpath))
        if not safe.startswith(folder):
            return text_response("ERROR 403: Path traversal denied\n", 403)
        if not os.path.isfile(safe):
            return text_response(f"ERROR 404: File not found: {subpath}\n", 404)
        return send_file(safe, as_attachment=True, download_name=os.path.basename(safe))

    def _serve_replace_info(self, prefix: str, folder: str, subpath: str) -> tuple:
        if not subpath:
            return text_response("ERROR 400: /.replace requires a file path\n", 400)
        safe = os.path.normpath(os.path.join(folder, subpath))
        if not safe.startswith(folder):
            return text_response("ERROR 403: Path traversal denied\n", 403)
        if not os.path.isfile(safe):
            return text_response(f"ERROR 404: File not found: {subpath}\n", 404)

        virtual_path = prefix + "/" + subpath
        editable = self._is_editable(prefix, subpath)
        stat = os.stat(safe)

        lines = [
            f"# /.replace — {virtual_path}\n\n",
            f"path = {toml_str(virtual_path)}\n",
            f"size = {stat.st_size}\n",
            f"editable = {str(editable).lower()}\n",
        ]
        if editable:
            lines += [
                f"\n# POST the new file content to this URL to overwrite the file.\n",
                f"# Example:\n",
                f"#   curl -X POST {virtual_path}/.replace \\\n",
                f"#        -H 'Content-Type: text/plain' \\\n",
                f"#        --data-binary @local_file.txt\n",
            ]
        else:
            lines.append("\n# This file is not marked as editable in the mount configuration.\n")
        return text_response("".join(lines))

    def _serve_replace(self, prefix: str, folder: str, subpath: str) -> tuple:
        if not subpath:
            return text_response("ERROR 400: /.replace requires a file path\n", 400)
        if not self._is_editable(prefix, subpath):
            return text_response(
                f"ERROR 403: {prefix}/{subpath} is not marked as editable\n", 403
            )
        safe = os.path.normpath(os.path.join(folder, subpath))
        if not safe.startswith(folder):
            return text_response("ERROR 403: Path traversal denied\n", 403)
        if not os.path.isfile(safe):
            return text_response(f"ERROR 404: File not found: {subpath}\n", 404)

        data = request.get_data()
        try:
            with open(safe, "wb") as f:
                f.write(data)
        except OSError as e:
            return text_response(f"ERROR 500: {e}\n", 500)

        virtual_path = prefix + "/" + subpath
        return text_response(
            f"OK\npath = {toml_str(virtual_path)}\nbytes_written = {len(data)}\n"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serve_links(proxy, prefix: str, base_url: str, page_sub: str) -> tuple:
    from .utils import text_response, toml_str
    params = None  # query params not forwarded for .links (avoids import of request here)
    try:
        _, _, links = proxy.fetch_text(page_sub, params)
    except Exception as exc:
        return text_response(f"ERROR 502: {exc}\n", 502)
    page_url = base_url.rstrip("/") + ("/" + page_sub.lstrip("/") if page_sub else "/")
    lines = [f"# Links on: {page_url}\n", f"# {len(links)} link(s)\n\n"]
    for href, link_text in links:
        lines.append("[[links]]\n")
        lines.append(f"url = {toml_str(href)}\n")
        lines.append(f"text = {toml_str(link_text)}\n")
        lines.append("\n")
    return text_response("".join(lines))


def _list_dir(folder: str, dir_path: str, prefix: str, subpath: str) -> list[dict]:
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
        url_path = prefix + "/" + rel
        stat = os.stat(full)
        entries.append({
            "name": name,
            "type": "dir" if os.path.isdir(full) else "file",
            "size": stat.st_size if os.path.isfile(full) else None,
            "path": url_path,
        })
    return entries
