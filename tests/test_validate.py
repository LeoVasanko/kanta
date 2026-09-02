"""Tests for the @kanta.validate integrity-validation callbacks."""

import pytest

from tests.support import Data, make_kanta

pytestmark = pytest.mark.asyncio


async def test_validate_passes_on_valid_data(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "d.kantadb", Data, format_config)
    calls = []

    @kanta.validate
    def check(data: Data):
        calls.append(data.counter)
        assert data.counter >= 0

    async with kanta:
        with kanta.transaction("inc", log=False) as data:
            data.counter = 1

    assert calls  # ran during bootstrap/open and the transaction


async def test_validate_failure_rolls_back_transaction(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "d.kantadb", Data, format_config)

    @kanta.validate
    def check(data: Data):
        if data.counter < 0:
            raise ValueError("counter must not go negative")

    await kanta.open(log=False)
    with pytest.raises(ValueError, match="negative"):
        with kanta.transaction("dec", log=False) as data:
            data.counter = -1
    assert kanta.data.counter == 0  # rolled back
    await kanta.close()

    # The invalid change never reached the history.
    kanta2 = make_kanta(tmp_path / "d.kantadb", Data, format_config)
    async with kanta2:
        assert kanta2.data.counter == 0


async def test_validate_runs_on_open_after_replay(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "d.kantadb", Data, format_config)
    async with kanta:
        with kanta.transaction("set", log=False) as data:
            data.counter = 5

    kanta2 = make_kanta(tmp_path / "d.kantadb", Data, format_config)
    seen = []

    @kanta2.validate
    def check(data: Data):
        seen.append(data.counter)

    async with kanta2:
        pass
    assert 5 in seen


async def test_validate_failure_aborts_open(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "d.kantadb", Data, format_config)
    async with kanta:
        with kanta.transaction("set", log=False) as data:
            data.counter = 5

    kanta2 = make_kanta(tmp_path / "d.kantadb", Data, format_config)

    @kanta2.validate
    def check(data: Data):
        raise ValueError("always inconsistent")

    with pytest.raises(ValueError, match="inconsistent"):
        await kanta2.open(log=False)

    # The failed open released the file: a fresh instance can open it.
    kanta3 = make_kanta(tmp_path / "d.kantadb", Data, format_config)
    async with kanta3:
        assert kanta3.data.counter == 5


async def test_multiple_validators_stop_at_first_failure(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "d.kantadb", Data, format_config)
    calls = []

    @kanta.validate
    def first(data: Data):
        calls.append("first")
        if data.counter > 1:
            raise ValueError("too big")

    @kanta.validate
    def second(data: Data):
        calls.append("second")

    await kanta.open(log=False)
    calls.clear()
    with pytest.raises(ValueError, match="too big"):
        with kanta.transaction("bump", log=False) as data:
            data.counter = 2
    assert calls == ["first"]
    await kanta.close()


async def test_validate_rejects_async_callback(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "d.kantadb", Data, format_config)

    with pytest.raises(TypeError, match="must not be async"):

        @kanta.validate
        async def check(data: Data):
            pass
