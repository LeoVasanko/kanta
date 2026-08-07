"""Internal implementation for Kanta."""

from __future__ import annotations

import asyncio
import copy
import importlib
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Generic, TypeVar

from kanta.callbacks import CallbackRegistry, InjectionContext
from kanta.exceptions import DatabaseError, DataIntegrityError, ReplayError
from kanta.logging import (
    _USER_PATH,
    LogEvent,
    bootstrap_logger,
    emit_event,
    migration_logger,
)
from kanta.migrations import MigrationResult, Migrations
from kanta.persistence import PersistenceMixin
from kanta.serialization import restore_data_in_place, struct_to_dict
from kanta.serialization.base import replay

_logger = logging.getLogger(__name__)

T = TypeVar("T")


def _log_callback_error(callback_error, callback):
    """Report a failing logging callback and continue with the next one."""
    _logger.exception("Log callback %r failed: %s", callback, callback_error)


class KantaImpl(PersistenceMixin, Generic[T]):
    """Internal state and logic for Kanta."""

    def __init__(self, **kwargs: Any):
        self.data_type = kwargs.pop("type")
        self.data: T = kwargs.pop("data")
        self._kanta = kwargs.pop("kanta", None)
        migrations = kwargs.pop("migrations", None)
        self.ctx = SimpleNamespace()
        super().__init__(**kwargs)
        self.migrations: Migrations | None = None
        if migrations is not None:
            module = (
                importlib.import_module(migrations)
                if isinstance(migrations, str)
                else migrations
            )
            self.migrations = Migrations.from_module(module)

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
        self.version = self.migrations.dbver if self.migrations is not None else 0

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

    def add_logmigr(self, callback) -> None:
        """Register one migration logging callback."""
        self.callback_registry.register("logmigr", callback)

    def add_logemit(self, callback) -> None:
        """Register one log emitter callback."""
        self.callback_registry.register("logemit", callback)

    async def _handle_migration_log(
        self,
        migration_result: MigrationResult,
        previous_version: int,
        log: bool | logging.Logger,
    ) -> None:
        """Route migration logging to callback or default logger."""
        assert isinstance(migration_result, MigrationResult)

        if self.callback_registry.has("logmigr"):
            await self.callback_registry.invoke(
                "logmigr",
                InjectionContext(
                    kanta=self._kanta,
                    migration_result=migration_result,
                ),
                on_error=_log_callback_error,
            )
            return

        if log is False:
            return

        migration_log = log if isinstance(log, logging.Logger) else migration_logger

        changed = [m for m in migration_result.migrations if m.changed]
        if not changed:
            return

        descriptions = [f"{m.name} ({m.description})" for m in changed]
        emit_event(
            LogEvent(
                kind="migrated",
                logger=migration_log,
                kanta=self._kanta,
                filename=str(self.filename),
                from_version=previous_version,
                to_version=migration_result.version,
                migrations=descriptions,
            ),
            self.callback_registry.logemit_handlers,
        )

    async def open(
        self,
        *,
        create: bool = True,
        readonly: bool = False,
        log: bool | logging.Logger = True,
    ) -> None:
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

        # From this point the file is open and must be closed via close().
        self.opened = True

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

            migration_result = None
            state_before_migrations = None
            previous_version = rr.version
            if self.migrations is not None:
                state_before_migrations = copy.deepcopy(rr.state)
                migration_result = self.migrations.apply(
                    rr.state, rr.version, self._kanta
                )
                rr.version = migration_result.version

            migrations_ran = rr.version != previous_version

            self.snapshot.ts = (
                datetime.fromtimestamp(rr.last_snapshot_mtime, UTC)
                if rr.last_snapshot_mtime is not None
                else None
            )

            self.statedict = copy.deepcopy(
                state_before_migrations
                if state_before_migrations is not None
                else rr.state
            )
            self.data = restore_data_in_place(
                self.data,
                rr.state,
                self.data_type,
                serializer=self.serializer,
            )
            self.version = rr.version
            self.mtime = rr.m
            if log is not False and not migrations_ran:
                logger = log if isinstance(log, logging.Logger) else bootstrap_logger
                emit_event(
                    LogEvent(
                        kind="opened",
                        logger=logger,
                        level=logging.DEBUG,
                        kanta=self._kanta,
                        filename=str(self.filename.resolve()),
                    ),
                    self.callback_registry.logemit_handlers,
                )
            normalized = struct_to_dict(self.data, serializer=self.serializer)
            if self.readonly:
                self.statedict = copy.deepcopy(normalized)
            else:
                # One record per open: migration changes and normalization are
                # grouped into migrate:vN, or migrate:msgspec when only the
                # serialization drifted.
                previous = self.statedict
                action = (
                    f"migrate:v{self.version}" if migrations_ran else "migrate:msgspec"
                )
                record = self.queue_change(action, normalized, mtime=False)
                # The migration summary introduces the diff, so log it first.
                if migrations_ran and migration_result is not None:
                    await self._handle_migration_log(
                        migration_result, previous_version, log
                    )
                if (
                    record is not None
                    and log is not False
                    and not (migrations_ran and self.callback_registry.has("logmigr"))
                ):
                    logger = (
                        log if isinstance(log, logging.Logger) else migration_logger
                    )
                    emit_event(
                        LogEvent(
                            kind="change",
                            logger=logger,
                            level=logging.DEBUG,
                            kanta=self._kanta,
                            action=action,
                            diff=record.diff,
                            previous=previous,
                        ),
                        self.callback_registry.logemit_handlers,
                    )
                if migrations_ran or record is not None:
                    self.snapshot.request_force()
                    await self.flush()
                    self.snapshot.maybe_write(
                        self.file,
                        self.version,
                        self.statedict,
                        m=self.mtime,
                        now=self.now,
                    )
        elif self.readonly:
            self.opened = False
            self.file.close()
            raise DataIntegrityError(
                "Cannot open empty database in read-only mode",
                db_path=self.filename,
                action="open",
            )
        else:
            try:
                if self.callback_registry.has("bootstrap"):
                    await self.callback_registry.invoke(
                        "bootstrap",
                        InjectionContext(data=self.data, kanta=self._kanta),
                    )

                self.statedict = {}
                current = struct_to_dict(self.data, serializer=self.serializer)
                record = self.queue_change(
                    self.bootstrap_action,
                    current,
                    user=self.bootstrap_user,
                    mtime=self.bootstrap_mtime,
                    force=True,
                )

                if record is not None and log is not False:
                    logger = (
                        log if isinstance(log, logging.Logger) else bootstrap_logger
                    )
                    emit_event(
                        LogEvent(
                            kind="created",
                            logger=logger,
                            kanta=self._kanta,
                            filename=str(self.filename.resolve()),
                        ),
                        self.callback_registry.logemit_handlers,
                    )
                    logfmt = self.callback_registry.build_logfmt(
                        InjectionContext(
                            previous_state={},
                            current_state=current,
                            kanta=self._kanta,
                        )
                    )
                    formatted_user = self.bootstrap_user
                    if formatted_user is not None and logfmt is not None:
                        resolved = logfmt(formatted_user, _USER_PATH)
                        if resolved is not None:
                            formatted_user = resolved
                    emit_event(
                        LogEvent(
                            kind="change",
                            logger=logger,
                            kanta=self._kanta,
                            action=self.bootstrap_action,
                            user=formatted_user,
                            diff=record.diff,
                            previous={},
                            current=current,
                            logfmt=logfmt,
                        ),
                        self.callback_registry.logemit_handlers,
                    )
            except Exception:
                self.opened = False
                self.file.close()
                try:
                    await asyncio.to_thread(self.filename.unlink, missing_ok=True)
                except FileNotFoundError:
                    pass
                raise

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
