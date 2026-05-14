"""
Skills app — serve Anthropic-style SKILL.md files with progressive disclosure.

mount_skills(inact_app, prefix, *, store=None) registers:

  GET  {prefix}              list skills (frontmatter only)
                             ?tag=<tag>  ?q=<substring>
  GET  {prefix}/search       search skills by name/description
                             ?q=<substring>  (required)
  GET  {prefix}/{name}       raw SKILL.md (frontmatter + body)

A skill is a directory containing a SKILL.md file:

  <root>/<skill-name>/SKILL.md

SKILL.md frontmatter (required: name, description):

  ---
  name: orca-input-writer
  description: Use when generating ORCA input decks ...
  tags: [quntur, expert]
  version: 1
  allowed-tools: [...]            # optional hint
  ---

  <markdown body>

Modules contribute roots into a shared SkillStore via register_root().
Name collisions across roots fail fast at register time.

Phase 2 (deferred): bundled assets (/files endpoints, traversal guard).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml
from fastapi import Request

from ..utils import text_response, toml_str, Response


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (metadata, body). Empty metadata if no frontmatter present."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, m.group(2)


@dataclass
class SkillEntry:
    name: str
    description: str
    tags: list[str]
    version: str | int | None
    allowed_tools: list[str]
    path: Path           # path to SKILL.md
    extra: dict = field(default_factory=dict)

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def meta(self) -> dict:
        d = {
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
        }
        if self.version is not None:
            d["version"] = self.version
        if self.allowed_tools:
            d["allowed_tools"] = list(self.allowed_tools)
        return d


class SkillStore:
    """Aggregates SKILL.md files from one or more roots. Name-unique."""

    def __init__(self) -> None:
        self._skills: dict[str, SkillEntry] = {}
        self._roots: list[tuple[Path, list[str]]] = []
        self._tag_descriptions: dict[str, str] = {}

    # -- tag descriptions ----------------------------------------------------

    def describe_tag(self, name: str, description: str) -> None:
        """Attach a one-line description to a tag (shown on index pages)."""
        self._tag_descriptions[name] = description

    def tag_descriptions(self) -> dict[str, str]:
        """Return descriptions for tags actually in use, sorted by name."""
        used: set[str] = set()
        for s in self._skills.values():
            used.update(s.tags)
        return {
            name: self._tag_descriptions[name]
            for name in sorted(used)
            if name in self._tag_descriptions
        }

    # -- registration --------------------------------------------------------

    def register_root(
        self,
        path: str | Path,
        *,
        default_tags: Iterable[str] = (),
    ) -> int:
        """Scan `path/<skill>/SKILL.md`, add each. Returns count added.

        Raises ValueError on duplicate name across this or prior roots.
        """
        root = Path(path)
        if not root.is_dir():
            return 0
        defaults = list(default_tags)
        added = 0
        # Optional sibling file: <root>/TAGS.yaml — `{tag: description}`.
        tags_file = root / "TAGS.yaml"
        if tags_file.is_file():
            try:
                data = yaml.safe_load(tags_file.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                data = None
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str):
                        self._tag_descriptions[k] = v.strip()

        for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            entry = self._load(skill_md, defaults)
            if entry is None:
                continue
            if entry.name in self._skills:
                prior = self._skills[entry.name].path
                raise ValueError(
                    f"duplicate skill name {entry.name!r}: "
                    f"{prior} and {entry.path}"
                )
            self._skills[entry.name] = entry
            added += 1
        self._roots.append((root, defaults))
        return added

    def reload(self) -> None:
        """Rescan all registered roots from scratch."""
        roots = list(self._roots)
        self._skills.clear()
        self._roots.clear()
        for root, defaults in roots:
            self.register_root(root, default_tags=defaults)

    def _load(self, skill_md: Path, default_tags: list[str]) -> SkillEntry | None:
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            return None
        meta, _body = _parse_frontmatter(text)

        name = meta.get("name")
        description = meta.get("description")
        if not name or not description:
            return None
        if not isinstance(name, str) or not isinstance(description, str):
            return None

        raw_tags = meta.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        tags: list[str] = []
        for t in list(default_tags) + list(raw_tags):
            if isinstance(t, str) and t and t not in tags:
                tags.append(t)

        allowed = meta.get("allowed-tools") or meta.get("allowed_tools") or []
        if isinstance(allowed, str):
            allowed = [allowed]
        allowed = [a for a in allowed if isinstance(a, str)]

        return SkillEntry(
            name=name,
            description=description,
            tags=tags,
            version=meta.get("version"),
            allowed_tools=allowed,
            path=skill_md,
            extra={k: v for k, v in meta.items()
                   if k not in {"name", "description", "tags", "version",
                                "allowed-tools", "allowed_tools"}},
        )

    # -- query ---------------------------------------------------------------

    def list(
        self,
        *,
        tag: str | None = None,
        q: str | None = None,
    ) -> list[SkillEntry]:
        out = list(self._skills.values())
        if tag:
            out = [s for s in out if tag in s.tags]
        if q:
            ql = q.lower()
            out = [s for s in out
                   if ql in s.name.lower() or ql in s.description.lower()]
        out.sort(key=lambda s: s.name)
        return out

    def get(self, name: str) -> SkillEntry | None:
        return self._skills.get(name)

    def __len__(self) -> int:
        return len(self._skills)


# ---------------------------------------------------------------------------
# HTTP attachment
# ---------------------------------------------------------------------------

def _toml_list_value(items: list[str]) -> str:
    return "[" + ", ".join(toml_str(x) for x in items) + "]"


def _format_list(prefix: str, entries: list[SkillEntry],
                 tag_descriptions: dict[str, str]) -> str:
    if not entries:
        return f"# No skills mounted at {prefix}\n"
    rows = [f"# {len(entries)} skill(s) at {prefix}\n\n"]
    for tag, desc in tag_descriptions.items():
        rows.append("[[tags]]\n")
        rows.append(f"name = {toml_str(tag)}\n")
        rows.append(f"description = {toml_str(desc)}\n\n")
    for s in entries:
        rows.append("[[skills]]\n")
        rows.append(f"name = {toml_str(s.name)}\n")
        rows.append(f"description = {toml_str(s.description)}\n")
        rows.append(f"tags = {_toml_list_value(s.tags)}\n")
        if s.version is not None:
            rows.append(f"version = {toml_str(str(s.version))}\n")
        rows.append(f"url = {toml_str(f'GET {prefix}/{s.name}')}\n\n")
    return "".join(rows)


def _format_search(prefix: str, q: str, entries: list[SkillEntry]) -> str:
    if not entries:
        return f"# No skills matching {toml_str(q)}\n"
    rows = [f"# {len(entries)} skill(s) matching {toml_str(q)}\n\n"]
    for s in entries:
        rows.append("[[results]]\n")
        rows.append(f"name = {toml_str(s.name)}\n")
        rows.append(f"description = {toml_str(s.description)}\n")
        rows.append(f"tags = {_toml_list_value(s.tags)}\n")
        if s.version is not None:
            rows.append(f"version = {toml_str(str(s.version))}\n")
        rows.append(f"url = {toml_str(f'GET {prefix}/{s.name}')}\n\n")
    return "".join(rows)


def _attach_skills(inact_app, prefix: str, store: SkillStore) -> None:
    def _index(request: Request):
        tag = request.query_params.get("tag") or None
        q = request.query_params.get("q") or None
        entries = store.list(tag=tag, q=q)
        return text_response(_format_list(prefix, entries,
                                          store.tag_descriptions()))

    def _search(request: Request):
        q = request.query_params.get("q") or None
        if not q:
            return text_response("ERROR 400: missing required query param 'q'\n", 400)
        entries = store.list(q=q)
        return text_response(_format_search(prefix, q, entries))

    def _detail(name: str):
        entry = store.get(name)
        if entry is None:
            return text_response(f"ERROR 404: unknown skill {name!r}\n", 404)
        return Response(
            content=entry.read(),
            status_code=200,
            media_type="text/markdown; charset=utf-8",
        )

    fastapi_app = inact_app.app
    fastapi_app.add_api_route(prefix, _index, methods=["GET"])
    # /search must be registered before /{name} so FastAPI doesn't consume "search" as a skill name.
    fastapi_app.add_api_route(prefix + "/search", _search, methods=["GET"])
    fastapi_app.add_api_route(prefix + "/{name}", _detail, methods=["GET"])

    def _human(path: str, request: Request | None = None):
        from ..render import render_markdown
        from ..utils import html_response
        # Caller passes the full request path (e.g. "/skills" or
        # "/skills/<name>"); strip the mount prefix to recover the
        # in-app subpath.
        rel = path[len(prefix):] if path.startswith(prefix) else path
        sub = rel.strip("/")
        if not sub:
            entries = store.list()
            lines = [f"# Skills ({len(entries)})\n\n"]
            tag_descs = store.tag_descriptions()
            if tag_descs:
                lines.append("## Tags\n\n")
                for tag, desc in tag_descs.items():
                    lines.append(f"- `{tag}` — {desc}\n")
                lines.append("\n## Skills\n\n")
            for s in entries:
                tag_str = ", ".join(f"`{t}`" for t in s.tags)
                lines.append(
                    f"- [{s.name}](/_human{prefix}/{s.name}) "
                    f"— {s.description}"
                )
                if tag_str:
                    lines.append(f"  \n  tags: {tag_str}")
                lines.append("\n")
            md = "\n".join(lines)
            return render_markdown(md, prefix)
        if sub == "search":
            q = (request.query_params.get("q") or "") if request else ""
            entries = store.list(q=q or None)
            header = f"# Search results for {q!r}" if q else "# All skills"
            lines = [f"{header} ({len(entries)})\n\n"]
            for s in entries:
                tag_str = ", ".join(f"`{t}`" for t in s.tags)
                lines.append(
                    f"- [{s.name}](/_human{prefix}/{s.name}) "
                    f"— {s.description}"
                )
                if tag_str:
                    lines.append(f"  \n  tags: {tag_str}")
                lines.append("\n")
            return render_markdown("\n".join(lines), prefix + "/search")
        entry = store.get(sub)
        if entry is None:
            return html_response(
                f"<h1>404</h1><p>unknown skill {sub!r}</p>", 404
            )
        return render_markdown(entry.read(), prefix + "/" + sub)

    inact_app._human_views[prefix] = _human
    parts = [p for p in prefix.strip("/").split("/") if p]
    label = "skills"
    if len(parts) >= 2 and parts[-1] == "skills":
        label = parts[-2] + " skills"
    inact_app.add_nav_item(label, "/_human" + prefix + "/")


# ---------------------------------------------------------------------------
# Mount function
# ---------------------------------------------------------------------------

def mount_skills(
    inact_app,
    prefix: str = "/skills",
    *,
    store: SkillStore | None = None,
) -> SkillStore:
    """Mount the skills app at *prefix*. Returns the store.

    Pass a pre-existing *store* to share across mounts; modules can call
    ``store.register_root(...)`` after mount to contribute their own
    skill directories.

    Example::

        from inact.apps.skills import mount_skills
        skills_store = mount_skills(app, "/skills")
        mount_estructural(app, ..., skills_store=skills_store)
        mount_quntur(app,     ..., skills_store=skills_store)

    Each module's ``mount_<module>`` is expected to call
    ``skills_store.register_root(<dir>, default_tags=[<module>])``.
    """
    p = "/" + prefix.strip("/")
    if store is None:
        store = SkillStore()
    _attach_skills(inact_app, p, store)
    inact_app._app_mounts.append((p, (
        f"\nSkills  {p}/\n"
        f"---\n"
        f"# SKILL.md files agents load as instructions/prompts.\n"
        f"\n  GET  {p}/            # list all skills (TOML)\n"
        f"  GET  {p}/?tag=geometry\n"
        f"  GET  {p}/?q=orca      # full-text search on index\n"
        f"  GET  {p}/search?q=orca  # dedicated search endpoint\n"
        f"  GET  {p}/<name>       # raw SKILL.md body\n"
        f"  GET  /_human{p}/      # browser-readable index\n"
    )))
    return store
