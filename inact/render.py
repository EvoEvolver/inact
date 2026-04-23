import re

import markdown as _md

from .pages import normalize_md, normalize_toml
from .utils import html_response

# ---------------------------------------------------------------------------
# Shared styles
# ---------------------------------------------------------------------------

_CSS = """
*, *::before, *::after { box-sizing: border-box; }
body {
    font-family: system-ui, -apple-system, sans-serif;
    background: #f5f5f5; color: #222; margin: 0; padding: 0;
    min-height: 100vh;
}
.wrap {
    max-width: 800px; margin: 0 auto; padding: 24px 20px 60px;
}

/* ── nav / breadcrumb ── */
nav {
    display: flex; flex-wrap: wrap; align-items: center;
    gap: 4px 6px; padding: 10px 20px;
    background: #fff; border-bottom: 1px solid #e0e0e0;
    font-size: 13px; position: sticky; top: 0; z-index: 10;
}
nav a { color: #0066cc; text-decoration: none; }
nav a:hover { text-decoration: underline; }
nav .sep { color: #ccc; }
nav .pill {
    margin-left: auto; background: #f0f0f0; color: #555;
    border-radius: 12px; padding: 3px 10px; font-size: 12px;
    text-decoration: none;
}
nav .pill:hover { background: #e0e0e0; text-decoration: none; }

/* ── markdown content ── */
.md { line-height: 1.7; }
.md h1 { font-size: 28px; margin: 0 0 20px; }
.md h2 { font-size: 20px; margin: 32px 0 12px; }
.md h3 { font-size: 16px; margin: 24px 0 8px; }
.md p  { margin: 0 0 16px; }
.md ul, .md ol { padding-left: 24px; margin: 0 0 16px; }
.md li { margin-bottom: 4px; }
.md a  { color: #0066cc; }
.md pre {
    background: #1e1e1e; color: #d4d4d4; padding: 16px 20px;
    border-radius: 8px; overflow-x: auto; font-size: 13px;
    line-height: 1.6; margin: 0 0 16px;
}
.md code {
    background: #f0f0f0; padding: 2px 6px; border-radius: 4px;
    font-size: 13px; font-family: ui-monospace, monospace;
}
.md pre code { background: none; padding: 0; color: inherit; }
.md blockquote {
    border-left: 4px solid #ddd; margin: 0 0 16px; padding: 4px 16px;
    color: #666;
}
.md table { border-collapse: collapse; width: 100%; margin: 0 0 16px; font-size: 14px; }
.md th, .md td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
.md th { background: #f6f8fa; font-weight: 600; }
.meta-box {
    background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
    padding: 14px 18px; margin-bottom: 24px;
    display: grid; grid-template-columns: max-content 1fr;
    gap: 6px 16px; font-size: 13px;
}
.meta-box dt { font-weight: 600; color: #666; }
.meta-box dd { margin: 0; word-break: break-word; }

/* ── tabs ── */
.tabs {
    display: flex; gap: 4px; margin-bottom: 16px;
}
.tab-btn {
    background: #e8e8e8; border: none; border-radius: 6px;
    padding: 6px 14px; font-size: 13px; cursor: pointer; color: #555;
}
.tab-btn.active { background: #0066cc; color: #fff; }
.tab-btn:hover:not(.active) { background: #ddd; }

/* ── TOML structural view ── */
.toml-scalars {
    background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
    overflow: hidden; margin-bottom: 20px;
}
.toml-scalars table { width: 100%; border-collapse: collapse; font-size: 14px; }
.toml-scalars td { padding: 9px 16px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
.toml-scalars tr:last-child td { border-bottom: none; }
.toml-scalars .kkey { font-weight: 600; color: #555; width: 30%; font-family: ui-monospace, monospace; font-size: 13px; }
.toml-scalars .kval { color: #222; word-break: break-word; }
.toml-scalars .kval a { color: #0066cc; }
.toml-array { margin-bottom: 24px; }
.toml-array h3 {
    font-size: 14px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .04em; color: #888; margin: 0 0 8px;
}
.toml-array table {
    width: 100%; border-collapse: collapse; background: #fff;
    border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden;
    font-size: 13px;
}
.toml-array th {
    background: #f6f8fa; font-weight: 600; padding: 8px 12px;
    text-align: left; color: #555; font-family: ui-monospace, monospace;
    font-size: 12px; border-bottom: 1px solid #e0e0e0;
}
.toml-array td {
    padding: 8px 12px; border-bottom: 1px solid #f0f0f0;
    vertical-align: top; word-break: break-word; max-width: 300px;
    overflow: hidden; text-overflow: ellipsis;
}
.toml-array tr:last-child td { border-bottom: none; }
.toml-bool-t { color: #22863a; font-weight: 600; }
.toml-bool-f { color: #cb2431; font-weight: 600; }
.toml-num    { color: #005cc5; }

/* ── TOML raw / syntax highlight ── */
.toml-raw {
    background: #1e1e1e; color: #d4d4d4; padding: 20px;
    border-radius: 8px; overflow-x: auto; font-size: 13px;
    line-height: 1.7; font-family: ui-monospace, monospace;
    white-space: pre;
}
.tc  { color: #6a9955; }  /* comment  */
.tk  { color: #9cdcfe; }  /* key      */
.ts  { color: #ce9178; }  /* string   */
.tn  { color: #b5cea8; }  /* number   */
.tb  { color: #569cd6; }  /* bool     */
.th  { color: #dcdcaa; font-weight: bold; }  /* section header */

/* ── directory listing ── */
.ls-table {
    background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
    overflow: hidden; width: 100%; border-collapse: collapse; font-size: 14px;
}
.ls-table th {
    background: #f6f8fa; font-weight: 600; padding: 10px 16px;
    text-align: left; border-bottom: 1px solid #e0e0e0; font-size: 13px; color: #555;
}
.ls-table td { padding: 10px 16px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }
.ls-table tr:last-child td { border-bottom: none; }
.ls-table a { color: #0066cc; text-decoration: none; }
.ls-table a:hover { text-decoration: underline; }
.ls-size { color: #888; font-size: 12px; }
.ls-raw  { color: #aaa; font-size: 12px; }
h1.page-title { font-size: 22px; margin: 0 0 20px; font-weight: 700; }
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
<div class="wrap">
{body}
</div>
{script}
</body>
</html>
"""

