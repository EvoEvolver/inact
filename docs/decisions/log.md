# Decision Log

Append-only. Newest at top. Each entry: date, decision, why, consequences.
Capture only durable, cross-cutting choices — not routine code changes.

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
