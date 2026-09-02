# Usage Patterns: Opening and Data Handling

This document covers the basics of opening a database and working with the live data object: who owns it, where initial data comes from, and the lifecycle patterns Kanta supports.

## The data object belongs to you

You pass the root state object to `Kanta(...)`, and Kanta never replaces it with a new instance. Replay, migrations, transaction rollbacks — all restore or mutate the object's contents in place, preserving its identity. This means you may hold external references to the object (or to parts of it) and they stay valid for the lifetime of the `Kanta` instance:

```python
data = Data()
kanta = Kanta("data.kantadb", data)
await kanta.open()

assert kanta.data is data  # always the same object
```

Access the state as `kanta.data`, via your own reference, or both — they are the same object. Mutations must happen inside a transaction (see below); Kanta treats any detected out-of-transaction mutation as a fatal consistency violation.

Note that replacing the whole object is also possible (`kanta.data = Data()` has a setter), but then previously held references point at the old object — prefer in-place mutation.

## Where initial data comes from

The object passed to `Kanta(...)` seeds the database: when `open()` creates a brand-new (missing or empty) file, it writes a single bootstrap change record containing that object's contents, optionally modified by `@kanta.bootstrap` handlers (see [Bootstrap and open modes](bootstrap.md)).

When the file already exists, the constructor argument is *not* used as state — replay rebuilds the contents of `self.data` in place from the stored records, and the argument only defines the struct type. Consequence: if you close an instance, delete the file, and `open()` again, the new database is bootstrapped from the object's *current* contents — the old database's final state, not its original initial values. For a genuinely fresh start, construct a new data object (or reset the fields in a bootstrap handler).

## Lifecycle patterns

### Context manager (single database, scoped lifetime)

```python
async with Kanta("data.kantadb", Data()) as kanta:
    with kanta.transaction(action="create_user") as data:
        data.users[user_id] = User(name="Alice")
```

`async with` guarantees `open()`/`close()` pairing: the final flush happens on exit even on errors. Both `as` bindings are pure shortcuts — `async with Kanta(...) as kanta` binds the `Kanta` instance itself, and `with kanta.transaction(...) as data` binds exactly `kanta.data`. Use them or don't:

```python
async with Kanta("data.kantadb", Data()) as db:
    with db.transaction(action="rename"):
        db.data.users[user_id].name = "Bob"  # same object as `data` above
```

### Module-level global (typical application state)

When the database lives as long as the process, define it once and open/close at application startup and shutdown:

```python
kanta = Kanta("data.kantadb", Data())
data = kanta.data  # optional: your own direct reference


async def startup() -> None:
    await kanta.open()


async def shutdown() -> None:
    await kanta.close()


async def create_user(name: str) -> None:
    with kanta.transaction(action="create_user") as d:
        d.users[uuid7()] = User(name=name)
```

Transactions are synchronous context managers, so no `await` is needed per operation; the background task flushes queued changes periodically.

### Dynamically created databases (per-project, per-tenant, …)

Nothing requires module-level definitions. An app managing many databases simply constructs instances on demand and tracks them itself:

```python
class ProjectStore:
    def __init__(self) -> None:
        self.projects: dict[str, Kanta[Data]] = {}

    async def get(self, project_id: str) -> Kanta[Data]:
        kanta = self.projects.get(project_id)
        if kanta is None:
            kanta = Kanta(f"projects/{project_id}.kantadb", Data())
            await kanta.open()
            self.projects[project_id] = kanta
        return kanta
```

Remember that each open writer holds an exclusive lock on its file, so keep one `Kanta` instance per path and close instances you no longer need.

## Transactions in brief

- `with kanta.transaction(action=..., user=...) as data:` yields the live state object for mutation.
- On success, Kanta computes a diff against the pre-transaction state and queues a change record; on exception, the in-memory data is rolled back and the exception is re-raised.
- Nested transactions are rejected.
- A transaction that changes nothing queues no record.

See [On-disk format](database.md) for the full transaction semantics and [Validation](validation.md) for integrity checks that run inside each transaction.
