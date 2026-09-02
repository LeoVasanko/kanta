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

## Documentation

- [Usage patterns](https://git.zi.fi/LeoVasanko/kanta/src/branch/main/docs/usage.md) — opening, data ownership, and lifecycle patterns
- [Bootstrap and open modes](https://git.zi.fi/LeoVasanko/kanta/src/branch/main/docs/bootstrap.md) — seeding new databases, strict and read-only opens
- [Validation](https://git.zi.fi/LeoVasanko/kanta/src/branch/main/docs/validation.md) — `@kanta.validate` integrity checks on open and transactions
- [Migrations](https://git.zi.fi/LeoVasanko/kanta/src/branch/main/docs/migrations.md) — versioned schema evolution with `migrate_vN`
- [Retention and rotation](https://git.zi.fi/LeoVasanko/kanta/src/branch/main/docs/rotation.md) — bounding history to a time window
- [Fatal error handlers](https://git.zi.fi/LeoVasanko/kanta/src/branch/main/docs/fatal-errors.md) — observing background write failures
- [On-disk format](https://git.zi.fi/LeoVasanko/kanta/src/branch/main/docs/database.md) — record layout and invariants

Kanta in JSON mode (default) stores newline-delimited records, so transaction history is viewable in any text editor; MsgPack mode uses binary records with length and checksum to guard against corruption.
