# Kanta Database Format and Design Principles

This document describes the on-disk format and design principles of Kanta.
It is intentionally focused on the current standalone package behavior.

## Core Principles

1. Append-only durability
- State changes are persisted as appended JSON lines.
- Existing lines are never edited in place.

2. Differential persistence
- Kanta stores diffs (patches), not full state, for normal writes.
- This keeps write volume small and preserves a clear change history.

3. Deterministic replay
- Current state is reconstructed by replaying log records in order.
- Snapshot records accelerate replay while preserving deterministic results.

4. Transactional in-memory writes
- Application code mutates in-memory data inside `kanta.transaction(...)`.
- On success, Kanta computes and queues a diff record.
- On failure, in-memory data is rolled back.

5. Explicit schema evolution
- Schema migration functions are versioned (`migrate_vN`).
- Migrations run at open time and advance the stored version.

## On-Disk Record Types

Kanta uses a newline-delimited stream where each line is either a change
record or a snapshot record.

### Change record

One JSON object per line:

```json
{"ts":"2026-06-10T02:55:00Z","a":"update","v":5,"u":"user-id","m":"2026-06-10T02:55:00Z","diff":{"users":{"alice":{"age":31}}}}
```

Fields:
- `ts`: UTC timestamp of the record.
- `a`: action name.
- `v`: schema version after this change.
- `u`: optional actor identifier.
- `m`: optional domain modification timestamp.
- `diff`: jsondiff patch payload.

### Snapshot record

Snapshot lines are prefixed with `SNAPSHOT `, followed by JSON:

```text
SNAPSHOT {"ts":"2026-06-10T00:00:00Z","v":5,"state":{"users":{}},"m":"2026-06-10T00:00:00Z"}
```

Fields:
- `ts`: snapshot creation time.
- `v`: schema version represented by the snapshot.
- `state`: full state dictionary.
- `m`: optional domain modification timestamp.

## Replay Model

1. Find the last snapshot in the file, if present.
2. Initialize replay state from snapshot state (or `{}` if none).
3. Replay subsequent change records in order using patch application.
4. The final replay state becomes in-memory `kanta.data`.

This model provides fast startup for large logs while retaining append-only
history.

## Serialization Semantics

- In-memory data is defined by an application `msgspec.Struct` type.
- Kanta round-trips through plain builtins for persistence and diffing.
- Dict keys are serialized as strings (`str_keys=True`) for stable JSON form.
- Normalization changes introduced by struct decode/encode are logged as
  `migrate:msgspec` when they produce a diff.

## Transaction Semantics

- `kanta.transaction(action=...)` captures a pre-transaction snapshot dict.
- By default a transaction updates the modification time `m` to the current UTC
  time.
- `mtime=True|False|datetime` controls the modification time `m`:
  - `True` (default) sets `m` to the current UTC time.
  - `False` omits `m`, leaving the previous modification time in effect.
  - A `datetime` sets `m` to that explicit value.
- System operations such as `migrate:msgspec` use `mtime=False` so they are not
  considered modifications and do not advance `m`.
- On success:
  - compute diff between previous builtins and current builtins,
  - queue a `ChangeRecord` if non-empty,
  - update `kanta.mtime` when the change carries an `m` value.
- On exception:
  - restore in-memory data from snapshot,
  - re-raise the exception.

Nested transactions are rejected.

## Modification Time

`kanta.mtime` exposes the last modification time carried forward from change
records. It is updated by normal transactions and preserved across snapshots and
reloads, while system operations such as migrations leave it unchanged.

## Flush and Lifecycle

- Writes are queued in memory.
- `kanta.flush()` appends queued records to disk.
- A background async task can flush periodically.
- `kanta.close()` performs final flush and releases file resources.
- `async with Kanta(...)` guarantees open/close lifecycle management.

### Open Modes

- `await kanta.open()` (default) creates the database file if missing.
- `await kanta.open(create=False)` fails when the file is missing or empty.

### Bootstrap Callbacks

- Bootstrap callbacks run during `open()` when the database is empty.
- Register callbacks via:
  - `@kanta.bootstrap`
  - `@kanta.bootstrap(action=..., user=..., mtime=...)`
- Bootstrap callbacks may be sync or async and receive the live root data
  object.
- Multiple bootstrap callbacks are supported:
  - callbacks execute in registration order,
  - exactly one bootstrap `ChangeRecord` is queued,
  - bootstrap metadata (`action`, `user`, `mtime`) is taken from the last
    callback registration.
- If any bootstrap callback raises, Kanta closes and removes the database file,
  then re-raises the exception.

### Fatal Error Handlers

- Fatal background persistence errors can be handled with `@kanta.fatal_error`.
- Handlers may be sync or async.
- Multiple handlers are supported and invoked in registration order.

## Migrations

- Migration source is configured on `Kanta(...)` via `migrations=`.
- Accepted values:
  - imported module object,
  - import path string.
- Migrations mutate replayed dict state in-place and return the new version.

## Safety Invariants

- Any detected out-of-transaction mutation is treated as a fatal consistency
  violation.
- Flush failures mark the instance as failed and trigger shutdown behavior.
- Object identity of `kanta.data` is preserved across rollback when possible,
  minimizing stale-reference hazards for callers.
