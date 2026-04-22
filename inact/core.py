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

import os
from typing import Callable

from flask import Flask, request

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

    def mount(self, prefix: str, folder: str):
        """
        Mount *folder* under *prefix*.

        Provides:
          GET {prefix}/.ls                   list root
          GET {prefix}/<subpath>/.ls         list subdirectory
          GET {prefix}/.grep?q=<term>        grep root
          GET {prefix}/<subpath>/.grep?q=…   grep subdirectory
          GET {prefix}/<file>                serve file (plain text)
        """
        prefix = prefix.rstrip("/")
        abs_folder = os.path.abspath(folder)
        self._mounts[prefix] = abs_folder

        app = self.app
        ep = "_inact_mount_" + prefix.replace("/", "__")

        @app.route(prefix + "/", defaults={"subpath": ""}, endpoint=ep + "_root")
        @app.route(prefix + "/<path:subpath>", endpoint=ep)
        def _mount_handler(subpath: str, _prefix=prefix, _folder=abs_folder):
            return self._handle_mount(_prefix, _folder, subpath)

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
                full = os.path.normpath(os.path.join(folder, subpath)) if subpath else folder
                if not full.startswith(folder):
                    return text_response("ERROR 403: Forbidden\n", 403)

                if os.path.isdir(full):
                    entries = _list_dir(folder, full, prefix, subpath)
                    return render_ls(entries, path, prefix + ("/" + subpath if subpath else ""))

                if os.path.isfile(full):
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
        if not children and not mount_children:
            lines.append("No help registered for this path.\n")
        return "".join(lines)

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

        return self._serve_file(prefix, folder, subpath)

    def _serve_ls(self, prefix: str, folder: str, subpath: str) -> tuple:
        dir_path = os.path.normpath(os.path.join(folder, subpath)) if subpath else folder
        if not dir_path.startswith(folder):
            return text_response("ERROR 403: Forbidden\n", 403)
        if not os.path.isdir(dir_path):
            return text_response(f"ERROR 404: Not a directory: {subpath}\n", 404)

        entries = _list_dir(folder, dir_path, prefix, subpath)
        url_base = prefix + ("/" + subpath if subpath else "")
        lines = [f"# Directory listing: {url_base}\n", f"# {len(entries)} entries\n\n"]
        for e in entries:
            lines.append("[[entries]]\n")
            lines.append(f'name = {toml_str(e["name"])}\n')
            lines.append(f'type = {toml_str(e["type"])}\n')
            lines.append(f'path = {toml_str(e["path"])}\n')
            if e.get("size") is not None:
                lines.append(f'size = {e["size"]}\n')
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

    def _serve_file(self, prefix: str, folder: str, subpath: str) -> tuple:
        safe = os.path.normpath(os.path.join(folder, subpath))
        if not safe.startswith(folder):
            return text_response("ERROR 403: Path traversal denied\n", 403)
        if not os.path.isfile(safe):
            return text_response(f"ERROR 404: File not found: {subpath}\n", 404)
        try:
            content = open(safe, encoding="utf-8").read()
        except Exception as e:
            return text_response(f"ERROR 500: {e}\n", 500)
        return text_response(content)


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
