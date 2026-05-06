# Agent Operating Map — inact

AI-first Flask toolkit. Every route returns `text/plain` by default for agents/curl;
`/_human/<path>` auto-renders HTML for browsers. Apps mount under prefixes and
follow a uniform `attach_*` / `mount_*` pattern.

Read this file first. Drop into deeper docs only when needed.

## Where to start reading

| You want to… | Read |
|---|---|
| Understand the framework surface | [`README.md`](README.md), [`inact/__init__.py`](inact/__init__.py) |
| Build a new app | [`docs/writing-an-app.md`](docs/writing-an-app.md) |
| Understand layer ownership / what not to break | [`docs/boundaries.md`](docs/boundaries.md) |
| See past design decisions | [`docs/decisions/log.md`](docs/decisions/log.md) |
| Index of all docs | [`docs/README.md`](docs/README.md) |
| See an end-to-end example app | [`example/app.py`](example/app.py) and others in `example/` |

## Repo layout

```
inact/
  core.py          # Inact class — routes, /_human, /.help, mounts
  pages.py         # MdContent / TomlContent + normalizers
  render.py        # HTML rendering for human view
  storage.py       # Storage abstraction (SQLite + Postgres, ? placeholders)
  handlers.py      # File handlers (PDF, CSV, …)
  utils.py         # text_response / html_response / toml_str / format_table
  apps/            # Mountable apps. Each = data class + attach_* + mount_*
    workspace/     # Multi-app bundle (todo, mailbox, message, register, …)
    slurm/         # (currently empty)
    a2a.py auth.py files.py forms.py issues.py jobs.py mcp.py
    notify.py remote.py s3.py search.py sql.py website.py
example/           # Runnable demo apps
workspace/         # Standalone Inact server image (Dockerfile + server.py)
docs/              # Human + agent docs (this harness)
tests/             # pytest suite (currently `test_remote.py` only)
.agent/            # Local agent scratch (git-ignored)
```

## Core conventions

- Every agent-facing response is `text/plain` via `text_response(body, status=200)`.
- Errors: `"ERROR {code}: description\n"` with matching HTTP status.
- Lists encoded as TOML `[[array]]` blocks. Each item carries a `url` field.
- SQL placeholders are `?` (Postgres backend translates to `%s`).
- Endpoint names must be globally unique. Convention: `_inact_{app}_{prefix.replace('/','__')}`.
- Help registered via `inact_app._app_mounts.append((prefix, text_or_callable))`.
- Human view registered via `inact_app._human_views[prefix] = fn(path) -> html_response(...)`.
- Public exports are routed through `inact/__init__.py`. Add new `mount_*` there.

## Validation commands

```bash
uv sync                                    # install deps incl. dev group
uv run pytest                              # run tests
uv run python -m compileall inact          # syntax sanity
uv run python example/app.py               # smoke a demo app on :5000
curl -s http://localhost:5000/.help        # confirm route + help wiring
curl -s http://localhost:5000/_human/      # confirm human view
```

When changing an app, exercise its three surfaces: agent (`curl /<prefix>/`),
help (`curl /<prefix>/.help`), human (`curl /_human/<prefix>/`).

## Style

- Plain Python 3.11+, no type-checking enforcement, but annotate public APIs.
- Keep app modules self-contained. New shared helpers go in `inact/utils.py`
  or `inact/storage.py` only when reused by 2+ apps.
- No JS frameworks in `_human` views — vanilla HTML+inline JS, narrow scope.
- Don't introduce a second response convention. TOML lists + plain text only.

## Plans and decisions

- Non-trivial work in flight: write `docs/plans/<topic>.active.md`.
  When done, rename suffix to `.summary.md` (do not move folders).
- Durable decisions (cross-cutting choices, deprecations, schema rules) go to
  [`docs/decisions/log.md`](docs/decisions/log.md). Append, don't rewrite.
- Per-session scratch (open threads, half-formed ideas): `.agent/chat_history.md`.
  Local to your machine, git-ignored, not shared truth.

## Boundaries (one-liner; full version in `docs/boundaries.md`)

- `inact/core.py` owns routing, `/_human` dispatch, `/.help` resolution. Apps
  must not register their own `/_human/*` or `*/.help` Flask routes — they
  hook through `_human_views` and `_app_mounts`.
- `inact/storage.py` owns DB connection + placeholder translation. App data
  classes own only schema (DDL) and queries.
- App modules own their routes, schema, and human view. They must not import
  from each other; share only via `inact/storage.py`, `inact/utils.py`,
  `inact/handlers.py`, `inact/pages.py`.
