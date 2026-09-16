import logging

import pytest

from kanta.serialization import JsonSerializer, MsgPackSerializer


@pytest.fixture(
    params=[
        ("json", JsonSerializer),
        ("msgpack", MsgPackSerializer),
    ],
    ids=["json", "msgpack"],
)
def format_config(request):
    return request.param


@pytest.fixture(autouse=True)
def _kanta_event_loggers_propagate():
    """Let kanta's event loggers propagate so caplog captures their records.

    Kanta configures them with ``propagate = False`` at import time, which
    would hide their records from pytest's root-logger capture handler.
    """
    names = ("kanta.bootstrap", "kanta.migration", "kanta.transaction")
    loggers = [logging.getLogger(name) for name in names]
    previous = [logger.propagate for logger in loggers]
    for logger in loggers:
        logger.propagate = True
    yield
    for logger, propagate in zip(loggers, previous):
        logger.propagate = propagate
