# Writing an inact App

An inact app is a self-contained Python module that registers HTTP routes on
an `Inact` instance, stores its own data, and optionally injects a human-facing
HTML page and help text.  All existing apps (`mailbox`, `todo`, `message`, …)
follow the same pattern.

---

## Structure

Every app has three layers:

```
inact/apps/myapp.py
├── Data layer      — a class that wraps Storage and owns the SQL schema
├── attach_myapp()  — registers Flask routes, injects help + human view
└── mount_myapp()   — resolves storage/config and calls attach_myapp
```

---

## 1. Data layer

```python
from ..storage import Storage

_DDL = [
    """CREATE TABLE IF NOT EXISTS notes (
        id         TEXT    PRIMARY KEY,
        content    TEXT    NOT NULL DEFAULT '',
        created_at BIGINT  NOT NULL
    )""",
]

class NoteStore:
    def __init__(self, storage: Storage):
        self._s = storage
        self._s.init(_DDL)          # creates tables if they don't exist

    def create(self, content: str) -> str:
        import uuid, time
        note_id = str(uuid.uuid4())
        self._s.execute(
            "INSERT INTO notes VALUES (?, ?, ?)",
            (note_id, content, int(time.time())),
        )
        return note_id

    def list_all(self) -> list[dict]:
        return self._s.fetchall("SELECT * FROM notes ORDER BY created_at DESC")

    def get(self, note_id: str) -> dict | None:
        return self._s.fetchone("SELECT * FROM notes WHERE id = ?", (note_id,))

    def delete(self, note_id: str) -> bool:
        return self._s.execute("DELETE FROM notes WHERE id = ?", (note_id,)) > 0
```

**Storage rules:**
- Use `?` as the placeholder in all SQL — both SQLite and PostgreSQL backends
  accept it (the Postgres backend translates to `%s` automatically).
- Call `self._s.init(_DDL)` in `__init__` — it creates tables with
  `CREATE TABLE IF NOT EXISTS`, so it is safe to call repeatedly.
- Use `self._s.batch(ops)` for multi-statement transactions.

---

## 2. Route attachment

```python
from flask import request
from ..utils import text_response, toml_str

def attach_notes(inact_app, prefix: str, store: NoteStore) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_notes_" + prefix.replace("/", "__")   # unique endpoint prefix
    flask_app = inact_app.app

    def _root():
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            content = (body.get("content") or "").strip()
            if not content:
                return text_response("ERROR 400: 'content' required\n", 400)
            note_id = store.create(content)
            return text_response(f"OK\nid = {toml_str(note_id)}\n")

        notes = store.list_all()
        lines = [f"# Notes\n# {len(notes)} note(s)\n\n"]
        for n in notes:
            lines.append("[[notes]]\n")
            lines.append(f"id      = {toml_str(n['id'])}\n")
            lines.append(f"content = {toml_str(n['content'])}\n")
            lines.append(f"url     = {toml_str(prefix + '/' + n['id'])}\n")
            lines.append("\n")
        return text_response("".join(lines))

    def _note(note_id: str):
        if request.method == "DELETE":
            ok = store.delete(note_id)
            return text_response("OK\n" if ok else "ERROR 404: not found\n",
                                 200 if ok else 404)
        n = store.get(note_id)
        if not n:
            return text_response("ERROR 404: note not found\n", 404)
        return text_response(
            f"id      = {toml_str(n['id'])}\n"
            f"content = {toml_str(n['content'])}\n"
        )

    flask_app.add_url_rule(
        prefix + "/",
        endpoint=ep + "_root", view_func=_root, methods=["GET", "POST"])
    flask_app.add_url_rule(
        prefix + "/<note_id>",
        endpoint=ep + "_note", view_func=_note, methods=["GET", "DELETE"])
```

**Route conventions:**
- All responses are `text/plain` via `text_response(body, status=200)`.
- Errors are `"ERROR {code}: description\n"` with the matching HTTP status.
- Lists use TOML `[[array]]` entries.
- Every item includes a `url` field pointing to its own endpoint.
- Endpoint names must be globally unique — prefix them with `_inact_{app}_{prefix}`.

---

## 3. Injecting a `/.help` page

Register help text in `_app_mounts` so `GET /.help` and `GET /notes/.help`
automatically describe your app:

```python
inact_app._app_mounts.append((prefix, (
    f"\nNotes: {prefix}\n"
    f"  GET    {prefix}/           list notes\n"
    f"  POST   {prefix}/           create note  body: {{\"content\":\"...\"}}\n"
    f"  GET    {prefix}/{{id}}       get note\n"
    f"  DELETE {prefix}/{{id}}       delete note\n"
)))
```