_TAB_SCRIPT = """<script>
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const group = btn.dataset.group;
    document.querySelectorAll(`.tab-btn[data-group="${group}"]`)
      .forEach(b => b.classList.remove('active'));
    document.querySelectorAll(`.tab-pane[data-group="${group}"]`)
      .forEach(p => p.hidden = true);
    btn.classList.add('active');
    document.getElementById(btn.dataset.target).hidden = false;
  });
});
</script>"""


# ---------------------------------------------------------------------------
# Nav breadcrumb
# ---------------------------------------------------------------------------

def _nav(path: str, extra_links: list[tuple[str, str]] | None = None) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    crumbs = ['<a href="/_human/">home</a>']
    for i, part in enumerate(parts):
        href = "/_human/" + "/".join(parts[: i + 1])
        crumbs.append(f'<a href="{href}">{part}</a>')
    nav = ' <span class="sep">/</span> '.join(crumbs)
    raw_href = ("/" + "/".join(parts)) if parts else "/"
    pills = f'<a class="pill" href="{raw_href}">raw</a>'
    if extra_links:
        for label, href in extra_links:
            pills += f' <a class="pill" href="{href}">{label}</a>'
    return nav + " " + pills


def _page(title: str, path: str, body_html: str, script: str = "",
          extra_nav: list[tuple[str, str]] | None = None) -> tuple:
    html = _TEMPLATE.format(
        title=title, css=_CSS,
        nav=_nav(path, extra_nav),
        body=body_html,
        script=script,
    )
    return html_response(html)


# ---------------------------------------------------------------------------
# TOML rendering helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cell(v) -> str:
    if isinstance(v, bool):
        cls = "toml-bool-t" if v else "toml-bool-f"
        return f'<span class="{cls}">{"true" if v else "false"}</span>'
    if isinstance(v, (int, float)):
        return f'<span class="toml-num">{v}</span>'
    if isinstance(v, list):
        return f'<em style="color:#aaa">[{len(v)} items]</em>'
    if isinstance(v, dict):
        return f'<em style="color:#aaa">{{…}}</em>'
    s = str(v)
    if s.startswith(("http://", "https://", "/")):
        return f'<a href="{_esc(s)}">{_esc(s)}</a>'
    return _esc(s)


