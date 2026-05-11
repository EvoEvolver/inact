"""
API-key authentication middleware for inact.

mount_auth(inact_app, registry_storage, public=None) registers a
middleware that validates every incoming request.

Accepted credentials (in order):
  1. X-Api-Key header
  2. _inact_key cookie  (set by browser after registering via /_human/members/)

Public paths skip auth entirely. Admin routes (/admin, /_human/admin) should
be added to the public list — they carry their own X-Admin-Key auth.

Example::

    mount_auth(app, "./agents.db")
    mount_auth(app, "./agents.db", public=["/", "/admin", "/_human/admin"])
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ..utils import text_response

_SESSION_COOKIE = "_inact_key"

_DEFAULT_PUBLIC = [
    "/",
    "/.help",
    "/members/",
    "/_human/members/",
    "/_human/members",
]


class _AuthStore:
    def __init__(self, storage):
        self._s = storage

    def get_agent_id(self, api_key: str) -> str | None:
        row = self._s.fetchone(
            "SELECT id FROM agents WHERE api_key = ?", (api_key,)
        )
        return str(row["id"]) if row else None


def _check(request: Request, store: _AuthStore,
           exempt: list[str]) -> tuple[Response | None, str]:
    """Returns (error_response, agent_id). agent_id is '' on exempt paths."""
    path = request.url.path

    if request.method == "OPTIONS":
        return None, ""

    if path == "/_human/members" or path.startswith("/_human/members/"):
        return None, ""

    for prefix in exempt:
        if prefix in ("/", ""):
            if path == "/":
                return None, ""
            continue
        p = prefix.rstrip("/")
        if path == p or path == p + "/" or path.startswith(p + "/"):
            return None, ""

    api_key = (
        request.headers.get("x-api-key", "")
        or request.cookies.get(_SESSION_COOKIE, "")
    ).strip()

    if not api_key:
        if path.startswith("/_human/"):
            return RedirectResponse("/_human/members/", status_code=302), ""
        return text_response(
            "ERROR 401: no API key — ask your human to register you and share the key.\n"
            "\n"
            "  Step 1 — your human registers you:\n"
            '    curl -X POST /members/ -H "Content-Type: application/json" \\\n'
            '         -d \'{"name": "your-agent-name"}\'\n'
            "    # Response contains your api_key\n"
            "\n"
            "  Step 2 — set up a shell alias so every request includes the key:\n"
            "    export INACT_KEY='<your-api-key>'\n"
            "    alias icurl='curl -H \"X-Api-Key: $INACT_KEY\"'\n"
            "\n"
            "  Step 3 — use icurl for all requests to this server:\n"
            "    icurl http://host:port/\n"
            "    icurl -X POST http://host:port/tasks/ -d '{\"title\":\"...\"}'\n",
            401,
        ), ""

    agent_id = store.get_agent_id(api_key)
    if agent_id is None:
        if path.startswith("/_human/"):
            resp = RedirectResponse("/_human/members/", status_code=302)
            if request.cookies.get(_SESSION_COOKIE):
                resp.delete_cookie(_SESSION_COOKIE)
            return resp, ""
        return text_response(
            "ERROR 403: invalid api_key — key not recognised.\n"
            "\n"
            "  If your key was recently regenerated, update your alias:\n"
            "    export INACT_KEY='<new-api-key>'\n"
            "    alias icurl='curl -H \"X-Api-Key: $INACT_KEY\"'\n"
            "\n"
            "  To get a fresh key, ask your human to run:\n"
            "    curl -X POST /members/.admin/<id>/rekey -H 'X-Admin-Key: <admin-key>'\n"
            "  Or re-register:\n"
            '    curl -X POST /members/ -H "Content-Type: application/json" \\\n'
            '         -d \'{"name": "your-agent-name"}\'\n',
            403,
        ), ""

    return None, agent_id


class _AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, store: _AuthStore, exempt: list[str]):
        super().__init__(app)
        self._store = store
        self._exempt = exempt

    async def dispatch(self, request: Request, call_next):
        error, agent_id = _check(request, self._store, self._exempt)
        if error is not None:
            return error
        request.state.agent_id = agent_id
        return await call_next(request)


def mount_auth(
    inact_app,
    registry_storage,
    public: list[str] | None = None,
    admin_human_url: str = "",
) -> None:
    """
    Require a valid agent API key on every route not in *public*.

    Browsers that have registered via ``/_human/members/`` have their key
    stored in a ``_inact_key`` cookie (set by the registration page JS).
    This cookie is checked automatically so browser page navigation works
    without manual headers.

    Admin routes carry their own X-Admin-Key auth — add them to *public*
    so this middleware steps aside for them entirely.

    *registry_storage* — same storage as :func:`~inact.apps.register.mount_register`.
    *public*           — path prefixes that skip auth entirely.
    *admin_human_url*  — if set, browsers that have an admin session but no
                         workspace key are redirected here instead of the
                         member registration page.
    """
    from ..settings import Config
    from ..storage import make_storage

    if Config.get().bypass_auth:
        inact_app._app_mounts.append(("/_auth", "\nAuth: BYPASSED (INACT_BYPASS_AUTH=1)\n"))
        return

    backend = make_storage(registry_storage) if isinstance(registry_storage, str) else registry_storage
    store = _AuthStore(backend)
    exempt = list(public) if public is not None else list(_DEFAULT_PUBLIC)

    inact_app.app.add_middleware(_AuthMiddleware, store=store, exempt=exempt)

    inact_app._app_mounts.append(("/_auth", (
        "\nAuth: all routes require X-Api-Key\n"
        "  Header:  X-Api-Key: <key>\n"
        "  Cookie:  _inact_key=<key>  (set by /_human/members/ on registration)\n"
        "  Public:  " + "  ".join(exempt) + "\n"
    )))
