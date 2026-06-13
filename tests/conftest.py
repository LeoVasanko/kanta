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
