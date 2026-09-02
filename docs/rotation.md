# Database Rotation

Goal: bound the on-disk history of a kanta database to a configurable retention
window (e.g. the last 30 days) by *rotating* the database file: the old content
is copied to a timestamped sibling file and the main file is truncated and
rewritten in place with only the retained history plus fresh snapshots. Normal
operation stays append-only under the exclusive lock; rotation is the only
operation that rewrites the file.

## Current facts the design must respect

- The writer holds an exclusive `flock` on the file from `open()` until
  `close()` (`kanta/filelock.py`). No other process can safely touch the file
  while a writer has it open.
- Records are append-only frames. Each `ChangeRecord` carries `ts` (record time)
  and `m` (modification time); snapshots carry `ts`, `v` (schema version) and
  `state` (`kanta/structs.py`).
- Replay reads the whole file, then starts from the **last snapshot**
  (`framer.scan_last_snapshot`, `serialization/base.py:replay`). Anything before
  the last snapshot is already logically dead.
- Snapshot state is validated in tooling: replayed state must equal snapshot
  state (`kanta/replaylog.py`). A snapshot is therefore a consistency
  checkpoint, not just an accelerator.
- `BinFramer` checksums are **offset-keyed** (checksum includes the absolute
  `record_offset`). A binary frame copied to a different byte offset is
  corrupted. `LineFramer` (JSONL) has no checksums.
- There is **no fsync/fdatasync** anywhere; durability currently relies on the
  OS page cache. Rotation must not make this worse, and should fix it for the
  rotation path at minimum.
- Migrations run on open, after replay, against the snapshot/replay version.
  A snapshot records the version it was written at, so "db already migrated"
  survives in the snapshot even if the migrations produced no change records.

## Rotated file naming

The history that aged out is preserved at:

```
{stem}@{ISO-8601 timestamp}.kantadb
```

- `{stem}` is the original filename with its extension stripped
  (`Path(filename).stem`).
- The timestamp is the **ts of the last record dropped by the rotation** (see
  step 4 — the leading snapshot of the rewritten main file carries the same
  ts), not the current time. The name tells you exactly which point in history
  the rotated file ends at. Rendered in ISO 8601 basic format at second
  precision (e.g. `20260902T143000Z`). The exact microsecond timestamp of the
  cutoff remains available inside the file (it is the ``ts`` of the final
  line of the rotated file and of the snapshot at the start of the new file);
  a second rotation within the same second cannot occur because rotation
  requires history to have aged past the cutoff.
- The rotated name always ends in `.kantadb`, regardless of the original
  extension. Users may name their databases with no extension, `.kantadb`, or
  anything else (`.db`, …). Since the rotated name is derived from the *stem*,
  all of these work uniformly: `data` → `data@20260902T143000Z.kantadb`,
  `data.kantadb` → `data@….kantadb`, `data.db` → `data@….kantadb`.
- Rotated files live in the same directory.
- Collision: if a rotated file with the same name already exists (rotation
  rerun over identical history — should be prevented by the eligibility check
  below, but be defensive), append a disambiguating suffix rather than
  overwriting.

## Why in-place rewrite (and not rename-and-recreate)

An earlier draft renamed the locked file away and created a fresh file at the
main path. That opens a race: between the rename and the creation of the new
file, a second instance can open the (now missing) main path with `O_CREAT`,
acquire its own lock on the fresh inode, and bootstrap an empty database. The
rotating instance then cannot lock the path it needs, and two divergent
databases exist. `flock` is attached to the open file description (inode), not
the path — renaming never blocks a newcomer.

Instead, rotation **never renames or unlinks the main file and never releases
its lock**:

- Unix: `ftruncate(fd, 0)` on the open, locked fd is unaffected by the flock
  and does not affect it. Subsequent writes use `lseek(fd, 0, SEEK_END)` +
  `os.write` (`filelock.py:226`), which work identically after a truncate, so
  append-mode operation continues unchanged.
- Windows: this is also the *more* portable option — the DB is opened with
  `FILE_SHARE_READ` only (`filelock.py:240`), so renaming the locked file would
  fail outright on Windows. In-place rewrite only needs `SetFilePointer(0)` +
  `SetEndOfFile` on a handle we own.
- The main path therefore exists and remains locked throughout; a second
  instance opening it at any moment gets either the old content or the new,
  never a missing or half-created file, and never its own lock.

