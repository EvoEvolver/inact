"""
API-key authentication middleware for inact.

mount_auth(inact_app, registry_storage, public=None) registers a
Flask before_request hook that validates every incoming request.

Accepted credentials (in order):
  1. X-Api-Key header
  2. ?api_key= query parameter
  3. _inact_key cookie  (set by browser after registering in /_human/agents/)

Public paths skip auth entirely. Defaults:
  /          home / docs
  /.help     help pages
  /members/   agent listing (discovery + self-registration)
  /_human/members/   human registration page

All other routes — including /_human/* pages beyond registration — require
a valid key.  Browsers get the key via the _inact_key cookie which the
register page sets automatically on registration.

Example::

    mount_auth(app, "./agents.db")
    mount_auth(app, "./agents.db", public=["/", "/members/", "/_human/members/"])
"""

from __future__ import annotations

from flask import request

from ..utils import text_response, html_response

_SESSION_COOKIE = "_inact_key"

_DEFAULT_PUBLIC = [
    "/",
    "/.help",
    "/members/",           # registration + listing
    "/_human/members/",    # human registration page
    "/_human/members",
]


class _AuthStore:
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
    admin_key: str = "",
) -> None:
    """
    Require a valid API key on every route not in *public*.

    Browsers that have registered via ``/_human/members/`` have their key
    stored in a ``_inact_key`` cookie (set by the registration page JS).
    This cookie is checked automatically so browser page navigation works
    without manual headers.

    *registry_storage* — same storage as :func:`~inact.apps.register.mount_register`.
    *public*           — path prefixes that skip auth entirely.
    *admin_key*        — if set, this key is accepted on all routes regardless of the agents table.
    """
    from ..storage import make_storage

    backend = make_storage(registry_storage) if isinstance(registry_storage, str) else registry_storage
    store = _AuthStore(backend)
    exempt = list(public) if public is not None else list(_DEFAULT_PUBLIC)

    def _check():
        path = request.path

        if request.method == "OPTIONS":
            return None

        # Exempt public prefixes
        for prefix in exempt:
            if prefix in ("/", ""):
                # exact match only — don't exempt everything
                if path == "/":
                    return None
                continue
            p = prefix.rstrip("/")
            if path == p or path == p + "/" or path.startswith(p + "/"):
                return None

        # Accept X-Admin-Key directly
        if admin_key and request.headers.get("X-Admin-Key", "").strip() == admin_key:
            return None

        # Resolve key: header → query param → cookie
        api_key = (
            request.headers.get("X-Api-Key", "")
            or request.args.get("api_key", "")
            or request.cookies.get(_SESSION_COOKIE, "")
        ).strip()

        if not api_key:
            # Browser page request → redirect to register page
            if path.startswith("/_human/"):
                from flask import redirect
                return redirect("/_human/members/")
            return text_response(
                "ERROR 401: X-Api-Key header required\n"
                "  Register at POST /members/ to get an API key.\n",
                401,
            )

        if not (store.valid_key(api_key) or (admin_key and api_key == admin_key)):
            if path.startswith("/_human/"):
                from flask import redirect
                return redirect("/_human/members/")
            return text_response("ERROR 403: invalid api_key\n", 403)

        return None

    inact_app.app.before_request(_check)

    inact_app._app_mounts.append(("/_auth", (
        "\nAuth: all routes require X-Api-Key\n"
        "  Header:  X-Api-Key: <key>\n"
        "  Cookie:  _inact_key=\u003ckey\u003e  (set by /_human/members/ on registration)\n"
        "  Public:  " + "  ".join(exempt) + "\n"
    )))
