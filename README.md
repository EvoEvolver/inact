# inact

AI-oriented web framework built on Flask. Every route returns plain text by default (curl/agent-friendly), with automatic HTML rendering at `/_human/<path>` for humans.

## Install

```bash
pip install inact
```

## Quick start

```python
from inact import Inact, MdContent, TomlContent

app = Inact(__name__)

@app.inact_md("/")
def index():
    return MdContent("# Hello\n\nThis is an agent-friendly page.", title="Home")

@app.inact_toml("/status")
def status():
    return TomlContent({"service": {"status": "running"}}, annotation="GET /status")

app.mount("/docs", "./docs/")

app.run()
```

## Features

- **`inact_md`** — Markdown + frontmatter routes. Plain text for agents, rendered HTML at `/_human/<path>`.
- **`inact_toml`** — TOML routes with optional `# annotation` lines at the top.
- **`/_human/<path>`** — Auto-registered HTML rendering for every route and mounted file.
- **`/.help`** — Contextual help on every path, inherited from nearest ancestor if not defined.
- **`mount(prefix, folder)`** — Serve a local folder with `/.ls` (file listing) and `/.grep?q=` (content search).

## Response types

| Handler returns | Agent sees | Human sees (`/_human/`) |
|---|---|---|
| `MdContent(body, **meta)` | plain markdown | rendered HTML |
| `TomlContent(data, annotation=...)` | TOML with `# comment` header | structured HTML |
| raw `str` | string as-is | detected and rendered |

## License

MIT