The only new capability `LockedFile` needs is a `replace_content(data)` method
(seek 0, truncate, write, fsync) implemented per platform.

## When to rotate: at open time, not at runtime

Rotation happens **inside `Kanta.open()`, after acquiring the lock, before
replay**, gated by a retention option (see Configuration). Rationale:

- The lock is already held and no background flush loop is running yet, so the
  file is quiescent — no in-flight `pending_changes`, no concurrent snapshots.
- Runtime rotation would have to fence the background writer, drain the queue,
  and prove no record lands in the file after the cutoff was computed. That
  is a second synchronization protocol for a rare operation; not worth it.
- Open-time rotation also means rotation never races with `request_snapshot()`
  or migration snapshot writes, which all happen under the same open() sequence.

Consequence: a database that is never reopened never rotates. Document this;
for long-running services, rotation takes effect on the next restart.

## Rotation algorithm (under the exclusive lock)

Let `cutoff = now - retention`. Steps 1–3 operate on the bytes already read
into memory by `open_and_read`; no second disk read is needed.

1. **Check eligibility.** Skip rotation when there is nothing to do:
   - The file contains **no change records older than `cutoff`** — the
     retention window already covers all history.
   - The file contains **no change records at all** (snapshot-only file).
     Opening a long-untouched database may legitimately rotate it down to a
     single snapshot (that *is* the intended purge), but once a file has been
     reduced to just a snapshot, rotating it again would be a pure no-op
     rewrite. Treat "no change records" as "already fully rotated" and skip.

2. **Find the replay base.** Replay normally starts at the most recent
   snapshot, but that snapshot's `ts` is likely newer than `cutoff` — replaying
   from it would silently drop history we intend to keep. Instead, scan
   **backwards from the end of file**, collecting snapshots newest-first, and
   pick the oldest snapshot `S` whose `ts <= cutoff` (i.e. walk back past
   snapshots until one covers the required range, or until start of file). If
   no such snapshot exists, `S` is "start of file" and the retained range is
   replayed from the empty initial state.
   - For `LineFramer` this is a reverse scan for `\nSNAPSHOT ` lines.
   - For `BinFramer` frames are forward-scannable only; keep the forward scan
     but record every snapshot position, then pick from the collected list.

3. **Replay and validate.** Replay from `S` (or start of file) forward to end
   of file, keeping every record with `ts >= cutoff`. At **every** snapshot
   encountered after `S`, validate that the replayed state equals the snapshot
   state; a mismatch means the history is corrupt or the chosen base is wrong —
   abort rotation (leave the original file untouched) and surface the error.
   The last snapshot in the file must always validate; if even that fails,
   rotation must not proceed.
   - Records with `ts < cutoff` are applied to the replay (they are needed to
     reach the cutoff state) but not retained in the output.
   - Remember `cutoff_end`: the byte offset in the original content just after
     the last record with `ts < cutoff` (frame-boundary aligned). The rotated
     file will be truncated to this length in step 6.

4. **Copy the original aside.** `shutil.copy2(main_path, rotated_path)` —
   no lock needed on the copy, and no temporary name: the content is written
   directly to its final `{stem}@{ts}.kantadb` name. `copy2` preserves
   metadata and, on filesystems with copy-on-write (btrfs, XFS with reflinks,
   APFS, …), performs a cheap reflink copy instead of duplicating data; it is
   also generally faster than re-writing the same bytes from memory. The
   original bytes remain readable from the locked fd if the copy fails, so a
   failure here simply aborts rotation.

5. **Rewrite the main file in place.** On the locked fd: seek to 0, truncate
   to 0, write the new content, `fdatasync`. The new content is, in order:
   1. A **snapshot of the state at the cutoff** — the replayed state after
      applying all records with `ts < cutoff`, stamped with the **schema
      version in effect at the cutoff**. Its `ts` is the **ts of the last
      pre-cutoff record** (not the rotation time), and this is exactly the
      timestamp used in the rotated filename. This snapshot is the new replay
      base and carries the version forward so migrations are not re-run; it is
      always written.
   2. The retained change records (`ts >= cutoff`), **recreated record by
      record** — no internal snapshots are carried over, even if the original
      file had many in the retained range.
   3. A **final snapshot** of the state after the last retained record,
      stamped with the version of the last retained record — written **only
      if** there were
      retained change records (and, in line with the existing snapshot policy
      in `kanta/snapshot.py`, only when a meaningful number of changes
      accumulated; a handful of trailing changes need not force one). If no
      records survived the cutoff, the new file consists of the single leading
      snapshot and nothing else — this is the steady state for databases whose
      history has fully aged out, and the eligibility check in step 1 prevents
      re-rotating such files.

