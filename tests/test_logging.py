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
    kanta_logger = logging.getLogger("kanta")
    configure_logging()
    assert kanta_logger.level == logging.INFO
    assert not kanta_logger.propagate
    assert kanta_logger.handlers


def test_configure_logging_disables_specific_loggers():
    configure_logging(bootstrap=False, migration=False, transaction=False)
    assert not logging.getLogger("kanta.bootstrap").propagate
    assert not logging.getLogger("kanta.migration").propagate
    assert not logging.getLogger("kanta.transaction").propagate


def test_configure_logging_skiproot_false_leaves_kanta_propagation():
    kanta_logger = logging.getLogger("kanta")
    kanta_logger.handlers.clear()
    configure_logging(bootstrap=False, skiproot=False)
    assert kanta_logger.propagate
    assert not kanta_logger.handlers
    assert not logging.getLogger("kanta.bootstrap").propagate


def test_log_change_no_diff(capsys):
    kanta_logger = logging.getLogger("kanta")
    kanta_logger.handlers.clear()
    configure_logging()
    log_change("test", {})
    captured = capsys.readouterr()
    assert "test" in captured.err


def test_log_change_appends_extra_string(capsys, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    kanta_logger = logging.getLogger("kanta")
    kanta_logger.handlers.clear()
    configure_logging()
    log_change("export", {}, extra="mydb.db")
    captured = capsys.readouterr()
    assert "export" in captured.err
    assert f"{ESC}38;5;250mmydb.db{ESC}0m" in captured.err


def test_log_change_log_diff_false(capsys, monkeypatch):
    kanta_logger = logging.getLogger("kanta")
    kanta_logger.handlers.clear()
    configure_logging()

    def _boom(*args, **kwargs):
        raise AssertionError("format_diff should not be called")

    monkeypatch.setattr("kanta.logging.format_diff", _boom)
    log_change("update", {"counter": 5}, previous={}, log_diff=False)
    captured = capsys.readouterr()
    assert "update" in captured.err
    assert "counter" not in captured.err


def test_configure_logging_diff_false(capsys):
    kanta_logger = logging.getLogger("kanta")
    kanta_logger.handlers.clear()
    configure_logging(diff=False)
    log_change("update", {"counter": 5}, previous={})
    captured = capsys.readouterr()
    assert "update" in captured.err
    assert "counter" not in captured.err


def test_configure_logging_diff_true_reenables(capsys):
    kanta_logger = logging.getLogger("kanta")
    kanta_logger.handlers.clear()
    configure_logging(diff=False)
    configure_logging(diff=True)
    log_change("update", {"counter": 5}, previous={})
    captured = capsys.readouterr()
    assert "counter" in captured.err
