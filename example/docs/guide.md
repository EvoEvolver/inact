---
title: Getting Started Guide
tags: intro, setup
---
# Getting Started

## Installation

```bash
uv add inact
```

## Quick Start

```python
from inact import Inact

app = Inact(__name__)

@app.md_route("/")
def index():
    return """---
title: My AI Site
help: |
  Welcome! This site is agent-friendly.
  GET /          main page (plain text)
  GET /_human/   rendered HTML
---
# Hello

This is an AI-oriented website built with inact.
"""

app.run()
```

## Features

- **Plain text by default** — every route returns `text/plain`, curl-friendly
- **HTML on demand** — visit `/_human/<path>` for rendered views
- **Built-in help** — every path has a `/.help` endpoint
- **File browsing** — mounted folders expose `/.ls` and `/.grep`
