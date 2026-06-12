"""JSONL persistence layer with background flush task."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Generic, TypeVar

from kanta.exceptions import DatabaseError
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
        migration_ctx: Any | None = None,
        serializer: Serializer | None = None,
        fatal_error: Callable[[DatabaseError], None] | None = None,
        flush_interval: float = 0.1,
    ):
        """Initialize a Kanta persistence instance.

        Args:
            filename: Path to the database file.
            data: Caller-owned root msgspec.Struct state instance.
            type: Optional explicit root type. Defaults to ``type(data)``.
            migrations: Optional migrations module object or import path.
            migration_ctx: Optional context object passed to migration functions.
            flush_interval: Background flush interval in seconds.
            serializer: Optional serializer implementation.
            fatal_error: Optional callback invoked immediately when the
                background writer encounters a DatabaseError.

        Raises:
            ImportError: If ``migrations`` is a string path that cannot be imported.
            ValueError: If migration definitions are invalid.
        """
        active_serializer = serializer if serializer is not None else JsonSerializer()
        data_type = type if type is not None else data.__class__

        self._impl = KantaImpl(
            serializer=active_serializer,
            fatal_error=fatal_error,
            filename=filename,
            data=data,
            type=data_type,
            migrations=migrations,
            migration_ctx=migration_ctx,
            flush_interval=flush_interval,
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

    async def open(self) -> None:
        """Open the database file and start background persistence.

        This loads existing records, applies configured migrations, and starts
        the background flush task.

        Calling ``open`` more than once on the same instance is not allowed.

        Raises:
            kanta.exceptions.DatabaseError: If replay or decoding fails.
            kanta.exceptions.DataIntegrityError: If the instance is already open.
        """
        await self._impl.open()

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

    def transaction(
        self,
        action: str,
        *,
        user: str | None = None,
        user_display: str | None = None,
        resolver: Any = None,
    ):
        """Create a transactional mutation context manager.

        Args:
            action: Action label stored in the change record.
            user: Optional user identifier stored in metadata.
            user_display: Optional display name used for logging/resolution.
            resolver: Optional callable for resolving identifiers in logs.

        Returns:
            A context manager yielding the live state object for mutation.

        Notes:
            On successful exit, a diff is queued for persistence.
            If an exception is raised inside the context, in-memory changes are
            rolled back.
        """
        return _transaction(
            self._impl, action, user=user, user_display=user_display, resolver=resolver
        )
