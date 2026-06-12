"""Framing strategies for on-disk record storage.

Framers handle how encoded payloads are delimited and scanned on disk,
independent of the payload encoding (JSON, MessagePack, etc.).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Protocol

from kanta.exceptions import ReplayError

try:
    from blake3 import blake3
except ImportError:
    raise ImportError("Install kanta[bin] for binary framing / msgpack support.")


class Framer(Protocol):
    """Handles on-disk record framing and scanning, independent of payload encoding."""

    def frame_change(self, payload: bytes, *, record_offset: int = 0) -> bytes:
        """Wrap an encoded change payload for writing."""
        raise NotImplementedError

    def frame_snapshot(self, payload: bytes, *, record_offset: int = 0) -> bytes:
        """Wrap an encoded snapshot payload for writing."""
        raise NotImplementedError

    def scan_last_snapshot(self, data: bytes) -> tuple[bytes | None, int, int]:
        """Find the last snapshot payload and the byte offset to resume iteration from.

        Returns:
            (snapshot_payload_or_None, resume_offset, snapshot_byte_pos)
        """
        raise NotImplementedError

    def iter_records(
        self, data: bytes, offset: int = 0
    ) -> Iterator[tuple[bool, bytes, int, int]]:
        """Iterate records from *offset* onward.

        Yields (is_snapshot, payload, line_number, byte_pos) tuples.
        ``line_number`` is 1-based for text framers and 0 for binary framers.
        Raises ReplayError on corruption.
        """
        raise NotImplementedError


class LineFramer:
    """Line-delimited framer for text-based formats such as JSONL."""

    SNAPSHOT_PREFIX = b"SNAPSHOT "

    def frame_change(self, payload: bytes, *, record_offset: int = 0) -> bytes:
        _ = record_offset
        return payload + b"\n"

    def frame_snapshot(self, payload: bytes, *, record_offset: int = 0) -> bytes:
        _ = record_offset
        return self.SNAPSHOT_PREFIX + payload + b"\n"

    def scan_last_snapshot(self, data: bytes) -> tuple[bytes | None, int, int]:
        marker = b"\n" + self.SNAPSHOT_PREFIX
        pos = data.rfind(marker)
        if pos != -1:
            pos += 1
        elif data.startswith(self.SNAPSHOT_PREFIX):
            pos = 0
        else:
            return None, 0, 0

        end = data.find(b"\n", pos)
        if end == -1:
            line_number = data[:pos].count(b"\n") + 1
            raise ReplayError(
                "incomplete snapshot line at end of file",
                line_number=line_number,
                byte_pos=pos,
                record_type="snapshot",
            )

        payload = data[pos + len(self.SNAPSHOT_PREFIX) : end]
        return payload, end + 1, pos

    def iter_records(
        self, data: bytes, offset: int = 0
    ) -> Iterator[tuple[bool, bytes, int, int]]:
        idx = offset
        line_number = data[:offset].count(b"\n") + 1
        while idx < len(data):
            line_start = idx
            end = data.find(b"\n", idx)
            if end == -1:
                raw = data[idx:]
                idx = len(data)
            else:
                raw = data[idx:end]
                idx = end + 1

            line = raw.strip()
            if line:
                is_snapshot = line.startswith(self.SNAPSHOT_PREFIX)
                payload = line[len(self.SNAPSHOT_PREFIX) :] if is_snapshot else line
                yield is_snapshot, payload, line_number, line_start
            line_number += 1


class BinFramer:
    """Length-prefixed binary framer for formats such as MessagePack.

    First 4 bytes of the file are a per-file random sync seed.

    Change frame::

        <4-byte bitwise-not sync seed>
        <4-byte little-endian length>
        <8-byte checksum>
        <payload>

    Snapshot frame::

        <4-byte sync seed>
        <4-byte little-endian length>
        <8-byte checksum>
        <payload>

    The checksum is BLAKE3 keyed by the per-file sync seed (zero-padded to
    32 bytes), truncated to 8 bytes.
    """

    _SYNC_SIZE = 4
    _LEN_SIZE = 4
    _OFFSET_SIZE = 8
    _CHECKSUM_SIZE = 8

    def __init__(self) -> None:
        self.set_sync(secrets.token_bytes(self._SYNC_SIZE))

    def set_sync(self, value: bytes) -> None:
        if len(value) != self._SYNC_SIZE:
            raise ValueError("binary sync seed must be exactly 4 bytes")
        self._sync_snapshot = value
        self._sync_change = bytes((~b) & 0xFF for b in value)

    def _ensure_sync_seed_for_write(self, record_offset: int) -> tuple[bytes, int]:
        if record_offset == 0:
            return self._sync_snapshot, self._SYNC_SIZE
        return b"", record_offset

    def _load_sync_seed_from_data(self, data: bytes) -> None:
        if not data:
            return
        if len(data) < self._SYNC_SIZE:
            raise ReplayError("missing binary sync header", line_number=0, byte_pos=0)
        self.set_sync(data[: self._SYNC_SIZE])

    def _checksum(
        self, *, is_snapshot: bool, record_offset: int, payload_len: int, payload: bytes
    ) -> bytes:
        domain = b"SNAPSHOT" if is_snapshot else b"DIFFRECD"
        offset_part = record_offset.to_bytes(self._OFFSET_SIZE, "little")
        h = blake3(
            payload, key=b"Kanta blake3" + domain + self._sync_snapshot + offset_part
        )
        return h.digest(length=self._CHECKSUM_SIZE)

    def frame_change(self, payload: bytes, *, record_offset: int = 0) -> bytes:
        header, effective_offset = self._ensure_sync_seed_for_write(record_offset)
        payload_len = len(payload)
        checksum = self._checksum(
            is_snapshot=False,
            record_offset=effective_offset,
            payload_len=payload_len,
            payload=payload,
        )
        framed = (
            self._sync_change
            + payload_len.to_bytes(self._LEN_SIZE, "little")
            + checksum
            + payload
        )
        return header + framed

    def frame_snapshot(self, payload: bytes, *, record_offset: int = 0) -> bytes:
        header, effective_offset = self._ensure_sync_seed_for_write(record_offset)
        payload_len = len(payload)
        checksum = self._checksum(
            is_snapshot=True,
            record_offset=effective_offset,
            payload_len=payload_len,
            payload=payload,
        )
        framed = (
            self._sync_snapshot
            + payload_len.to_bytes(self._LEN_SIZE, "little")
            + checksum
            + payload
        )
        return header + framed

    def scan_last_snapshot(self, data: bytes) -> tuple[bytes | None, int, int]:
        if not data:
            return None, 0, 0
        self._load_sync_seed_from_data(data)
        snapshot_payload: bytes | None = None
        snapshot_resume_offset = self._SYNC_SIZE
        snapshot_byte_pos = 0
        for (
            is_snapshot,
            payload,
            next_offset,
            record_offset,
        ) in self._iter_records_internal(data, self._SYNC_SIZE):
            if is_snapshot:
                snapshot_payload = payload
                snapshot_resume_offset = next_offset
                snapshot_byte_pos = record_offset
        return snapshot_payload, snapshot_resume_offset, snapshot_byte_pos

    def _iter_records_internal(
        self, data: bytes, start_offset: int
    ) -> Iterator[tuple[bool, bytes, int, int]]:
        if not data:
            return
        self._load_sync_seed_from_data(data)

        idx = start_offset
        while idx < len(data):
            record_offset = idx
            if idx + self._SYNC_SIZE > len(data):
                raise ReplayError(
                    "incomplete frame at end of file",
                    line_number=0,
                    byte_pos=record_offset,
                )

            frame_seed = data[idx : idx + self._SYNC_SIZE]
            if frame_seed == self._sync_snapshot:
                is_snapshot = True
            elif frame_seed == self._sync_change:
                is_snapshot = False
            else:
                raise ReplayError(
                    "invalid frame marker",
                    line_number=0,
                    byte_pos=record_offset,
                )
            idx += self._SYNC_SIZE

            min_record = self._LEN_SIZE + self._CHECKSUM_SIZE
            if idx + min_record > len(data):
                raise ReplayError(
                    "incomplete frame at end of file",
                    line_number=0,
                    byte_pos=record_offset,
                    record_type="snapshot" if is_snapshot else "change",
                )

            payload_len = int.from_bytes(data[idx : idx + self._LEN_SIZE], "little")
            idx += self._LEN_SIZE

            checksum = data[idx : idx + self._CHECKSUM_SIZE]
            idx += self._CHECKSUM_SIZE

            if idx + payload_len > len(data):
                raise ReplayError(
                    "incomplete frame at end of file",
                    line_number=0,
                    byte_pos=record_offset,
                    record_type="snapshot" if is_snapshot else "change",
                )

            payload = data[idx : idx + payload_len]
            expected = self._checksum(
                is_snapshot=is_snapshot,
                record_offset=record_offset,
                payload_len=payload_len,
                payload=payload,
            )
            if checksum != expected:
                raise ReplayError(
                    "invalid frame checksum",
                    line_number=0,
                    byte_pos=record_offset,
                    record_type="snapshot" if is_snapshot else "change",
                )

            idx += payload_len
            yield is_snapshot, payload, idx, record_offset

    def iter_records(
        self, data: bytes, offset: int = 0
    ) -> Iterator[tuple[bool, bytes, int, int]]:
        if not data:
            return
        self._load_sync_seed_from_data(data)
        start = self._SYNC_SIZE if offset == 0 else offset
        for is_snapshot, payload, _, record_offset in self._iter_records_internal(
            data, start
        ):
            yield is_snapshot, payload, 0, record_offset