def _struct_html(data: dict) -> str:
    parts = []

    # Top-level scalars → key/value table
    scalars = {k: v for k, v in data.items()
               if not isinstance(v, (dict, list))}
    if scalars:
        rows = "".join(
            f'<tr><td class="kkey">{_esc(k)}</td>'
            f'<td class="kval">{_cell(v)}</td></tr>'
            for k, v in scalars.items()
        )
        parts.append(f'<div class="toml-scalars"><table>{rows}</table></div>')

    # Arrays of dicts → table
    for k, v in data.items():
        if not isinstance(v, list) or not v:
            continue
        if all(isinstance(item, dict) for item in v):
            cols = list(v[0].keys())
            head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
            body_rows = ""
            for item in v:
                cells = "".join(f"<td>{_cell(item.get(c, ''))}</td>" for c in cols)
                body_rows += f"<tr>{cells}</tr>"
            label = f"{k} ({len(v)})"
            parts.append(
                f'<div class="toml-array"><h3>{_esc(label)}</h3>'
                f'<table><thead><tr>{head}</tr></thead>'
                f'<tbody>{body_rows}</tbody></table></div>'
            )
        else:
            items = " ".join(f'<code>{_cell(i)}</code>' for i in v)
            parts.append(
                f'<div class="toml-array"><h3>{_esc(k)}</h3><p>{items}</p></div>'
            )

    # Nested dicts
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        inner = _struct_html(v)
        parts.append(
            f'<div class="toml-array"><h3>{_esc(k)}</h3>{inner}</div>'
        )

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
            val_esc = _highlight_toml_value(val.strip())
            lines.append(
                f'{_esc(indent)}<span class="tk">{_esc(key)}</span>'
                f'{_esc(eq)}{val_esc}{_esc(trail)}{nl}'
            )
        else:
            lines.append(_esc(raw_line))

    return "".join(lines)


def _highlight_toml_value(val: str) -> str:
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

    meta_html = ""
    skip = {"title", "help"}
    items = [(k, v) for k, v in metadata.items() if k not in skip]
    if items:
        pairs = "".join(f"<dt>{_esc(str(k))}</dt><dd>{_esc(str(v))}</dd>"
                        for k, v in items)
        meta_html = f'<dl class="meta-box">{pairs}</dl>'

    content_html = _md.markdown(
        body, extensions=["fenced_code", "tables", "toc", "attr_list"]
    )
    body_html = f'<h1 class="page-title">{_esc(str(title))}</h1>' + meta_html + f'<div class="md">{content_html}</div>'
    return _page(str(title), path, body_html)


def render_toml(value, path: str) -> tuple:
    data, toml_text = normalize_toml(value)
    title = (
        data.get("title")
        or (data.get("meta", {}).get("title") if isinstance(data.get("meta"), dict) else None)
        or path.rsplit("/", 1)[-1]
        or "TOML"
    )

    struct_html = _struct_html(data)
    raw_html = f'<div class="toml-raw">{_highlight_toml(toml_text)}</div>'

    body_html = f"""
<h1 class="page-title">{_esc(str(title))}</h1>
<div class="tabs">
  <button class="tab-btn active" data-group="toml" data-target="pane-struct">Structured</button>
  <button class="tab-btn"        data-group="toml" data-target="pane-raw">Raw TOML</button>
</div>
<div id="pane-struct" class="tab-pane" data-group="toml">{struct_html}</div>
<div id="pane-raw"    class="tab-pane" data-group="toml" hidden>{raw_html}</div>
"""
    return _page(str(title), path, body_html, script=_TAB_SCRIPT)


def render_plain(content: str, path: str) -> tuple:
    title = path.rsplit("/", 1)[-1] or "Page"
    body_html = (
        f'<h1 class="page-title">{_esc(title)}</h1>'
        f'<div class="toml-raw" style="background:#1e1e1e">{_esc(content)}</div>'
    )
    return _page(title, path, body_html)


def render_ls(entries: list[dict], path: str, prefix: str) -> tuple:
    title = f"/{prefix.strip('/')}"
    rows = ""
    for e in entries:
        href_raw   = e["path"]
        href_human = "/_human" + href_raw
        icon = "▶" if e["type"] == "dir" else "·"
        size = f'{e["size"]:,} B' if e.get("size") is not None else "—"
        name = e["name"] + ("/" if e["type"] == "dir" else "")
        rows += (
            f"<tr>"
            f'<td style="color:#aaa;font-family:monospace;width:20px">{icon}</td>'
            f'<td><a href="{href_human}">{_esc(name)}</a></td>'
            f'<td class="ls-size">{size}</td>'
            f'<td><a class="ls-raw" href="{href_raw}">raw</a></td>'
            f"</tr>\n"
        )
    body_html = f"""
<h1 class="page-title">{_esc(title)}</h1>
<table class="ls-table">
<thead><tr><th></th><th>Name</th><th>Size</th><th></th></tr></thead>
<tbody>{rows}</tbody>
</table>
"""
    return _page(title, path, body_html)
