# Validation

`@kanta.validate` registers an integrity validator for the database state, beyond the structural validation msgspec already performs during decoding.

```python
@kanta.validate
def check_users(data: Data) -> None:
    for user in data.users.values():
        if not user.name:
            raise ValueError("user without a name")
```

Validators receive the live data object (and optionally the `Kanta` instance as a second annotated parameter) and must **raise an exception** when the data is inconsistent. They are not intended to modify or correct the data — only to fail.

Validators run:

- **on open** — after replay, msgspec decoding and migrations, before the database becomes usable; a failure aborts the open and releases the file,
- **after each transaction** — before the change is committed to history; a failure rolls the transaction back, so invalid state never reaches the log.

Multiple validators may be registered; they run in registration order until the first failure. Validators must be synchronous (transactions are synchronous) — async callbacks are rejected at registration time.
