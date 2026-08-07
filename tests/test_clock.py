from datetime import UTC, datetime, timedelta

import pytest

from .support import (
    Data,
    make_kanta,
    make_migrations_module,
    read_changes,
    read_last_snapshot,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_clock_rejects_non_callable(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    with pytest.raises(TypeError, match="must be callable"):
        kanta.clock(42)


def test_clock_rejects_required_argument(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    with pytest.raises(TypeError, match="must not require arguments"):

        @kanta.clock
        def fake_now(tz) -> datetime:
            return T0


@pytest.mark.asyncio
async def test_clock_rejects_non_datetime_result(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    @kanta.clock
    def fake_now() -> datetime:
        return "noon"

    with pytest.raises(TypeError, match="must return a datetime"):
        await kanta.open(log=False)


@pytest.mark.asyncio
async def test_clock_controls_record_timestamps(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    current = T0

    @kanta.clock
    def fake_now() -> datetime:
        return current

    await kanta.open(log=False)
    current = T0 + timedelta(hours=1)
    with kanta.transaction(action="update") as data:
        data.counter = 1
    current = T0 + timedelta(hours=2)
    with kanta.transaction(action="repair", mtime=False) as data:
        data.counter = 2
    await kanta.close()

    bootstrap, update, repair = read_changes(path, format_config)
    assert bootstrap.ts == T0
    assert bootstrap.m == T0
    assert update.ts == T0 + timedelta(hours=1)
    assert update.m == T0 + timedelta(hours=1)
    # System operation: stamped by the clock, but m is not updated.
    assert repair.ts == T0 + timedelta(hours=2)
    assert repair.m is None
    assert kanta.mtime == T0 + timedelta(hours=1)


@pytest.mark.asyncio
async def test_clock_not_read_without_changes(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)
    reads = 0

    @kanta.clock
    def fake_now() -> datetime:
        nonlocal reads
        reads += 1
        return T0

    await kanta.open(log=False)  # bootstrap record: one read
    reads = 0

    with kanta.transaction(action="noop"):
        pass  # no changes, no record, no clock read
    await kanta.close()  # no snapshot written, no clock read

    assert reads == 0


@pytest.mark.asyncio
async def test_clock_controls_migration_and_snapshot_timestamps(
    tmp_path, format_config
):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.clock
    def fake_now() -> datetime:
        return T0

    await kanta.open(log=False)
    await kanta.close()

    def migrate_v1(d):
        """Bump counter"""
        d["counter"] = 1

    migrations = make_migrations_module("clock_migrations", "migrate_v1", migrate_v1)
    t1 = T0 + timedelta(days=1)
    kanta2 = make_kanta(path, Data, format_config, migrations=migrations)

    @kanta2.clock
    def fake_now2() -> datetime:
        return t1

    await kanta2.open(log=False)
    await kanta2.close()

    migrate_records = [
        r for r in read_changes(path, format_config) if r.a.startswith("migrate:")
    ]
    assert migrate_records
    assert all(r.ts == t1 for r in migrate_records)

    snapshot = read_last_snapshot(path, format_config)
    assert snapshot is not None
    assert snapshot.ts == t1
    # mtime is carried forward from the last real modification.
    assert snapshot.m == T0