The string is appended verbatim to the `/.help` output whenever the requested
path matches `prefix`.  You can also register a callable instead of a string.

---

## 4. Injecting a `/_human/<path>` page

Register an HTML view in `_human_views` so `GET /_human/notes/` returns a
browser-friendly page instead of a 404.  The callable receives the full path
and must return a Flask response tuple.

```python
from ..utils import html_response

def _human_view(path: str):
    p = prefix
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Notes</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 640px;
          margin: 40px auto; padding: 0 16px; color: #222; }}
  input {{ border: 1px solid #ddd; border-radius: 6px; padding: 8px 12px;
           font-size: 14px; width: 100%; }}
  button {{ background: #0066cc; color: #fff; border: none;
            border-radius: 6px; padding: 8px 16px; cursor: pointer; }}
  .note {{ padding: 12px 0; border-bottom: 1px solid #eee; font-size: 14px; }}
</style>
</head>
<body>
<h1>Notes</h1>
<div style="display:flex;gap:8px;margin:16px 0">
  <input id="inp" placeholder="New note…">
  <button id="add-btn">Add</button>
</div>
<div id="list">Loading…</div>
<script>
const P = {p!r};
async function load() {{
  const text = await fetch(P + '/').then(r => r.text());
  const notes = text.split('[[notes]]').slice(1).map(b => {{
    const id = (b.match(/id\\s*=\\s*"([^"]+)"/)  || [])[1];
    const ct = (b.match(/content\\s*=\\s*"([^"]+)"/) || [])[1];
    return {{id, ct}};
  }}).filter(n => n.id);
  document.getElementById('list').innerHTML = notes.map(n =>
    `<div class="note">${{n.ct}} <a href="#" data-id="${{n.id}}"
     style="color:#cc0000;font-size:12px;float:right">delete</a></div>`
  ).join('') || '<p style="color:#bbb">No notes yet.</p>';
  document.querySelectorAll('[data-id]').forEach(a => {{
    a.addEventListener('click', async e => {{
      e.preventDefault();
      await fetch(P + '/' + a.dataset.id, {{method:'DELETE'}});
      load();
    }});
  }});
}}
document.getElementById('add-btn').addEventListener('click', async () => {{
  const content = document.getElementById('inp').value.trim();
  if (!content) return;
  await fetch(P + '/', {{method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{content}})}});
  document.getElementById('inp').value = '';
  load();
}});
load();
</script>
</body></html>"""
    return html_response(html)

inact_app._human_views[prefix] = _human_view
```

Now `GET /_human/notes/` and any sub-path like `/_human/notes/some-id` all
dispatch to `_human_view`.  The `path` argument gives you the full requested
path in case you want to render different content for sub-paths.

---

## 5. The mount function

```python
def mount_notes(inact_app, prefix: str, storage) -> None:
    """
    Mount a notes app at *prefix*.

    *storage* — database URL/path or a Storage instance.

    Example::

        mount_notes(app, "/notes", "./notes.db")
    """
    from ..storage import make_storage
    p = "/" + prefix.strip("/")
    backend = make_storage(storage) if isinstance(storage, str) else storage
    store = NoteStore(backend)
    attach_notes(inact_app, p, store)
```

`attach_notes` registers routes, help text, and the human view — all in one call.

---

## 6. Export it

Add to `inact/apps/__init__.py` (or directly in `inact/__init__.py`):

```python
from .apps.notes import NoteStore, mount_notes
```

---

## Full example

```python
from inact import Inact
from inact.apps.notes import mount_notes   # your new app

app = Inact(__name__)
mount_notes(app, "/notes", "./notes.db")

if __name__ == "__main__":
    app.run(debug=True)
```

```
# Agent view
curl http://localhost:5000/notes/
curl -X POST http://localhost:5000/notes/ \
     -H 'Content-Type: application/json' \
     -d '{"content": "remember to call bob"}'

# Human view (browser)
open http://localhost:5000/_human/notes/

# Help
curl http://localhost:5000/.help
curl http://localhost:5000/notes/.help
```

---

## Checklist

| Step | What |
|---|---|
| DDL + data class | `_DDL` list, `XyzStore.__init__` calls `self._s.init(_DDL)` |
| Routes | `attach_xyz` uses `flask_app.add_url_rule`, unique endpoint names |
| Responses | `text_response(toml_str)` for agents, errors as `ERROR N: msg\n` |
| Help | Append `(prefix, help_text)` to `inact_app._app_mounts` |
| Human page | Register `fn(path) -> html_response(...)` in `inact_app._human_views` |
| Mount | `mount_xyz` resolves storage, creates store, calls `attach_xyz` |
| Export | Add to `inact/__init__.py` |
