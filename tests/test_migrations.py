from types import ModuleType

from kanta.migrate import MigrationRegistry


def test_register_and_apply():
    reg = MigrationRegistry()

    @reg.register
    def migrate_v1(d, ctx):
        d["version"] = 1

    @reg.register
    def migrate_v2(d, ctx):
        d["version"] = 2

    state = {}
    new_ver = reg.apply(state, current_version=0, silent=True)
    assert new_ver == 2
    assert state["version"] == 2


def test_no_migrations_needed():
    reg = MigrationRegistry()

    @reg.register
    def migrate_v1(d, ctx):
        d["x"] = 1

    state = {"x": 1}
    new_ver = reg.apply(state, current_version=1, silent=True)
    assert new_ver == 1


def test_from_module():
    mod = ModuleType("fake_migrations")

    def migrate_v1(d, ctx):
        d["v"] = 1

    def migrate_v2(d, ctx):
        d["v"] = 2

    mod.__dict__["migrate_v1"] = migrate_v1
    mod.__dict__["migrate_v2"] = migrate_v2

    reg = MigrationRegistry.from_module(mod)
    assert reg.dbver == 2

    state = {}
    new_ver = reg.apply(state, current_version=0, silent=True)
    assert new_ver == 2
    assert state["v"] == 2
