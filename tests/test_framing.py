import pytest

from kanta.exceptions import ReplayError
from kanta.serialization.framing import BinFramer


def test_roundtrip_with_sync_header_and_checksum():
    framer = BinFramer()
    first = framer.frame_change(b"c1", record_offset=0)
    second = framer.frame_snapshot(b"snap", record_offset=len(first))
    third = framer.frame_change(b"c2", record_offset=len(first) + len(second))
    data = first + second + third

    assert framer._sync_snapshot is not None
    assert data[:4] == framer._sync_snapshot

    snap_payload, resume_offset, snap_pos = framer.scan_last_snapshot(data)
    assert snap_payload == b"snap"
    assert snap_pos == len(first)
    assert list(framer.iter_records(data, resume_offset)) == [
        (False, b"c2", 0, len(first) + len(second))
    ]
    assert list(framer.iter_records(data, 0)) == [
        (False, b"c1", 0, 4),
        (True, b"snap", 0, len(first)),
        (False, b"c2", 0, len(first) + len(second)),
    ]


def test_no_serialized_offset_in_change_frame():
    framer = BinFramer()
    data = framer.frame_change(b"payload", record_offset=0)
    assert data[4:8] == bytes((~b) & 0xFF for b in framer._sync_snapshot)
    payload_len = int.from_bytes(data[8:12], "little")
    assert len(data) == 4 + 4 + 4 + 8 + payload_len


def test_detects_tampered_checksum():
    framer = BinFramer()
    data = bytearray(framer.frame_change(b"payload", record_offset=0))
    checksum_start = 4 + 4 + 4
    data[checksum_start] ^= 0x01

    with pytest.raises(ReplayError, match="invalid frame checksum") as exc_info:
        list(framer.iter_records(bytes(data), 0))
    assert exc_info.value.line_number == 0
    assert exc_info.value.byte_pos == 4


def test_detects_invalid_frame_marker():
    framer = BinFramer()
    data = bytearray(framer.frame_change(b"payload", record_offset=0))
    data[4:8] = b"BAD!"

    with pytest.raises(ReplayError, match="invalid frame marker") as exc_info:
        list(framer.iter_records(bytes(data), 0))
    assert exc_info.value.line_number == 0
    assert exc_info.value.byte_pos == 4
