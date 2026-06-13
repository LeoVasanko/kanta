import logging

from kanta.logging import configure_logging, log_change
from kanta.logging import logger


def test_configure_logging():
    configure_logging()
    assert logger.level == logging.INFO


def test_log_change_no_diff(capsys):
    logger.handlers.clear()
    configure_logging()
    log_change("test", {})
    captured = capsys.readouterr()
    assert "test" in captured.err
