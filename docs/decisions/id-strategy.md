# ID Strategy – inact

**Date**: 2026-05-06  
**Status**: Accepted

## Rule

All persistent entities in inact **MUST** use an integer primary key with the `AUTOINCREMENT` keyword:

```sql
CREATE TABLE IF NOT EXISTS my_entity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ...
);
```

## Rationale

- `INTEGER PRIMARY KEY AUTOINCREMENT` guarantees that SQLite (and Postgres via the same DDL pattern) will **never reuse** a deleted row's ID.
- This prevents subtle bugs where a deleted agent, issue, session, or task is accidentally resurrected with the same ID.
- Simplicity: the numeric ID is short, fast to index, and easy to read in logs and URLs.

## Allowed alternatives

Only use the following when integer sequential IDs are truly unsuitable:

| Type     | When to use                          | Example                     |
|----------|--------------------------------------|-----------------------------|
| UUIDv4   | Cross-system or public-facing IDs   | `api_key` for agents        |
| ULID     | Time-sortable, globally unique IDs  | Distributed tracing         |
| Snowflake| High-throughput distributed systems | (rarely needed)             |

Never use `RANDOM()` or `uuid_generate_v4()` as the primary key unless there is a documented performance or security reason accepted in `docs/decisions/`.

## Implementation checklist for new apps

1. In `_DDL`, always write:
   ```sql
   id INTEGER PRIMARY KEY AUTOINCREMENT
   ```
2. When inserting, let the database generate the ID (do **not** use `COALESCE(MAX(id), 0) + 1`).
3. Reference rows only by this numeric `id` in routes (`/issues/42`) and foreign keys.
4. Expose meaningful external identifiers (e.g. `api_key`, `slug`) as separate `UNIQUE` columns when needed.

**Violations of this rule will be treated as bugs and must be fixed before merge.**