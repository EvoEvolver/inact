"""
S3 FileSystem backend for inact.

mount_s3(inact_app, prefix, s3_url, ...) mounts an S3 bucket/prefix using
the same route surface as :func:`~inact.apps.files.mount_files`.

Requires: pip install boto3

Examples::

    mount_s3(app, "/reports", "s3://my-bucket/reports/")

    # MinIO / LocalStack
    mount_s3(app, "/data", "s3://mybucket",
             endpoint_url="http://localhost:9000",
             aws_access_key_id="minioadmin",
             aws_secret_access_key="minioadmin")

    # With CSV pagination
    from inact import CSVHandler
    mount_s3(app, "/data", "s3://mybucket/prefix/", handlers=[CSVHandler()])
"""

from __future__ import annotations

import os
import tempfile
from urllib.parse import urlparse

from .files import FileSystem, mount_files


class S3FS(FileSystem):
    """S3-backed :class:`~inact.apps.files.FileSystem`."""

    def __init__(self, bucket: str, prefix: str, client):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._s3 = client

    def _key(self, subpath: str) -> str:
        subpath = subpath.strip("/")
        return (self.prefix + "/" + subpath) if self.prefix else subpath

    def _dir_prefix(self, subpath: str) -> str:
        base = self._key(subpath)
        return (base.rstrip("/") + "/") if base else ""

    def list(self, subpath: str = "") -> tuple[list[str], list[dict]]:
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
                name = key[len(dir_prefix):]
                if name:
                    files.append({
                        "name": name,
                        "size": obj["Size"],
                        "last_modified": str(obj["LastModified"]),
                    })
        return sorted(subdirs), sorted(files, key=lambda x: x["name"])

    def get_bytes(self, subpath: str) -> bytes:
        obj = self._s3.get_object(Bucket=self.bucket, Key=self._key(subpath))
        return obj["Body"].read()

    def put(self, subpath: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=self._key(subpath), Body=data)

    def head(self, subpath: str) -> dict:
        r = self._s3.head_object(Bucket=self.bucket, Key=self._key(subpath))
        return {
            "size": r["ContentLength"],
            "last_modified": str(r["LastModified"]),
            "content_type": r.get("ContentType", "application/octet-stream"),
            "s3_key": self._key(subpath),
        }

    def exists(self, subpath: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._key(subpath))
            return True
        except Exception:
            return False

    def download_to_temp(self, subpath: str) -> tuple[str, bool]:
        content = self.get_bytes(subpath)
        suffix = os.path.splitext(subpath)[1]
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        f.write(content)
        f.close()
        return f.name, True  # temp file — caller must unlink

    def label(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"


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
    Mount an S3 bucket/prefix at *prefix*.

    Thin wrapper around :func:`~inact.apps.files.mount_files` with an
    :class:`S3FS` backend — identical route surface and handler support.
    """
    try:
        import boto3
    except ImportError:
        raise RuntimeError("boto3 is required: pip install boto3")

    parsed = urlparse(s3_url)
    bucket = parsed.netloc
    key_prefix = parsed.path.strip("/")

    kwargs: dict = {}
    if region_name:
        kwargs["region_name"] = region_name
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    if aws_access_key_id:
        kwargs["aws_access_key_id"] = aws_access_key_id
    if aws_secret_access_key:
        kwargs["aws_secret_access_key"] = aws_secret_access_key

    client = boto3.client("s3", **kwargs)
    fs = S3FS(bucket, key_prefix, client)
    mount_files(inact_app, prefix, fs, handlers=handlers)

    # Append S3 setup docs to the help entry just registered by mount_files().
    # Agents reading <prefix>/.help will see both the route surface and how to
    # reproduce this mount in their own code.
    p = prefix.rstrip("/") or "/"
    cred_example = ""
    if aws_access_key_id:
        cred_example = (
            f'\n             aws_access_key_id="{aws_access_key_id}",'
            f'\n             aws_secret_access_key="<secret>",'
        )
    endpoint_example = f'\n             endpoint_url="{endpoint_url}",' if endpoint_url else ""
    region_example = f'\n             region_name="{region_name}",' if region_name else ""
    s3_doc = (
        f"\n  --- How to mount this S3 filesystem in your own code ---\n"
        f"  pip install boto3 inact\n\n"
        f"  from inact import mount_s3\n"
        f"  mount_s3(app, \"{p}\", \"{s3_url}\","
        f"{region_example}{endpoint_example}{cred_example})\n\n"
        f"  Key parameters:\n"
        f"    s3_url              s3://bucket or s3://bucket/prefix\n"
        f"    region_name         AWS region (or set AWS_DEFAULT_REGION)\n"
        f"    endpoint_url        MinIO / LocalStack / S3-compatible endpoint\n"
        f"    aws_access_key_id   explicit credentials (or use env vars /\n"
        f"    aws_secret_access_key  ~/.aws/credentials / IAM role)\n"
        f"    handlers            list of FileHandler instances (e.g. CSVHandler)\n\n"
        f"  Env vars boto3 picks up automatically:\n"
        f"    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION\n"
    )
    if inact_app._app_mounts:
        mp, ht = inact_app._app_mounts[-1]
        inact_app._app_mounts[-1] = (mp, ht + s3_doc)
