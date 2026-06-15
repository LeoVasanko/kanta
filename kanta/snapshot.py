"""Internal snapshot behavior and state."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from kanta.structs import Snapshot
from kanta.serialization import JsonSerializer, Serializer
from kanta.serialization.framing import Framer, LineFramer

_logger = logging.getLogger(__name__)

MINDIFFS = 100


class SnapshotState:
    """Internal snapshot counters and write policy."""

    def __init__(
        self,
        min_diffs: int = MINDIFFS,
        serializer: Serializer | None = None,
        framer: Framer | None = None,
    ) -> None:
        self.ts: datetime | None = None
        self.changes: int = 0
        self._force_pending: bool = False
        self._min_diffs = min_diffs
        self._serializer = serializer if serializer is not None else JsonSerializer()
        self._framer = framer if framer is not None else LineFramer()

    def request_force(self) -> None:
        """Force snapshot write on next check."""
        self._force_pending = True

    def record_changes(self, count: int) -> None:
        self.changes += count

    def maybe_write(
        self, file, version: int, state: dict, m: datetime | None = None
    ) -> None:
        """Write snapshot when thresholds/time policy allows it."""
        force = self._force_pending
        now = datetime.now(UTC)
        if not force:
            if self.changes < self._min_diffs:
                return
            if now.weekday() != 6:  # 6 = Sunday
                return
            sunday_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if self.ts is not None and self.ts >= sunday_midnight:
                return
        if not file.is_open:
            return
        try:
            self._write(file, version, state, now, m=m)
            self._force_pending = False
        except Exception as exc:
            _logger.error("snapshot: failed to write snapshot: %r", exc)

    def _write(
        self, file, version: int, state: dict, now: datetime, m: datetime | None = None
    ) -> None:
        """Write a snapshot and update internal state."""
        payload = self._serializer.encode(Snapshot(ts=now, v=version, state=state, m=m))
        record_offset = file.size() if hasattr(file, "size") else 0
        file.write(self._framer.frame_snapshot(payload, record_offset=record_offset))
        self.changes = 0
        self.ts = now
