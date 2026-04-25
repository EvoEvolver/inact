"""
File system mount for inact.

mount_files(inact_app, prefix, folder_or_fs, handlers=None, editable=False)

  folder_or_fs — a local path string OR any :class:`FileSystem` instance
                 (e.g. :class:`~inact.apps.s3.S3FS` for S3-backed storage).

Registers:
  GET  {prefix}/                    list root (TOML)
  GET  {prefix}/<path>/.ls          list sub-directory
  GET  {prefix}/.grep?q=<term>      search content
  GET  {prefix}/<file>              serve file (plain text / handler)
  GET  {prefix}/<file>/.info        metadata + page count
  GET  {prefix}/<file>/.download    raw download
  GET  {prefix}/<file>/p/<N>        paginated (handler required)
  POST {prefix}/<file>/.append      append line/row
  GET  {prefix}/<file>/.replace     replace info  (editable only)
  POST {prefix}/<file>/.replace     overwrite file (editable only)
"""

from __future__ import annotations

import csv
import fnmatch
import io
import os
import re
import tempfile

from flask import request, send_file

from ..utils import text_response, toml_str

PAGE_RE = re.compile(r"^(.+)/p/(\d+)$")


# ---------------------------------------------------------------------------
# FileSystem abstraction
# ---------------------------------------------------------------------------

class FileSystem:
    """
    Storage backend abstraction used by :func:`mount_files`.

    Implement :meth:`list`, :meth:`get_bytes`, :meth:`put`, :meth:`head`,
    and :meth:`exists`.  Everything else has default implementations.
    """

    def list(self, subpath: str = "") -> tuple[list[str], list[dict]]:
        """
        Return ``(subdirs, files)`` at *subpath*.
        Each file dict must have ``"name"`` and ``"size"``; extra keys are
        shown as-is in the listing.
        """
        raise NotImplementedError

    def get_bytes(self, subpath: str) -> bytes:
        raise NotImplementedError

    def put(self, subpath: str, data: bytes) -> None:
        raise NotImplementedError

    def head(self, subpath: str) -> dict:
        """Return at minimum ``{"size": int}``."""
        raise NotImplementedError

    def exists(self, subpath: str) -> bool:
        raise NotImplementedError

    def grep(self, subpath: str, query: str,
             max_results: int = 200) -> list[tuple[str, int, str]]:
        """Return list of ``(relative_path, lineno, line_text)`` matches."""
        _, files = self.list(subpath)
        q = query.lower()
        matches: list[tuple[str, int, str]] = []
        for f in files:
            key = (subpath + "/" + f["name"]).lstrip("/")
            try:
                text = self.get_text(key)
            except Exception:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if q in line.lower():
                    matches.append((key, lineno, line))
                    if len(matches) >= max_results:
                        return matches
        return matches

    # ------------------------------------------------------------------
    # Helpers with defaults

    def get_text(self, subpath: str) -> str:
        return self.get_bytes(subpath).decode("utf-8", errors="replace")

    def append(self, subpath: str, data: bytes) -> None:
        self.put(subpath, self.get_bytes(subpath) + data)

    def download_to_temp(self, subpath: str) -> tuple[str, bool]:
        """
        Return ``(local_path, should_unlink)`` for handler use.
        If ``should_unlink`` is True the caller must ``os.unlink`` the path
        when done; if False the path is the original file and must not be deleted.
        """
        content = self.get_bytes(subpath)
        suffix = os.path.splitext(subpath)[1]
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        f.write(content)
        f.close()
        return f.name, True

    def label(self) -> str:
        """Short label shown in help text."""
        return ""


# ---------------------------------------------------------------------------
# Local filesystem backend
# ---------------------------------------------------------------------------

