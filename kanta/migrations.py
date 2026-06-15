"""Database schema migration framework.

Migrations are numbered functions discovered automatically via a decorator
or by prefix. Each runs exactly once based on the current version.
"""

from __future__ import annotations

import copy
import importlib
import inspect
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from kanta.diff import compute_diff
from kanta.exceptions import DatabaseError

# Cache registries by imported module object so that many Kanta instances using
# the same migrations module do not re-scan it each time.
_module_registry_cache: dict[ModuleType, Migrations] = {}


@dataclass
class MigrationInfo:
    """Information about a single migration that ran."""

    name: str
    description: str
    version: int
    changed: bool
    diff: dict | None = None
    before: dict | None = None


@dataclass
class MigrationResult:
    """Result of applying migrations."""

    version: int
    migrations: list[MigrationInfo]


class Migrations:
    """Registry of schema migration functions.

    Usage::

        migrations = Migrations()

        @migrations.register
        def migrate_v1(d: dict, kanta) -> None:
            d.setdefault("version", 1)
            kanta.ctx.note = "migrated"

        @migrations.register
        def migrate_v2(d: dict) -> None:
            d.setdefault("version", 2)

        result = migrations.apply(state, current_version=0, kanta=kanta)
        new_version = result.version

    Or load from a module::

        migrations = Migrations.from_module("myapp.migrations")
        result = migrations.apply(state, current_version=0, kanta=kanta)
    """

    def __init__(self) -> None:
        self._migrations: dict[int, Any] = {}

    @staticmethod
    def _migration_version(fn: Any) -> int:
        name = getattr(fn, "__name__", "")
        if not name.startswith("migrate_v"):
            raise ValueError(f"Invalid migration function name: {name!r}")
        suffix = name.removeprefix("migrate_v")
        if not suffix.isdigit() or int(suffix) <= 0:
            raise ValueError(f"Invalid migration version in function name: {name!r}")
        return int(suffix)

    def register(self, fn):
        """Decorator to register a migration function."""
        version = self._migration_version(fn)
        self._migrations[version] = fn
        return fn

    @classmethod
    def from_module(cls, module: str | ModuleType) -> Migrations:
        """Create or retrieve a cached registry by scanning a module.

        Args:
            module: A module name (string) or an imported module object.
        """
        if isinstance(module, str):
            mod = importlib.import_module(module)
        else:
            mod = module

        try:
            return _module_registry_cache[mod]
        except KeyError:
            pass

        reg = cls()
        for name in dir(mod):
            if name.startswith("migrate_v"):
                fn = getattr(mod, name)
                if callable(fn):
                    version = reg._migration_version(fn)
                    reg._migrations[version] = fn
        _module_registry_cache[mod] = reg
        return reg

    @property
    def dbver(self) -> int:
        """Current schema version (= highest discovered migration, or 0)."""
        return max(self._migrations.keys(), default=0)

    @property
    def minver(self) -> int:
        """Minimum supported current version (first migration minus 1, or 0)."""
        return min(self._migrations.keys(), default=1) - 1

    @staticmethod
    def _call_migration(fn: Any, data_dict: dict[str, Any], kanta: Any) -> None:
        """Call *fn* with the data dict and, if accepted, the Kanta instance."""
        try:
            inspect.signature(fn).bind(data_dict, kanta)
        except TypeError:
            fn(data_dict)
        else:
            fn(data_dict, kanta)

    def apply(
        self,
        data_dict: dict[str, Any],
        current_version: int,
        kanta: Any,
    ) -> MigrationResult:
        """Apply pending migrations to *data_dict* in place.

        Missing intermediate migration steps are silently skipped.

        Raises:
            DatabaseError: If the database version is newer than the highest
                supported version or older than the minimum supported version.

        Returns a :class:`MigrationResult` describing the new version and every
        migration that ran.
        """
        if current_version > self.dbver:
            raise DatabaseError(
                f"Database version v{current_version} is newer than the "
                f"highest supported version v{self.dbver}"
            )
        if current_version < self.minver:
            raise DatabaseError(
                f"Database version v{current_version} is older than the "
                f"minimum supported version v{self.minver}"
            )

        migrations: list[MigrationInfo] = []
        for version in sorted(self._migrations.keys()):
            if version <= current_version:
                continue
            fn = self._migrations[version]
            before = copy.deepcopy(data_dict)
            self._call_migration(fn, data_dict, kanta)
            current_version = version
            changed = before != data_dict
            diff = compute_diff(before, data_dict) if changed else None
            desc = (fn.__doc__ or f"v{version}").split("\n")[0].rstrip(".")
            migrations.append(
                MigrationInfo(
                    name=fn.__name__,
                    description=desc,
                    version=version,
                    changed=changed,
                    diff=diff,
                    before=before,
                )
            )
        return MigrationResult(version=current_version, migrations=migrations)
