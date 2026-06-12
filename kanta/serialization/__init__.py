"""Serializer package with pluggable format classes."""

from __future__ import annotations

from typing import Any, TypeVar

from .base import ReplayResult, Serializer
from .framing import BinFramer, Framer, LineFramer
from .json import JsonSerializer
from .msgpack import MsgPackSerializer

T = TypeVar("T")

_DEFAULT_SERIALIZER = JsonSerializer()


def struct_to_dict(obj: T, serializer: Serializer | None = None) -> dict[str, Any]:
    """Convert a struct instance to plain builtins with the selected serializer."""
    active = serializer or _DEFAULT_SERIALIZER
    return active.decode(active.encode(obj), type=dict[str, Any])


def dict_to_struct(
    d: dict[str, Any],
    data_type: type[T],
    serializer: Serializer | None = None,
) -> T:
    """Decode plain builtins back into the configured struct type."""
    active = serializer or _DEFAULT_SERIALIZER
    return active.decode(active.encode(d), type=data_type)


def restore_data_in_place(
    data: T,
    snapshot_dict: dict[str, Any],
    data_type: type[T],
    serializer: Serializer | None = None,
) -> T:
    """Restore data from snapshot while preserving object identity when possible."""
    restored = dict_to_struct(snapshot_dict, data_type, serializer=serializer)
    if type(restored) is not type(data):
        return restored
    for field_name in getattr(restored, "__struct_fields__", ()):
        setattr(data, field_name, getattr(restored, field_name))
    return data


__all__ = [
    "Framer",
    "JsonSerializer",
    "BinFramer",
    "LineFramer",
    "MsgPackSerializer",
    "ReplayResult",
    "Serializer",
    "dict_to_struct",
    "restore_data_in_place",
    "struct_to_dict",
]
