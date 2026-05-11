"""
Inact — AI-oriented FastAPI toolkit.

Provides:
  - inact_md / inact_toml   : content-typed route decorators
  - help()                  : register /.help for a path
  - Auto /_human/<path>     : HTML rendering for any registered route
  - Inherited /.help        : walks up the path tree automatically
"""

from __future__ import annotations

import inspect
import os
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from .pages import normalize_md, normalize_toml
from .render import render_markdown, render_toml, render_plain
from .utils import text_response

# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------

_RouteEntry = tuple[str, Callable]  # ("md" | "toml", fn)


class _BodyCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        body = await request.body()
        request.state.body = body
        return await call_next(request)


class Inact:
    def __init__(self, import_name_or_app: str | FastAPI = __name__):
        if isinstance(import_name_or_app, FastAPI):
            self.app = import_name_or_app
        else:
            self.app = FastAPI(
                title=import_name_or_app,
                docs_url=None,
                redoc_url=None,
                openapi_url=None,
            )

        self._routes: dict[str, _RouteEntry] = {}
        self._help: dict[str, Callable | str] = {}
        self._mounts: dict[str, str] = {}
        self._mount_handlers: dict[str, any] = {}
        self._mount_editable: dict[str, bool | list[str]] = {}
        self._website_mounts: dict[str, str] = {}
        self._app_mounts: list[tuple[str, str]] = []
        self._human_views: dict[str, Callable] = {}

        # Body cache must be innermost (added first); auth added later by mount_auth
        self.app.add_middleware(_BodyCacheMiddleware)

        # Plain-text validation errors instead of FastAPI's default JSON
        @self.app.exception_handler(RequestValidationError)
        async def _validation_error(request: Request, exc: RequestValidationError):
            detail = "; ".join(
                f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}"
                for e in exc.errors()
            )
            return text_response(f"ERROR 422: {detail}\n", 422)

        self._register_builtins()

    # -----------------------------------------------------------------------
    # Public decorators / methods
    # -----------------------------------------------------------------------

    def inact_md(self, path: str):
        """Register a Markdown + YAML-frontmatter route."""
        def decorator(fn: Callable):
            self._add_route(path, "md", fn)
            return fn
        return decorator

    def inact_toml(self, path: str):
        """Register a TOML route (plain text for agents, HTML for humans)."""
        def decorator(fn: Callable):
            self._add_route(path, "toml", fn)
            return fn
        return decorator

    def help(self, path: str):
        """Register help text for a path (used by /.help)."""
        def decorator(fn: Callable | str):
            self._help[path.rstrip("/") or "/"] = fn
            return fn
        return decorator

    def route(self, path: str, **kwargs):
        """Decorator that registers a route on the FastAPI app."""
        def decorator(fn: Callable):
            self.app.add_api_route(path, fn, **kwargs)
            return fn
        return decorator

    def add_nav_item(self, label: str, href: str) -> None:
        from .render import register_nav_item
        register_nav_item(label, href)

    def run(self, **kwargs):
        import uvicorn
        host = kwargs.pop("host", "127.0.0.1")
        port = kwargs.pop("port", 8000)
        debug = kwargs.pop("debug", False)
        uvicorn.run(self.app, host=host, port=port, reload=debug)

    # -----------------------------------------------------------------------
    # Built-in global routes
    # -----------------------------------------------------------------------

    def _register_builtins(self):
        def _human(request: Request, subpath: str = ""):
            return self._render_human("/" + subpath, request)

        def _root_help(request: Request):
            return self._serve_help("/")

        def _path_help(subpath: str, request: Request):
            return self._serve_help("/" + subpath)

        self.app.add_api_route("/_human", _human, methods=["GET"])
        self.app.add_api_route("/_human/", _human, methods=["GET"])
        self.app.add_api_route("/_human/{subpath:path}", _human, methods=["GET"])
        self.app.add_api_route("/.help", _root_help, methods=["GET"])
        self.app.add_api_route("/{subpath:path}/.help", _path_help, methods=["GET"])

    # -----------------------------------------------------------------------
    # Route registration helpers
    # -----------------------------------------------------------------------

    def _add_route(self, path: str, kind: str, fn: Callable):
        _needs_request = "request" in inspect.signature(fn).parameters
        self._routes[path] = (kind, fn, _needs_request)

        def _handler(request: Request, _kind=kind, _fn=fn, _nr=_needs_request):
            return self._serve_plain(_kind, _fn, request if _nr else None)

        self.app.add_api_route(path, _handler, methods=["GET"])

    def _serve_plain(self, kind: str, fn: Callable, request=None) -> Response:
        value = fn(request=request) if request is not None else fn()
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

    def _render_human(self, path: str, request: Request | None = None) -> Response:
        for prefix, view_fn in self._human_views.items():
            if path == prefix or path.startswith(prefix + "/"):
                if "request" in inspect.signature(view_fn).parameters and request is not None:
                    return view_fn(path, request=request)
                return view_fn(path)

        if path in self._routes:
            kind, fn, needs_req = self._routes[path]
            if needs_req and request is not None:
                value = fn(request=request)
            elif not needs_req:
                value = fn()
            else:
                value = None
            if value is not None:
                if kind == "md":
                    return render_markdown(value, path)
                if kind == "toml":
                    return render_toml(value, path)
                return render_plain(str(value), path)

        return text_response(f"ERROR 404: No human view for {path}\n", 404)

    # -----------------------------------------------------------------------
    # /.help
    # -----------------------------------------------------------------------

    def _serve_help(self, path: str) -> Response:
        text = self._find_help(path)
        if text:
            return text_response(text)
        stub = self._auto_help(path)
        return text_response(stub)

    def _find_help(self, path: str) -> str | None:
        parts = [p for p in path.rstrip("/").split("/") if p]
        for depth in range(len(parts), -1, -1):
            candidate = "/" + "/".join(parts[:depth])
            if candidate in self._help:
                entry = self._help[candidate]
                return entry() if callable(entry) else entry
            if candidate in self._routes:
                kind, fn, needs_req = self._routes[candidate]
                if kind == "md" and not needs_req:
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
                kind, _, _nr = self._routes[r]
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
