"""Persistence mixin for KantaImpl."""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from kanta.diff import compute_diff
from kanta.exceptions import DatabaseError, DataIntegrityError
from kanta.filelock import LockedFile
from kanta.kanta.structs import ChangeRecord
from kanta.serialization import JsonSerializer, Serializer
from kanta.serialization.framing import Framer
from kanta.snapshot import SnapshotState

_logger = logging.getLogger(__name__)


class PersistenceMixin:
    """Persistence-related behavior for Kanta implementations."""

    filename: Path
    file: LockedFile
    flush_failed: bool
    statedict: dict[str, Any]
    pending_changes: deque[ChangeRecord]
    pending_lock: threading.Lock
    snapshot: SnapshotState
    serializer: Serializer
    framer: Framer
    background_task: asyncio.Task | None
    fatal_error: Callable[[DatabaseError], None] | None
    background_error: DatabaseError | None
    flush_interval: float
    version: int
    opened: bool

    def __init__(self, **kwargs: Any) -> None:
        """Initialize persistence-owned state used by mixin methods."""
        filename = kwargs.pop("filename")
        flush_interval = kwargs.pop("flush_interval", 0.1)
        serializer = kwargs.pop("serializer", None)
        fatal_error = kwargs.pop("fatal_error", None)
        super().__init__(**kwargs)
        self.filename = Path(filename)
        self.file = LockedFile()
        self.flush_failed = False
        self.statedict = {}
        self.pending_changes = deque()
        self.pending_lock = threading.Lock()
        self.serializer = serializer or JsonSerializer()
        self.framer = self.serializer.framer_cls()
        self.snapshot = SnapshotState(serializer=self.serializer, framer=self.framer)
        self.background_task = None
        self.fatal_error = fatal_error
        self.background_error = None
        self.flush_interval = flush_interval
        self.version = 0

    async def _background_loop(self) -> None:
        """Background task that periodically flushes changes to disk."""
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                await self.flush()
                self.maybe_snapshot()
            except asyncio.CancelledError:
                await self.flush()
                self.maybe_snapshot()
                break
            except DatabaseError as e:
                self.background_error = e
                if self.fatal_error is not None:
                    try:
                        self.fatal_error(e)
                    except Exception as callback_error:
                        _logger.exception(
                            "Background error callback failed: %s", callback_error
                        )
                _logger.error("Background flush loop stopped: %s", e)
                break

    def maybe_snapshot(self) -> None:
        """Evaluate and possibly write a snapshot from current state."""
        self.snapshot.maybe_write(self.file, self.version, self.statedict)

    def queue_change(
        self,
        action: str,
        current: dict,
        user: str | None = None,
        m: datetime | None = None,
    ) -> None:
        """Queue a change record internally (thread-safe)."""
        diff = compute_diff(self.statedict, current)
        if not diff:
            return
        with self.pending_lock:
            self.pending_changes.append(
                ChangeRecord(
                    a=action,
                    v=self.version,
                    u=user,
                    m=m,
                    diff=diff,
                )
            )
        self.statedict = copy.deepcopy(current)

    def flush_sync(self) -> None:
        """Synchronously flush all pending changes to disk."""
        if not self.opened:
            raise DataIntegrityError(
                "Kanta instance must be opened before flush_sync",
                db_path=self.filename,
                action="flush_sync",
            )

        if self.flush_failed:
            return

        with self.pending_lock:
            if not self.pending_changes:
                return
            changes_to_write = list(self.pending_changes)

        if not self.file.is_open:
            self.file.open(self.filename, create=True)

        try:
            base_offset = self.file.size()
            records = []
            running_size = 0
            for change in changes_to_write:
                framed = self.framer.frame_change(
                    self.serializer.encode(change),
                    record_offset=base_offset + running_size,
                )
                records.append(framed)
                running_size += len(framed)
            if not records:
                with self.pending_lock:
                    self.pending_changes.clear()
                return

            self.file.write(b"".join(records))
            self.snapshot.record_changes(len(records))
            with self.pending_lock:
                for _ in changes_to_write:
                    self.pending_changes.popleft()
        except OSError as e:
            _logger.error("Failed to flush database: %s", e)
            self.flush_failed = True
            raise DatabaseError(
                f"Failed to flush database: {e}",
                db_path=self.filename,
                cause_type=type(e).__name__,
            ) from e

    async def flush(self) -> None:
        """Write all pending changes to disk via threadpool-backed file I/O."""
        if not self.opened:
            raise DataIntegrityError(
                "Kanta instance must be opened before flush",
                db_path=self.filename,
                action="flush",
            )

        if self.flush_failed:
            return

        with self.pending_lock:
            if not self.pending_changes:
                return
            changes_to_write = list(self.pending_changes)

        if not self.file.is_open:
            await asyncio.to_thread(self.file.open, self.filename, create=True)

        try:
            base_offset = await asyncio.to_thread(self.file.size)
            records = []
            running_size = 0
            for change in changes_to_write:
                framed = self.framer.frame_change(
                    self.serializer.encode(change),
                    record_offset=base_offset + running_size,
                )
                records.append(framed)
                running_size += len(framed)
            if not records:
                with self.pending_lock:
                    self.pending_changes.clear()
                return

            await asyncio.to_thread(self.file.write, b"".join(records))
            self.snapshot.record_changes(len(records))
            with self.pending_lock:
                for _ in changes_to_write:
                    self.pending_changes.popleft()
        except OSError as e:
            _logger.error("Failed to flush database: %s", e)
            self.flush_failed = True
            raise DatabaseError(
                f"Failed to flush database: {e}",
                db_path=self.filename,
                cause_type=type(e).__name__,
            ) from e
