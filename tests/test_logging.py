import logging

from kanta.logging import changes_logger, configure_logging, log_change


def test_configure_logging():
    configure_logging()
    assert changes_logger.level == logging.INFO


def test_log_change_no_diff(capsys):
    changes_logger.handlers.clear()
    configure_logging()
    log_change("test", {})
    captured = capsys.readouterr()
    assert "test" in captured.err
