import markdown as _md

from .pages import normalize_md, normalize_toml
from .utils import html_response

_CSS = """
* { box-sizing: border-box; }
body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 860px; margin: 0 auto; padding: 1rem 2rem;
    color: #1a1a1a; line-height: 1.65; background: #fff;
}
nav {
    display: flex; flex-wrap: wrap; gap: 0.5rem 1rem;
    padding: 0.5rem 0; border-bottom: 1px solid #e0e0e0;
    margin-bottom: 2rem; font-size: 0.85rem;
}
nav a { color: #0550ae; text-decoration: none; }
nav a:hover { text-decoration: underline; }
nav .sep { color: #999; }
h1, h2, h3, h4 { margin-top: 1.5em; margin-bottom: 0.5em; }
h1 { font-size: 1.75rem; }
pre {
    background: #f6f8fa; padding: 1rem; border-radius: 6px;
    overflow-x: auto; font-size: 0.875rem; line-height: 1.5;
}
code {
    background: #f6f8fa; padding: 0.15em 0.4em;
    border-radius: 4px; font-size: 0.875em; font-family: monospace;
}
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.9rem; }
th, td { border: 1px solid #d0d7de; padding: 0.5rem 0.75rem; text-align: left; }
th { background: #f6f8fa; font-weight: 600; }
a { color: #0550ae; }
blockquote {
    border-left: 4px solid #d0d7de; margin: 1rem 0;
    padding: 0.25rem 1rem; color: #57606a;
}
.meta {
    background: #f6f8fa; border: 1px solid #d0d7de;
    border-radius: 6px; padding: 0.75rem 1rem;
    margin-bottom: 1.5rem; font-size: 0.85rem; display: grid;
    grid-template-columns: max-content 1fr; gap: 0.25rem 1rem;
}
.meta dt { font-weight: 600; color: #57606a; }
.meta dd { margin: 0; }
.toml-block { font-family: monospace; white-space: pre; }
"""

_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<nav>{nav}</nav>
{body}
</body>
</html>
"""

_RESERVED = {"._human", ".ls", ".grep", ".help"}


def _nav(path: str) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    crumbs = ['<a href="/_human/">~</a>']
    for i, part in enumerate(parts):
        href = "/_human/" + "/".join(parts[: i + 1])
        crumbs.append(f'<a href="{href}">{part}</a>')
    raw_href = ("/" + "/".join(parts)) if parts else "/"
    crumbs.append(f'<a href="{raw_href}">raw</a>')
    crumbs.append(f'<a href="{raw_href}/.help">help</a>')
    return ' <span class="sep">/</span> '.join(crumbs)


def _page(title: str, path: str, body_html: str) -> tuple:
    html = _TEMPLATE.format(
        title=title, css=_CSS, nav=_nav(path), body=body_html
    )
    return html_response(html)


def render_markdown(value, path: str) -> tuple:
    metadata, body = normalize_md(value)
    title = metadata.get("title", path.rsplit("/", 1)[-1] or "Home")

    meta_html = ""
    skip = {"title", "help"}
    meta_items = [(k, v) for k, v in metadata.items() if k not in skip]
    if meta_items:
        pairs = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in meta_items)
        meta_html = f'<dl class="meta">{pairs}</dl>'

    content_html = _md.markdown(
        body,
        extensions=["fenced_code", "tables", "toc", "attr_list"],
    )
    return _page(title, path, meta_html + content_html)


def render_toml(value, path: str) -> tuple:
    data, toml_text = normalize_toml(value)
    title = (
        data.get("title")
        or (data.get("meta", {}).get("title") if isinstance(data.get("meta"), dict) else None)
        or path.rsplit("/", 1)[-1]
        or "TOML"
    )

    meta_html = ""
    if "title" in data:
        pairs = "".join(
            f"<dt>{k}</dt><dd>{v}</dd>"
            for k, v in data.items()
            if k != "title" and not isinstance(v, (dict, list))
        )
        if pairs:
            meta_html = f'<dl class="meta">{pairs}</dl>'

    body_html = meta_html + f'<pre class="toml-block">{_escape(toml_text)}</pre>'
    return _page(str(title), path, body_html)


def render_plain(content: str, path: str) -> tuple:
    title = path.rsplit("/", 1)[-1] or "Page"
    body_html = f"<pre>{_escape(content)}</pre>"
    return _page(title, path, body_html)


def render_ls(entries: list[dict], path: str, prefix: str) -> tuple:
    title = f"Index of {prefix}"
    rows = ""
    for e in entries:
        href_raw = e["path"]
        href_human = "/_human" + href_raw
        icon = "📁" if e["type"] == "dir" else "📄"
        size = f'{e["size"]:,}' if e.get("size") is not None else ""
        rows += (
            f"<tr>"
            f'<td>{icon} <a href="{href_human}">{e["name"]}</a></td>'
            f"<td>{e['type']}</td>"
            f"<td>{size}</td>"
            f'<td><a href="{href_raw}">raw</a></td>'
            f"</tr>\n"
        )
    body_html = f"""
<h1>Index of <code>{prefix}</code></h1>
<table>
<thead><tr><th>Name</th><th>Type</th><th>Size</th><th></th></tr></thead>
<tbody>{rows}</tbody>
</table>
"""
    return _page(title, path, body_html)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
