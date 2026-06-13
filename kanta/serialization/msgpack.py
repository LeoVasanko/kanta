"""MessagePack serializer implementation."""

from __future__ import annotations

from typing import Any, TypeVar

import msgspec

from kanta.serialization.framing import BinFramer, Framer

T = TypeVar("T")


class MsgPackSerializer:
    """Binary serializer using MessagePack format."""

    framer_cls: type[Framer] = BinFramer

    def encode(self, obj: Any) -> bytes:
        return msgspec.msgpack.encode(obj)

    def decode(self, payload: bytes, *, type: type[T]) -> T:
        return msgspec.msgpack.decode(payload, type=type)
