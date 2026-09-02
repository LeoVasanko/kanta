# Bootstrap and Open Modes

When `open()` creates a brand-new database, it always writes a single bootstrap change record from the initial data object you passed to `Kanta(...)`. The simplest bootstrap is therefore the object itself — no extra code is required.

Bootstrap handlers are optional. Use them only when you need to modify the initial state at creation time, for example to seed defaults or perform expensive/external setup that should happen exactly once:

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

Whether or not handlers are registered, exactly one bootstrap change record is written when a new database is created. The record contains the initial object, or the state after all bootstrap handlers have run. When handlers are present:

- they run in registration order,
- bootstrap metadata (`action`, `user`, `mtime`) is taken from the last registration.

If any bootstrap handler raises, Kanta closes and removes the database file, then re-raises the error.

## Strict open mode

```python
await kanta.open(create=False)
```

With `create=False`, open fails if the database file does not exist or is empty.

## Read-only mode

Read-only mode opens an existing database without locking it or starting the background flush task. This is useful for readers that must not block the writer or modify the file:

```python
await kanta.open(readonly=True)
```

In read-only mode, records are replayed and migrations are applied in memory, but transactions and explicit flushes are rejected and the file is never created if missing.