class LocalFS(FileSystem):
    """Local directory backed :class:`FileSystem`."""

    def __init__(self, folder: str):
        self.folder = os.path.abspath(folder)

    def _safe(self, subpath: str) -> str:
        path = os.path.normpath(os.path.join(self.folder, subpath.lstrip("/"))) \
               if subpath else self.folder
        if not path.startswith(self.folder):
            raise PermissionError("Path traversal denied")
        return path

    def list(self, subpath: str = "") -> tuple[list[str], list[dict]]:
        dir_path = self._safe(subpath)
        if not os.path.isdir(dir_path):
            raise FileNotFoundError(subpath or self.folder)
        subdirs, files = [], []
        for name in sorted(os.listdir(dir_path)):
            if name.startswith("."):
                continue
            full = os.path.join(dir_path, name)
            if os.path.isdir(full):
                subdirs.append(name)
            else:
                files.append({"name": name, "size": os.stat(full).st_size})
        return subdirs, files

    def get_bytes(self, subpath: str) -> bytes:
        with open(self._safe(subpath), "rb") as f:
            return f.read()

    def get_text(self, subpath: str) -> str:
        with open(self._safe(subpath), encoding="utf-8", errors="replace") as f:
            return f.read()

    def put(self, subpath: str, data: bytes) -> None:
        with open(self._safe(subpath), "wb") as f:
            f.write(data)

    def append(self, subpath: str, data: bytes) -> None:
        with open(self._safe(subpath), "ab") as f:
            f.write(data)

    def head(self, subpath: str) -> dict:
        stat = os.stat(self._safe(subpath))
        return {"size": stat.st_size}

    def exists(self, subpath: str) -> bool:
        try:
            return os.path.isfile(self._safe(subpath))
        except PermissionError:
            return False

    def download_to_temp(self, subpath: str) -> tuple[str, bool]:
        return self._safe(subpath), False  # already local — never delete

    def grep(self, subpath: str, query: str,
             max_results: int = 200) -> list[tuple[str, int, str]]:
        search_dir = self._safe(subpath) if subpath else self.folder
        q = query.lower()
        matches: list[tuple[str, int, str]] = []
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
            for fname in sorted(files):
                if fname.startswith("."):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if q in line.lower():
                                rel = os.path.relpath(fpath, self.folder)
                                matches.append((rel, lineno, line.rstrip()))
                                if len(matches) >= max_results:
                                    return matches
                except OSError:
                    pass
        return matches

    def label(self) -> str:
        return self.folder


# ---------------------------------------------------------------------------
# Editable helper
# ---------------------------------------------------------------------------

