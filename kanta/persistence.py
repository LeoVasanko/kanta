"""Persistence mixin for KantaImpl."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kanta.callbacks import CallbackRegistry, InjectionContext
from kanta.diff import compute_diff
from kanta.exceptions import DatabaseError, DataIntegrityError
from kanta.filelock import LockedFile
from kanta.structs import ChangeRecord
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
    snapshot: SnapshotState
    serializer: Serializer
    framer: Framer
    background_task: asyncio.Task | None
    callback_registry: CallbackRegistry
    background_error: DatabaseError | None
    flush_interval: float
    version: int
    opened: bool
    mtime: datetime | None

    def __init__(self, **kwargs: Any) -> None:
        """Initialize persistence-owned state used by mixin methods."""
        filename = kwargs.pop("filename")
        flush_interval = kwargs.pop("flush_interval", 0.1)
        serializer = kwargs.pop("serializer", None)
        super().__init__(**kwargs)
        self.filename = Path(filename)
        self.file = LockedFile()
        self.flush_failed = False
        self.statedict = {}
        self.pending_changes = deque()
        self.serializer = serializer or JsonSerializer()
        self.framer = self.serializer.framer_cls()
        self.snapshot = SnapshotState(serializer=self.serializer, framer=self.framer)
        self.background_task = None
        self.callback_registry = CallbackRegistry()
        self.background_error = None
        self.flush_interval = flush_interval
        self.version = 0
        self.mtime: datetime | None = None

    def add_fatal_error(self, callback) -> None:
        """Register one fatal error callback in call order."""
        self.callback_registry.register("fatal_error", callback)

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

                def _log_callback_error(callback_error, callback):
                    _logger.exception(
                        "Background error callback %r failed: %s",
                        callback,
                        callback_error,
                    )

                await self.callback_registry.invoke(
                    "fatal_error",
                    InjectionContext(error=e, kanta=self._kanta),
                    on_error=_log_callback_error,
                )
                _logger.error("Background flush loop stopped: %s", e)
                break

    def maybe_snapshot(self) -> None:
        """Evaluate and possibly write a snapshot from current state."""
        self.snapshot.maybe_write(self.file, self.version, self.statedict, m=self.mtime)

    def queue_change(
        self,
        action: str,
        current: dict,
        *,
        user: str | None = None,
        mtime: bool | datetime = True,
    ) -> ChangeRecord | None:
        """Queue a change record internally (thread-safe).

        Args:
            action: Action label stored in the change record.
            current: New serialized state after the change.
            user: Optional actor identifier.
            mtime: Controls the modification timestamp. ``True`` (default)
                sets ``m`` to the current UTC time. ``False`` omits ``m`` so the
                previous modification time remains in effect; this is used for
                system operations that are not considered modifications. A
                :class:`~datetime.datetime` value sets ``m`` to that explicit time.

        Returns:
            The queued :class:`ChangeRecord`, or ``None`` if the diff was empty.
        """
        now = datetime.now(UTC)

        if mtime is True:
            m = now
        elif mtime is False:
            m = None
        elif isinstance(mtime, datetime):
            m = mtime
        else:
            raise TypeError("mtime must be True, False, or a datetime")

        diff = compute_diff(self.statedict, current)
        if not diff:
            return None

        record = ChangeRecord(
            ts=now,
            a=action,
            v=self.version,
            u=user,
            m=m,
            diff=diff,
        )
        self.pending_changes.append(record)
        self.statedict = copy.deepcopy(current)
        if m is not None:
            self.mtime = m
        return record

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
                self.pending_changes.clear()
                return

            self.file.write(b"".join(records))
            self.snapshot.record_changes(len(records))
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
                self.pending_changes.clear()
                return

            await asyncio.to_thread(self.file.write, b"".join(records))
            self.snapshot.record_changes(len(records))
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
