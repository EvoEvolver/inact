import json

from fastapi import Request
from fastapi.responses import Response, HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware


def text_response(body: str, status: int = 200) -> Response:
    return Response(content=body, status_code=status, media_type="text/plain; charset=utf-8")


def html_response(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(content=body, status_code=status)


def _body(request: Request) -> dict:
    try:
        return json.loads(getattr(request.state, "body", b"") or b"{}") or {}
    except Exception:
        return {}


def toml_str(s: str) -> str:
    return ('"' + s
            .replace("\\", "\\\\")
            .replace('"',  '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
            + '"')


def server_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", "http")
    return f"{proto}://{request.headers.get('host', 'localhost')}"


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    sep = "  ".join("-" * w for w in widths)
    header = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    lines = [header, sep]
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines) + "\n"
