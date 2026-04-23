"""
File system mount for inact — serve a local folder over HTTP.

mount_files(inact_app, prefix, folder, handlers=None, editable=False) registers:

  GET  {prefix}/                    list root directory (TOML)
  GET  {prefix}/<path>/.ls          list subdirectory
  GET  {prefix}/.grep?q=<term>      search content in root
  GET  {prefix}/<path>/.grep?q=…    search in subdirectory
  GET  {prefix}/<file>              serve file (plain text / handler output)
  GET  {prefix}/<file>/.info        file metadata + page count
  GET  {prefix}/<file>/.download    download raw file
  GET  {prefix}/<file>/p/<N>        paginated file (handler required)
  GET  {prefix}/<file>/.replace     show replace info (editable only)
  POST {prefix}/<file>/.replace     overwrite file content (editable only)

*handlers* — list of :class:`~inact.handlers.FileHandler` instances.
*editable* — ``True`` makes all files writable; a list of glob patterns
(e.g. ``["*.md", "notes/"]``) restricts editability to matching paths.
"""

from __future__ import annotations

import fnmatch
import os
import re

from flask import request, send_file

from ..utils import text_response, toml_str

# Matches pagination suffix: <file_subpath>/p/<page_number>
PAGE_RE = re.compile(r"^(.+)/p/(\d+)$")


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
# Request handlers
# ---------------------------------------------------------------------------

