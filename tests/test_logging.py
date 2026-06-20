import logging

import pytest

from kanta.logging import configure_logging, log_change, transaction_logger


@pytest.fixture(autouse=True)
def _reset_kanta_loggers():
    yield
    for name in ("kanta", "kanta.transaction", "kanta.bootstrap", "kanta.migration"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
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
