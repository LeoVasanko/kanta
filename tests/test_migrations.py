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
    result = reg.apply(state, current_version=0, kanta=kanta)
    assert result.version == 2
    assert state["version"] == 2


def test_no_migrations_needed():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d, kanta):
        d["x"] = 1

    state = {"x": 1}
    result = reg.apply(state, current_version=1, kanta=kanta)
    assert result.version == 1


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
    result = reg.apply(state, current_version=0, kanta=kanta)
    assert result.version == 2
    assert state["v"] == 2


def test_migrations_can_use_kanta_ctx():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d, kanta):
        kanta.ctx.source = "migration"
        d["source"] = kanta.ctx.source

    state = {}
    result = reg.apply(state, current_version=0, kanta=kanta)
    assert result.version == 1
    assert state["source"] == "migration"
    assert kanta.ctx.source == "migration"


def test_migration_can_omit_kanta_argument():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d):
        d["x"] = 1

    state = {}
    result = reg.apply(state, current_version=0, kanta=kanta)
    assert result.version == 1
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
        reg.apply({}, current_version=2, kanta=kanta)


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
        reg.apply({}, current_version=1, kanta=kanta)


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
    result = reg.apply(state, current_version=1, kanta=kanta)
    assert result.version == 3
    assert state["x"] == 1
    assert state["y"] == 3


def test_old_migrations_deleted_current_supported():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v3(d):
        d["x"] = 3

    state = {"x": 2}
    result = reg.apply(state, current_version=2, kanta=kanta)
    assert result.version == 3
    assert state["x"] == 3


def test_apply_returns_change_information():
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

    result = reg.apply({}, current_version=0, kanta=kanta)
    assert result.version == 3
    assert len(result.migrations) == 3

    assert result.migrations[0].name == "migrate_v1"
    assert result.migrations[0].description == "Set x"
    assert result.migrations[0].changed is True
    assert result.migrations[0].diff == {"x": 1}

    assert result.migrations[1].name == "migrate_v2"
    assert result.migrations[1].description == "No-op"
    assert result.migrations[1].changed is False
    assert result.migrations[1].diff is None

    assert result.migrations[2].name == "migrate_v3"
    assert result.migrations[2].description == "Set y"
    assert result.migrations[2].changed is True
    assert result.migrations[2].diff == {"y": 3}


def test_description_defaults_to_version_when_no_docstring():
    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d):
        d["x"] = 1

    result = reg.apply({}, current_version=0, kanta=kanta)
    assert result.migrations[0].description == "v1"


def test_report_fields():
    from kanta import MigrationReport

    reg = Migrations()
    kanta = _DummyKanta()

    @reg.register
    def migrate_v1(d):
        d["x"] = 1

    report = reg.apply({"x": 0}, current_version=0, kanta=kanta)
    assert isinstance(report, MigrationReport)
    assert report.original == 0
    assert report.version == 1
    assert [m.name for m in report.applied] == ["migrate_v1"]
    # Deprecated alias still works.
    assert report.migrations is report.applied
