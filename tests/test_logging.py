import logging

import pytest

from kanta.logging import (
    configure_logging,
    format_action_header,
    log_change,
)
from kanta.tty import ESC


def test_format_action_header():
    header = format_action_header("update", "alice", "tgt")
    assert header == (
        f"{ESC}1;34mupdate{ESC}0m {ESC}38;5;250mtgt{ESC}0m by {ESC}34malice{ESC}0m"
    )


def test_format_action_header_action_only():
    assert format_action_header("update") == f"{ESC}1;34mupdate{ESC}0m"


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


def test_configure_logging_defaults():
    configure_logging()
    for name in ("kanta.bootstrap", "kanta.migration", "kanta.transaction"):
        logger = logging.getLogger(name)
        assert logger.level == logging.NOTSET  # inherits the root level
        assert not logger.propagate
        assert logger.handlers


def test_configure_logging_disables_specific_loggers():
    configure_logging(bootstrap=False, migration=False, transaction=False)
    assert logging.getLogger("kanta.bootstrap").disabled
    assert logging.getLogger("kanta.migration").disabled
    assert logging.getLogger("kanta.transaction").disabled


def test_configure_logging_skiproot_false_routes_via_root():
    configure_logging(skiproot=False)
    for name in ("kanta.bootstrap", "kanta.migration", "kanta.transaction"):
        logger = logging.getLogger(name)
        assert logger.propagate
        assert not logger.handlers


def _setup_logging(**kwargs):
    """Default kanta logging with the event loggers lifted to INFO.

    Event loggers inherit the root level (WARNING under pytest); output
    assertions need INFO.
    """
    configure_logging(**kwargs)
    for name in ("kanta.bootstrap", "kanta.migration", "kanta.transaction"):
        logging.getLogger(name).setLevel(logging.INFO)


def test_log_change_no_diff(capsys):
    _setup_logging()
    log_change("test", {})
    captured = capsys.readouterr()
    assert "test" in captured.err


def test_log_change_appends_extra_string(capsys, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    _setup_logging()
    log_change("export", {}, extra="mydb.db")
    captured = capsys.readouterr()
    assert "export" in captured.err
    assert f"{ESC}38;5;250mmydb.db{ESC}0m" in captured.err


def test_log_change_log_diff_false(capsys, monkeypatch):
    _setup_logging()

    def _boom(*args, **kwargs):
        raise AssertionError("format_diff should not be called")

    monkeypatch.setattr("kanta.logging.format_diff", _boom)
    log_change("update", {"counter": 5}, previous={}, log_diff=False)
    captured = capsys.readouterr()
    assert "update" in captured.err
    assert "counter" not in captured.err


def test_configure_logging_diff_false(capsys):
    _setup_logging(diff=False)
    log_change("update", {"counter": 5}, previous={})
    captured = capsys.readouterr()
    assert "update" in captured.err
    assert "counter" not in captured.err


def test_configure_logging_diff_true_reenables(capsys):
    _setup_logging(diff=False)
    configure_logging(diff=True)
    logging.getLogger("kanta.transaction").setLevel(logging.INFO)
    log_change("update", {"counter": 5}, previous={})
    captured = capsys.readouterr()
    assert "counter" in captured.err
