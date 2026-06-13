"""Tests for mtime handling and the public ``kanta.mtime`` property."""

from datetime import UTC, datetime

import pytest

from kanta.structs import ChangeRecord

from .support import Data, make_kanta, seed_single_change


def _read_last_change(path, format_config):
    name, serializer_cls = format_config
    serializer = serializer_cls()
    framer = serializer.framer_cls()
    last = None
    for is_snapshot, payload, _, _ in framer.iter_records(path.read_bytes(), 0):
        if is_snapshot:
            continue
        last = serializer.decode(payload, type=ChangeRecord)
    assert last is not None
    return last


@pytest.mark.asyncio
async def test_default_transaction_updates_mtime(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    before = datetime.now(UTC)
    with kanta.transaction(action="inc") as data:
        data.counter = 1
    await kanta.flush()
    await kanta.close()

    rec = _read_last_change(path, format_config)
    assert rec.ts == rec.m
    assert before <= rec.m <= datetime.now(UTC)
    assert kanta.mtime == rec.m


@pytest.mark.asyncio
async def test_transaction_custom_mtime(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    custom_m = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    with kanta.transaction(action="inc", mtime=custom_m) as data:
        data.counter = 1
    await kanta.flush()
    await kanta.close()

    rec = _read_last_change(path, format_config)
    assert rec.m == custom_m
    assert kanta.mtime == custom_m


@pytest.mark.asyncio
async def test_transaction_mtime_false_preserves_mtime(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    first_m = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    with kanta.transaction(action="first", mtime=first_m) as data:
        data.counter = 1

    with kanta.transaction(action="second", mtime=False) as data:
        data.counter = 2

    await kanta.flush()
    await kanta.close()

    records = []
    name, serializer_cls = format_config
    serializer = serializer_cls()
    framer = serializer.framer_cls()
    for is_snapshot, payload, _, _ in framer.iter_records(path.read_bytes(), 0):
        if is_snapshot:
            continue
        records.append(serializer.decode(payload, type=ChangeRecord))

    assert records[0].m == first_m
    assert records[1].m is None
    assert kanta.mtime == first_m


@pytest.mark.asyncio
async def test_migration_does_not_update_mtime(tmp_path, format_config):
    path = tmp_path / "test.db"
    seed_m = datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
    seed_single_change(
        path,
        ChangeRecord(
            ts=seed_m,
            m=seed_m,
            a="seed",
            v=0,
            diff={"counter": 0},
        ),
        format_config,
    )

    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    assert kanta.mtime == seed_m

    new_m = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    with kanta.transaction(action="inc", mtime=new_m) as data:
        data.counter = 5
    await kanta.flush()

    assert kanta.mtime == new_m
    await kanta.close()


@pytest.mark.asyncio
async def test_rollback_does_not_update_mtime(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    seed_m = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    with kanta.transaction(action="seed", mtime=seed_m) as data:
        data.counter = 1

    before = kanta.mtime

    try:
        with kanta.transaction(
            action="boom", mtime=datetime(2099, 1, 1, tzinfo=UTC)
        ) as data:
            data.counter = 99
            raise RuntimeError("fail")
    except RuntimeError:
        pass

    assert kanta.data.counter == 1
    assert kanta.mtime == before
    await kanta.close()
