import logging
from types import ModuleType, SimpleNamespace

import pytest

from kanta.exceptions import DatabaseError
from kanta.migrations import Migrations


class _DummyKanta:
    def __init__(self):
        self.ctx = SimpleNamespace()


def test_register_and_apply():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d, kanta):
        d["version"] = 1

    @reg.register
    def migrate_v2(d, kanta):
        d["version"] = 2

    state = {}
    new_ver = reg.apply(state, current_version=0, kanta=kanta, silent=True)
    assert new_ver == 2
    assert state["version"] == 2


def test_no_migrations_needed():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d, kanta):
        d["x"] = 1

    state = {"x": 1}
    new_ver = reg.apply(state, current_version=1, kanta=kanta, silent=True)
    assert new_ver == 1


def test_from_module():
    mod = ModuleType("fake_migrations")
    kanta = _DummyKanta()

    def migrate_v1(d, kanta):
        d["v"] = 1

    def migrate_v2(d, kanta):
        d["v"] = 2

    mod.__dict__["migrate_v1"] = migrate_v1
    mod.__dict__["migrate_v2"] = migrate_v2

    reg = Migrations.from_module(mod)
    assert reg.dbver == 2

    state = {}
    new_ver = reg.apply(state, current_version=0, kanta=kanta, silent=True)
    assert new_ver == 2
    assert state["v"] == 2


def test_migrations_can_use_kanta_ctx():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d, kanta):
        kanta.ctx.source = "migration"
        d["source"] = kanta.ctx.source

    state = {}
    new_ver = reg.apply(state, current_version=0, kanta=kanta, silent=True)
    assert new_ver == 1
    assert state["source"] == "migration"
    assert kanta.ctx.source == "migration"


def test_migration_can_omit_kanta_argument():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d):
        d["x"] = 1

    state = {}
    new_ver = reg.apply(state, current_version=0, kanta=kanta, silent=True)
    assert new_ver == 1
    assert state["x"] == 1


def test_version_too_new():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d):
        d["x"] = 1

    with pytest.raises(
        DatabaseError,
        match="Database version v2 is newer than the highest supported version v1",
    ):
        reg.apply({}, current_version=2, kanta=kanta, silent=True)


def test_version_too_old():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v3(d):
        d["x"] = 3

    with pytest.raises(
        DatabaseError,
        match="Database version v1 is older than the minimum supported version v2",
    ):
        reg.apply({}, current_version=1, kanta=kanta, silent=True)


def test_missing_middle_migration_is_skipped():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d):
        d["x"] = 1

    @reg.register
    def migrate_v3(d):
        d["y"] = 3

    state = {"x": 1}
    new_ver = reg.apply(state, current_version=1, kanta=kanta, silent=True)
    assert new_ver == 3
    assert state["x"] == 1
    assert state["y"] == 3


def test_old_migrations_deleted_current_supported():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v3(d):
        d["x"] = 3

    state = {"x": 2}
    new_ver = reg.apply(state, current_version=2, kanta=kanta, silent=True)
    assert new_ver == 3
    assert state["x"] == 3


def test_migration_log_only_when_changed(caplog):
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d):
        """Set x."""
        d["x"] = 1

    @reg.register
    def migrate_v2(d):
        """No-op."""
        pass

    @reg.register
    def migrate_v3(d):
        """Set y."""
        d["y"] = 3

    with caplog.at_level(logging.INFO, logger="kanta.migrations"):
        reg.apply({}, current_version=0, kanta=kanta)

    messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert len(messages) == 2
    assert "migrate_v1" in messages[0]
    assert "Set x" in messages[0]
    assert "migrate_v3" in messages[1]
    assert "Set y" in messages[1]


def test_no_op_migration_produces_no_log(caplog):
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d):
        """No-op."""
        pass

    with caplog.at_level(logging.INFO, logger="kanta.migrations"):
        reg.apply({}, current_version=0, kanta=kanta)

    info_messages = [r for r in caplog.records if r.levelno == logging.INFO]
    assert not info_messages
