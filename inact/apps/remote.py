"""
Remote Inact app mounts.

This module mounts a remote Inact route subtree into a local Inact app using a
server-side reverse proxy. It is meant for cases where the remote app should
look like part of the local workspace without importing its private code.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx
from flask import Response, request

from ..utils import text_response

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}


class RemoteInactProxy:
    """Reverse proxy for one mounted remote Inact app."""

    def __init__(
        self,
        prefix: str,
        base_url: str,
        *,
        token: str | None = None,
        token_header: str = "X-ElAgenteHarness-Token",
        timeout: float = 120.0,
    ):
        self.prefix = "/" + prefix.strip("/")
        self.base_url = base_url.rstrip("/")
        self.human_base_url = _human_base_url(base_url, self.prefix)
        self.token = token
        self.token_header = token_header
        self.timeout = timeout

    def route_url(self, subpath: str = "") -> str:
        return _join_url(self.base_url, subpath)

    def human_url(self, subpath: str = "") -> str:
        return _join_url(self.human_base_url, subpath)

    def forward(self, target_url: str) -> Response | tuple:
        headers = _forward_headers(request.headers)
        if self.token:
            headers[self.token_header] = self.token

        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                upstream = client.request(
                    request.method,
                    target_url,
                    params=request.args,
                    content=request.get_data(),
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            return text_response(f"ERROR 502: upstream request timed out: {exc}\n", 502)
        except httpx.HTTPError as exc:
            return text_response(f"ERROR 502: upstream request failed: {exc}\n", 502)

        return Response(
            upstream.content,
            status=upstream.status_code,
            headers=_response_headers(upstream.headers),
        )


def mount_remote_inact(
    inact_app,
    prefix: str,
    base_url: str,
    *,
    token: str | None = None,
    token_header: str = "X-ElAgenteHarness-Token",
    timeout: float = 120.0,
) -> None:
    """
    Mount a remote Inact app under *prefix*.

    Example::

        mount_remote_inact(app, "/chem", "http://harness/chem")

    Requests to ``/chem/...`` are proxied to ``http://harness/chem/...``.
    Requests to ``/_human/chem/...`` are proxied to
    ``http://harness/_human/chem/...``.
    """
    proxy = RemoteInactProxy(
        prefix,
        base_url,
        token=token,
        token_header=token_header,
        timeout=timeout,
    )
    p = proxy.prefix
    ep = "_inact_remote_" + p.replace("/", "__")
    flask_app = inact_app.app
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

    def _route_handler(subpath: str = ""):
        return proxy.forward(proxy.route_url(subpath))

    def _human_handler(subpath: str = ""):
        return proxy.forward(proxy.human_url(subpath))

    flask_app.add_url_rule(
        p,
        endpoint=ep + "_root",
        view_func=lambda: _route_handler(""),
        methods=methods,
    )
    flask_app.add_url_rule(
        p + "/",
        endpoint=ep + "_root_slash",
        view_func=lambda: _route_handler(""),
        methods=methods,
    )
    flask_app.add_url_rule(
        p + "/<path:subpath>",
        endpoint=ep,
        view_func=_route_handler,
        methods=methods,
    )

    human_prefix = "/_human" + p
    flask_app.add_url_rule(
        human_prefix,
        endpoint=ep + "_human_root",
        view_func=lambda: _human_handler(""),
        methods=methods,
    )
    flask_app.add_url_rule(
        human_prefix + "/",
        endpoint=ep + "_human_root_slash",
        view_func=lambda: _human_handler(""),
        methods=methods,
    )
    flask_app.add_url_rule(
        human_prefix + "/<path:subpath>",
        endpoint=ep + "_human",
        view_func=_human_handler,
        methods=methods,
    )

    inact_app._app_mounts.append((p, (
        f"\nRemote Inact app: {p}  ({proxy.base_url})\n"
        f"  GET/POST/etc  {p}/<path>         proxy remote app route\n"
        f"  GET/POST/etc  /_human{p}/<path>  proxy remote human view\n"
    )))


def _join_url(base_url: str, subpath: str) -> str:
    if not subpath:
        return base_url + "/"
    return base_url.rstrip("/") + "/" + subpath.lstrip("/")


def _human_base_url(base_url: str, prefix: str) -> str:
    parts = urlsplit(base_url.rstrip("/"))
    base_path = parts.path.rstrip("/")
    prefix_path = prefix.rstrip("/")
    if base_path == prefix_path:
        parent_path = ""
    elif base_path.endswith(prefix_path):
        parent_path = base_path[:-len(prefix_path)].rstrip("/")
    else:
        parent_path = base_path
    human_path = parent_path + "/_human" + prefix
    return urlunsplit((parts.scheme, parts.netloc, human_path, "", ""))


def _forward_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    forwarded = {}
    for name, value in headers:
        if name.lower() not in _HOP_BY_HOP_HEADERS:
            forwarded[name] = value
    return forwarded


def _response_headers(headers: httpx.Headers) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in headers.multi_items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
    ]
