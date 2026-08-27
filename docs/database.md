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
- Normalization changes introduced by struct decode/encode are logged together
  with migrations as `migrate:vN`, or as `migrate:msgspec` when no migration
  ran but normalization still produces a diff.

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
- `await kanta.open(readonly=True)` opens an existing database read-only.
  - The file is opened without acquiring a lock and without a background flush
    task.
  - Existing records are replayed and migrations are still applied in memory.
  - Transactions and explicit flushes are rejected.
  - The file is never created if missing.

### Callbacks

All callbacks are registered via decorators and receive arguments by their
annotation types.  Parameters without a supported annotation are only allowed
when they have a default value.

#### Bootstrap Callbacks

- When `open()` creates a new database, it always writes a single bootstrap
  `ChangeRecord`.
- The simplest bootstrap is the initial data object passed to `Kanta(...)`;
  bootstrap callbacks are optional and only needed when you want to modify or
  enrich that object at creation time.
- Register callbacks via:
  - `@kanta.bootstrap`
  - `@kanta.bootstrap(action=..., user=..., mtime=...)`
- Bootstrap callbacks may be sync or async.  The live root data object is
  injected by annotating a parameter with the struct type passed to `Kanta`,
  and the `Kanta` instance itself can be injected by annotating a parameter
  with `Kanta`.
- Multiple bootstrap callbacks are supported:
  - callbacks execute in registration order,
  - exactly one bootstrap `ChangeRecord` is queued,
  - bootstrap metadata (`action`, `user`, `mtime`) is taken from the last
    callback registration.
- If no bootstrap callbacks are registered, the bootstrap record still uses
  `action="bootstrap"` and contains the initial data object.
- If any bootstrap callback raises, Kanta closes and removes the database file,
  then re-raises the exception.

#### Fatal Error Handlers

- Fatal background persistence errors can be handled with `@kanta.fatal_error`.
- Handlers may be sync or async.  The `DatabaseError` is injected by annotating
  a parameter with `DatabaseError`; `Kanta` may also be injected.
- Multiple handlers are supported and invoked in registration order.  A failing
  handler is logged and does not prevent subsequent handlers from running.

#### Clock

- `@kanta.clock` registers a callback `() -> datetime` that replaces the
  default UTC clock.  Its value is used for all record timestamps (`ts`, and
  `m` when `mtime` is `True`) and for snapshot timestamps.
- The clock is only read when a timestamp is actually produced; no-op
  transactions and skipped snapshot checks do not read it.
- Register before `open()` so that bootstrap and migration records use the
  custom clock as well.  This is mainly useful for tests and reproducible
  demos.

#### Transaction Log Formatting

- Logfmt callbacks prettify identifiers in the change log and are registered with
  `@kanta.logfmt`.
- A logfmt callback is called for every value Kanta renders: diff values, path
  components, and the transaction `user`.  It receives the value as its first
  parameter and optionally a `path: str` parameter with the dot-notation path
  to the value.  The special path `"$user"` is used when rendering the
  transaction actor, replacing the old `user_display` parameter.
- The callback returns `str | None`: a string replaces the default rendering,
  while `None` means "fall through to the next formatter".
- State dicts are injected by parameter name or annotation tag, which share
  the same vocabulary: `prev` receives the previous state dict and `state`
  the current one.  Matching by name ignores the annotation entirely.  The
  `DictPrev`/`DictState` aliases (`Annotated[dict, "prev"]` /
  `Annotated[dict, "state"]`) work under any parameter name, and a tag takes
  precedence over the name.  `DictPre` and `DictPost` are kept as aliases of
  `DictPrev` and `DictState`.  The `Kanta` instance can also be injected.
- Alternatively, a logfmt callback can be a class inheriting from `LogFmt`; the
  framework instantiates it with the state dicts and calls its
  `resolve(value, path) -> str | None` method.
- Multiple logfmt callbacks are stacked in registration order; the first
  callback to return a non-`None` result wins.  If none handle a value, Kanta
  falls back to its default formatting.

The decorator accepts an optional ``path`` so the callback only runs for
values at that exact path:

```python
@kanta.logfmt(path="$user")
def resolve_user(value: str, state: dict) -> str | None:
    return state.get("users", {}).get(value, {}).get("name")

@kanta.logfmt(path="users.uuid-1")
def resolve_user_key(value: str) -> str | None:
    return names_by_id.get(value)
```

#### Transaction Log Headers

