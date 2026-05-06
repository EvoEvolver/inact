# Docs Index

Source of truth for the inact project. Both humans and agents land here.

## Start here

- [`../AGENTS.md`](../AGENTS.md) — agent operating map, conventions, validation
- [`../README.md`](../README.md) — public-facing user intro

## How-to

- [`writing-an-app.md`](writing-an-app.md) — full walkthrough for adding a
  mountable app (data class + `attach_*` + `mount_*` + `/_human` view)

## Architecture / rules

- [`boundaries.md`](boundaries.md) — what each layer owns; do-not rules
- [`decisions/log.md`](decisions/log.md) — durable design decisions

## Plans (work in flight or recently completed)

- `plans/<topic>.active.md` — currently executing
- `plans/<topic>.summary.md` — finished, kept for context

(directory created on first plan)

## Contracts (stable seams)

- `contracts/` — public surfaces other code/agents depend on
  (created when first contract is documented)

## Work summaries

- `work-summaries/YYYY-MM-DD-<topic>.md` — post-hoc records of significant
  refactors. Created as needed.
