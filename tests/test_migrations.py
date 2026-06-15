from types import ModuleType, SimpleNamespace

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
