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

import json
import os
import re
from typing import Callable

from flask import Flask, request

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
        self._mcp_mounts: dict[str, str] = {}  # prefix -> MCP server URL

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

    def mount(self, prefix: str, folder: str, handlers: list[FileHandler] | None = None):
        """
        Mount *folder* under *prefix*.

        Provides:
          GET {prefix}/.ls                   list root
          GET {prefix}/<subpath>/.ls         list subdirectory
          GET {prefix}/.grep?q=<term>        grep root
          GET {prefix}/<subpath>/.grep?q=…   grep subdirectory
          GET {prefix}/<file>                serve file (plain text)
          GET {prefix}/<file>/.info          file metadata + page count
          GET {prefix}/<file>/p/<N>          page N of a paginated file (handler required)

        Pass *handlers* to inject custom renderers for specific file types, e.g.::

            app.mount("/docs", "./docs", handlers=[PDFHandler()])
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

        app = self.app
        ep = "_inact_mount_" + prefix.replace("/", "__")

        @app.route(prefix + "/", defaults={"subpath": ""}, endpoint=ep + "_root")
        @app.route(prefix + "/<path:subpath>", endpoint=ep)
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

    def route(self, path: str, **kwargs):
        """Pass-through to Flask's @app.route."""
        return self.app.route(path, **kwargs)

    def run(self, **kwargs):
        self.app.run(**kwargs)

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
                lines.append(f"    {p}/.ls          list files\n")
                lines.append(f"    {p}/.grep?q=…    search files\n")
        if mcp_children:
            lines.append("\nMounted MCP servers:\n")
            for p in mcp_children:
                lines.append(f"  {p}/  (url: {self._mcp_mounts[p]})\n")
                lines.append(f"    {p}/.tools              list tools\n")
                lines.append(f"    {p}/.resources          list resources\n")
                lines.append(f"    {p}/call/<name>         call a tool (POST, JSON body)\n")
                lines.append(f"    {p}/resource?uri=…      read a resource\n")
        if not children and not mount_children and not mcp_children:
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
        lines = [f"# Directory listing: {url_base}\n", f"# {len(entries)} entries\n\n"]
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
        return text_response("".join(lines))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
