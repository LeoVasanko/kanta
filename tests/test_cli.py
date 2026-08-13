"""Tests for the ``python -m kanta`` CLI output formatting."""

from datetime import UTC, datetime

from kanta.__main__ import _format_ts, main
from kanta.serialization import JsonSerializer
from kanta.serialization.framing import LineFramer
from kanta.structs import ChangeRecord, Snapshot


def test_format_ts_strips_microseconds():
    """Timestamps are rendered without microsecond precision."""
    dt = datetime(2026, 8, 12, 10, 6, 52, 375398, tzinfo=UTC)
    assert _format_ts(dt) == "2026-08-12 10:06:52"


def test_cli_snapshot_line_format(tmp_path, capsys):
    """Snapshot lines are timestamped and colored with metadata."""
    path = tmp_path / "test.kantadb"
    ts = datetime(2026, 8, 12, 10, 6, 52, 375398, tzinfo=UTC)
    mtime = datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC)
    serializer = JsonSerializer()
    framer = LineFramer()

    snapshot = Snapshot(ts=ts, v=1, m=mtime, state={"counter": 5})
    change = ChangeRecord(ts=ts, a="inc", v=1, u="user1", diff={"counter": 6})
    data = (
        framer.frame_snapshot(serializer.encode(snapshot), record_offset=0)
        + framer.frame_change(serializer.encode(change), record_offset=0)
    )
    path.write_bytes(data)

    code = main([str(path)])
    assert code == 0

    err = capsys.readouterr().err
    # No microsecond precision anywhere.
    assert "10:06:52" in err
    assert "10:06:52.375398" not in err

    # Snapshot line: bright white snapshot/sN, white version/mtime, dark size.
    assert "\x1b[97msnapshot s0" in err
    assert "\x1b[38;5;250m v1 2026-08-12 09:00:00" in err
    assert "\x1b[38;5;242m 13 B" in err
