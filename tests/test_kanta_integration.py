import asyncio
import logging
import sys
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from kanta.exceptions import DatabaseError, DataIntegrityError, FileLockError
from kanta.migrations import MigrationResult
from kanta.serialization import struct_to_dict

from .support import (
    Data,
    EvolvableDataV1,
    EvolvableDataV2,
    ExoticData,
    User,
    change_actions,
    fixed_change,
    make_kanta,
    make_migrations_module,
    read_changes,
    read_last_snapshot,
    seed_single_change,
)


@pytest.mark.asyncio
async def test_load_empty(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)
    await kanta.open()
    assert isinstance(kanta.data, Data)
    assert kanta.data.users == {}
    await kanta.close()


@pytest.mark.asyncio
async def test_new_file_writes_bootstrap_record_without_handlers(
    tmp_path, format_config
):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()
    await kanta.close()

    records = read_changes(path, format_config)
    assert len(records) == 1
    assert records[0].a == "bootstrap"
    assert records[0].diff == {"$replace": {"users": {}, "counter": 0}}


@pytest.mark.asyncio
async def test_new_file_persists_initial_state_for_roundtrip(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(
        path, Data(counter=5, users={"alice": User(name="Alice")}), format_config
    )
    await kanta.open()
    await kanta.close()

    records = read_changes(path, format_config)
    assert len(records) == 1
    assert records[0].a == "bootstrap"
    assert records[0].diff == {
        "$replace": {"users": {"alice": {"name": "Alice", "age": 0}}, "counter": 5}
    }

    kanta2 = make_kanta(path, Data, format_config)
    await kanta2.open()
    assert kanta2.data.counter == 5
    assert kanta2.data.users["alice"].name == "Alice"
    await kanta2.close()


@pytest.mark.asyncio
async def test_reopen_without_changes_does_not_force_snapshot(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data(counter=5), format_config)
    await kanta.open()
    await kanta.close()

    # No snapshot should exist after the initial bootstrap and close.
    assert read_last_snapshot(path, format_config) is None

    kanta2 = make_kanta(path, Data, format_config)
    await kanta2.open()
    assert kanta2.data.counter == 5
    await kanta2.close()

    # Re-opening without migrations or normalization changes must not force one.
    assert read_last_snapshot(path, format_config) is None


@pytest.mark.asyncio
async def test_open_overwrites_caller_owned_root_data(tmp_path, format_config):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("seed", {"counter": 7}), format_config)

    root = Data(counter=99, users={"stale": User(name="Stale", age=1)})
    kanta = make_kanta(path, root, format_config)
    await kanta.open()

    assert kanta.data is root
    assert root.counter == 7
    assert root.users == {}

    await kanta.close()


