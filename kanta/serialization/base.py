"""Serializer interfaces for Kanta journal formats."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, TypeVar

import msgspec

from kanta.exceptions import ReplayError
from kanta.structs import ChangeRecord, Snapshot
from kanta.serialization.framing import Framer

T = TypeVar("T")


class ReplayResult:
    """Result of replaying serialized database bytes."""

    def __init__(
        self,
        state: dict[str, Any],
        version: int = 0,
        has_migration: bool = False,
        last_snapshot_mtime: float | None = None,
        m: datetime | None = None,
    ):
        self.state = state
        self.version = version
        self.has_migration = has_migration
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
        version = change.v
        state = _patch_state(state, change.diff)
    return ReplayResult(
        state=state,
        version=version,
        has_migration=has_migration,
        last_snapshot_mtime=last_snapshot_mtime,
        m=m,
    )


def _patch_state(state: dict, diff: dict) -> dict:
    return apply_diff(state, diff)


def _unescape(value: str) -> str:
    """Reverse jsondiff's ``$$`` escaping; command strings pass through.

    Only a ``$$`` prefix is stripped: jsondiff escapes ``$x`` to ``$$x``,
    while single ``$`` strings occur verbatim in our own diffs (we do not
    escape values) and must be left alone.
    """
    if value.startswith("$$"):
        return value[1:]
    return value


def unmarshal(diff: Any) -> Any:
    """Unescape a marshaled diff (keys, values and ``$delete`` entries).

    Needed for jsondiff-produced diffs, which escape ``$``-prefixed values
    as well as keys; our own producer escapes keys only, so unescaping
    values is a no-op for them.
    """
    if isinstance(diff, dict):
        return {
            _unescape(k) if isinstance(k, str) else k: unmarshal(v)
            for k, v in diff.items()
        }
    if isinstance(diff, list):
        return [unmarshal(v) for v in diff]
    if isinstance(diff, str):
        return _unescape(diff)
    return diff


def apply_diff(state: Any, diff: Any) -> Any:
    """Apply a diff.

    Understands our own format (plain assignment + ``$delete``) and
    jsondiff's marshaled syntax: ``$replace``, positional
    ``$delete``/``$insert`` and per-index nested diffs on lists. A bare
    dict over a non-dict old value is a wholesale replacement.
    """
    return _apply(state, unmarshal(diff))


def _is_list_patch(diff: dict) -> bool:
    """Whether a dict diff against a list state is a jsondiff list edit."""
    for key in diff:
        if key in ("$delete", "$insert"):
            continue
        try:
            int(key)
        except (ValueError, TypeError):
            return False
    return True


def _apply(state: Any, diff: Any) -> Any:
    if not isinstance(diff, dict):
        return diff
    if not diff:
        return state
    if "$replace" in diff:
        return diff["$replace"]

    if isinstance(state, list):
        if not _is_list_patch(diff):
            # Our own producer replaces a list with a dict (or any other
            # type) by plain assignment — no $replace wrapper.
            return diff
        result = list(state)
        deletes = diff.get("$delete")
        if deletes:
            for pos in deletes:
                result.pop(pos)
        for pos, value in diff.get("$insert", []):
            result.insert(pos, value)
        for key, value in diff.items():
            if key in ("$delete", "$insert"):
                continue
            pos = int(key)
            result[pos] = _apply(result[pos], value)
        return result

    result = dict(state) if isinstance(state, dict) else {}
    for key, value in diff.items():
        if key == "$delete":
            keys = value if isinstance(value, list) else [value]
            for k in keys:
                result.pop(k, None)
        elif key == "$insert":
            continue
        elif key in result:
            result[key] = _apply(result[key], value)
        else:
            result[key] = value
    return result
