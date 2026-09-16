"""Database rotation: bound on-disk history to a retention window.

See docs/rotation.md for the design. All planning happens on the in-memory
bytes of the database file; the caller (KantaImpl.open) performs the actual
copy-aside, in-place rewrite and rotated-file trimming under the file lock.
"""

from __future__ import annotations

import copy
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kanta.exceptions import DatabaseError
from kanta.structs import ChangeRecord, Snapshot
from kanta.serialization.base import Serializer, apply_diff
from kanta.serialization.framing import Framer

_logger = logging.getLogger("kanta")


@dataclass
class RotationPlan:
    """Everything needed to execute a rotation on disk."""

    new_content: bytes
    cutoff_end: int  # byte length of the dropped-history prefix
    rotated_ts: datetime  # ts of the last dropped change record
    retained_changes: int


def rotated_path_for(path: Path, ts: datetime) -> Path:
    """Sibling path for the rotated history: ``{stem}@{ISO-basic-ts}.kantadb``.

    The timestamp uses ISO 8601 basic format at second precision (e.g.
    ``20260902T143000Z``); the exact microsecond timestamp remains available
    inside the file if ever needed. On collision an incrementing suffix is
    inserted before the extension.
    """
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.stem}@{stamp}.kantadb")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}@{stamp}.{n}.kantadb")
        n += 1
    return candidate


class _Entry:
    """One parsed record frame with its byte range."""

    __slots__ = ("is_snapshot", "record", "byte_pos", "end_pos")

    def __init__(
        self,
        is_snapshot: bool,
        record: ChangeRecord | Snapshot,
        byte_pos: int,
        end_pos: int,
    ) -> None:
        self.is_snapshot = is_snapshot
        self.record = record
        self.byte_pos = byte_pos
        self.end_pos = end_pos


def _scan(content: bytes, *, framer: Framer, serializer: Serializer) -> list[_Entry]:
    """Decode every record in *content* with byte ranges."""
    raw = list(framer.iter_records(content, 0))
    entries: list[_Entry] = []
    for i, (is_snapshot, payload, _line, byte_pos) in enumerate(raw):
        end_pos = raw[i + 1][3] if i + 1 < len(raw) else len(content)
        record = serializer.decode(
            payload, type=Snapshot if is_snapshot else ChangeRecord
        )
        entries.append(_Entry(is_snapshot, record, byte_pos, end_pos))
    return entries


def plan_rotation(
    content: bytes,
    *,
    framer: Framer,
    serializer: Serializer,
    cutoff: datetime,
    now: datetime,
    min_diffs: int,
) -> RotationPlan | None:
    """Plan a rotation of *content*, or return None when there is nothing to do.

    Raises:
        DatabaseError: If replay from the chosen base snapshot does not match
            a snapshot found inside the file (corrupt history). Rotation must
            be aborted and the original file left untouched.
    """
    if not content:
        return None
    entries = _scan(content, framer=framer, serializer=serializer)
    changes = [e for e in entries if not e.is_snapshot]
    if not changes:
        return None  # snapshot-only file: already fully rotated

    dropped = [e for e in changes if e.record.ts < cutoff]
    if not dropped:
        return None  # retention window covers all history

    retained = [e for e in changes if e.record.ts >= cutoff]
    rotated_ts = dropped[-1].record.ts
    cutoff_end = retained[0].byte_pos if retained else len(content)

    # Replay base: walk snapshots newest-first and take the first (newest)
    # one predating the cutoff; fall back to start of file.
    base: _Entry | None = None
    for e in reversed([e for e in entries if e.is_snapshot]):
        if e.record.ts <= cutoff:
            base = e
            break

    state: dict[str, Any] = {}
    version = 0
    m: datetime | None = None
    if base is not None:
        snap = base.record
        assert isinstance(snap, Snapshot)
        state = dict(snap.state)
        version = snap.v
        m = snap.m

    # The cutoff state starts from the replay base: when the base snapshot
    # already predates the cutoff, it may itself be the cutoff state.
    state_at_cutoff: dict[str, Any] | None = (
        copy.deepcopy(state) if base is not None else None
    )
    version_at_cutoff = version
    m_at_cutoff = m
    final_version = version
    final_m = m

    for e in entries:
        if base is not None and e.byte_pos <= base.byte_pos:
            continue
        if e.is_snapshot:
            snap = e.record
            assert isinstance(snap, Snapshot)
            if snap.state != state:
                raise DatabaseError(
                    "rotation aborted: replayed state does not match snapshot "
                    f"at byte {e.byte_pos}",
                    action="rotate",
                )
            if snap.m is not None:
                m = snap.m
            continue
        change = e.record
        assert isinstance(change, ChangeRecord)
        state = apply_diff(state, change.diff)
        version = change.v
        if change.m is not None:
            m = change.m
        if change.ts < cutoff:
            state_at_cutoff = copy.deepcopy(state)
            version_at_cutoff = version
            m_at_cutoff = m
        final_version = version
        final_m = m

    # There is at least one dropped change, so the cutoff state is known.
    assert state_at_cutoff is not None

    # Build the new content: leading cutoff snapshot, retained changes
    # re-framed at fresh offsets, and a final snapshot when enough changes
    # survived to warrant one (mirrors the regular snapshot policy).
    out = bytearray()
    leading = serializer.encode(
        Snapshot(
            ts=rotated_ts, v=version_at_cutoff, state=state_at_cutoff, m=m_at_cutoff
        )
    )
    out += framer.frame_snapshot(leading, record_offset=0)
    for e in retained:
        payload = serializer.encode(e.record)
        out += framer.frame_change(payload, record_offset=len(out))
    if len(retained) >= min_diffs:
        closing = serializer.encode(
            Snapshot(ts=now, v=final_version, state=state, m=final_m)
        )
        out += framer.frame_snapshot(closing, record_offset=len(out))

    return RotationPlan(
        new_content=bytes(out),
        cutoff_end=cutoff_end,
        rotated_ts=rotated_ts,
        retained_changes=len(retained),
    )


def execute_rotation(
    path: Path, file, plan: RotationPlan, *, log: bool | logging.Logger = True
) -> Path:
    """Execute a planned rotation on disk. Caller must hold the lock on *file*.

    1. Copy the original content aside to ``{stem}@{ts}.kantadb``.
    2. Rewrite the locked file in place with the new content and fsync.
    3. Trim the rotated copy to the dropped-history prefix.

    Returns the rotated file path.
    """
    rotated = rotated_path_for(path, plan.rotated_ts)
    shutil.copy2(path, rotated)
    file.replace_content(plan.new_content)
    with open(rotated, "r+b") as f:
        f.truncate(plan.cutoff_end)
    if log:
        _logger.info(
            "Rotated database %s: kept %d change record(s), "
            "moved history before %s to %s",
            path,
            plan.retained_changes,
            plan.rotated_ts.isoformat(),
            rotated,
        )
    return rotated
