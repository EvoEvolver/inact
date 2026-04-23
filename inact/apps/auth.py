"""
API-key authentication middleware for inact.

mount_auth(inact_app, registry_storage, public=None) registers a
Flask before_request hook that validates every incoming request against
the agents registry.

Requests must include:
    X-Api-Key: <api_key received at registration>

Public paths are exempt (no key needed). Defaults:
    /_human/*    browser UI pages
    /.help        help pages
    /agents/      agent listing (discovery)

Example::

    mount_auth(app, "./agents.db")

    # custom public paths
    mount_auth(app, "./agents.db", public=["/agents/", "/_human/", "/status"])
"""

from __future__ import annotations

from flask import request

from ..utils import text_response

_DEFAULT_PUBLIC = [
    "/_human",
    "/.help",
    "/agents/",     # agent listing — public so new agents can discover others
]


class _AuthStore:
    """Minimal wrapper — only needs to validate an api_key."""

    def __init__(self, storage):
        self._s = storage

    def valid_key(self, api_key: str) -> bool:
        row = self._s.fetchone(
            "SELECT id FROM agents WHERE api_key = ?", (api_key,)
        )
        return row is not None


def mount_auth(
    inact_app,
    registry_storage,
    public: list[str] | None = None,
) -> None:
    """
    Require ``X-Api-Key`` on every route that is not in *public*.

    *registry_storage* — same storage passed to :func:`~inact.apps.register.mount_register`.
    *public*           — list of path prefixes that skip auth (default: see module docstring).

    Example::

        mount_auth(app, "./agents.db")
        mount_auth(app, "./agents.db", public=["/agents/", "/_human/", "/status"])
    """
    from ..storage import make_storage

    backend = make_storage(registry_storage) if isinstance(registry_storage, str) else registry_storage
    store = _AuthStore(backend)

    exempt = list(public) if public is not None else list(_DEFAULT_PUBLIC)

    def _check():
        path = request.path

        # Always allow OPTIONS (CORS preflight)
        if request.method == "OPTIONS":
            return None

        # Exempt public prefixes
        for prefix in exempt:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/") or path == prefix.rstrip("/"):
                return None

        api_key = (
            request.headers.get("X-Api-Key", "")
            or request.args.get("api_key", "")
        ).strip()

        if not api_key:
            return text_response(
                "ERROR 401: X-Api-Key header required\n"
                "  Register at POST /agents/ to get an API key.\n",
                401,
            )

        if not store.valid_key(api_key):
            return text_response("ERROR 403: invalid api_key\n", 403)

        return None  # allow

    inact_app.app.before_request(_check)

    inact_app._app_mounts.append(("/_auth", (
        "\nAuth: all routes require X-Api-Key header\n"
        "  Register: POST /agents/  → get api_key\n"
        "  Use:      X-Api-Key: <key>  on every request\n"
        "  Public:   " + "  ".join(exempt) + "\n"
    )))
