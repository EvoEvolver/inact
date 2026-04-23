"""
S3 file system mount for inact.

mount_s3(inact_app, prefix, s3_url, ...) registers:

  GET  {prefix}/               list root (TOML)
  GET  {prefix}/<path>/.ls     list "sub-directory" (common prefix)
  GET  {prefix}/.grep?q=<term> search object keys
  GET  {prefix}/<key>          serve object (plain text)
  GET  {prefix}/<key>/.info    object metadata
  GET  {prefix}/<key>/.download download raw bytes
  GET  {prefix}/<key>/p/<N>    paginated view (handler required)
  POST {prefix}/<key>/.append  append line/row (download → modify → re-upload)

Requires: pip install boto3

Examples::

    # AWS S3
    mount_s3(app, "/data", "s3://my-bucket/reports/")

    # MinIO / LocalStack
    mount_s3(app, "/data", "s3://my-bucket",
             endpoint_url="http://localhost:9000",
             aws_access_key_id="minioadmin",
             aws_secret_access_key="minioadmin")

    # With CSV pagination
    from inact import CSVHandler
    mount_s3(app, "/data", "s3://my-bucket", handlers=[CSVHandler()])
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from urllib.parse import urlparse

from flask import request

from ..utils import text_response, toml_str
from .files import PAGE_RE


# ---------------------------------------------------------------------------
# S3 client wrapper
# ---------------------------------------------------------------------------

class S3Mount:
    """
    Thin wrapper around a boto3 S3 client scoped to a single bucket + prefix.
    All ``subpath`` arguments are relative to that prefix.
    """

    def __init__(self, bucket: str, prefix: str, client):
        self.bucket = bucket
        self.prefix = prefix.strip("/")  # no slashes on either end
        self._s3 = client

    # ------------------------------------------------------------------
    # Key helpers

    def _full_key(self, subpath: str) -> str:
        subpath = subpath.strip("/")
        return (self.prefix + "/" + subpath) if self.prefix else subpath

    def _strip_prefix(self, key: str, dir_prefix: str) -> str:
        return key[len(dir_prefix):]

    def _dir_prefix(self, subpath: str) -> str:
        """The S3 'directory' prefix for *subpath* (always ends with '/')."""
        base = self._full_key(subpath)
        return (base.rstrip("/") + "/") if base else ""

    # ------------------------------------------------------------------
    # List

    def list(self, subpath: str = "") -> tuple[list[str], list[dict]]:
        """
        List direct children of *subpath*.
        Returns ``(subdirs, files)`` where each file is
        ``{"name": str, "size": int, "last_modified": str}``.
        """
        dir_prefix = self._dir_prefix(subpath)
        paginator = self._s3.get_paginator("list_objects_v2")
        subdirs, files = [], []
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=dir_prefix, Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes", []):
                name = cp["Prefix"][len(dir_prefix):].rstrip("/")
                if name:
                    subdirs.append(name)
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                name = self._strip_prefix(key, dir_prefix)
                if name:
                    files.append({
                        "name": name,
                        "size": obj["Size"],
                        "last_modified": str(obj["LastModified"]),
                    })
        return sorted(subdirs), sorted(files, key=lambda x: x["name"])

    # ------------------------------------------------------------------
    # Object I/O

    def get_bytes(self, subpath: str) -> bytes:
        obj = self._s3.get_object(Bucket=self.bucket, Key=self._full_key(subpath))
        return obj["Body"].read()

    def get_text(self, subpath: str) -> str:
        return self.get_bytes(subpath).decode("utf-8", errors="replace")

    def head(self, subpath: str) -> dict:
        r = self._s3.head_object(Bucket=self.bucket, Key=self._full_key(subpath))
        return {
            "size": r["ContentLength"],
            "last_modified": str(r["LastModified"]),
            "content_type": r.get("ContentType", "application/octet-stream"),
            "etag": r.get("ETag", "").strip('"'),
        }

    def put(self, subpath: str, body: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=self._full_key(subpath), Body=body)

    def append(self, subpath: str, extra: bytes) -> None:
        existing = self.get_bytes(subpath)
        self.put(subpath, existing + extra)

    def download_to_temp(self, subpath: str) -> str:
        """Download object to a temp file; caller must unlink when done."""
        content = self.get_bytes(subpath)
        suffix = os.path.splitext(subpath)[1]
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        f.write(content)
        f.close()
        return f.name

    def exists(self, subpath: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._full_key(subpath))
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _s3_ls(s3: S3Mount, prefix: str, subpath: str, handlers: dict) -> tuple:
    url_base = prefix + ("/" + subpath if subpath else "")
    try:
        subdirs, files = s3.list(subpath)
    except Exception as exc:
        return text_response(f"ERROR 502: S3 list failed: {exc}\n", 502)

    total = len(subdirs) + len(files)
    lines = [
        f"# S3 listing: {url_base}\n",
        f"# {total} entries  (s3://{s3.bucket}/{s3._dir_prefix(subpath)})\n",
        "# tip: append /.download to any file path to get raw bytes\n\n",
    ]
    for name in subdirs:
        path = f"{prefix}/{subpath}/{name}".replace("//", "/")
        lines.append("[[entries]]\n")
        lines.append(f'name = {toml_str(name + "/")}\n')
        lines.append(f'type = "dir"\n')
        lines.append(f'path = {toml_str(path + "/")}\n')
        lines.append("\n")
    for f in files:
        path = f"{prefix}/{subpath}/{f['name']}".replace("//", "/")
        _, ext = os.path.splitext(f["name"].lower())
        lines.append("[[entries]]\n")
        lines.append(f'name = {toml_str(f["name"])}\n')
        lines.append(f'type = "file"\n')
        lines.append(f'path = {toml_str(path)}\n')
        lines.append(f'size = {f["size"]}\n')
        lines.append(f'last_modified = {toml_str(f["last_modified"])}\n')
        if ext in handlers:
            lines.append(f'handler = {toml_str(type(handlers[ext]).__name__)}\n')
            lines.append(f'info = {toml_str(path + "/.info")}\n')
            lines.append(f'append = {toml_str(path + "/.append")}\n')
        lines.append("\n")
    return text_response("".join(lines))


def _s3_grep(s3: S3Mount, prefix: str, subpath: str, query: str) -> tuple:
    if not query:
        url_base = prefix + ("/" + subpath if subpath else "")
        return text_response(
            f"ERROR 400: ?q= required\n\nUsage: GET {url_base}/.grep?q=keyword\n", 400
        )
    try:
        _, files = s3.list(subpath)
    except Exception as exc:
        return text_response(f"ERROR 502: S3 list failed: {exc}\n", 502)

    matches = []
    q_lower = query.lower()
    for f in files:
        file_sub = (subpath + "/" + f["name"]).lstrip("/")
        try:
            text = s3.get_text(file_sub)
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if q_lower in line.lower():
                matches.append((file_sub, lineno, line))
                if len(matches) >= 200:
                    break
        if len(matches) >= 200:
            break

    url_base = prefix + ("/" + subpath if subpath else "")
    lines = [f"# Grep: {toml_str(query)} in {url_base}\n", f"# {len(matches)} match(es)\n\n"]
    for file_sub, lineno, line_text in matches:
        lines.append("[[matches]]\n")
        lines.append(f'file = {toml_str(prefix + "/" + file_sub)}\n')
        lines.append(f"line = {lineno}\n")
        lines.append(f"text = {toml_str(line_text)}\n")
        lines.append("\n")
    return text_response("".join(lines))


def _s3_info(s3: S3Mount, prefix: str, subpath: str, handlers: dict) -> tuple:
    if not subpath:
        return text_response("ERROR 400: /.info requires a file path\n", 400)
    try:
        meta = s3.head(subpath)
    except Exception as exc:
        return text_response(f"ERROR 404: {exc}\n", 404)

    virtual_path = prefix + "/" + subpath
    _, ext = os.path.splitext(subpath.lower())
    handler = handlers.get(ext)

    lines = [
        f"# S3 object info: {virtual_path}\n\n",
        f"path          = {toml_str(virtual_path)}\n",
        f"s3_key        = {toml_str(s3._full_key(subpath))}\n",
        f"size          = {meta['size']}\n",
        f"last_modified = {toml_str(meta['last_modified'])}\n",
        f"content_type  = {toml_str(meta['content_type'])}\n",
        f"download      = {toml_str(virtual_path + '/.download')}\n",
    ]
    if handler is not None:
        lines.append(f"handler = {toml_str(type(handler).__name__)}\n")
        tmp = s3.download_to_temp(subpath)
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
            os.unlink(tmp)
    return text_response("".join(lines))


def _s3_serve(s3: S3Mount, prefix: str, subpath: str, handlers: dict,
              page: int = 1) -> tuple:
    if not subpath:
        return text_response("ERROR 400: file path required\n", 400)
    _, ext = os.path.splitext(subpath.lower())
    handler = handlers.get(ext)

    if handler is not None:
        tmp = s3.download_to_temp(subpath)
        try:
            content, status = handler.serve(tmp, prefix + "/" + subpath, page)
        finally:
            os.unlink(tmp)
        return text_response(content, status)

    try:
        text = s3.get_text(subpath)
    except Exception as exc:
        return text_response(f"ERROR 404: {exc}\n", 404)
    return text_response(text)


def _s3_append(s3: S3Mount, prefix: str, subpath: str, handlers: dict) -> tuple:
    if not subpath:
        return text_response("ERROR 400: /.append requires a file path\n", 400)
    if not s3.exists(subpath):
        return text_response(f"ERROR 404: object not found: {subpath}\n", 404)

    _, ext = os.path.splitext(subpath.lower())
    handler = handlers.get(ext)

    body = request.get_json(force=True, silent=True)
    if body is not None:
        if isinstance(body, list):
            values = [str(v) for v in body]
        elif isinstance(body, dict):
            # order values by CSV header if handler supports it
            if hasattr(handler, "header"):
                tmp = s3.download_to_temp(subpath)
                try:
                    hdr = handler.header(tmp)
                finally:
                    os.unlink(tmp)
                values = [str(body.get(h, "")) for h in hdr] if hdr else list(str(v) for v in body.values())
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
        s3.append(subpath, extra)
    except Exception as exc:
        return text_response(f"ERROR 502: S3 write failed: {exc}\n", 502)

    virtual_path = prefix + "/" + subpath
    return text_response(f"OK\npath = {toml_str(virtual_path)}\nrow  = {toml_str(appended)}\n")


def _handle_s3(s3: S3Mount, prefix: str, subpath: str, handlers: dict,
               inact_app) -> tuple:
    if subpath == ".help" or subpath.endswith("/.help"):
        file_part = subpath[:-5].rstrip("/") if subpath.endswith("/.help") else ""
        return inact_app._serve_help(prefix + ("/" + file_part if file_part else ""))

    if subpath == "" or subpath == ".ls" or subpath.endswith("/.ls"):
        dir_sub = subpath[:-3].rstrip("/") if subpath.endswith("/.ls") else (
            "" if subpath in ("", ".ls") else subpath
        )
        return _s3_ls(s3, prefix, dir_sub, handlers)

    if subpath == ".grep" or subpath.endswith("/.grep"):
        q = request.args.get("q", "").strip()
        dir_sub = subpath[:-6].rstrip("/") if subpath.endswith("/.grep") else ""
        return _s3_grep(s3, prefix, dir_sub, q)

    if subpath.endswith("/.info"):
        return _s3_info(s3, prefix, subpath[:-6].rstrip("/"), handlers)

    if subpath.endswith("/.download"):
        file_sub = subpath[:-10].rstrip("/")
        try:
            content = s3.get_bytes(file_sub)
        except Exception as exc:
            return text_response(f"ERROR 404: {exc}\n", 404)
        fname = os.path.basename(file_sub)
        from flask import Response
        return Response(
            content,
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
            content_type="application/octet-stream",
        )

    if subpath.endswith("/.append"):
        return _s3_append(s3, prefix, subpath[:-8].rstrip("/"), handlers)

    m = PAGE_RE.match(subpath)
    if m:
        file_sub, page = m.group(1), int(m.group(2))
        _, ext = os.path.splitext(file_sub.lower())
        if ext in handlers:
            return _s3_serve(s3, prefix, file_sub, handlers, page)

    return _s3_serve(s3, prefix, subpath, handlers)


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_s3(
    inact_app,
    prefix: str,
    s3_url: str,
    handlers=None,
    region_name: str | None = None,
    endpoint_url: str | None = None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
) -> None:
    """
    Mount an S3 bucket (or prefix within a bucket) at *prefix*.

    *s3_url* — ``s3://bucket`` or ``s3://bucket/key-prefix/``

    *handlers* — list of :class:`~inact.handlers.FileHandler` instances
    (e.g. ``[CSVHandler()]``) for custom rendering and pagination.

    Boto3 credentials are resolved in the standard order (env vars, ``~/.aws``,
    IAM role) unless overridden by the keyword arguments.

    Example::

        mount_s3(app, "/reports", "s3://my-bucket/reports/")

        # MinIO / LocalStack
        mount_s3(app, "/data", "s3://mybucket",
                 endpoint_url="http://localhost:9000",
                 aws_access_key_id="minioadmin",
                 aws_secret_access_key="minioadmin")
    """
    try:
        import boto3
    except ImportError:
        raise RuntimeError("boto3 is required: pip install boto3")

    parsed = urlparse(s3_url)
    bucket = parsed.netloc
    key_prefix = parsed.path.strip("/")

    boto_kwargs: dict = {}
    if region_name:
        boto_kwargs["region_name"] = region_name
    if endpoint_url:
        boto_kwargs["endpoint_url"] = endpoint_url
    if aws_access_key_id:
        boto_kwargs["aws_access_key_id"] = aws_access_key_id
    if aws_secret_access_key:
        boto_kwargs["aws_secret_access_key"] = aws_secret_access_key

    client = boto3.client("s3", **boto_kwargs)
    s3 = S3Mount(bucket, key_prefix, client)

    h_dict: dict = {}
    if handlers:
        for handler in handlers:
            for ext in handler.extensions:
                key = ext.lower() if ext.startswith(".") else "." + ext.lower()
                h_dict[key] = handler

    prefix = prefix.rstrip("/")
    ep = "_inact_s3_" + prefix.replace("/", "__")

    @inact_app.app.route(prefix + "/", defaults={"subpath": ""}, endpoint=ep + "_root",
                         methods=["GET", "POST"])
    @inact_app.app.route(prefix + "/<path:subpath>", endpoint=ep, methods=["GET", "POST"])
    def _handler(subpath: str, _s3=s3, _prefix=prefix, _handlers=h_dict):
        return _handle_s3(_s3, _prefix, subpath, _handlers, inact_app)

    p = prefix or "/"
    help_text = (
        f"\nS3: {p}  (s3://{bucket}/{key_prefix})\n"
        f"  GET  {p}/           list root\n"
        f"  GET  {p}/<path>/.ls list sub-prefix\n"
        f"  GET  {p}/.grep?q=…  search object keys + content\n"
        f"  GET  {p}/<key>      serve object\n"
        f"  GET  {p}/<key>/.info      metadata\n"
        f"  GET  {p}/<key>/.download  raw download\n"
    )
    if handlers:
        help_text += f"  GET  {p}/<key>/p/<N>       paginated view\n"
        help_text += f"  POST {p}/<key>/.append     append row\n"
    inact_app._app_mounts.append((prefix, help_text))
