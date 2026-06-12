from datetime import UTC, datetime

from kanta import ChangeRecord, Snapshot, replay
from kanta.serialization.framing import LineFramer


def test_empty_data():
    rr = replay(b"")
    assert rr.state == {}
    assert rr.version == 0


def test_single_change():
    rec = ChangeRecord(a="test", v=1, diff={"name": "Alice"})
    data = b"" + __import__("msgspec").json.encode(rec) + b"\n"
    rr = replay(data)
    assert rr.state == {"name": "Alice"}
    assert rr.version == 1


def test_snapshot_then_change():
    snap = Snapshot(ts=datetime.now(UTC), v=1, state={"counter": 5})
    line = LineFramer.SNAPSHOT_PREFIX + __import__("msgspec").json.encode(snap) + b"\n"
    rec = ChangeRecord(a="inc", v=1, diff={"counter": 6})
    line += __import__("msgspec").json.encode(rec) + b"\n"
    rr = replay(line)
    assert rr.state == {"counter": 6}


def test_migration_flag():
    rec = ChangeRecord(a="migrate:v1", v=1, diff={"x": 1})
    data = __import__("msgspec").json.encode(rec) + b"\n"
    rr = replay(data)
    assert rr.has_migration is True
