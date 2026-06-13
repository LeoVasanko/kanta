"""JSON serializer implementation."""

from __future__ import annotations

from typing import Any, TypeVar

import msgspec

from kanta.serialization.framing import Framer, LineFramer

T = TypeVar("T")


class JsonSerializer:
    """Line-based JSON serializer."""

    framer_cls: type[Framer] = LineFramer

    def encode(self, obj: Any) -> bytes:
        return msgspec.json.encode(obj)

    def decode(self, payload: bytes, *, type: type[T]) -> T:
        return msgspec.json.decode(payload, type=type)
