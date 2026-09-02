# Database Rotation

Kanta can bound the on-disk history of a database to a configurable retention window (e.g. the last 30 days) by *rotating* the database file: the aged-out content is copied to a timestamped sibling file and the main file is truncated and rewritten in place with only the retained history plus fresh snapshots. Normal operation stays append-only under the exclusive lock; rotation is the only operation that rewrites the file.

## Configuration

Rotation is enabled with a keyword option on `Kanta(...)`:

- `retention: timedelta | int | None = None` — history window to keep; a plain `int` is interpreted as a number of days. `None` (default) disables rotation entirely.

Rotation uses the same clock as record timestamps, so a custom `@kanta.clock` callback controls it as well (useful in tests).

## When rotation runs

Rotation happens inside `Kanta.open()`, after acquiring the exclusive lock and before replay. At that point the file is quiescent: no background flush loop is running yet and no records are in flight. Rotation therefore never races with the background writer, snapshot requests, or migration snapshot writes.

Consequence: a database that is never reopened never rotates. For long-running services, rotation takes effect on the next restart.

## Rotated file naming

The history that aged out is preserved at:

```
{stem}@{ISO-8601 timestamp}.kantadb
```

- `{stem}` is the original filename with its extension stripped (`Path(filename).stem`), so databases named `data`, `data.kantadb`, or `data.db` all rotate to `data@….kantadb`.
- The timestamp is the `ts` of the last record dropped by the rotation (the leading snapshot of the rewritten main file carries the same `ts`), not the rotation time — the name tells you exactly which point in history the rotated file ends at. It is rendered in ISO 8601 basic format at second precision (e.g. `20260902T143000Z`); the exact microsecond timestamp of the cutoff remains available inside the file as the `ts` of its final line and of the leading snapshot of the new file.
- Rotated files live in the same directory as the main file.
- On collision (a rotated file with the same name already exists), an incrementing suffix is inserted before the extension (`data@….1.kantadb`, `data@….2.kantadb`, …) rather than overwriting.
- Rotated files are never deleted by rotation.

## Rotation algorithm

Let `cutoff = now - retention`. All planning happens on the in-memory bytes of the database file already read by open; no second disk read is needed.

1. **Eligibility.** Rotation is skipped when there is nothing to do: when the file contains no change records older than `cutoff` (the retention window already covers all history), or when the file contains no change records at all (a snapshot-only file is treated as already fully rotated and never re-rotated).

2. **Replay base.** The base is the newest snapshot whose `ts <= cutoff`, falling back to the start of file when no such snapshot exists. Starting at the most recent snapshot would silently drop history that must be retained.

3. **Replay and validate.** The file is replayed from the base forward to the end. Records with `ts < cutoff` are applied to the replay (they are needed to reach the cutoff state) but not retained in the output. At every snapshot encountered after the base, the replayed state is validated against the snapshot state; a mismatch means the history is corrupt, and rotation is aborted with a `DatabaseError`, leaving the original file untouched. The byte offset just after the last record with `ts < cutoff` (frame-boundary aligned) is remembered as `cutoff_end`.

4. **Copy the original aside.** The main file is copied with `shutil.copy2` directly to its final `{stem}@{ts}.kantadb` name. On filesystems with copy-on-write this performs a cheap reflink copy. A failure here aborts rotation with the original file intact.

5. **Rewrite the main file in place.** On the locked file descriptor the content is replaced (seek 0, truncate, write, fsync) with, in order:
   1. A **snapshot of the state at the cutoff**, stamped with the schema version and modification time in effect at the cutoff. Its `ts` is the `ts` of the last pre-cutoff record — the same timestamp used in the rotated filename. This snapshot is the new replay base and carries the version forward so migrations are not re-run; it is always written.
   2. The retained change records (`ts >= cutoff`), recreated record by record — no internal snapshots are carried over.
   3. A **final snapshot** of the state after the last retained record, written only when enough changes were retained to warrant one (the same policy as regular snapshot writes). If no records survived the cutoff, the new file consists of the single leading snapshot and nothing else — the steady state for databases whose history has fully aged out.

6. **Trim the rotated copy.** The rotated file is truncated to `cutoff_end` bytes, so it contains only the dropped history and does not duplicate the records retained in the main file. The cut is at a frame boundary, so the rotated file remains a valid, replayable database on its own. This happens only after step 5's fsync, so until then the rotated file still holds the complete original content as a crash-recovery anchor.

7. **Continue normal open.** Replay and migrations proceed on the same locked file. Because the leading snapshot carries the current version, migrations run exactly as they would have against the old content.

Failure rule: any error before step 5 leaves the main file byte-identical (only an extra copy exists). A crash during step 5 may leave the main file torn, but the rotated copy still holds the complete original content — recovery is copying it back. After step 6 the split is complete and both files are consistent.

## Design notes

**In-place rewrite, not rename-and-recreate.** The writer holds an exclusive `flock` on the file from `open()` until `close()`, and `flock` is attached to the open file description (inode), not the path. Renaming the locked file away and creating a fresh file at the main path would open a race: between the rename and the creation, a second instance could open the missing path with `O_CREAT`, acquire its own lock on the fresh inode, and bootstrap a divergent database. On Windows, renaming the locked file would fail outright (the database is opened with `FILE_SHARE_READ` only). Rotation therefore never renames or unlinks the main file and never releases its lock; a second instance opening the path at any moment gets either the old content or the new, and never its own lock. The only primitive this requires is `LockedFile.replace_content()` (seek 0, truncate, write, fsync).

**Rewrite (re-frame), not verbatim copy**, for all records written to the main file. `BinFramer` checksums are offset-keyed (the checksum includes the absolute `record_offset`), so a verbatim byte copy to a new offset would be unreadable; binary records are re-framed at their new offsets. Rewriting also normalizes encoding drift and lets rotation drop the redundant intermediate snapshots the original file accumulated. The rotated copy is the one place where verbatim bytes are used — a raw `copy2` plus a frame-aligned tail truncation — which is safe precisely because it preserves original offsets: the truncated prefix keeps every frame at its original `record_offset`, so binary checksums stay valid.

## Integrity guarantees

- Rotation runs under the exclusive lock, before the background writer starts.
- The main path is never renamed, unlinked, or unlocked during rotation; no bootstrap race with a second instance is possible.
- Replay is validated against every snapshot in range; any validation failure aborts rotation with the original file intact.
- A leading cutoff snapshot (ts = last pre-cutoff record, schema version at the cutoff) is always written; a final snapshot is written only when warranted by retained changes.
- The full original content sits at `{stem}@{ts}.kantadb` before the main file is touched, and is only trimmed to the dropped-history prefix after the rewritten main file is fsynced.
- Rotated files are never deleted by rotation.
- Files with no change records (already reduced to a snapshot) are never re-rotated.
