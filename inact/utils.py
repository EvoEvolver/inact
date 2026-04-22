from flask import request


def text_response(body: str, status: int = 200) -> tuple:
    return body, status, {"Content-Type": "text/plain; charset=utf-8"}


def html_response(body: str, status: int = 200) -> tuple:
    return body, status, {"Content-Type": "text/html; charset=utf-8"}


def toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def server_base() -> str:
    proto = request.headers.get("X-Forwarded-Proto", "http")
    return f"{proto}://{request.host}"


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
