# Decision Log

Append-only. Newest at top. Each entry: date, decision, why, consequences.
Capture only durable, cross-cutting choices — not routine code changes.

---

## 2026-05-11 — Centralize recursive tool discovery

**Decision:** Add `inact.apps.tools.mount_tool_tree` as the shared helper for
agent-facing hierarchical tool navigation.

**Why:** Multiple harness modules need folder-first, arbitrary-depth tool
discovery while keeping direct `POST /tools/<name>` execution flat.

**Consequences:** Apps should mount recursive `GET /tools` discovery through
`mount_tool_tree` instead of hand-rolling folder routes. Sparse folders can be
collapsed upward with `min_folder_tools` so navigation stays shallow. Unknown
folders and invalid paths return graceful `ERROR <code>:` text responses.

---

## 2026-05-06 — Adopt repo-embedded agent harness

**Decision:** Add `AGENTS.md`, `CLAUDE.md`, `docs/README.md`,
`docs/boundaries.md`, this log, and `.agent/` (git-ignored).

**Why:** Provide a stable entry point for AI coding agents and a shared place
for durable decisions. Prior state had only `README.md` and
`docs/writing-an-app.md`; agents had no map of conventions or boundaries.

**Consequences:** New non-trivial work should add a `docs/plans/<topic>.active.md`.
Cross-cutting decisions land here, not in commit messages alone.

---

<!-- Template:

## YYYY-MM-DD — <short title>

**Decision:** …

**Why:** …

**Consequences:** …

-->
