"""
S3 FileSystem backend for inact — mounts an S3 bucket to a local directory
via rclone, then serves it with :func:`~inact.apps.files.mount_files`.

Because the bucket is a real local path, code-server (or any other tool)
can open it directly.

Requires: rclone installed and available in PATH.
Install: https://rclone.org/install/

Examples::

    mount_s3(app, "/files", "s3://my-bucket/prefix/",
             mount_dir="./data/s3_mount")

    # MinIO / LocalStack / any S3-compatible
    mount_s3(app, "/files", "s3://mybucket",
             endpoint_url="http://localhost:9000",
             aws_access_key_id="minioadmin",
             aws_secret_access_key="minioadmin",
             mount_dir="./data/s3_mount",
             editable=True)

Credentials are read from constructor kwargs first, then from the standard
environment variables AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
AWS_ENDPOINT_URL / AWS_DEFAULT_REGION.

The local mount path is stored in ``inact_app.fs_local_paths[prefix]`` so
you can point code-server (or anything else) at the right directory.
"""

from __future__ import annotations

import atexit
import logging
import os
import platform
import subprocess
import tempfile
import time
from urllib.parse import urlparse

from .files import mount_files

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# rclone helpers
# ---------------------------------------------------------------------------

def _write_rclone_config(
    name: str,
    bucket: str,
    endpoint_url: str,
    access_key: str,
    secret_key: str,
    region: str,
) -> str:
    lines = [
        f"[{name}]",
        "type = s3",
        f"provider = {'Other' if endpoint_url else 'AWS'}",
    ]
    if access_key:
        lines.append(f"access_key_id = {access_key}")
    if secret_key:
        lines.append(f"secret_access_key = {secret_key}")
    if endpoint_url:
        lines.append(f"endpoint = {endpoint_url}")
    if region:
        lines.append(f"region = {region}")
    lines.append("no_check_bucket = true")

    cfg = tempfile.mktemp(suffix=".conf")
    with open(cfg, "w") as f:
        f.write("\n".join(lines) + "\n")
    return cfg


def _unmount(path: str) -> None:
    try:
        if platform.system() == "Darwin":
            subprocess.run(["umount", path], check=False, capture_output=True)
        else:
            subprocess.run(["fusermount", "-u", path], check=False, capture_output=True)
    except Exception:
        pass


def _wait_for_mount(path: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.ismount(path):
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Public mount function
# ---------------------------------------------------------------------------

def mount_s3(
    inact_app,
    prefix: str,
    s3_url: str,
    mount_dir: str | None = None,
    endpoint_url: str | None = None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    region_name: str | None = None,
    handlers=None,
    editable: bool = False,
    rclone_extra_args: list[str] | None = None,
) -> str:
    """
    Mount an S3 bucket/prefix to a local directory via rclone, then serve
    it with mount_files. Returns the local mount path.

    *mount_dir*  — local directory for the FUSE mount (auto temp dir if None).
                   Pass a stable path (e.g. ``./data/s3_mount``) so code-server
                   can be pointed at it persistently.
    """
    parsed = urlparse(s3_url)
    bucket     = parsed.netloc
    key_prefix = parsed.path.strip("/")

    # Resolve credentials: kwargs > env vars
    access_key = aws_access_key_id  or os.environ.get("AWS_ACCESS_KEY_ID",      "")
    secret_key = aws_secret_access_key or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    endpoint   = endpoint_url        or os.environ.get("AWS_ENDPOINT_URL",       "")
    region     = region_name         or os.environ.get("AWS_DEFAULT_REGION",     "us-east-1")

    # Local mount point
    if mount_dir is None:
        mount_dir = tempfile.mkdtemp(prefix="inact_s3_")
    else:
        os.makedirs(mount_dir, exist_ok=True)

    # Write rclone config
    remote_name = "s3remote"
    cfg_path = _write_rclone_config(remote_name, bucket, endpoint, access_key, secret_key, region)

    # Build rclone remote path
    remote = f"{remote_name}:{bucket}"
    if key_prefix:
        remote += f"/{key_prefix}"

    # Build command
    cmd = [
        "rclone", "mount", remote, mount_dir,
        "--config",        cfg_path,
        "--vfs-cache-mode", "writes",   # read-through, cache writes locally
        "--dir-cache-time", "10s",
        "--allow-non-empty",
        "--no-modtime",
    ]
    if platform.system() != "Darwin":
        cmd.append("--allow-other")
    if rclone_extra_args:
        cmd.extend(rclone_extra_args)

    log.info("s3: mounting %s → %s", remote, mount_dir)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    # Wait for FUSE mount to become active
    if not _wait_for_mount(mount_dir):
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise RuntimeError(
            f"rclone failed to mount {s3_url} at {mount_dir} within 15 s.\n"
            f"rclone stderr: {err}\n"
            f"Check that rclone is installed and credentials are correct."
        )

    log.info("s3: %s ready at %s", s3_url, mount_dir)

    # Unmount on exit
    atexit.register(_unmount, mount_dir)
    atexit.register(proc.terminate)

    # Serve with mount_files — same routes as local files
    mount_files(inact_app, prefix, mount_dir, handlers=handlers, editable=editable)

    # Store local path so code-server (or anything else) can find it
    if not hasattr(inact_app, "fs_local_paths"):
        inact_app.fs_local_paths = {}
    inact_app.fs_local_paths[prefix] = os.path.abspath(mount_dir)

    return os.path.abspath(mount_dir)
