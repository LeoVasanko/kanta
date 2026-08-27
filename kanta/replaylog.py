"""Line-oriented replay and range selection for kantadb files.

Support machinery for the ``python -m kanta`` CLI: decoding a file into
positioned events, resolving ``-r`` range specifications to line numbers,
replaying state over a line range, and building change log events.  Internal
for now; not part of the public API.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Union

import msgspec

from kanta.callbacks import InjectionContext
from kanta.diff import patch_state
from kanta.exceptions import ReplayError
from kanta.logging import _USER_PATH, LogEvent, transaction_logger
from kanta.structs import ChangeRecord, Snapshot

if TYPE_CHECKING:
    from kanta import Kanta


@dataclasses.dataclass
class SnapshotEvent:
    """A snapshot record positioned in the file."""

    line_number: int
    byte_pos: int
    record_index: int
    snap: Snapshot

    @property
    def version(self) -> int:
        return self.snap.v


@dataclasses.dataclass
class ChangeEvent:
    """A change record positioned in the file."""

    line_number: int
    byte_pos: int
    record_index: int
    record: ChangeRecord

    @property
    def version(self) -> int:
        return self.record.v


Event = Union[SnapshotEvent, ChangeEvent]


class RangeNotFoundError(Exception):
    """A single-item range specification that does not exist in the file.

    The message is fully formatted for display, including the offending
    input and how many items of that kind the file contains.
    """


@dataclasses.dataclass
class Selection:
    """A resolved range specification.

    Either a ``[start_line, end_line)`` line range, or a single snapshot
    (``snapshot`` set), used to show the snapshot state without replaying
    further records.
    """

    start_line: int
    end_line: int
    snapshot: SnapshotEvent | None = None


def record_label(line_number: int, record_index: int) -> str:
    """Return a padded record label based on line number, falling back to record index."""
    number = line_number if line_number else record_index
    return f"l{number:<3}"


def scan_events(content: bytes, kanta: Kanta[Any]) -> tuple[list[Event], int]:
    """Decode all records, validating snapshot consistency.

    Uses the Kanta instance's serializer and framer.  Returns the events in
    file order and the number of change records.  Raises :class:`ReplayError`
    with a located, display-ready message on decode failures or when a
    snapshot does not match the replayed state.
    """
    impl = kanta._impl
    state: dict[str, Any] = {}
    events: list[Event] = []
    change_count = 0
    for is_snapshot, payload, line_number, byte_pos in impl.framer.iter_records(
        content, 0
    ):
        record_index = len(events) + 1
        label = record_label(line_number, record_index)
        try:
            if is_snapshot:
                snap = impl.serializer.decode(payload, type=Snapshot)
                if record_index > 1 and state != snap.state:
                    raise ReplayError(
                        f"Snapshot mismatch at {label}: replayed state"
                        " does not equal the snapshot state.",
                        line_number=line_number,
                        byte_pos=byte_pos,
                        record_type="snapshot",
                    )
                state = snap.state
                events.append(SnapshotEvent(line_number, byte_pos, record_index, snap))
            else:
                record = impl.serializer.decode(payload, type=ChangeRecord)
                state = patch_state(state, record.diff)
                events.append(ChangeEvent(line_number, byte_pos, record_index, record))
                change_count += 1
        except msgspec.DecodeError as exc:
            raise ReplayError(
                f"Parse error at {label}: {exc}",
                line_number=line_number,
                byte_pos=byte_pos,
            ) from exc
    return events, change_count


def replay_events(
    events: list[Event], end_line: int
) -> Iterator[tuple[Event, dict[str, Any] | None, dict[str, Any]]]:
    """Replay events with line numbers below ``end_line``.

    Yields ``(event, previous, state)`` per event: ``previous`` is the state
    before a change (``None`` for snapshots) and ``state`` the state after
    the event.
    """
    state: dict[str, Any] = {}
    for event in events:
        if event.line_number >= end_line:
            break
        if isinstance(event, SnapshotEvent):
            state = event.snap.state
            yield event, None, state
        else:
            previous = copy.deepcopy(state)
            state = patch_state(state, event.record.diff)
            yield event, previous, state


def record_change_event(
    record: ChangeRecord,
    previous: dict[str, Any],
    current: dict[str, Any],
    kanta: Kanta[Any],
) -> LogEvent:
    """Build a change :class:`LogEvent` for a replayed record.

    The Kanta instance's logfmt callbacks are used for value formatting and
    for resolving the user/actor name; the event can then be dispatched with
    :func:`kanta.logging.emit_event` and the instance's logemit handlers.
    """
    registry = kanta._impl.callback_registry
    logfmt = registry.build_logfmt(
        InjectionContext(
            kanta=kanta,
            previous_state=previous,
            current_state=current,
        )
    )
    user = record.u
    if user is not None:
        resolved = logfmt(user, _USER_PATH)
        if resolved is not None:
            user = resolved
    return LogEvent(
        kind="change",
        logger=transaction_logger,
        kanta=kanta,
        action=record.a,
        user=user,
        diff=record.diff,
        previous=previous,
        logfmt=logfmt,
    )


def _plural(count: int, word: str) -> str:
    """Return e.g. ``1 snapshot`` or ``2 snapshots``."""
    return f"{count} {word}{'' if count == 1 else 's'}"


def end_of_file(events: list[Event]) -> int:
    """Return the sentinel line number just past the last line of the file."""
    return events[-1].line_number + 1 if events else 0


def _change_lines(events: list[Event]) -> list[int]:
    """Return the line numbers of all change records, in file order."""
    return [e.line_number for e in events if isinstance(e, ChangeEvent)]


def _snapshot_lines(events: list[Event]) -> list[int]:
    """Return the line numbers addressed by s0, s1, ...

    If the file begins with a snapshot, s0 is that snapshot (l1) and s1 is
    the next snapshot.  Otherwise the file begins with change records (empty
    initial state): s0 is l0, the position before the start of the file, and
    s1 is the first snapshot.
    """
    lines = [e.line_number for e in events if isinstance(e, SnapshotEvent)]
    if events and isinstance(events[0], SnapshotEvent):
        return lines
    return [0, *lines]


def _version_lines(events: list[Event]) -> dict[int, int]:
    """Map each version to the line where it first appears; v0 is l0."""
    lines: dict[int, int] = {0: 0}
    for event in events:
        lines.setdefault(event.version, event.line_number)
    return lines


def _event_at_line(events: list[Event], line: int) -> Event | None:
    """Return the event whose file line number exactly matches ``line``."""
    for event in events:
        if event.line_number == line:
            return event
    return None


def _parse_bound(bound_str: str) -> tuple[str, int | None]:
    """Parse a range bound with optional unit prefix (l, s, v) or change index."""
    if not bound_str:
        return "change", None
    unit_map = {"l": "line", "s": "snapshot", "v": "version"}
    if bound_str[0] in unit_map:
        unit = unit_map[bound_str[0]]
        rest = bound_str[1:]
        if not rest:
            raise ValueError(f"empty value in {bound_str!r}")
        return unit, int(rest)
    return "change", int(bound_str)


def _bound_to_line(
    unit: str,
    value: int | None,
    events: list[Event],
    total: int,
    is_start: bool,
) -> int:
    """Convert a range bound to a line number.

    Out-of-range values are truncated to l0 (before the first line) or to
    the line just past the end of the file rather than erroring; a missing
    bound means the corresponding file end.
    """
    eof = end_of_file(events)
    if value is None:
        return 0 if is_start else eof

    if unit == "change":
        lines = _change_lines(events)
        if value < 0:
            value = total + value
        value = max(0, min(value, total))
        return lines[value] if value < total else eof

    if unit == "line":
        if value < 0:
            raise ValueError("line numbers do not support negative indexing")
        return value

    if unit == "snapshot":
        lines = _snapshot_lines(events)
        idx = len(lines) + value if value < 0 else value
        if idx < 0:
            return 0
        return lines[idx] if idx < len(lines) else eof

    if unit == "version":
        if value < 0:
            raise ValueError("version numbers do not support negative indexing")
        return _version_lines(events).get(value, eof)

    raise ValueError(f"unknown range unit: {unit}")


def _resolve_range(range_str: str, events: list[Event], total: int) -> tuple[int, int]:
    """Parse a range string into a [start_line, end_line) line range."""
    sep = ".." if ".." in range_str else ":"
    start_str, end_str = range_str.split(sep, 1)
    start_unit, start_val = _parse_bound(start_str)
    end_unit, end_val = _parse_bound(end_str)
    start_line = _bound_to_line(start_unit, start_val, events, total, is_start=True)
    end_line = _bound_to_line(end_unit, end_val, events, total, is_start=False)
    # ``..`` makes the end bound inclusive.
    if sep == ".." and end_val is not None:
        end_line += 1
    return min(start_line, end_line), end_line


def _negative_check(unit: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{unit} numbers do not support negative indexing")


def select(spec: str, events: list[Event], total: int) -> Selection:
    """Resolve a range specification against the scanned events.

    ``total`` is the number of change records.  Returns a :class:`Selection`:
    a line range, or a single snapshot for snapshot selections (``sN``, or
    ``lN`` pointing at a snapshot).  Ranges truncate out-of-bounds values;
    a single index must exist and raises :class:`RangeNotFoundError`
    otherwise.  Syntax errors raise :class:`ValueError`.
    """
    if ":" in spec or ".." in spec:
        start_line, end_line = _resolve_range(spec, events, total)
        return Selection(start_line, end_line)

    # A single index must exist; out-of-bounds is an error.
    unit, value = _parse_bound(spec)
    if value is None:
        raise ValueError("single bound must not be empty")

    if unit == "snapshot":
        lines = _snapshot_lines(events)
        idx = len(lines) + value if value < 0 else value
        n_snapshots = sum(isinstance(e, SnapshotEvent) for e in events)
        count = _plural(n_snapshots, "snapshot")
        if not 0 <= idx < len(lines):
            raise RangeNotFoundError(f"Snapshot {spec!r} not found in file ({count})")
        event = _event_at_line(events, lines[idx])
        if event is None:
            # s0 with an empty initial state (l0): not a real record, so it
            # cannot be selected as a single item.
            raise RangeNotFoundError(
                f"Snapshot {spec!r} not found in file: the file starts"
                f" with an empty initial state ({count})"
            )
        assert isinstance(event, SnapshotEvent)
        return Selection(event.line_number, event.line_number + 1, event)

    if unit == "line":
        _negative_check(unit, value)
        event = _event_at_line(events, value)
        if event is None:
            n_lines = events[-1].line_number if events else 0
            raise RangeNotFoundError(
                f"Line {spec!r} not found in file ({_plural(n_lines, 'line')})"
            )
        if isinstance(event, SnapshotEvent):
            return Selection(value, value + 1, event)
        return Selection(value, value + 1)

    if unit == "version":
        _negative_check(unit, value)
        lines = _version_lines(events)
        if value not in lines:
            raise RangeNotFoundError(
                f"Version {spec!r} not found in file ({_plural(len(lines), 'version')})"
            )
        start_line = lines[value]
        later = [line for line in lines.values() if line > start_line]
        return Selection(start_line, min(later) if later else end_of_file(events))

    lines = _change_lines(events)
    idx = total + value if value < 0 else value
    if not 0 <= idx < total:
        raise RangeNotFoundError(
            f"Change index {spec!r} not found in file ({_plural(total, 'change')})"
        )
    end_line = lines[idx + 1] if idx + 1 < total else end_of_file(events)
    return Selection(lines[idx], end_line)