def _is_editable(spec, subpath: str) -> bool:
    if spec is False:
        return False
    if spec is True:
        return True
    for pattern in spec:
        if pattern.endswith("/"):
            if subpath == pattern.rstrip("/") or subpath.startswith(pattern):
                return True
        elif fnmatch.fnmatch(subpath, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Route handlers  (all take a FileSystem instance)
# ---------------------------------------------------------------------------

def serve_ls(fs: FileSystem, prefix: str, subpath: str,
             handlers: dict, editable_spec=False) -> tuple:
    url_base = prefix + ("/" + subpath if subpath else "")
    try:
        subdirs, files = fs.list(subpath)
    except PermissionError:
        return text_response("ERROR 403: Forbidden\n", 403)
    except FileNotFoundError:
        return text_response(f"ERROR 404: Not a directory: {subpath}\n", 404)
    except Exception as exc:
        return text_response(f"ERROR 502: {exc}\n", 502)

    total = len(subdirs) + len(files)
    lines = [
        f"# Directory listing: {url_base}\n",
        f"# {total} entries\n",
        "# tip: append /.download to any file path to get the raw file\n\n",
    ]
    for name in subdirs:
        path = (prefix + "/" + subpath + "/" + name).replace("//", "/")
        lines += ["[[entries]]\n", f'name = {toml_str(name + "/")}\n',
                  'type = "dir"\n', f'path = {toml_str(path + "/")}\n', "\n"]
    for f in files:
        path = (prefix + "/" + subpath + "/" + f["name"]).replace("//", "/")
        _, ext = os.path.splitext(f["name"].lower())
        lines.append("[[entries]]\n")
        lines.append(f'name = {toml_str(f["name"])}\n')
        lines.append('type = "file"\n')
        lines.append(f'path = {toml_str(path)}\n')
        if f.get("size") is not None:
            lines.append(f'size = {f["size"]}\n')
        for k, v in f.items():
            if k not in ("name", "size"):
                lines.append(f'{k} = {toml_str(str(v))}\n')
        if ext in handlers:
            lines.append(f'handler = {toml_str(type(handlers[ext]).__name__)}\n')
            lines.append(f'info   = {toml_str(path + "/.info")}\n')
            lines.append(f'append = {toml_str(path + "/.append")}\n')
        if _is_editable(editable_spec, f["name"]):
            lines.append("editable = true\n")
            lines.append(f'replace  = {toml_str(path + "/.replace")}\n')
        lines.append("\n")
    return text_response("".join(lines))


def serve_grep(fs: FileSystem, prefix: str, subpath: str, query: str) -> tuple:
    if not query:
        url_base = prefix + ("/" + subpath if subpath else "")
        return text_response(
            f"ERROR 400: Missing query.\n\nUsage: GET {url_base}/.grep?q=keyword\n", 400
        )
    try:
        matches = fs.grep(subpath, query)
    except Exception as exc:
        return text_response(f"ERROR 502: {exc}\n", 502)

    url_base = prefix + ("/" + subpath if subpath else "")
    lines = [f"# Grep: {toml_str(query)} in {url_base}\n",
             f"# {len(matches)} match(es)\n\n"]
    for rel, lineno, line_text in matches:
        lines += ["[[matches]]\n", f'file = {toml_str(prefix + "/" + rel)}\n',
                  f"line = {lineno}\n", f"text = {toml_str(line_text)}\n", "\n"]
    return text_response("".join(lines))


def serve_file(fs: FileSystem, prefix: str, subpath: str,
               handlers: dict, page: int = 1) -> tuple:
    _, ext = os.path.splitext(subpath.lower())
    handler = handlers.get(ext)
    virtual_path = prefix + "/" + subpath

    if handler is not None:
        tmp, owned = fs.download_to_temp(subpath)
        try:
            content, status = handler.serve(tmp, virtual_path, page)
        finally:
            if owned:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return text_response(content, status)

    try:
        text = fs.get_text(subpath)
    except PermissionError:
        return text_response("ERROR 403: Forbidden\n", 403)
    except Exception as exc:
        return text_response(f"ERROR 404: {exc}\n", 404)
    return text_response(text)


def serve_info(fs: FileSystem, prefix: str, subpath: str,
               handlers: dict, editable_spec=False) -> tuple:
    if not subpath:
        return text_response("ERROR 400: /.info requires a file path\n", 400)
    try:
        meta = fs.head(subpath)
    except Exception as exc:
        return text_response(f"ERROR 404: {exc}\n", 404)

    virtual_path = prefix + "/" + subpath
    _, ext = os.path.splitext(subpath.lower())
    handler = handlers.get(ext)

    lines = [f"# File info: {virtual_path}\n\n",
             f"path     = {toml_str(virtual_path)}\n",
             f"size     = {meta['size']}\n",
             f"download = {toml_str(virtual_path + '/.download')}\n"]
    for k, v in meta.items():
        if k != "size":
            lines.append(f"{k} = {toml_str(str(v))}\n")

    if handler is not None:
        lines.append(f"handler = {toml_str(type(handler).__name__)}\n")
        tmp, owned = fs.download_to_temp(subpath)
        try:
            pages = handler.page_count(tmp)
            if pages is not None:
                lines.append(f"pages            = {pages}\n")
                lines.append(f"page_url_pattern = {toml_str(virtual_path + '/p/{N}')}\n")
                lines.append(f"first_page       = {toml_str(virtual_path + '/p/1')}\n")
                lines.append(f"append           = {toml_str(virtual_path + '/.append')}\n")
        except Exception:
            pass
        finally:
            if owned:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    if _is_editable(editable_spec, subpath):
        lines.append("editable = true\n")
        lines.append(f"replace  = {toml_str(virtual_path + '/.replace')}\n")
    return text_response("".join(lines))


def serve_download(fs: FileSystem, prefix: str, subpath: str):
    if not subpath:
        return text_response("ERROR 400: /.download requires a file path\n", 400)
    try:
        content = fs.get_bytes(subpath)
    except PermissionError:
        return text_response("ERROR 403: Forbidden\n", 403)
    except Exception as exc:
        return text_response(f"ERROR 404: {exc}\n", 404)
    fname = os.path.basename(subpath)
    # If it's a local path, use send_file for efficiency
    if isinstance(fs, LocalFS):
        return send_file(fs._safe(subpath), as_attachment=True, download_name=fname)
    from flask import Response
    return Response(content,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'},
                    content_type="application/octet-stream")


def serve_append(fs: FileSystem, prefix: str, subpath: str, handlers: dict) -> tuple:
    if not subpath:
        return text_response("ERROR 400: /.append requires a file path\n", 400)
    if not fs.exists(subpath):
        return text_response(f"ERROR 404: not found: {subpath}\n", 404)

    _, ext = os.path.splitext(subpath.lower())
    handler = handlers.get(ext)

    body = request.get_json(force=True, silent=True)
    if body is not None:
        if isinstance(body, list):
            values = [str(v) for v in body]
        elif isinstance(body, dict):
            if hasattr(handler, "header"):
                tmp, owned = fs.download_to_temp(subpath)
                try:
                    hdr = handler.header(tmp)
                finally:
                    if owned:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                values = [str(body.get(h, "")) for h in hdr] if hdr \
                         else [str(v) for v in body.values()]
            else:
                values = [str(v) for v in body.values()]
        else:
            return text_response("ERROR 400: body must be a JSON array or object\n", 400)
        buf = io.StringIO()
        csv.writer(buf).writerow(values)
        extra = buf.getvalue().encode("utf-8")
        appended = ",".join(values)
    else:
        line = request.get_data(as_text=True)
        if not line.endswith("\n"):
            line += "\n"
        extra = line.encode("utf-8")
        appended = line.rstrip("\n")

    try:
        fs.append(subpath, extra)
    except Exception as exc:
        return text_response(f"ERROR 500: {exc}\n", 500)

    virtual_path = prefix + "/" + subpath
    return text_response(f"OK\npath = {toml_str(virtual_path)}\nrow  = {toml_str(appended)}\n")


def serve_replace_info(fs: FileSystem, prefix: str, subpath: str,
                       editable_spec=False) -> tuple:
    if not subpath:
        return text_response("ERROR 400: /.replace requires a file path\n", 400)
    try:
        meta = fs.head(subpath)
    except Exception as exc:
        return text_response(f"ERROR 404: {exc}\n", 404)

    virtual_path = prefix + "/" + subpath
    editable = _is_editable(editable_spec, subpath)
    lines = [f"# /.replace — {virtual_path}\n\n",
             f"path     = {toml_str(virtual_path)}\n",
             f"size     = {meta['size']}\n",
             f"editable = {str(editable).lower()}\n"]
    if editable:
        lines += [f"\n# POST the new file content to overwrite:\n",
                  f"#   curl -X POST {virtual_path}/.replace \\\n",
                  f"#        -H 'Content-Type: text/plain' \\\n",
                  f"#        --data-binary @local_file.txt\n"]
    else:
        lines.append("\n# This file is not marked as editable.\n")
    return text_response("".join(lines))


def serve_replace(fs: FileSystem, prefix: str, subpath: str,
                  editable_spec=False) -> tuple:
    if not subpath:
        return text_response("ERROR 400: /.replace requires a file path\n", 400)
    if not _is_editable(editable_spec, subpath):
        return text_response(f"ERROR 403: {subpath} is not editable\n", 403)
    data = request.get_data()
    try:
        fs.put(subpath, data)
    except Exception as exc:
        return text_response(f"ERROR 500: {exc}\n", 500)
    virtual_path = prefix + "/" + subpath
    return text_response(
        f"OK\npath = {toml_str(virtual_path)}\nbytes_written = {len(data)}\n"
    )


# ---------------------------------------------------------------------------
# Mount dispatcher
# ---------------------------------------------------------------------------

def handle_mount(fs: FileSystem, inact_app, prefix: str, subpath: str,
                 handlers: dict, editable_spec=False) -> tuple:
    if subpath == ".help" or subpath.endswith("/.help"):
        file_part = subpath[:-5].rstrip("/") if subpath.endswith("/.help") else ""
        return inact_app._serve_help(prefix + ("/" + file_part if file_part else ""))

    if subpath == "" or subpath == ".ls" or subpath.endswith("/.ls"):
        dir_sub = subpath[:-3].rstrip("/") if subpath.endswith("/.ls") else (
            "" if subpath in ("", ".ls") else subpath
        )
        return serve_ls(fs, prefix, dir_sub, handlers, editable_spec)

    if subpath == ".grep" or subpath.endswith("/.grep"):
        q = request.args.get("q", "").strip()
        dir_sub = subpath[:-6].rstrip("/") if subpath.endswith("/.grep") else ""
        return serve_grep(fs, prefix, dir_sub, q)

    if subpath.endswith("/.info"):
        return serve_info(fs, prefix, subpath[:-6].rstrip("/"), handlers, editable_spec)

    if subpath.endswith("/.download"):
        return serve_download(fs, prefix, subpath[:-10].rstrip("/"))

    if subpath.endswith("/.append"):
        return serve_append(fs, prefix, subpath[:-8].rstrip("/"), handlers)

    if subpath.endswith("/.replace"):
        file_sub = subpath[:-9].rstrip("/")
        if request.method == "POST":
            return serve_replace(fs, prefix, file_sub, editable_spec)
        return serve_replace_info(fs, prefix, file_sub, editable_spec)

    m = PAGE_RE.match(subpath)
    if m:
        file_sub, page = m.group(1), int(m.group(2))
        _, ext = os.path.splitext(file_sub.lower())
        if ext in handlers:
            return serve_file(fs, prefix, file_sub, handlers, page)

    return serve_file(fs, prefix, subpath, handlers)


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_files(
    inact_app,
    prefix: str,
    folder_or_fs,
    handlers=None,
    editable=False,
    code_server_port: int | None = None,
) -> None:
    """
    Mount a directory (or any :class:`FileSystem` backend) at *prefix*.

    *folder_or_fs* — a local path string **or** a :class:`FileSystem` instance
    such as :class:`~inact.apps.s3.S3FS`.

    *handlers* — list of :class:`~inact.handlers.FileHandler` instances.
    *editable* — ``True`` or list of glob patterns (local only).

    Example::

        mount_files(app, "/docs", "./docs")
        mount_files(app, "/docs", "./docs", handlers=[CSVHandler()], editable=["*.csv"])

        # S3-backed (via S3FS)
        from inact.apps.s3 import S3FS
        mount_files(app, "/data", S3FS("my-bucket", "prefix/", client))
    """
    if isinstance(folder_or_fs, str):
        fs: FileSystem = LocalFS(folder_or_fs)
    else:
        fs = folder_or_fs

    h_dict: dict = {}
    if handlers:
        for handler in handlers:
            for ext in handler.extensions:
                key = ext.lower() if ext.startswith(".") else "." + ext.lower()
                h_dict[key] = handler

    editable_spec = editable
    if isinstance(fs, LocalFS):
        inact_app._mounts[prefix.rstrip("/")] = fs.folder
        if h_dict:
            inact_app._mount_handlers[prefix.rstrip("/")] = h_dict
        if editable is not False:
            inact_app._mount_editable[prefix.rstrip("/")] = editable

    prefix = prefix.rstrip("/")
    ep = "_inact_mount_" + prefix.replace("/", "__")

    @inact_app.app.route(prefix + "/", defaults={"subpath": ""}, endpoint=ep + "_root",
                         methods=["GET", "POST"])
    @inact_app.app.route(prefix + "/<path:subpath>", endpoint=ep, methods=["GET", "POST"])
    def _handler(subpath: str, _fs=fs, _prefix=prefix,
                 _handlers=h_dict, _editable=editable_spec):
        return handle_mount(_fs, inact_app, _prefix, subpath, _handlers, _editable)

    p = prefix or "/"
    label = fs.label()
    help_text = (
        f"\nFiles: {p}" + (f"  ({label})" if label else "") + "\n"
        f"  GET  {p}/           list root\n"
        f"  GET  {p}/<path>/.ls list directory\n"
        f"  GET  {p}/.grep?q=…  search content\n"
        f"  GET  {p}/<file>     serve file\n"
        f"  GET  {p}/<file>/.info      metadata\n"
        f"  GET  {p}/<file>/.download  raw download\n"
    )
    if h_dict:
        help_text += f"  GET  {p}/<file>/p/<N>      paginated\n"
        help_text += f"  POST {p}/<file>/.append    append row\n"
    if editable is not False:
        help_text += f"  POST {p}/<file>/.replace   overwrite file\n"
    inact_app._app_mounts.append((prefix, help_text))

    # Store local path when backed by a real directory (useful for code-server)
    local_path = os.path.abspath(folder_or_fs) if isinstance(folder_or_fs, str) else None
    if local_path:
        if not hasattr(inact_app, "fs_local_paths"):
            inact_app.fs_local_paths = {}
        inact_app.fs_local_paths[prefix] = local_path

    # Start code-server if a port is configured.
    # nginx proxies /_vscode → code-server so the iframe is same-origin.
    _vscode_enabled = code_server_port is not None
    if _vscode_enabled:
        import atexit, subprocess as _sp
        _path_arg = local_path or "."
        # --user-data-dir avoids permission issues in containers
        # stderr=None so crashes show up in Docker/Railway logs
        _proc = _sp.Popen(
            ["code-server",
             "--port",          str(code_server_port),
             "--auth",          "none",
             "--base-path",     "/_vscode",
             "--user-data-dir", "/tmp/code-server-data",
             _path_arg],
            stdout=_sp.DEVNULL, stderr=None,
        )
        atexit.register(_proc.terminate)
        import logging as _log
        _log.getLogger(__name__).info(
            "code-server started on :%d for %s", code_server_port, _path_arg
        )

    def _human(path: str):
        from inact.render import render_template, workspace_nav
        from inact.utils import html_response
        if _vscode_enabled:
            return html_response(render_template("vscode_embed.html",
                title="Files",
                vscode_src="/_vscode/",
                workspace_links=workspace_nav("/_human/files/"),
                show_identity=True))
        return html_response(render_template("files_human.html",
            title="Files", prefix=prefix, nav="", pills=[],
            workspace_links=workspace_nav("/_human/files/"),
            show_identity=True))

    inact_app._human_views[prefix] = _human
