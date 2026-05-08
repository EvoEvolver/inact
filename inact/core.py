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

from flask import Flask

from .pages import normalize_md, normalize_toml
from .render import render_markdown, render_toml, render_plain
from .utils import text_response

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
        self._mount_handlers: dict[str, any] = {}  # prefix -> {ext -> handler}
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

    def add_nav_item(self, label: str, href: str) -> None:
        """Register a human-nav entry for a mounted app."""
        from .render import register_nav_item
        register_nav_item(label, href)

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