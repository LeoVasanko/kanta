# Kanta database

Kanta is a small embedded NoSQL store for async Python apps. It keeps live state in memory, writes transactional diffs to an append-only log, and supports versioned schema migrations.

## Why Kanta

- Fast synchronous reads and modifications on native Python objects
- Durable writes with append-only file and periodic snapshots
- Transaction semantics with rollback on failure
- Explicit schema evolution via `migrate_vN` functions
- Line-based JSON or binary MessagePack, or bring your own serializer
- Even in JSON we can use non-string keys, bytes, datetimes, UUID and other types

This design is often preferable when you want low-latency local persistence without operating a separate database service. You get straightforward deployment, auditable history, and deterministic replay while keeping application state ergonomic to work with.

Queries and updates on native data are far faster than over an SQL server connection, and we can can provide fully synchronous operation. The limitation is that you can only use the same database within a single process at a time, but this is well suited for async programming.

## Quick Start

```python
import asyncio
import msgspec
from uuid import UUID, uuid7

from kanta import Kanta


class User(msgspec.Struct):
    name: str = ""
    email: str | None = None


class Data(msgspec.Struct):
    users: dict[UUID, User] = {}


async def main() -> None:
    async with Kanta("data.kantadb", Data()) as kanta:
        user_id = uuid7()
        with kanta.transaction(action="create_user") as data:
            data.users[user_id] = User(name="Alice")


asyncio.run(main())
```

## Core Concepts

1. Define your schema as a `msgspec.Struct` root object.
2. Mutate data inside `with kanta.transaction(...):`.
3. Let Kanta flush queued changes to disk in the background.
4. Use snapshots and replay for fast startup and full history.

## Bootstrap and Open Modes

Kanta supports open-time bootstrap callbacks for initializing a brand-new
database before `open()` returns.

Register bootstrap handlers with a decorator:

```python
kanta = Kanta("data.kantadb", Data())

@kanta.bootstrap(action="seed", user="system")
def seed_defaults(data) -> None:
    data.users["admin"] = User(name="Admin")

await kanta.open()
```

You can also use `@kanta.bootstrap` with no arguments and async handlers:

```python
@kanta.bootstrap
async def bootstrap_async(data) -> None:
    data.counter = 1
```

When multiple bootstrap handlers are registered:
- they run in registration order,
- exactly one bootstrap change record is queued,
- bootstrap metadata (`action`, `user`, `mtime`) is taken from the last
  registration.

If any bootstrap handler raises, Kanta closes and removes the database file,
then re-raises the error.

`open()` also supports strict open mode:

```python
await kanta.open(create=False)
```

With `create=False`, open fails if the database file does not exist or is
empty.

## Fatal Error Handlers

Fatal background write errors can be observed with a decorator:

```python
import os
import signal

@kanta.fatal_error
async def on_fatal(err):
    os.kill(os.getpid(), signal.SIGTERM)  # Die
```

Multiple fatal handlers are supported and run in registration order.

## Migrations

Adding or removing a field and other such simple operations are automatic, but when the time comes to really change your data model, implement a `migrate_v1` function that converts your old data to the new form. This works on plain built-in dict and other types, to avoid needing to preserve old versions of your structs.

Pass a module (or import path) containing `migrate_vN` functions:

```python
kanta = Kanta("data.kantadb", Data(), migrations="myapp.migrations")
await kanta.open()
```

Kanta tracks migration version metadata automatically, and fast forwards your database to current version by running all the migrations needed while opening the database.

## On-Disk Format

Kanta in JSON mode (default) stores newline-delimited records. Transaction history is viewable by any simple text editor, and rollbacks to prior state are done by simply removing final lines (one per transaction)

MsgPack mode uses binary records with length and checksum to avoid data corruption.

- Change line: JSON object with metadata + `diff`
- Snapshot line: `SNAPSHOT { ... full state ... }`

See `docs/database.md` for format details and invariants.