- By default a transaction is logged with an `action by user` header followed
  by the diff lines.  Added paths are colored green, deleted paths red.
- `kanta.transaction(..., extra=...)` accepts a display-only value that is
  shown after the action in the header.  Anything other than `None` is
  printed str-converted (colored by Kanta), unless a custom logemit handler
  does something else with it; it is never persisted in the `ChangeRecord`.
- `kanta.transaction(..., logdiff=False)` skips building and printing the diff
  body and logs only the header, which is useful for large or noisy
  changesets.  Diff output can also be disabled globally with
  `configure_logging(diff=False)`; diff lines are emitted on the
  `kanta.transaction.diff` child logger so applications can route or silence
  them separately from the headers.

#### Log Emitters

- Every change-related message Kanta emits (transaction/bootstrap/migration
  changes, file created/opened lines, migration summaries, aborted
  transactions) is described by a `kanta.logging.LogEvent` and dispatched
  through
  `kanta.logging.emit_event`.  Kanta's own output goes through the same
  mechanism: when no `logemit` callback handles an event,
  `kanta.logging.default_emit` renders it with the built-in formatting.
- A `LogEvent` carries the event `kind` (`"change"`, `"created"`,
  `"opened"`, `"migrated"`, `"aborted"`), the preferred `logger` and `level`,
  the
  `kanta` instance, and all relevant state: `action`, `user`, `extra`,
  `error` (for aborted transactions), `diff`, `previous`/`current` state
  dicts, the built `logfmt` chain, and version info for migration events.
  Application-specific context (e.g. a connection id) can be stored in
  `kanta.ctx` — a user-writable namespace — and read back in callbacks as
  `event.kanta.ctx`, which also covers creation/bootstrap events.
- The built-in formatting is assembled from standard blocks that custom
  emitters can reuse as-is or replace piecemeal:
  - `event.header` — a lazy property producing the default one-line header
    for any kind: `<action>[ <extra>][ by <user>]` for changes,
    `<action>[ <extra>][ by <user>] transaction aborted: <error>` for aborts,
    and the `🛢️ <file> created|opened|migrated ...` summaries.  It is
    settable: assign
    `event.header = ...` and return truthy to restyle the header while
    keeping the default diff routing.
  - `event.diff_lines` — a lazy property producing the pretty diff body for
    change events (built only if accessed).
  - `default_emit` itself is just `header` plus the `diff_lines` routing.
- `@kanta.logemit` registers a callback receiving the event.  The callback
  decides what is logged and where: it may log one or more messages on
  `event.logger`, log somewhere else, or nothing at all.  A falsy return
  value marks the event handled and stops the chain; a truthy return value
  passes the event — possibly modified — to the next registered callback.
  When all callbacks pass, `default_emit` renders the event; a callback may
  also call `default_emit(event)` itself to delegate events it does not
  customize.  Operational diagnostics (integrity errors, background flush
  failures) do not go through this mechanism.
- Logging never breaks functionality: a crashing `logemit` callback is
  reported with `logger.exception` and the event falls back to the built-in
  formatting; if the built-in formatting itself fails, the error is reported
  and swallowed.  The same applies to `logfmt` callbacks (a failing one is
  treated as a fall-through) and `logmigr` callbacks.

```python
@kanta.logemit
def emit(ev: LogEvent):
    if ev.kind != "change":
        return default_emit(ev)  # delegate, no chaining needed
    # Restyle the header; default_emit keeps routing the diff body.
    ev.header = str(Line().user(ev.user or "-", width=20)(" ").action(ev.action))
    return True
```

#### Terminal Formatting Helpers

- `kanta.tty` provides the building blocks used by Kanta's own rendering:
  - `colors`: the mutable color palette.  Colors are bare SGR parameter
    strings (e.g. `"1;34"`, `"38;5;226"`) without escape framing.  Attributes
    are read at render time, so assignments (`colors.action = "36"`) and
    additions (`colors.session = "38;5;226"`) take effect immediately.
  - `Line`: builds a terminal string part by part.  Calling it appends
    content (`str`-converted); `.<colorname>` arms a palette color for the
    next call only, and the reset is folded into a single escape sequence
    with whatever color comes next.  `width=`/`align=` pad by display width;
    `str(line)` finishes the line and restores default colors.
  - `strip_ansi`, `displaywidth` (wide chars and emoji count correctly) and
    `pad` for working with pre-colored strings.

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