6. **Trim the rotated copy.** Truncate `{stem}@{ts}.kantadb` to `cutoff_end`
   bytes, so it contains **only the dropped history** and does not duplicate
   the records retained in the main file. The cut is at a frame boundary, so
   the rotated file remains a valid, replayable database on its own (it is a
   prefix of a valid log). This truncation happens only after step 5's fsync,
   so until then the rotated file still holds the complete original content as
   a crash-recovery anchor.

7. **Continue normal open.** Replay/migrations proceed on the same locked fd.
   Because the leading snapshot carries the current version, migrations run
   exactly as they would have against the old content.

Failure rule: any error before step 5 leaves the main file byte-identical
(only an extra copy exists). A crash during step 5 may leave the main file
torn, but the rotated copy still holds the complete original content
(truncated only after the main file is durable) — recovery is copying it back.
After step 6 the split is complete and both files are consistent.

## Verbatim copy or rewrite?

**Rewrite (re-frame), not verbatim copy**, for all records written to the main
file:

- `BinFramer` checksums include `record_offset`, so a verbatim byte copy to a
  new offset is unreadable. Binary records must be re-framed at their new
  offsets regardless.
- Rewriting also normalizes encoding drift and lets us drop the redundant
  intermediate snapshots the original file accumulated: none of them are
  carried over — the new file contains only the leading cutoff snapshot, the
  recreated change records, and (conditionally) the final snapshot.

The rotated copy is the one place where verbatim bytes are used — a raw
`copy2` plus a frame-aligned tail truncation — which is safe precisely because
it preserves original offsets (the truncated prefix keeps every frame at its
original `record_offset`, so binary checksums stay valid).

## Configuration

Add keyword options to `Kanta(...)` (`kanta/kanta.py`), surfaced through
`open()`:

- `retention: timedelta | int | None = None` — history window to keep; a plain
  `int` is interpreted as a number of days. `None` (default) disables rotation
  entirely; current behavior is unchanged.
- `rotate_keep: int = 3` (optional, later) — how many rotated backups to
  retain; older ones are pruned at rotation time.

Rotation uses `impl.now()` so the `@Kanta.clock` test clock controls it, same
as record timestamps.

## Integrity checklist

- Rotation runs under the exclusive lock, before the background writer starts.
- The main path is never renamed, unlinked, or unlocked during rotation; no
  bootstrap race with a second instance is possible.
- Replay base is chosen by walking snapshots backwards until the retained range
  is covered; replay is validated against every snapshot in range.
- A leading cutoff snapshot (ts = last pre-cutoff record, current schema
  version) is always written; a final snapshot is written only when warranted
  by retained changes.
- The full original content sits at `{stem}@{ts}.kantadb` before the main file
  is touched, and is only trimmed to the dropped-history prefix after the
  rewritten main file is `fdatasync`ed.
- Rotated files are never deleted by the rotation itself.
- Any validation failure aborts rotation with the original file intact.
- Files with no change records (already reduced to a snapshot) are never
  re-rotated.

## Testing notes

- Use the test clock (`tests/test_clock.py`) to age records past the cutoff.
- Cover both framers: JSONL rotation and BinFramer rotation (assert the
  rewritten binary file passes checksum validation and replays identically,
  and that the truncated rotated prefix still passes checksum validation).
- Assert state equality before/after rotation, version continuity (no
  re-migration), correct behavior when no snapshot precedes the cutoff, when
  the newest snapshot is already older than the cutoff, and when retention
  covers everything (no-op).
- Naming: databases named `x`, `x.kantadb`, and `x.db` all rotate to
  `x@{ts}.kantadb`; the timestamp equals the last dropped record's ts and the
  leading snapshot's ts.
- Assert the rotated file ends exactly at the last dropped record's frame
  boundary (no overlap with the retained history in the main file).
- No-change files: a snapshot-only database opened with retention set is left
  untouched (no copy, no rewrite).
- Aged-out database: all history older than the cutoff → new file contains
  exactly one snapshot; opening it again performs no rotation.
- Concurrency: while one instance rotates, a second instance opening the main
  path must fail with the normal "already locked" error at every stage.