def serve_ls(inact_app, prefix: str, folder: str, subpath: str) -> tuple:
    dir_path = os.path.normpath(os.path.join(folder, subpath)) if subpath else folder
    if not dir_path.startswith(folder):
        return text_response("ERROR 403: Forbidden\n", 403)
    if not os.path.isdir(dir_path):
        return text_response(f"ERROR 404: Not a directory: {subpath}\n", 404)

    entries = _list_dir(folder, dir_path, prefix, subpath)
    handlers = inact_app._mount_handlers.get(prefix, {})
    editable_spec = inact_app._mount_editable.get(prefix, False)
    url_base = prefix + ("/" + subpath if subpath else "")
    lines = [
        f"# Directory listing: {url_base}\n",
        f"# {len(entries)} entries\n",
        "# tip: append /.download to any file path to get the raw file\n\n",
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
            rel_file = os.path.relpath(os.path.join(dir_path, e["name"]), folder)
            if _is_editable(editable_spec, rel_file):
                lines.append("editable = true\n")
                lines.append(f'replace = {toml_str(e["path"] + "/.replace")}\n')
        lines.append("\n")
    return text_response("".join(lines))


def serve_grep(prefix: str, folder: str, subpath: str, query: str) -> tuple:
    if not query:
        base = prefix + ("/" + subpath if subpath else "")
        return text_response(
            f"ERROR 400: Missing query parameter.\n\nUsage: GET {base}/.grep?q=keyword\n", 400
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
    lines = [f"# Grep: {toml_str(query)} in {url_base}\n", f"# {len(matches)} match(es)\n\n"]
    for rel_path, lineno, line_text in matches:
        lines.append("[[matches]]\n")
        lines.append(f'file = {toml_str(prefix + "/" + rel_path)}\n')
        lines.append(f"line = {lineno}\n")
        lines.append(f"text = {toml_str(line_text)}\n")
        lines.append("\n")
    return text_response("".join(lines))


def serve_file(inact_app, prefix: str, folder: str, subpath: str, page: int = 1) -> tuple:
    safe = os.path.normpath(os.path.join(folder, subpath))
    if not safe.startswith(folder):
        return text_response("ERROR 403: Path traversal denied\n", 403)
    if not os.path.isfile(safe):
        return text_response(f"ERROR 404: File not found: {subpath}\n", 404)

    _, ext = os.path.splitext(subpath.lower())
    handler = inact_app._mount_handlers.get(prefix, {}).get(ext)
    if handler is not None:
        content, status = handler.serve(safe, prefix + "/" + subpath, page)
        return text_response(content, status)

    try:
        content = open(safe, encoding="utf-8").read()
    except Exception as e:
        return text_response(f"ERROR 500: {e}\n", 500)
    return text_response(content)


def serve_info(inact_app, prefix: str, folder: str, subpath: str) -> tuple:
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
    handler = inact_app._mount_handlers.get(prefix, {}).get(ext)
    editable_spec = inact_app._mount_editable.get(prefix, False)

    lines = [
        f"# File info: {virtual_path}\n\n",
        f"path     = {toml_str(virtual_path)}\n",
        f"size     = {stat.st_size}\n",
        f"download = {toml_str(virtual_path + '/.download')}\n",
    ]
    if handler is not None:
        lines.append(f"handler = {toml_str(type(handler).__name__)}\n")
        try:
            pages = handler.page_count(safe)
            if pages is not None:
                lines.append(f"pages           = {pages}\n")
                lines.append(f"page_url_pattern = {toml_str(virtual_path + '/p/{{N}}')}\n")
                lines.append(f"first_page      = {toml_str(virtual_path + '/p/1')}\n")
        except Exception:
            pass
    if _is_editable(editable_spec, subpath):
        lines.append(f"editable = true\n")
        lines.append(f"replace  = {toml_str(virtual_path + '/.replace')}\n")
    return text_response("".join(lines))


def serve_download(prefix: str, folder: str, subpath: str):
    if not subpath:
        return text_response("ERROR 400: /.download requires a file path\n", 400)
    safe = os.path.normpath(os.path.join(folder, subpath))
    if not safe.startswith(folder):
        return text_response("ERROR 403: Path traversal denied\n", 403)
    if not os.path.isfile(safe):
        return text_response(f"ERROR 404: File not found: {subpath}\n", 404)
    return send_file(safe, as_attachment=True, download_name=os.path.basename(safe))


def serve_replace_info(inact_app, prefix: str, folder: str, subpath: str) -> tuple:
    if not subpath:
        return text_response("ERROR 400: /.replace requires a file path\n", 400)
    safe = os.path.normpath(os.path.join(folder, subpath))
    if not safe.startswith(folder):
        return text_response("ERROR 403: Path traversal denied\n", 403)
    if not os.path.isfile(safe):
        return text_response(f"ERROR 404: File not found: {subpath}\n", 404)

    virtual_path = prefix + "/" + subpath
    editable_spec = inact_app._mount_editable.get(prefix, False)
    editable = _is_editable(editable_spec, subpath)
    stat = os.stat(safe)

    lines = [
        f"# /.replace — {virtual_path}\n\n",
        f"path     = {toml_str(virtual_path)}\n",
        f"size     = {stat.st_size}\n",
        f"editable = {str(editable).lower()}\n",
    ]
    if editable:
        lines += [
            f"\n# POST the new file content to this URL to overwrite the file.\n",
            f"#   curl -X POST {virtual_path}/.replace \\\n",
            f"#        -H 'Content-Type: text/plain' \\\n",
            f"#        --data-binary @local_file.txt\n",
        ]
    else:
        lines.append("\n# This file is not marked as editable in the mount configuration.\n")
    return text_response("".join(lines))


def serve_append(inact_app, prefix: str, folder: str, subpath: str) -> tuple:
    if not subpath:
        return text_response("ERROR 400: /.append requires a file path\n", 400)
    safe = os.path.normpath(os.path.join(folder, subpath))
    if not safe.startswith(folder):
        return text_response("ERROR 403: Path traversal denied\n", 403)
    if not os.path.isfile(safe):
        return text_response(f"ERROR 404: File not found: {subpath}\n", 404)

    _, ext = os.path.splitext(subpath.lower())
    handler = inact_app._mount_handlers.get(prefix, {}).get(ext)

    body = request.get_json(force=True, silent=True)
    if body is not None:
        # JSON array → row values; JSON object → order by CSV header
        if isinstance(body, list):
            values = [str(v) for v in body]
        elif isinstance(body, dict):
            if hasattr(handler, "header"):
                hdr = handler.header(safe)
                values = [str(body.get(h, "")) for h in hdr] if hdr else [str(v) for v in body.values()]
            else:
                values = [str(v) for v in body.values()]
        else:
            return text_response("ERROR 400: body must be a JSON array or object\n", 400)

        if hasattr(handler, "append_row"):
            handler.append_row(safe, values)
        else:
            import csv, io
            buf = io.StringIO()
            csv.writer(buf).writerow(values)
            with open(safe, "a", encoding="utf-8", newline="") as f:
                f.write(buf.getvalue())
        appended = ",".join(values)
    else:
        # plain text — append line as-is
        line = request.get_data(as_text=True)
        if not line.endswith("\n"):
            line += "\n"
        try:
            with open(safe, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            return text_response(f"ERROR 500: {e}\n", 500)
        appended = line.rstrip("\n")

    virtual_path = prefix + "/" + subpath
    return text_response(f"OK\npath   = {toml_str(virtual_path)}\nrow    = {toml_str(appended)}\n")


def serve_replace(inact_app, prefix: str, folder: str, subpath: str) -> tuple:
    if not subpath:
        return text_response("ERROR 400: /.replace requires a file path\n", 400)
    editable_spec = inact_app._mount_editable.get(prefix, False)
    if not _is_editable(editable_spec, subpath):
        return text_response(f"ERROR 403: {prefix}/{subpath} is not marked as editable\n", 403)
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
    return text_response(f"OK\npath = {toml_str(virtual_path)}\nbytes_written = {len(data)}\n")


# ---------------------------------------------------------------------------
# Mount dispatcher
# ---------------------------------------------------------------------------

def handle_mount(inact_app, prefix: str, folder: str, subpath: str) -> tuple:
    if subpath == ".help" or subpath.endswith("/.help"):
        file_part = subpath[:-5].rstrip("/") if subpath.endswith("/.help") else ""
        return inact_app._serve_help(prefix + ("/" + file_part if file_part else ""))

    if subpath == "" or subpath == ".ls" or subpath.endswith("/.ls"):
        dir_sub = subpath[:-3].rstrip("/") if subpath.endswith("/.ls") else (
            "" if subpath in ("", ".ls") else subpath
        )
        return serve_ls(inact_app, prefix, folder, dir_sub)

    if subpath == ".grep" or subpath.endswith("/.grep"):
        q = request.args.get("q", "").strip()
        dir_sub = subpath[:-6].rstrip("/") if subpath.endswith("/.grep") else ""
        return serve_grep(prefix, folder, dir_sub, q)

    if subpath.endswith("/.info"):
        return serve_info(inact_app, prefix, folder, subpath[:-6].rstrip("/"))

    if subpath.endswith("/.download"):
        return serve_download(prefix, folder, subpath[:-10].rstrip("/"))

    if subpath.endswith("/.append"):
        return serve_append(inact_app, prefix, folder, subpath[:-8].rstrip("/"))

    if subpath.endswith("/.replace"):
        file_sub = subpath[:-9].rstrip("/")
        if request.method == "POST":
            return serve_replace(inact_app, prefix, folder, file_sub)
        return serve_replace_info(inact_app, prefix, folder, file_sub)

    m = PAGE_RE.match(subpath)
    if m:
        file_sub, page = m.group(1), int(m.group(2))
        _, ext = os.path.splitext(file_sub.lower())
        if ext in inact_app._mount_handlers.get(prefix, {}):
            return serve_file(inact_app, prefix, folder, file_sub, page)

    return serve_file(inact_app, prefix, folder, subpath)


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_files(
    inact_app,
    prefix: str,
    folder: str,
    handlers=None,
    editable=False,
) -> None:
    """
    Mount *folder* under *prefix*.

    Provides directory listing (/.ls), content search (/.grep), file serving,
    metadata (/.info), raw download (/.download), pagination (/p/<N>),
    and optional file editing (/.replace).

    *handlers* — list of :class:`~inact.handlers.FileHandler` instances.
    *editable* — ``True`` or list of glob patterns for editable paths.

    Example::

        mount_files(app, "/docs", "./docs")
        mount_files(app, "/notes", "./notes", editable=["*.md"])
    """
    from ..handlers import FileHandler

    prefix = prefix.rstrip("/")
    abs_folder = os.path.abspath(folder)

    inact_app._mounts[prefix] = abs_folder

    if handlers:
        h_dict: dict[str, FileHandler] = {}
        for handler in handlers:
            for ext in handler.extensions:
                h_dict[ext.lower() if ext.startswith(".") else "." + ext.lower()] = handler
        inact_app._mount_handlers[prefix] = h_dict

    if editable is not False:
        inact_app._mount_editable[prefix] = editable

    ep = "_inact_mount_" + prefix.replace("/", "__")

    @inact_app.app.route(prefix + "/", defaults={"subpath": ""}, endpoint=ep + "_root", methods=["GET", "POST"])
    @inact_app.app.route(prefix + "/<path:subpath>", endpoint=ep, methods=["GET", "POST"])
    def _handler(subpath: str, _prefix=prefix, _folder=abs_folder):
        return handle_mount(inact_app, _prefix, _folder, subpath)

    p = prefix or "/"
    help_text = (
        f"\nFiles: {p}\n"
        f"  GET  {p}/           list root\n"
        f"  GET  {p}/<path>/.ls list directory\n"
        f"  GET  {p}/.grep?q=… search content\n"
        f"  GET  {p}/<file>    serve file\n"
        f"  GET  {p}/<file>/.info      metadata\n"
        f"  GET  {p}/<file>/.download  raw download\n"
    )
    if handlers:
        help_text += f"  GET  {p}/<file>/p/<N>       paginated file\n"
        help_text += f"  POST {p}/<file>/.append     append row  body: JSON array or object\n"
    if editable is not False:
        help_text += f"  POST {p}/<file>/.replace    overwrite file\n"
    inact_app._app_mounts.append((prefix, help_text))
