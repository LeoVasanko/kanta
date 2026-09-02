# Migrations

Adding or removing a field and other such simple operations are automatic, but when the time comes to really change your data model, implement a `migrate_v1` function that converts your old data to the new form. This works on plain built-in dict and other types, to avoid needing to preserve old versions of your structs.

Pass a module (or import path) containing `migrate_vN` functions:

```python
kanta = Kanta("data.kantadb", Data(), migrations="myapp.migrations")
await kanta.open()
```

Kanta tracks migration version metadata automatically, and fast forwards your database to current version by running all the migrations needed while opening the database.
