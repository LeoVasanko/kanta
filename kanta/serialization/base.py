"""Serializer interfaces for Kanta journal formats."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, TypeVar

import msgspec

from kanta.exceptions import ReplayError
from kanta.kanta.structs import ChangeRecord, Snapshot
from kanta.serialization.framing import Framer

T = TypeVar("T")


class ReplayResult:
    """Result of replaying serialized database bytes."""

    def __init__(
        self,
        state: dict[str, Any],
        version: int = 0,
        has_migration: bool = False,
        last_patch_mtime: float | None = None,
        last_snapshot_mtime: float | None = None,
        m: datetime | None = None,
    ):
        self.state = state
        self.version = version
        self.has_migration = has_migration
        self.last_patch_mtime = last_patch_mtime
        self.last_snapshot_mtime = last_snapshot_mtime
        self.m = m


class Serializer(Protocol):
    """Format-specific serializer contract used by persistence and loading."""

    framer_cls: type[Framer]

    def encode(self, obj: Any) -> bytes:
        """Encode one record payload."""
        raise NotImplementedError

    def decode(self, payload: bytes, *, type: type[T]) -> T:
        """Decode one record payload to the provided type."""
        raise NotImplementedError


def replay(
    data: bytes,
    *,
    framer: Framer,
    decode: Callable[..., Any],
) -> ReplayResult:
    """Rebuild database state from raw file bytes using the supplied framer."""
    snap_payload, offset, snap_byte_pos = framer.scan_last_snapshot(data)

    state: dict[str, Any] = {}
    version = 0
    last_snapshot_mtime: float | None = None
    m: datetime | None = None
    has_migration = False
    last_patch_mtime: float | None = None

    if snap_payload is not None:
        try:
            snap = decode(snap_payload, type=Snapshot)
        except msgspec.DecodeError as e:
            raise ReplayError(
                "invalid snapshot record",
                line_number=0,
                byte_pos=snap_byte_pos,
                record_type="snapshot",
            ) from e
        state = snap.state
        version = snap.v
        last_snapshot_mtime = snap.ts.timestamp()
        m = snap.m

    for is_snapshot, payload, line_number, byte_pos in framer.iter_records(
        data, offset
    ):
        if is_snapshot:
            try:
                snap = decode(payload, type=Snapshot)
            except Exception as e:
                raise ReplayError(
                    "invalid snapshot record",
                    line_number=line_number,
                    byte_pos=byte_pos,
                    record_type="snapshot",
                ) from e
            last_snapshot_mtime = snap.ts.timestamp()
            if snap.m is not None:
                m = snap.m
            continue

        try:
            change = decode(payload, type=ChangeRecord)
        except msgspec.DecodeError:
            raise ReplayError(
                "invalid record",
                line_number=line_number,
                byte_pos=byte_pos,
                record_type="change",
            ) from None

        if change.a.startswith("migrate"):
            has_migration = True
        if change.m is not None:
            m = change.m
        last_patch_mtime = change.ts.timestamp()
        version = change.v
        state = _patch_state(state, change.diff)
    return ReplayResult(
        state=state,
        version=version,
        has_migration=has_migration,
        last_patch_mtime=last_patch_mtime,
        last_snapshot_mtime=last_snapshot_mtime,
        m=m,
    )


def _patch_state(state: dict, diff: dict) -> dict:
    return _apply_diff(state, diff)


def _apply_diff(state: dict, diff: dict) -> dict:
    if not isinstance(diff, dict):
        return diff

    result = dict(state) if isinstance(state, dict) else state
    if not isinstance(result, dict):
        result = {}

    for key, value in diff.items():
        if key == "$replace":
            return value
        if key == "$delete":
            if isinstance(value, list):
                for k in value:
                    result.pop(k, None)
            else:
                result.pop(value, None)
            continue
        if isinstance(value, dict):
            old = result.get(key, {})
            if not isinstance(old, dict):
                old = {}
            result[key] = _apply_diff(old, value)
            continue
        result[key] = value

    return result
