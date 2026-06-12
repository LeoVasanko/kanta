"""On-disk record structures for the JSONL log."""

from datetime import UTC, datetime
from typing import Any

import msgspec


class ChangeRecord(msgspec.Struct, omit_defaults=True, kw_only=True):
    """A single change record in the JSONL log.

    Attributes:
        ts: Timestamp of the change.
        a: Action name (e.g., "sync", "migrate", "create_user").
        v: Schema version after this change.
        u: User/actor identifier (None for system operations).
        m: Last real (non-migration) modification time, carried forward.
        diff: The jsondiff patch representing the change.
    """

    ts: datetime = msgspec.field(default_factory=lambda: datetime.now(UTC))
    a: str = ""
    v: int = 0
    u: str | None = None
    m: datetime | None = None
    diff: dict


class Snapshot(msgspec.Struct, omit_defaults=True):
    """Full state snapshot embedded in the JSONL log."""

    ts: datetime
    v: int
    state: dict[str, Any]
    m: datetime | None = None
