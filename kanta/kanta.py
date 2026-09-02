"""Kanta DB main public API"""

from __future__ import annotations
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Generic, TypeVar

from kanta.kantaimpl import KantaImpl
from kanta.serialization import JsonSerializer, Serializer
from kanta.transaction import transaction as _transaction

T = TypeVar("T")


class Kanta(Generic[T]):
    """JSONL persistence layer for a msgspec.Struct database state.

    The application defines its schema as a msgspec.Struct (e.g. ``Data``,
    ``Project``).  The Kanta instance holds the live state as that struct type.
    Internally it round-trips through plain dicts for diffing, replay,
    and serialization.

    A background task periodically flushes pending changes to disk.
    Call `await kanta.open()` to start the background task,
    and `await kanta.close()` to stop it.

    All transactions are synchronous — they immediately affect the
    in-memory ``kanta.data``. Persistence happens asynchronously in
    the background (or via explicit ``await kanta.flush()``).

    Usage::

        class Data(msgspec.Struct):
            users: dict[str, User] = {}

        kanta = Kanta("data.db", Data())
        await kanta.open()

        with kanta.transaction(action="create_user") as data:
            data.users["alice"] = User(name="Alice")

        await kanta.close()
    """

    def __init__(
        self,
        filename: Path | str,
        data: T,
        *,
        type: type[T] | None = None,
        migrations: ModuleType | str | None = None,
        serializer: Serializer | None = None,
        flush_interval: float = 0.1,
        retention: timedelta | int | None = None,
    ):
        """Initialize a Kanta persistence instance.

        Args:
            filename: Path to the database file.
            data: Caller-owned root msgspec.Struct state instance.
            type: Optional explicit root type. Defaults to ``type(data)``.
            migrations: Optional migrations module object or import path.
            flush_interval: Background flush interval in seconds.
            serializer: Optional serializer implementation.
            retention: Optional history retention window, either a
                :class:`~datetime.timedelta` or a plain number of days. When
                set, opening the database rotates it: history older than
                ``now - retention`` is moved to a ``{stem}@{timestamp}.kantadb``
                sibling file and the main file is rewritten with a fresh
                snapshot plus the retained records (see ``docs/rotation.md``).
                ``None`` (default) disables rotation.

        Raises:
            ImportError: If ``migrations`` is a string path that cannot be imported.
            ValueError: If migration definitions are invalid.
        """
        active_serializer = serializer if serializer is not None else JsonSerializer()
        data_type = type if type is not None else data.__class__

        self._impl = KantaImpl(
            serializer=active_serializer,
            filename=filename,
            data=data,
            type=data_type,
            migrations=migrations,
            flush_interval=flush_interval,
            retention=retention,
            kanta=self,
        )

    @property
    def data(self) -> T:
        """Current in-memory state object.

        Returns:
            The live state instance of the configured ``type``.

        Notes:
            Mutations should only be performed via :meth:`transaction`
            to ensure proper diffing and persistence.
        """
        return self._impl.data

    @data.setter
    def data(self, value: T) -> None:
        """Replace the in-memory state object.

        Notes:
            Mutations should only be performed via :meth:`transaction`
            to ensure proper diffing and persistence.

        Args:
            value: New state object instance.
        """
        self._impl.data = value

    @property
    def version(self) -> int:
        """Current schema/database version.

        Returns:
            Integer version derived from migrations/replay state.
        """
        return self._impl.version

    @property
    def filename(self) -> Path:
        """Database file path.

        Returns:
            Filesystem path used for persistence.
        """
        return self._impl.filename

    @property
    def ctx(self) -> SimpleNamespace:
        """User-writable context namespace.

        Migration functions receive the ``Kanta`` instance and can read or
        mutate ``kanta.ctx`` during migrations.  Applications can also store
        arbitrary data here (e.g. a connection id); since
        :class:`kanta.logging.LogEvent` carries the Kanta instance, logemit
        callbacks can read it as ``event.kanta.ctx``.
        """
        return self._impl.ctx

    @property
    def mtime(self) -> datetime | None:
        """Last modification time carried forward from change records.

        Returns:
            The latest ``m`` value, or ``None`` if no modification time has
            been set yet. System operations such as migrations do not update
            this value.
        """
        return self._impl.mtime

    async def open(
        self,
        *,
        create: bool = True,
        readonly: bool = False,
        log: bool | logging.Logger = True,
    ) -> None:
        """Open the database file and start background persistence.

        This loads existing records, applies configured migrations, and starts
        the background flush task.

        Args:
            create: Whether to create the database file when missing.
                If False, opening fails when the file does not exist or is empty.
            readonly: If True, open the database read-only. No lock is acquired,
                no background flush task is started, and transactions are
                rejected. The file is not created if missing.
            log: Controls bootstrap and migration logging. ``True`` (default)
                uses the ``kanta.bootstrap`` logger for bootstrap records and
                the ``kanta.migration`` logger for migration output. ``False``
                suppresses the default bootstrap and migration logs. A
                :class:`~logging.Logger` instance writes default output to that
                logger instead. Custom ``@kanta.logmigr`` callbacks run
                regardless of this setting.

        Calling ``open`` more than once on the same instance is not allowed.

        Raises:
            kanta.exceptions.DatabaseError: If replay or decoding fails.
            kanta.exceptions.DataIntegrityError: If the instance is already open.
        """
        await self._impl.open(create=create, readonly=readonly, log=log)

    async def __aenter__(self) -> Kanta[T]:
        """Enter async context manager and open the database.

        Returns:
            The current ``Kanta`` instance itself.
        """
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Exit async context manager and close the database.

        Args:
            exc_type: Exception type raised inside the context, if any.
            exc: Exception instance raised inside the context, if any.
            tb: Traceback for the exception, if any.
        """
        await self.close()

    async def flush(self) -> None:
        """Asynchronously flush pending change records to disk."""
        await self._impl.flush()

    def request_snapshot(self) -> None:
        """Request a snapshot to be written on the next background iteration."""
        self._impl.snapshot.request_force()

    async def close(self) -> None:
        """Stop background task, flush pending changes, and close file lock."""
        await self._impl.close()

    def bootstrap(
        self,
        fn=None,
        *,
        action: str = "bootstrap",
        user: str | None = None,
        mtime: bool | datetime = True,
    ):
        """Register a bootstrap callback executed during :meth:`open`.

        Can be used as ``@kanta.bootstrap`` or ``@kanta.bootstrap(...)``.
        The callback receives the live ``data`` object and may be sync or async.
        """

        def _register(callback):
            self._impl.add_bootstrap(
                callback=callback,
                action=action,
                user=user,
                mtime=mtime,
            )
            return callback

        if fn is None:
            return _register
        return _register(fn)

    def fatal_error(self, fn=None):
        """Register fatal error handler callback.

        Can be used as ``@kanta.fatal_error``.
        The callback receives a :class:`kanta.exceptions.DatabaseError` and may
        be sync or async.
        """

        def _register(callback):
            self._impl.add_fatal_error(callback)
            return callback

        if fn is None:
            return _register
        return _register(fn)

    def clock(self, fn=None):
        """Register a clock callback replacing the default UTC clock.

        Can be used as ``@kanta.clock``.  The callback takes no arguments and
        must return a :class:`~datetime.datetime`; its value is used for all
        record timestamps (``ts``, and ``m`` when ``mtime`` is ``True``) and
        snapshot timestamps.  The clock is only read when a timestamp is
        actually produced, so read-count-dependent clocks (e.g. advancing on
        every read) stay deterministic.  Register before :meth:`open` so that
        bootstrap and migration records use the custom clock as well.  This is
        mainly useful for tests and reproducible demos.
        """

        def _register(callback):
            self._impl.add_clock(callback)
            return callback

        if fn is None:
            return _register
        return _register(fn)

    def logmigr(self, fn=None):
        """Register a migration logging callback.

        Can be used as ``@kanta.logmigr``.
        The callback receives a :class:`kanta.migrations.MigrationReport` and
        may be sync or async. If registered, it replaces the default migration
        logger output; the application is responsible for emitting any log
        messages.
        """

        def _register(callback):
            self._impl.add_logmigr(callback)
            return callback

        if fn is None:
            return _register
        return _register(fn)

    def logfmt(self, fn=None, *, path: str | None = None):
        """Register a transaction logfmt callback.

        Can be used as ``@kanta.logfmt`` or ``@kanta.logfmt(path=...)``.
        The callback is called for each value being rendered and receives the
        value plus an optional ``path: str`` parameter.  It must return
        ``str | None`` (or inherit from :class:`kanta.callbacks.LogFmt`).

        When ``path`` is given, the callback is only invoked for values whose
        dot-notation path matches the pattern (full match, shell-style wildcards
        such as ``*`` are supported).
        """

        def _register(callback):
            self._impl.add_logfmt(callback, path=path)
            return callback

        if fn is None:
            return _register
        return _register(fn)

    def logemit(self, fn=None):
        """Register a log emitter callback.

        Can be used as ``@kanta.logemit``.  The callback receives a single
        :class:`kanta.logging.LogEvent` describing the event, including the
        preferred logger and level, and decides what (if anything) is logged
        and where.

        A falsy return value marks the event as handled and stops the chain.
        A truthy return value passes the event — possibly modified — to the
        next registered callback; when all callbacks pass, Kanta renders the
        event with its built-in formatting
        (:func:`kanta.logging.default_emit`), which a callback may also call
        itself to delegate events it does not care about.
        """

        def _register(callback):
            self._impl.add_logemit(callback)
            return callback

        if fn is None:
            return _register
        return _register(fn)

    def transaction(
        self,
        action: str,
        *,
        user: str | None = None,
        extra: Any = None,
        mtime: bool | datetime = True,
        log: bool | logging.Logger = True,
        logdiff: bool = True,
    ):
        """Create a transactional mutation context manager.

        Args:
            action: Action label stored in the change record.
            user: Optional user identifier stored in metadata and rendered in
                the log header.  Register a ``@kanta.logfmt`` callback to format
                the user value; the path ``"$user"`` is passed for this case.
            extra: Optional display-only value shown after the action in the
                log header.  Anything other than ``None`` is printed
                str-converted (colored by Kanta), unless a custom
                ``@kanta.logemit`` handler does something else with it.  It is
                never persisted in the change record.
            mtime: Controls the modification time ``m``. ``True`` (default)
                sets ``m`` to the current UTC time. ``False`` omits ``m`` so the
                previous modification time remains in effect; this is used for
                system operations that are not considered modifications. A
                :class:`~datetime.datetime` value sets ``m`` to that explicit
                time.
            log: Controls transaction logging. ``True`` (default) uses the
                ``kanta.transaction`` logger. ``False`` suppresses the
                transaction log. A :class:`~logging.Logger` instance writes
                output to that logger instead.
            logdiff: Whether to build and print the diff body. ``False``
                skips diff formatting entirely and logs only the header,
                which is useful for large or noisy changesets. Diff output
                can also be disabled globally with
                ``configure_logging(diff=False)``.

        Returns:
            A context manager yielding the live state object for mutation.

        Notes:
            On successful exit, a diff is queued for persistence.
            If an exception is raised inside the context, in-memory changes are
            rolled back.
        """
        return _transaction(
            self._impl,
            action,
            user=user,
            extra=extra,
            mtime=mtime,
            log=log,
            logdiff=logdiff,
        )
