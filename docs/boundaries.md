# Boundaries

Ownership rules for inact. Read before refactoring across layers.

## Layer ownership

### `inact/core.py` — `Inact` class

Owns:
- HTTP route registration (`inact_md`, `inact_toml`, `route`).
- `/_human/<path>` dispatch (apps → routes → mounted files → website proxy).
- `/.help` resolution (per-path, with ancestor inheritance + auto-stub).
- Mount registries: `_routes`, `_help`, `_mounts`, `_mount_handlers`,
  `_mount_editable`, `_website_mounts`, `_app_mounts`, `_human_views`.
- MCP export (`to_mcp`) — auto-generated from registered routes/mounts.

May reference: app prefixes via `_app_mounts` / `_human_views`. Never imports
specific apps.

Must not: hardcode app-specific behavior, parse app responses.

### `inact/storage.py` — `Storage` + backends

Owns:
- Connection management (SQLite, PostgreSQL).
- `?` → `%s` placeholder translation for Postgres.
- `make_storage(url_or_path)` resolution.
- Public methods: `fetchall`, `fetchone`, `execute`, `init(ddl)`, `batch(ops)`.

Must not: know any app schema, hold app-specific helpers.

### `inact/pages.py` — content types

Owns: `MdContent`, `TomlContent`, `normalize_md`, `normalize_toml`.
Source of truth for what a route handler may return.

### `inact/render.py` — HTML rendering

Owns: `render_markdown`, `render_toml`, `render_plain`, `render_ls`,
nav-item registry. Pure presentation; no I/O, no DB.

### `inact/handlers.py` — file handlers

Owns per-extension serving (PDF page split, CSV chunking, …). Stateless;
called by `_render_human` and the `/files` mount.

### `inact/utils.py` — shared primitives

`text_response`, `html_response`, `toml_str`, `server_base`, `format_table`.
Add here only when ≥2 apps need it.

### `inact/apps/<app>.py` — mountable apps

Each app owns:
- Its DDL (schema is private to the app).
- Its data class (`XyzStore`) wrapping a `Storage` instance.
- `attach_xyz(inact_app, prefix, store)` — registers Flask routes,
  appends help to `_app_mounts`, registers human view in `_human_views`.
- `mount_xyz(inact_app, prefix, storage)` — resolves storage, instantiates
  store, calls `attach_*`.

Must not:
- Import from another `inact/apps/*` module (apps are siblings, not deps).
- Register `/_human/*` Flask routes directly — go through `_human_views`.
- Register `*/.help` Flask routes — go through `_app_mounts`.
- Mutate `inact_app._routes` directly (use decorators / `add_url_rule`).
- Use a placeholder other than `?` in SQL.
- Invent a new wire format. TOML `[[array]]` for lists; plain text otherwise.

## Source-of-truth rules

- DB rows are canonical. TOML output is a derived view. No agent flow may
  treat the TOML as authoritative for round-trip mutation — re-fetch by id.
- The `url` field in TOML items is the canonical pointer. App code must
  produce it from `prefix + "/" + id`, never from a request header.
- Help text in `_app_mounts` is the canonical user/agent contract for an app.
  If a route changes shape, update help in the same commit.

## Identifier rules

- App-generated ids: `uuid.uuid4()` strings. Don't reuse integer rowids
  across the wire.
- Prefixes are slash-normalized once at mount time (`"/" + prefix.strip("/")`).
  Downstream code assumes the normalized form.
- Endpoint names: `_inact_{app}_{prefix.replace('/', '__')}` plus a per-route
  suffix. Collisions raise at startup; don't suppress them.

## Failure semantics

- Path traversal in mounted folders → `403`. Already enforced in
  `_render_human`; don't add a second check downstream that swallows it.
- Missing rows → `ERROR 404: <thing> not found\n`. Status 404. Always.
- Bad input (missing required field) → `ERROR 400: <field> required\n`.
- DB errors propagate as `ERROR 500: <exc>\n`. Don't silently fall back to
  empty results — that masks data-loss bugs.

## Anti-patterns (do not)

- A second source of truth for help (e.g., a `HELP` constant alongside
  `_app_mounts` registration).
- Caching agent responses in the human view path (or vice-versa). Both
  re-render from the data layer.
- Cross-app FK references at the DB level. If two apps share data, the
  upstream app exposes a route; the downstream app calls it via
  `app.test_client()` or HTTP, not direct DB reads.
- App modules that pull `from inact.core import Inact` for a type — accept
  the app instance with no annotation, or use `"Inact"` as a forward ref.
