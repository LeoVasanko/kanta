"""Internal implementation for Kanta."""

from __future__ import annotations

import asyncio
import copy
import importlib
import logging
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from kanta.callbacks import CallbackRegistry, InjectionContext
from kanta.exceptions import DatabaseError, DataIntegrityError, ReplayError
from kanta.migrate import MigrationRegistry
from kanta.persistence import PersistenceMixin
from kanta.serialization import restore_data_in_place, struct_to_dict
from kanta.serialization.base import replay

_logger = logging.getLogger(__name__)

T = TypeVar("T")


class KantaImpl(PersistenceMixin, Generic[T]):
    """Internal state and logic for Kanta."""

    def __init__(self, **kwargs: Any):
        self.data_type = kwargs.pop("type")
        self.data: T = kwargs.pop("data")
        self.migrations = kwargs.pop("migrations", None)
        self.migration_ctx = kwargs.pop("migration_ctx", None)
        self._kanta = kwargs.pop("kanta", None)
        super().__init__(**kwargs)
        self.migration_registry: MigrationRegistry | None = None
        if self.migrations is not None:
            module = (
                importlib.import_module(self.migrations)
                if isinstance(self.migrations, str)
                else self.migrations
            )
            self.migration_registry = MigrationRegistry.from_module(module)

        self.in_transaction = False
        self.transaction_snapshot: dict[str, Any] | None = None
        self.opened = False
        self.readonly = False
        self.bootstrap_action = "bootstrap"
        self.bootstrap_user: str | None = None
        self.bootstrap_mtime: bool | datetime = True

        self.callback_registry = CallbackRegistry(
            kanta_class=type(self._kanta) if self._kanta is not None else None,
            data_type=self.data_type,
        )

        self.statedict = struct_to_dict(self.data, serializer=self.serializer)
        self.version = (
            self.migration_registry.dbver if self.migration_registry is not None else 0
        )

    def add_bootstrap(
        self,
        *,
        callback,
        action: str,
        user: str | None,
        mtime: bool | datetime,
    ) -> None:
        """Add bootstrap callback and update bootstrap metadata."""
        self.callback_registry.register("bootstrap", callback)
        self.bootstrap_action = action
        self.bootstrap_user = user
        self.bootstrap_mtime = mtime

    def add_logfmt(self, callback, *, path: str | None = None) -> None:
        """Register one transaction logfmt callback."""
        self.callback_registry.register("logfmt", callback, path=path)

    async def open(self, *, create: bool = True, readonly: bool = False) -> None:
        """Open the database: load from disk, apply migrations, start background task."""
        if self.opened:
            raise DataIntegrityError(
                "Kanta instance is already open",
                db_path=self.filename,
                action="open",
            )

        self.readonly = readonly
        existed_before_open = self.filename.exists()

        # Read-only mode never creates the file.
        open_create = create and not readonly

        content = await asyncio.to_thread(
            self.file.open_and_read,
            self.filename,
            create=open_create,
            readonly=readonly,
        )

        if not create and (not existed_before_open or not content):
            self.file.close()
            reason = (
                "database file did not exist"
                if not existed_before_open
                else "database file is empty"
            )
            raise DataIntegrityError(
                f"Cannot open database: {reason}",
                db_path=self.filename,
                action="open",
            )

        if content:
            try:
                rr = replay(
                    content,
                    framer=self.framer,
                    decode=self.serializer.decode,
                )
            except ReplayError as e:
                raise DatabaseError(
                    f"{e}",
                    db_path=self.filename,
                    line_number=e.line_number,
                    byte_pos=e.byte_pos,
                    cause_type=type(e).__name__,
                ) from e
            except (OSError, ValueError, DatabaseError) as e:
                raise DatabaseError(
                    f"{e}",
                    db_path=self.filename,
                    cause_type=type(e).__name__,
                ) from e
            except Exception as e:
                _logger.exception("Unexpected error loading database")
                raise DatabaseError(
                    f"{e}",
                    db_path=self.filename,
                    cause_type=type(e).__name__,
                ) from e

            if self.migration_registry is not None:
                rr.version = self.migration_registry.apply(
                    rr.state, rr.version, self.migration_ctx
                )

            self.statedict = copy.deepcopy(rr.state)
            self.data = restore_data_in_place(
                self.data,
                rr.state,
                self.data_type,
                serializer=self.serializer,
            )
            self.version = rr.version
            self.mtime = rr.m
            normalized = struct_to_dict(self.data, serializer=self.serializer)
            if self.readonly:
                self.statedict = copy.deepcopy(normalized)
            else:
                self.queue_change("migrate:msgspec", normalized, mtime=False)
            self.snapshot.ts = (
                datetime.fromtimestamp(rr.last_snapshot_mtime, UTC)
                if rr.last_snapshot_mtime is not None
                else None
            )
        elif self.readonly:
            self.file.close()
            raise DataIntegrityError(
                "Cannot open empty database in read-only mode",
                db_path=self.filename,
                action="open",
            )
        elif self.callback_registry.has("bootstrap"):
            try:
                await self.callback_registry.invoke(
                    "bootstrap",
                    InjectionContext(data=self.data, kanta=self._kanta),
                )

                current = struct_to_dict(self.data, serializer=self.serializer)
                self.queue_change(
                    self.bootstrap_action,
                    current,
                    user=self.bootstrap_user,
                    mtime=self.bootstrap_mtime,
                )
            except Exception:
                self.file.close()
                try:
                    await asyncio.to_thread(self.filename.unlink, missing_ok=True)
                except FileNotFoundError:
                    pass
                raise

        self.opened = True

        if not self.readonly:
            self.background_task = asyncio.create_task(self._background_loop())

    async def close(self) -> None:
        """Stop the background task, flush pending changes, and release the file lock."""
        if not self.opened:
            return

        if self.background_task is not None:
            self.background_task.cancel()
            try:
                await self.background_task
            except asyncio.CancelledError:
                pass
            self.background_task = None

        # Always run a final flush in case the background task never reached
        # its cancellation handler.
        if not self.readonly:
            await self.flush()

        self.file.close()
        self.opened = False
