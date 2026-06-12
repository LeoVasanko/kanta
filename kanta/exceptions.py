"""Custom exception types for Kanta."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class DatabaseError(ValueError):
    """Exception raised for database loading errors."""

    def __init__(
        self,
        message: str,
        *,
        db_path: Path | None = None,
        line_number: int | None = None,
        byte_pos: int | None = None,
        cause_type: str | None = None,
    ):
        self.db_path = db_path
        self.line_number = line_number
        self.byte_pos = byte_pos
        self.cause_type = cause_type
        super().__init__(message)


class ReplayError(DatabaseError):
    """Structured replay error with source location metadata."""

    def __init__(
        self,
        message: str,
        *,
        line_number: int | None = None,
        byte_pos: int | None = None,
        record_type: str | None = None,
    ):
        self.record_type = record_type
        super().__init__(message, line_number=line_number, byte_pos=byte_pos)


class FileLockError(DatabaseError):
    """Raised when database file open/lock operations fail."""


class DataIntegrityError(RuntimeError):
    """Raised when in-memory data integrity invariants are violated."""

    def __init__(
        self,
        message: str,
        *,
        db_path: Path | None = None,
        action: str | None = None,
        diff: dict[str, Any] | None = None,
    ):
        self.db_path = db_path
        self.action = action
        self.diff = diff
        super().__init__(message)
