"""Tests for retention-based database rotation (docs/rotation.md)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kanta.structs import ChangeRecord, Snapshot
from tests.support import Data, make_kanta, read_changes

pytestmark = pytest.mark.asyncio

DAY = timedelta(days=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def make_clock(cell: list[datetime]):
    def clock() -> datetime:
        return cell[0]

    return clock


async def write_history(path: Path, format_config, days: list[int]) -> None:
    """Write one change per day offset (relative to T0) with a fake clock."""
    cell = [T0 + (days[0] - 1) * DAY]  # bootstrap predates all history
    kanta = make_kanta(path, Data, format_config)
    kanta.clock(make_clock(cell))
    await kanta.open(log=False)
    for day in days:
        cell[0] = T0 + day * DAY
        with kanta.transaction(f"day{day}", log=False) as data:
            data.counter += 1
        await kanta.flush()
    await kanta.close()


def read_all(path: Path, format_config):
    """All records (changes and snapshots) in file order."""
    _, serializer_cls = format_config
    serializer = serializer_cls()
    framer = serializer.framer_cls()
    out = []
    for is_snapshot, payload, _, _ in framer.iter_records(path.read_bytes(), 0):
        out.append(
            serializer.decode(payload, type=Snapshot if is_snapshot else ChangeRecord)
        )
    return out


def rotated_files(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.stem}@*.kantadb"))


async def test_rotation_splits_history(tmp_path, format_config):
    path = tmp_path / "data.kantadb"
    await write_history(path, format_config, days=[-40, -20, -5])

    cell = [T0]
    kanta = make_kanta(path, Data, format_config, retention=30 * DAY)
    kanta.clock(make_clock(cell))
    await kanta.open(log=False)
    assert kanta.data.counter == 3
    await kanta.close()

    rotated = rotated_files(path)
    assert len(rotated) == 1

    # Main file: leading snapshot (ts = last dropped record), the retained
    # changes, and no final snapshot (too few retained changes).
    records = read_all(path, format_config)
    assert isinstance(records[0], Snapshot)
    assert records[0].ts == T0 - 40 * DAY
    assert records[0].state["counter"] == 1
    changes = [r for r in records if isinstance(r, ChangeRecord)]
    assert [c.a for c in changes] == ["day-20", "day-5"]

    # Rotated file holds exactly the dropped history, ending at the last
    # dropped record whose ts matches the filename.
    stamp = (T0 - 40 * DAY).strftime("%Y%m%dT%H%M%SZ")
    assert rotated[0].name == f"data@{stamp}.kantadb"
    rrecords = read_all(rotated[0], format_config)
    assert [r.a for r in rrecords] == ["bootstrap", "day-40"]


async def test_rotation_reopens_cleanly_and_does_not_rerotate(tmp_path, format_config):
    path = tmp_path / "data.kantadb"
    await write_history(path, format_config, days=[-40, -5])

    cell = [T0]
    for expected_changes in (["day-5"], ["day-5"]):
        kanta = make_kanta(path, Data, format_config, retention=30 * DAY)
        kanta.clock(make_clock(cell))
        async with kanta:
            assert kanta.data.counter == 2
        assert [c.a for c in read_changes(path, format_config)] == expected_changes

    # Second open found a file whose history already fits the window.
    assert len(rotated_files(path)) == 1


async def test_rotation_noop_when_retention_covers_all(tmp_path, format_config):
    path = tmp_path / "data.kantadb"
    await write_history(path, format_config, days=[-5])
    before = path.read_bytes()

    cell = [T0]
    kanta = make_kanta(path, Data, format_config, retention=30 * DAY)
    kanta.clock(make_clock(cell))
    async with kanta:
        assert kanta.data.counter == 1

    assert rotated_files(path) == []
    assert path.read_bytes() == before


async def test_rotation_aged_out_database_reduces_to_single_snapshot(
    tmp_path, format_config
):
    path = tmp_path / "data.kantadb"
    await write_history(path, format_config, days=[-40, -35])

    cell = [T0]
    kanta = make_kanta(path, Data, format_config, retention=30 * DAY)
    kanta.clock(make_clock(cell))
    async with kanta:
        assert kanta.data.counter == 2

    records = read_all(path, format_config)
    assert len(records) == 1
    assert isinstance(records[0], Snapshot)
    assert records[0].state["counter"] == 2

    # Opening again must not rotate the snapshot-only file.
    before = path.read_bytes()
    kanta = make_kanta(path, Data, format_config, retention=30 * DAY)
    kanta.clock(make_clock(cell))
    async with kanta:
        assert kanta.data.counter == 2
    assert path.read_bytes() == before
    assert len(rotated_files(path)) == 1


async def test_rotation_validates_against_internal_snapshots(tmp_path, format_config):
    path = tmp_path / "data.kantadb"
    cell = [T0 - 40 * DAY]
    kanta = make_kanta(path, Data, format_config)
    kanta.clock(make_clock(cell))
    await kanta.open(log=False)
    with kanta.transaction("old", log=False) as data:
        data.counter = 1
    await kanta.flush()
    kanta.request_snapshot()
    kanta._impl.maybe_snapshot()
    cell[0] = T0 - 1 * DAY
    with kanta.transaction("new", log=False) as data:
        data.counter = 2
    await kanta.flush()
    await kanta.close()

    cell[0] = T0
    kanta = make_kanta(path, Data, format_config, retention=30 * DAY)
    kanta.clock(make_clock(cell))
    async with kanta:
        assert kanta.data.counter == 2

    records = read_all(path, format_config)
    assert isinstance(records[0], Snapshot)
    assert records[0].state["counter"] == 1
    assert [r.a for r in records if isinstance(r, ChangeRecord)] == ["new"]


@pytest.mark.parametrize("name", ["data", "data.db", "data.kantadb"])
async def test_rotated_naming_normalizes_extension(tmp_path, format_config, name):
    path = tmp_path / name
    await write_history(path, format_config, days=[-40, -5])

    cell = [T0]
    kanta = make_kanta(path, Data, format_config, retention=30 * DAY)
    kanta.clock(make_clock(cell))
    async with kanta:
        pass

    stamp = (T0 - 40 * DAY).strftime("%Y%m%dT%H%M%SZ")
    assert (tmp_path / f"data@{stamp}.kantadb").exists()


async def test_retention_accepts_int_days(tmp_path, format_config):
    path = tmp_path / "data.kantadb"
    await write_history(path, format_config, days=[-40, -5])

    cell = [T0]
    kanta = make_kanta(path, Data, format_config, retention=30)
    kanta.clock(make_clock(cell))
    async with kanta:
        assert kanta.data.counter == 2

    assert len(rotated_files(path)) == 1
    assert [c.a for c in read_changes(path, format_config)] == ["day-5"]


async def test_rotation_disabled_by_default(tmp_path, format_config):
    path = tmp_path / "data.kantadb"
    await write_history(path, format_config, days=[-40, -5])
    before = path.read_bytes()

    cell = [T0]
    kanta = make_kanta(path, Data, format_config)
    kanta.clock(make_clock(cell))
    async with kanta:
        assert kanta.data.counter == 2

    assert rotated_files(path) == []
    assert path.read_bytes() == before
