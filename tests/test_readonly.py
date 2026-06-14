"""Tests for Kanta read-only mode."""

import pytest

from kanta.exceptions import DataIntegrityError, FileLockError
from kanta.serialization import struct_to_dict

from .support import (
    Data,
    EvolvableDataV2,
    fixed_change,
    make_kanta,
    make_migrations_module,
    seed_single_change,
)


@pytest.mark.asyncio
async def test_readonly_opens_existing_database(tmp_path, format_config):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("seed", {"counter": 7}), format_config)

    kanta = make_kanta(path, Data, format_config)
    await kanta.open(readonly=True)

    assert isinstance(kanta.data, Data)
    assert kanta.data.counter == 7
    assert kanta._impl.readonly is True
    assert kanta._impl.background_task is None

    await kanta.close()


@pytest.mark.asyncio
async def test_readonly_missing_file_fails(tmp_path, format_config):
    path = tmp_path / "missing.db"
    kanta = make_kanta(path, Data, format_config)

    with pytest.raises(FileLockError):
        await kanta.open(readonly=True)

    assert not path.exists()


@pytest.mark.asyncio
async def test_readonly_empty_file_fails(tmp_path, format_config):
    path = tmp_path / "empty.db"
    path.touch()
    kanta = make_kanta(path, Data, format_config)

    with pytest.raises(DataIntegrityError, match="empty"):
        await kanta.open(readonly=True)


@pytest.mark.asyncio
async def test_readonly_transaction_fails(tmp_path, format_config):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("seed", {"counter": 1}), format_config)

    kanta = make_kanta(path, Data, format_config)
    await kanta.open(readonly=True)

    with pytest.raises(DataIntegrityError, match="read-only"):
        with kanta.transaction(action="inc") as data:
            data.counter = 2

    # In-memory state must remain unchanged.
    assert kanta.data.counter == 1
    await kanta.close()


@pytest.mark.asyncio
async def test_readonly_flush_fails(tmp_path, format_config):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("seed", {"counter": 1}), format_config)

    kanta = make_kanta(path, Data, format_config)
    await kanta.open(readonly=True)

    with pytest.raises(DataIntegrityError, match="read-only"):
        await kanta.flush()

    await kanta.close()


@pytest.mark.asyncio
async def test_readonly_create_true_does_not_create_file(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    with pytest.raises(FileLockError):
        await kanta.open(create=True, readonly=True)

    assert not path.exists()


@pytest.mark.asyncio
async def test_readonly_does_not_persist_changes(tmp_path, format_config):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("seed", {"counter": 1}), format_config)
    original_content = path.read_bytes()

    kanta = make_kanta(path, Data, format_config)
    await kanta.open(readonly=True)
    await kanta.close()

    assert path.read_bytes() == original_content


@pytest.mark.asyncio
async def test_readonly_runs_migrations(tmp_path, format_config):
    path = tmp_path / "test.db"
    seed_single_change(
        path,
        fixed_change("seed", {"counter": 1}, version=0),
        format_config,
    )

    def migrate_v1(data, kanta):
        data.setdefault("enabled", True)

    migrations = make_migrations_module("readonly_migrations", "migrate_v1", migrate_v1)

    kanta = make_kanta(path, EvolvableDataV2, format_config, migrations=migrations)
    await kanta.open(readonly=True)

    assert kanta.data.counter == 1
    # Migration ran in memory even though no change was persisted.
    assert struct_to_dict(kanta.data, serializer=kanta._impl.serializer) == {
        "counter": 1,
        "enabled": True,
    }
    assert not kanta._impl.pending_changes

    await kanta.close()


@pytest.mark.asyncio
async def test_readwrite_and_readonly_can_open_together(tmp_path, format_config):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("seed", {"counter": 1}), format_config)

    rw = make_kanta(path, Data, format_config)
    await rw.open()

    ro = make_kanta(path, Data, format_config)
    await ro.open(readonly=True)

    assert rw.data.counter == 1
    assert ro.data.counter == 1

    await ro.close()
    await rw.close()
