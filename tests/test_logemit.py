import logging
import sys

import pytest

from kanta.logging import (
    LogEvent,
    bootstrap_logger,
    configure_logging,
    emit_event,
    log_change,
    migration_logger,
    transaction_logger,
)
from kanta.migrations import MigrationResult
from tests.support import (
    Data,
    fixed_change,
    make_kanta,
    seed_single_change,
)


@pytest.fixture(autouse=True)
def _reset_kanta_loggers():
    yield
    for name in (
        "kanta",
        "kanta.transaction",
        "kanta.transaction.diff",
        "kanta.bootstrap",
        "kanta.migration",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
        logger.disabled = False
        logger.handlers.clear()


def _setup_logging(**kwargs):
    """Default kanta logging with the event loggers lifted to INFO.

    Event loggers inherit the root level (WARNING under pytest); output
    assertions need INFO.
    """
    configure_logging(**kwargs)
    for name in ("kanta.bootstrap", "kanta.migration", "kanta.transaction"):
        logging.getLogger(name).setLevel(logging.INFO)


def _change_event(**kwargs) -> LogEvent:
    return LogEvent(kind="change", logger=transaction_logger, action="update", **kwargs)


def test_emit_event_falsy_return_stops_chain(capsys):
    _setup_logging()
    calls = []

    def first(ev):
        calls.append("first")
        return None

    def second(ev):
        calls.append("second")

    emit_event(_change_event(), [first, second])
    assert calls == ["first"]
    assert capsys.readouterr().err == ""


def test_emit_event_truthy_return_falls_back_to_default(capsys):
    _setup_logging()
    emit_event(_change_event(), [lambda ev: True])
    assert "update" in capsys.readouterr().err


def test_emit_event_mutation_reaches_later_handlers_and_default(capsys):
    _setup_logging()
    calls = []

    def first(ev):
        calls.append("first")
        ev.extra = "tgt"
        return True

    def second(ev):
        calls.append(("second", ev.extra))
        return True

    emit_event(_change_event(), [first, second])
    assert calls == ["first", ("second", "tgt")]
    assert "tgt" in capsys.readouterr().err


def test_emit_event_handler_error_falls_back_to_default(capsys):
    _setup_logging()

    def boom(ev):
        raise RuntimeError("broken")

    emit_event(_change_event(), [boom])
    assert "update" in capsys.readouterr().err


def test_diff_lines_built_lazily(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("format_diff should not be called")

    monkeypatch.setattr("kanta.logging.format_diff", _boom)
    ev = _change_event(diff={"counter": 1})
    emit_event(ev, [lambda ev: None])  # handled without touching the diff
    monkeypatch.undo()
    assert len(ev.diff_lines) == 1
    assert "counter" in ev.diff_lines[0]


def test_default_emit_created_and_migrated(capsys):
    _setup_logging()
    emit_event(LogEvent(kind="created", logger=bootstrap_logger, filename="x.kantadb"))
    emit_event(
        LogEvent(
            kind="migrated",
            logger=migration_logger,
            filename="x.kantadb",
            from_version=0,
            to_version=1,
            migrations=["migrate_v1 (rename)"],
        )
    )
    err = capsys.readouterr().err
    assert "🛢️ x.kantadb created" in err
    assert "🛢️ x.kantadb migrated v0 -> v1: migrate_v1 (rename)" in err


def test_default_emit_strips_ansi_without_color_support(capsys, monkeypatch):
    """NO_COLOR output contains no ANSI codes; FORCE_COLOR keeps them."""
    _setup_logging()
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    emit_event(_change_event(diff={"counter": 1}))
    err = capsys.readouterr().err
    assert "\x1b[" not in err
    assert "counter" in err

    _setup_logging()
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    emit_event(_change_event(diff={"counter": 1}))
    assert "\x1b[" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_logemit_receives_transaction_events(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    events = []
    kanta.logemit(lambda ev: events.append(ev) or True)
    await kanta.open()

    with kanta.transaction(action="inc", user="u1", extra="x") as data:
        data.counter = 1

    await kanta.close()

    change = events[-1]
    assert change.kind == "change"
    assert change.action == "inc"
    assert change.user == "u1"
    assert change.extra == "x"
    assert change.diff == {"counter": 1}
    assert change.logger.name == "kanta.transaction"


def test_logemit_rejects_classes_and_async(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    class NotAFunction:
        pass

    with pytest.raises(TypeError):
        kanta.logemit(NotAFunction)

    async def ahandler(ev):
        return None

    with pytest.raises(TypeError):
        kanta.logemit(ahandler)


def _raise(*args, **kwargs):
    raise RuntimeError("formatting broken")


def test_log_change_never_raises(monkeypatch):
    monkeypatch.setattr("kanta.logging.format_action_header", _raise)
    log_change("update", {"counter": 1}, previous={})  # must not raise


@pytest.mark.asyncio
async def test_logging_failure_does_not_break_transaction(
    tmp_path, format_config, monkeypatch
):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    kanta.logemit(_raise)
    monkeypatch.setattr("kanta.logging.format_action_header", _raise)
    await kanta.open()

    with kanta.transaction(action="inc") as data:
        data.counter = 1

    await kanta.close()

    kanta2 = make_kanta(path, Data, format_config)
    kanta2.logemit(_raise)
    monkeypatch.setattr("kanta.logging.format_action_header", _raise)
    await kanta2.open()
    assert kanta2.data.counter == 1
    await kanta2.close()


@pytest.mark.asyncio
async def test_logfmt_failure_falls_back_to_default(tmp_path, format_config, caplog):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt
    def bad(value: str, path: str) -> str | None:
        raise RuntimeError("broken")

    await kanta.open()
    with caplog.at_level(logging.INFO, logger="kanta.transaction"):
        with kanta.transaction(action="inc", user="alice") as data:
            data.counter = 1
    await kanta.close()

    assert kanta.data.counter == 1
    assert "alice" in caplog.text  # raw rendering used despite the failure
    assert "counter" in caplog.text


@pytest.mark.asyncio
async def test_logmigr_failure_does_not_break_open(tmp_path, format_config):
    path = tmp_path / "test.db"
    seed_single_change(path, fixed_change("init", {"counter": 0}), format_config)

    mod = type(sys)("test_migrations_broken_logmigr")

    def migrate_v1(d, kanta):
        """Bump counter."""
        d["counter"] = 2

    mod.__dict__["migrate_v1"] = migrate_v1

    kanta = make_kanta(path, Data, format_config, migrations=mod)

    @kanta.logmigr
    def bad(summary: MigrationResult) -> None:
        raise RuntimeError("broken")

    await kanta.open()
    assert kanta.data.counter == 2
    await kanta.close()


@pytest.mark.asyncio
async def test_aborted_transaction_emits_event(
    tmp_path, format_config, caplog, monkeypatch
):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    events = []
    kanta.logemit(lambda ev: events.append(ev) or True)
    await kanta.open()

    with caplog.at_level(logging.WARNING, logger="kanta.transaction"):
        with pytest.raises(ValueError):
            with kanta.transaction(action="reset") as data:
                data.counter = 99
                raise ValueError("simulated failure")

    await kanta.close()

    aborted = events[-1]
    assert aborted.kind == "aborted"
    assert aborted.action == "reset"
    assert aborted.level == logging.WARNING
    assert isinstance(aborted.error, ValueError)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("\x1b[1;34mreset" in m for m in messages)  # action color, no quotes
    assert any(" transaction aborted: simulated failure" in m for m in messages)
    assert kanta.data.counter == 0  # rolled back


@pytest.mark.asyncio
async def test_aborted_transaction_includes_resolved_user(
    tmp_path, format_config, caplog
):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt
    def resolve(value: str, path: str) -> str | None:
        return "Alice" if value == "u1" else None

    await kanta.open()
    with caplog.at_level(logging.WARNING, logger="kanta.transaction"):
        with pytest.raises(ValueError):
            with kanta.transaction(action="reset", user="u1", extra="exp") as data:
                data.counter = 99
                raise ValueError("boom")
    await kanta.close()

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("exp" in m for m in messages)
    assert any(" by " in m and "Alice" in m for m in messages)
    assert any(" transaction aborted: boom" in m for m in messages)


def test_event_header_covers_all_kinds():
    created = LogEvent(kind="created", logger=transaction_logger, filename="x.db")
    assert created.header == "🛢️ x.db created"

    migrated = LogEvent(
        kind="migrated",
        logger=transaction_logger,
        filename="x.db",
        from_version=0,
        to_version=1,
        migrations=["migrate_v1 (rename)"],
    )
    assert migrated.header == "🛢️ x.db migrated v0 -> v1: migrate_v1 (rename)"

    aborted = LogEvent(
        kind="aborted",
        logger=transaction_logger,
        action="reset",
        user="alice",
        error=ValueError("boom"),
    )
    assert "transaction aborted: boom" in aborted.header
    assert "alice" in aborted.header


@pytest.mark.asyncio
async def test_event_carries_kanta_instance(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    events = []
    kanta.logemit(lambda ev: events.append(ev) or True)
    await kanta.open()
    with kanta.transaction(action="inc") as data:
        data.counter = 1
    await kanta.close()

    assert events
    assert all(ev.kanta is kanta for ev in events)


def test_header_is_settable_and_used_by_default_emit(capsys):
    _setup_logging()

    def restyle(ev):
        ev.header = f"CUSTOM {ev.action}"
        return True

    emit_event(_change_event(diff={"counter": 1}, previous={}), [restyle])
    err = capsys.readouterr().err
    assert "CUSTOM update" in err
    assert "counter" in err  # default diff routing still applies


@pytest.mark.asyncio
async def test_ctx_reachable_from_event(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    kanta.ctx.connection_id = 7
    seen = []
    kanta.logemit(lambda ev: seen.append(ev.kanta.ctx.connection_id) or True)
    await kanta.open()
    with kanta.transaction(action="inc") as data:
        data.counter = 1
    await kanta.close()

    assert seen and all(connection_id == 7 for connection_id in seen)
