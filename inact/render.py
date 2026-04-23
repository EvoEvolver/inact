import os
import re

import jinja2
import markdown as _md

from .pages import normalize_md, normalize_toml
from .utils import html_response

# ---------------------------------------------------------------------------
# Jinja2 environment
# ---------------------------------------------------------------------------

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
    autoescape=True,
)


def render_template(name: str, **ctx) -> str:
    return _env.get_template(name).render(**ctx)


# ---------------------------------------------------------------------------
# Nav breadcrumb helper
# ---------------------------------------------------------------------------

def _nav(path: str) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    crumbs = ['<a href="/_human/">home</a>']
    for i, part in enumerate(parts):
        href = "/_human/" + "/".join(parts[: i + 1])
        crumbs.append(f'<a href="{href}">{part}</a>')
    return ' <span class="sep">/</span> '.join(crumbs)


def _pills(path: str, extra: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    parts = [p for p in path.strip("/").split("/") if p]
    raw_href = ("/" + "/".join(parts)) if parts else "/"
    pills = [("raw", raw_href), ("help", raw_href + "/.help")]
    if extra:
        pills.extend(extra)
    return pills


def _page(template: str, title: str, path: str,
          extra_pills: list[tuple[str, str]] | None = None, **ctx) -> tuple:
    html = render_template(template,
        title=title,
        nav=_nav(path),
        pills=_pills(path, extra_pills),
        **ctx,
    )
    return html_response(html)


# ---------------------------------------------------------------------------
# TOML structural + highlight helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cell(v) -> str:
    if isinstance(v, bool):
        cls = "v-t" if v else "v-f"
        return f'<span class="{cls}">{"true" if v else "false"}</span>'
    if isinstance(v, (int, float)):
        return f'<span class="v-n">{v}</span>'
    if isinstance(v, list):
        return f'<em style="color:#aaa">[{len(v)} items]</em>'
    if isinstance(v, dict):
        return '<em style="color:#aaa">{…}</em>'
    s = str(v)
    if s.startswith(("http://", "https://", "/")):
        return f'<a href="{_esc(s)}">{_esc(s)}</a>'
    return _esc(s)


def _struct_html(data: dict) -> str:
    parts = []

    scalars = {k: v for k, v in data.items() if not isinstance(v, (dict, list))}
    if scalars:
        rows = "".join(
            f'<tr><td class="kkey">{_esc(k)}</td><td class="kval">{_cell(v)}</td></tr>'
            for k, v in scalars.items()
        )
        parts.append(f'<div class="toml-scalars"><table>{rows}</table></div>')

    for k, v in data.items():
        if not isinstance(v, list) or not v:
            continue
        if all(isinstance(item, dict) for item in v):
            cols = list(v[0].keys())
            head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
            body_rows = "".join(
                "<tr>" + "".join(f"<td>{_cell(item.get(c, ''))}</td>" for c in cols) + "</tr>"
                for item in v
            )
            parts.append(
                f'<div class="toml-array"><h3>{_esc(k)} ({len(v)})</h3>'
                f'<table><thead><tr>{head}</tr></thead><tbody>{body_rows}</tbody></table></div>'
            )
        else:
            items = " ".join(f'<code>{_cell(i)}</code>' for i in v)
            parts.append(f'<div class="toml-array"><h3>{_esc(k)}</h3><p>{items}</p></div>')

    for k, v in data.items():
        if isinstance(v, dict):
            parts.append(f'<div class="toml-array"><h3>{_esc(k)}</h3>{_struct_html(v)}</div>')

    return "".join(parts) or '<p style="color:#aaa">empty</p>'


def _highlight_toml(text: str) -> str:
    lines = []
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        nl = "\n" if raw_line.endswith("\n") else ""
        stripped = line.lstrip()
        if stripped.startswith("#"):
            lines.append(f'<span class="tc">{_esc(raw_line)}</span>')
            continue
        if re.match(r"^\s*\[\[", line):
            lines.append(f'<span class="th">{_esc(line)}</span>{nl}')
            continue
        if re.match(r"^\s*\[", line):
            lines.append(f'<span class="th">{_esc(line)}</span>{nl}')
            continue
        m = re.match(r"^(\s*)([\w.\-\"]+)(\s*=\s*)(.+?)(\s*)$", line)
        if m:
            indent, key, eq, val, trail = m.groups()
            lines.append(
                f'{_esc(indent)}<span class="tk">{_esc(key)}</span>'
                f'{_esc(eq)}{_highlight_value(val.strip())}{_esc(trail)}{nl}'
            )
        else:
            lines.append(_esc(raw_line))
    return "".join(lines)


def _highlight_value(val: str) -> str:
    if val.startswith('"') or val.startswith("'"):
        return f'<span class="ts">{_esc(val)}</span>'
    if val in ("true", "false"):
        return f'<span class="tb">{val}</span>'
    if re.match(r"^-?\d", val):
        return f'<span class="tn">{_esc(val)}</span>'
    return _esc(val)


# ---------------------------------------------------------------------------
# Public render functions
# ---------------------------------------------------------------------------

def render_markdown(value, path: str) -> tuple:
    metadata, body = normalize_md(value)
    title = metadata.get("title", path.rsplit("/", 1)[-1] or "Home")
    content_html = _md.markdown(
        body, extensions=["fenced_code", "tables", "toc", "attr_list"]
    )
    meta_items = [(k, str(v)) for k, v in metadata.items() if k not in {"title", "help"}]
    return _page("markdown.html", str(title), path,
                 content=content_html, meta_items=meta_items)


def render_toml(value, path: str) -> tuple:
    data, toml_text = normalize_toml(value)
    title = (
        data.get("title")
        or (data.get("meta", {}).get("title") if isinstance(data.get("meta"), dict) else None)
        or path.rsplit("/", 1)[-1]
        or "TOML"
    )
    return _page("toml.html", str(title), path,
                 struct=_struct_html(data),
                 raw_highlighted=_highlight_toml(toml_text))


def render_plain(content: str, path: str) -> tuple:
    title = path.rsplit("/", 1)[-1] or "Page"
    return _page("plain.html", title, path, content=content)


def render_ls(entries: list[dict], path: str, prefix: str) -> tuple:
    title = "/" + prefix.strip("/")
    enriched = [
        {**e, "human_href": "/_human" + e["path"]}
        for e in entries
    ]
    return _page("ls.html", title, path, entries=enriched)
