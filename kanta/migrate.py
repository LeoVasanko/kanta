"""Database schema migration framework.

Migrations are numbered functions discovered automatically via a decorator
or by prefix. Each runs exactly once based on the current version.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from types import ModuleType
from typing import Any

_logger = logging.getLogger(__name__)


class MigrationRegistry:
    """Registry of schema migration functions.

    Usage::

        registry = MigrationRegistry()

        @registry.register
        def migrate_v1(d: dict, kanta) -> None:
            d.setdefault("version", 1)
            kanta.ctx.note = "migrated"

        @registry.register
        def migrate_v2(d: dict) -> None:
            d.setdefault("version", 2)

        new_version = registry.apply(state, current_version=0, kanta=kanta)

    Or load from a module::

        registry = MigrationRegistry.from_module("myapp.migrations")
        new_version = registry.apply(state, current_version=0, kanta=kanta)
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
    def from_module(cls, module: str | ModuleType) -> MigrationRegistry:
        """Create a registry by scanning a module for ``migrate_vN`` functions.

        Args:
            module: A module name (string) or an imported module object.
        """
        reg = cls()
        if isinstance(module, str):
            mod = importlib.import_module(module)
        else:
            mod = module

        for name in dir(mod):
            if name.startswith("migrate_v"):
                fn = getattr(mod, name)
                if callable(fn):
                    version = reg._migration_version(fn)
                    reg._migrations[version] = fn
        return reg

    @property
    def dbver(self) -> int:
        """Current schema version (= highest discovered migration, or 0)."""
        return max(self._migrations.keys(), default=0)

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
        *,
        silent: bool = False,
    ) -> int:
        """Apply pending migrations to *data_dict* in place.

        Returns the new version after all migrations.
        """
        while current_version < self.dbver:
            next_version = current_version + 1
            fn = self._migrations.get(next_version)
            if fn is None:
                raise ValueError(
                    f"Missing migration step migrate_v{next_version} "
                    f"(highest discovered is v{self.dbver})"
                )
            self._call_migration(fn, data_dict, kanta)
            current_version = next_version
            if not silent:
                desc = (fn.__doc__ or fn.__name__).split("\n")[0].rstrip(".")
                _logger.info("Applied migration %s: %s", fn.__name__, desc)
        return current_version