@pytest.mark.asyncio
async def test_roundtrip(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    with kanta.transaction(action="inc") as data:
        data.counter = 1

    await kanta.flush()
    await kanta.close()

    kanta2 = make_kanta(path, Data, format_config)
    await kanta2.open()
    assert isinstance(kanta2.data, Data)
    assert kanta2.data.counter == 1
    await kanta2.close()


@pytest.mark.asyncio
async def test_rollback_on_error(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    try:
        with kanta.transaction(action="inc") as data:
            data.counter = 1
            raise ValueError("boom")
    except ValueError:
        pass

    assert kanta.data.counter == 0
    assert isinstance(kanta.data, Data)
    await kanta.close()


@pytest.mark.asyncio
async def test_bootstrap_creates_file(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    kanta.data = Data(counter=1)
    kanta._impl.statedict = {}

    with kanta.transaction(action="bootstrap") as data:
        data.counter = 1

    await kanta.flush()
    await kanta.close()
    assert path.exists()


@pytest.mark.asyncio
async def test_bootstrap_decorator_with_args(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.bootstrap(action="seed_init", user="system")
    def seed(data: Data):
        data.counter = 3

    await kanta.open()
    await kanta.close()

    assert change_actions(path, format_config) == ["seed_init"]


@pytest.mark.asyncio
async def test_bootstrap_decorator_without_args(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.bootstrap
    def seed(data: Data):
        data.counter = 4

    await kanta.open()
    await kanta.close()

    assert change_actions(path, format_config) == ["bootstrap"]


@pytest.mark.asyncio
async def test_bootstrap_decorator_async(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.bootstrap(action="async_seed")
    async def seed(data: Data):
        await asyncio.sleep(0)
        data.counter = 5

    await kanta.open()
    await kanta.close()

    assert change_actions(path, format_config) == ["async_seed"]


@pytest.mark.asyncio
async def test_bootstrap_decorator_multiple_handlers_in_order(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.bootstrap(action="boot_1")
    def seed_one(data: Data):
        data.counter = 1

    @kanta.bootstrap(action="boot_2")
    async def seed_two(data: Data):
        await asyncio.sleep(0)
        data.counter = 2

    await kanta.open()
    await kanta.close()

    assert change_actions(path, format_config) == ["boot_2"]


@pytest.mark.asyncio
async def test_bootstrap_failure_removes_database_file(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.bootstrap(action="boot_fail")
    def seed_fail(data: Data):
        data.counter = 10
        raise RuntimeError("bootstrap failed")

    with pytest.raises(RuntimeError, match="bootstrap failed"):
        await kanta.open()

    assert not path.exists()


@pytest.mark.asyncio
async def test_bootstrap_async_failure_removes_database_file(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.bootstrap(action="boot_fail_async")
    async def seed_fail(data: Data):
        await asyncio.sleep(0)
        data.counter = 10
        raise RuntimeError("bootstrap async failed")

    with pytest.raises(RuntimeError, match="bootstrap async failed"):
        await kanta.open()

    assert not path.exists()


@pytest.mark.asyncio
async def test_open_create_false_missing_file_fails(tmp_path, format_config):
    path = tmp_path / "missing.db"
    kanta = make_kanta(path, Data, format_config)

    with pytest.raises(FileLockError):
        await kanta.open(create=False)


@pytest.mark.asyncio
async def test_open_create_false_empty_file_fails(tmp_path, format_config):
    path = tmp_path / "empty.db"
    path.touch()
    kanta = make_kanta(path, Data, format_config)

    with pytest.raises(DataIntegrityError, match="empty"):
        await kanta.open(create=False)


@pytest.mark.asyncio
async def test_background_write_failure_notifies_decorator_callback(
    tmp_path, format_config, monkeypatch
):
    path = tmp_path / "test.db"
    errors: list[DatabaseError] = []
    signaled = asyncio.Event()

    kanta = make_kanta(
        path,
        Data,
        format_config,
        flush_interval=0.01,
    )

    @kanta.fatal_error
    async def on_fatal_error(err: DatabaseError) -> None:
        errors.append(err)
        signaled.set()

    await kanta.open()

    with kanta.transaction(action="inc") as data:
        data.counter = 1

    def fail_write(_data: bytes) -> None:
        raise OSError("simulated background write failure")

    monkeypatch.setattr(kanta._impl.file, "write", fail_write)

    await asyncio.wait_for(signaled.wait(), timeout=1.0)
    assert errors
    assert "Failed to flush database" in str(errors[0])

    await kanta.close()


@pytest.mark.asyncio
async def test_background_write_failure_notifies_multiple_callbacks_in_order(
    tmp_path, format_config, monkeypatch
):
    path = tmp_path / "test.db"
    calls: list[str] = []
    signaled = asyncio.Event()

    kanta = make_kanta(
        path,
        Data,
        format_config,
        flush_interval=0.01,
    )

    @kanta.fatal_error
    def on_fatal_error_sync(err: DatabaseError) -> None:
        calls.append("sync")

    @kanta.fatal_error
    async def on_fatal_error_async(err: DatabaseError) -> None:
        await asyncio.sleep(0)
        calls.append("async")
        signaled.set()

    await kanta.open()

    with kanta.transaction(action="inc") as data:
        data.counter = 1

    def fail_write(_data: bytes) -> None:
        raise OSError("simulated background write failure")

    monkeypatch.setattr(kanta._impl.file, "write", fail_write)

    await asyncio.wait_for(signaled.wait(), timeout=1.0)
    assert calls == ["sync", "async"]

    await kanta.close()


@pytest.mark.asyncio
async def test_snapshot(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config, flush_interval=0.01)
    await kanta.open()

    kanta.data = Data(counter=1)
    kanta._impl.statedict = struct_to_dict(kanta.data)
    kanta._impl.snapshot._min_diffs = 1
    kanta._impl.snapshot.request_force()

    with kanta.transaction(action="inc") as data:
        data.counter = 2

    await kanta.flush()
    await asyncio.sleep(0.05)
    await kanta.close()

    data = path.read_bytes()
    _, serializer_cls = format_config
    framer = serializer_cls().framer_cls()
    snap_payload, _, _ = framer.scan_last_snapshot(data)
    assert snap_payload is not None


@pytest.mark.asyncio
async def test_nested_struct_roundtrip(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    with kanta.transaction(action="create_user") as data:
        data.users["alice"] = User(name="Alice", age=30)

    await kanta.flush()
    await kanta.close()

    kanta2 = make_kanta(path, Data, format_config)
    await kanta2.open()
    assert isinstance(kanta2.data, Data)
    assert kanta2.data.users["alice"].name == "Alice"
    assert kanta2.data.users["alice"].age == 30

    with kanta2.transaction(action="update_user") as data:
        data.users["alice"].age = 31

    await kanta2.flush()
    await kanta2.close()

    kanta3 = make_kanta(path, Data, format_config)
    await kanta3.open()
    assert isinstance(kanta3.data, Data)
    assert kanta3.data.users["alice"].name == "Alice"
    assert kanta3.data.users["alice"].age == 31
    await kanta3.close()


@pytest.mark.asyncio
async def test_background_flush(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config, flush_interval=0.01)
    await kanta.open()

    with kanta.transaction(action="inc") as data:
        data.counter = 1

    await asyncio.sleep(0.05)
    await kanta.close()

    assert path.exists()
    reloaded = make_kanta(path, Data, format_config)
    await reloaded.open()
    assert reloaded.data.counter == 1
    await reloaded.close()


@pytest.mark.asyncio
async def test_async_with_open_close(tmp_path, format_config):
    path = tmp_path / "test.db"

    async with make_kanta(path, Data, format_config) as kanta:
        with kanta.transaction(action="inc") as data:
            data.counter = 1

    assert path.exists()
    reloaded = make_kanta(path, Data, format_config)
    await reloaded.open()
    assert reloaded.data.counter == 1
    await reloaded.close()


@pytest.mark.asyncio
async def test_open_twice_raises(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    await kanta.open()
    with pytest.raises(DataIntegrityError, match="already open"):
        await kanta.open()
    await kanta.close()


@pytest.mark.asyncio
async def test_migrations_from_module(tmp_path, format_config):
    path = tmp_path / "test.db"

    mod = type(sys)("test_migrations")

    def migrate_v1(d, kanta):
        d["version"] = 1

    mod.__dict__["migrate_v1"] = migrate_v1

    seed_single_change(path, fixed_change("init", {"counter": 0}), format_config)

    kanta = make_kanta(path, Data, format_config, migrations=mod)
    await kanta.open()
    assert kanta.version == 1
    await kanta.close()


@pytest.mark.asyncio
async def test_msgspec_normalization_logs_migration(tmp_path, format_config):
    path = tmp_path / "test.db"

    seed_single_change(
        path,
        fixed_change("seed", {"users": {"alice": {"name": "Alice", "age": 30}}}),
        format_config,
    )

    kanta = make_kanta(path, Data, format_config)
    await kanta.open()
    await kanta.close()

    assert "migrate:msgspec" in change_actions(path, format_config)


@pytest.mark.asyncio
async def test_empty_migration_writes_snapshot_and_is_not_reapplied(
    tmp_path, format_config
):
    path = tmp_path / "test.db"
    seed_single_change(
        path, fixed_change("init", {"counter": 0, "users": {}}), format_config
    )

    def migrate_v1(d, kanta):
        """No-op migration that only bumps the schema version."""
        pass

    mod = make_migrations_module("empty_migration_mod", "migrate_v1", migrate_v1)

    try:
        kanta = make_kanta(path, Data, format_config, migrations=mod)
        await kanta.open()
        assert kanta.version == 1
        await kanta.close()

        # Empty migrations must not produce empty change records.
        records = read_changes(path, format_config)
        migration_records = [r for r in records if r.a.startswith("migrate")]
        assert not migration_records

        # The version bump is persisted via a snapshot instead.
        snap = read_last_snapshot(path, format_config)
        assert snap is not None
        assert snap.v == 1
        assert snap.state == {"counter": 0, "users": {}}

        kanta2 = make_kanta(path, Data, format_config, migrations=mod)
        await kanta2.open()
        assert kanta2.version == 1
        await kanta2.close()

        # Re-opening must not create additional migration records or snapshots.
        records2 = read_changes(path, format_config)
        assert not [r for r in records2 if r.a.startswith("migrate")]
    finally:
        sys.modules.pop("empty_migration_mod", None)


@pytest.mark.asyncio
async def test_migration_with_changes_records_diff_and_snapshot(
    tmp_path, format_config
):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("init", {"counter": 0}), format_config)

    mod = type(sys)("test_migrations_changes")

    def migrate_v1(d, kanta):
        d["counter"] = 2

    mod.__dict__["migrate_v1"] = migrate_v1

    kanta = make_kanta(path, Data, format_config, migrations=mod)
    await kanta.open()
    assert kanta.version == 1
    assert kanta.data.counter == 2
    await kanta.close()

    records = read_changes(path, format_config)
    migration_records = [r for r in records if r.a.startswith("migrate")]
    # The version migration and the msgspec normalization that follows it are
    # grouped into a single migrate:vN record.
    assert len(migration_records) == 1
    assert migration_records[0].a == "migrate:v1"
    assert migration_records[0].v == 1
    assert migration_records[0].diff == {"counter": 2, "users": {}}

    snap = read_last_snapshot(path, format_config)
    assert snap is not None
    assert snap.v == 1
    assert snap.state == {"counter": 2, "users": {}}


@pytest.mark.asyncio
async def test_migration_summary_log_includes_filename(tmp_path, format_config, caplog):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("init", {"counter": 0}), format_config)

    mod = type(sys)("test_migrations_log")

    def migrate_v1(d, kanta):
        """Bump counter."""
        d["counter"] = 2

    mod.__dict__["migrate_v1"] = migrate_v1

    with caplog.at_level(logging.INFO, logger="kanta.migration"):
        kanta = make_kanta(path, Data, format_config, migrations=mod)
        await kanta.open()
        assert kanta.version == 1
        await kanta.close()

    info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_messages) == 1
    assert str(path) in info_messages[0]
    assert "v0 -> v1" in info_messages[0]
    assert "migrate_v1 (Bump counter)" in info_messages[0]


@pytest.mark.asyncio
async def test_open_log_false_suppresses_migration_log(tmp_path, format_config, caplog):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("init", {"counter": 0}), format_config)

    mod = type(sys)("test_migrations_silent")

    def migrate_v1(d, kanta):
        d["counter"] = 2

    mod.__dict__["migrate_v1"] = migrate_v1

    with caplog.at_level(logging.INFO, logger="kanta.migration"):
        kanta = make_kanta(path, Data, format_config, migrations=mod)
        await kanta.open(log=False)
        await kanta.close()

    info_messages = [r for r in caplog.records if r.levelno == logging.INFO]
    assert not info_messages


@pytest.mark.asyncio
async def test_open_log_true_logs_bootstrap(tmp_path, format_config, caplog):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    with caplog.at_level(logging.INFO, logger="kanta.bootstrap"):
        await kanta.open()
        await kanta.close()

    info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_messages) >= 2
    assert "created" in info_messages[0]
    assert "bootstrap" in info_messages[1]


@pytest.mark.asyncio
async def test_open_log_false_suppresses_bootstrap_log(tmp_path, format_config, caplog):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    with caplog.at_level(logging.INFO, logger="kanta.bootstrap"):
        await kanta.open(log=False)
        await kanta.close()

    info_messages = [r for r in caplog.records if r.levelno == logging.INFO]
    assert not info_messages


@pytest.mark.asyncio
async def test_open_log_custom_logger_logs_bootstrap(tmp_path, format_config, caplog):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    custom_logger = logging.getLogger("custom.bootstrap")
    custom_logger.setLevel(logging.INFO)

    with caplog.at_level(logging.INFO, logger="custom.bootstrap"):
        await kanta.open(log=custom_logger)
        await kanta.close()

    info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_messages) >= 2
    assert "created" in info_messages[0]
    assert "bootstrap" in info_messages[1]


@pytest.mark.asyncio
async def test_open_existing_database_logs_using_on_debug(
    tmp_path, format_config, caplog
):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()
    await kanta.close()

    kanta2 = make_kanta(path, Data, format_config)

    with caplog.at_level(logging.DEBUG, logger="kanta.bootstrap"):
        await kanta2.open()
        await kanta2.close()

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("opened" in m and str(path.resolve()) in m for m in debug_messages)


@pytest.mark.asyncio
async def test_logmigr_callback_replaces_default_logging(
    tmp_path, format_config, caplog
):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("init", {"counter": 0}), format_config)

    mod = type(sys)("test_migrations_callback")

    def migrate_v1(d, kanta):
        """Bump counter."""
        d["counter"] = 2

    mod.__dict__["migrate_v1"] = migrate_v1

    summaries = []

    kanta = make_kanta(path, Data, format_config, migrations=mod)

    @kanta.logmigr
    def collect(summary: MigrationResult):
        summaries.append(summary)

    with caplog.at_level(logging.INFO, logger="kanta.migration"):
        await kanta.open()
        await kanta.close()

    assert len(summaries) == 1
    assert summaries[0].version == 1
    assert summaries[0].migrations[0].name == "migrate_v1"
    info_messages = [r for r in caplog.records if r.levelno == logging.INFO]
    assert not info_messages


@pytest.mark.asyncio
async def test_logmigr_callback_report(tmp_path, format_config, caplog):
    import logging

    from kanta import MigrationReport

    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("init", {"counter": 0}), format_config)

    mod = type(sys)("test_migrations_report")

    def migrate_v1(d):
        """Bump counter."""
        d["counter"] = 2

    mod.__dict__["migrate_v1"] = migrate_v1

    reports = []

    kanta = make_kanta(path, Data, format_config, migrations=mod)

    @kanta.logmigr
    def collect(report: MigrationReport):
        reports.append(report)

    with caplog.at_level(logging.INFO, logger="kanta.migration"):
        await kanta.open()
        await kanta.close()

    assert len(reports) == 1
    assert reports[0].original == 0
    assert reports[0].version == 1
    assert [m.name for m in reports[0].applied] == ["migrate_v1"]


@pytest.mark.asyncio
async def test_transaction_log_false_suppresses_log(tmp_path, format_config, caplog):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    with caplog.at_level(logging.INFO, logger="kanta.transaction"):
        with kanta.transaction(action="inc", log=False) as data:
            data.counter = 1

    await kanta.close()

    info_messages = [r for r in caplog.records if r.levelno == logging.INFO]
    assert not info_messages


@pytest.mark.asyncio
async def test_transaction_logdiff_false_logs_header_only(
    tmp_path, format_config, caplog
):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    with caplog.at_level(logging.INFO, logger="kanta.transaction"):
        with kanta.transaction(action="inc", logdiff=False) as data:
            data.counter = 1

    await kanta.close()

    messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert len(messages) == 1
    assert "inc" in messages[0]
    assert "counter" not in messages[0]


@pytest.mark.asyncio
async def test_transaction_log_custom_logger(tmp_path, format_config, caplog):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    custom_logger = logging.getLogger("custom.transaction")
    custom_logger.setLevel(logging.INFO)

    with caplog.at_level(logging.INFO, logger="custom.transaction"):
        with kanta.transaction(action="inc", log=custom_logger) as data:
            data.counter = 1

    await kanta.close()

    info_messages = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_messages) >= 1
    assert "inc" in info_messages[0].message


@pytest.mark.asyncio
async def test_open_locked_file_raises_filelock_error(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta1 = make_kanta(path, Data, format_config)
    await kanta1.open()
    kanta2 = make_kanta(path, Data, format_config)
    try:
        with pytest.raises(FileLockError):
            await kanta2.open()
    finally:
        await kanta1.close()


@pytest.mark.asyncio
async def test_flush_write_failure_bubbles_database_error(
    tmp_path, format_config, monkeypatch
):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    await kanta.open()

    with kanta.transaction(action="inc") as data:
        data.counter = 1

    def fail_write(_data: bytes) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(kanta._impl.file, "write", fail_write)

    with pytest.raises(DatabaseError, match="Failed to flush database"):
        await kanta.flush()

    await kanta.close()


@pytest.mark.asyncio
async def test_background_write_failure_notifies_callback(
    tmp_path, format_config, monkeypatch
):
    path = tmp_path / "test.db"
    errors: list[DatabaseError] = []
    signaled = asyncio.Event()

    kanta = make_kanta(
        path,
        Data,
        format_config,
        flush_interval=0.01,
    )

    @kanta.fatal_error
    def on_fatal_error(err: DatabaseError) -> None:
        errors.append(err)
        signaled.set()

    await kanta.open()

    with kanta.transaction(action="inc") as data:
        data.counter = 1

    def fail_write(_data: bytes) -> None:
        raise OSError("simulated background write failure")

    monkeypatch.setattr(kanta._impl.file, "write", fail_write)

    await asyncio.wait_for(signaled.wait(), timeout=1.0)
    assert errors
    assert "Failed to flush database" in str(errors[0])
    assert kanta._impl.background_error is not None

    await kanta.close()


@pytest.mark.asyncio
async def test_migrations_from_module_path(tmp_path, format_config):
    path = tmp_path / "test.db"

    module_name = "test_migrations_path"
    mod = type(sys)(module_name)

    def migrate_v1(d, kanta):
        d["counter"] = 2

    mod.__dict__["migrate_v1"] = migrate_v1
    sys.modules[module_name] = mod

    seed_single_change(path, fixed_change("init", {"counter": 0}), format_config)

    try:
        kanta = make_kanta(path, Data, format_config, migrations=module_name)
        await kanta.open()
        assert kanta.version == 1
        assert kanta.data.counter == 2
        await kanta.close()
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.asyncio
async def test_uuid_datetime_bytes_keys_and_values_roundtrip(tmp_path, format_config):

    path = tmp_path / "test.db"
    kanta = make_kanta(path, ExoticData, format_config)
    await kanta.open()

    u = uuid4()
    dt = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    bkey = b"blob-key"
    bval = b"blob-value"

    with kanta.transaction(action="set_exotic") as data:
        data.uuid_values["u"] = u
        data.uuid_keys[u] = 1
        data.datetime_values["ts"] = dt
        data.datetime_keys[dt] = 2
        data.bytes_values["blob"] = bval
        data.bytes_keys[bkey] = 3

    await kanta.flush()
    await kanta.close()

    reloaded = make_kanta(path, ExoticData, format_config)
    await reloaded.open()
    assert reloaded.data.uuid_values["u"] == u
    assert reloaded.data.uuid_keys[u] == 1
    assert reloaded.data.datetime_values["ts"] == dt
    assert reloaded.data.datetime_keys[dt] == 2
    assert reloaded.data.bytes_values["blob"] == bval
    assert reloaded.data.bytes_keys[bkey] == 3
    await reloaded.close()


@pytest.mark.asyncio
async def test_schema_evolution_add_default_field_logs_migration(
    tmp_path, format_config
):
    path = tmp_path / "test.db"

    kanta_v1 = make_kanta(path, EvolvableDataV1, format_config)
    await kanta_v1.open()
    with kanta_v1.transaction(action="seed") as data:
        data.counter = 1
    await kanta_v1.flush()
    await kanta_v1.close()

    kanta_v2 = make_kanta(path, EvolvableDataV2, format_config)
    await kanta_v2.open()
    assert kanta_v2.data.counter == 1
    assert kanta_v2.data.enabled is True
    await kanta_v2.close()

    assert "migrate:msgspec" in change_actions(path, format_config)
