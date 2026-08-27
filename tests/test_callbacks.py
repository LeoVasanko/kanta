from typing import Any, Optional, Union

import pytest

from kanta import DictPrev, DictState, Kanta
from kanta.callbacks import DictPost, DictPre, LogFmt
from kanta.exceptions import DatabaseError

from .support import Data, User, make_kanta


def test_bootstrap_rejects_unannotated_param(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    with pytest.raises(TypeError, match="without an annotation or default"):

        @kanta.bootstrap
        def seed(data):
            data.counter = 1


def test_bootstrap_accepts_unknown_with_default(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    @kanta.bootstrap
    def seed(data: Data, extra: int = 0) -> None:
        data.counter = extra + 1

    # Should register without error.


def test_bootstrap_rejects_unknown_annotation(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    with pytest.raises(TypeError, match="unsupported annotation"):

        @kanta.bootstrap
        def seed(data: int):
            pass


def test_logfmt_requires_value_annotation(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    with pytest.raises(TypeError, match="value parameter.*must be annotated"):

        @kanta.logfmt
        def resolve_names(previous: DictPre, current: DictPost) -> str | None:
            return None


def test_logfmt_allows_missing_return_annotation(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    @kanta.logfmt
    def resolve_names(value: str, current: DictPost):
        return None


def test_logfmt_class_allows_missing_return_annotation(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    @kanta.logfmt
    class UserLogFmt(LogFmt):
        def resolve(self, value: str, path: str):
            return None


# fmt: off
def test_logfmt_accepts_optional_return_typing_forms(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    @kanta.logfmt
    def resolve_optional(value: str) -> Optional[str]:  # noqa: UP007
        return value

    @kanta.logfmt
    def resolve_union(value: str) -> Union[str, None]:  # noqa: UP007
        return value

    @kanta.logfmt
    def resolve_pipe(value: "str") -> "str | None":
        return value


def test_logfmt_class_accepts_optional_return_typing_forms(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    @kanta.logfmt
    class OptionalStyle(LogFmt):
        def resolve(self, value: str, path: str) -> Optional[str]:  # noqa: UP007
            return value

    @kanta.logfmt
    class UnionStyle(LogFmt):
        def resolve(self, value: str, path: str) -> Union[str, None]:  # noqa: UP007
            return value

    @kanta.logfmt
    class StringStyle(LogFmt):
        def resolve(self, value: "str", path: "str") -> "str | None":
            return value
# fmt: on


def test_logfmt_rejects_async_callback(tmp_path, format_config):
    kanta = make_kanta(tmp_path / "test.db", Data, format_config)

    with pytest.raises(TypeError, match="must not be async"):

        @kanta.logfmt
        async def resolve_names(value: str, current: DictPost) -> str | None:
            return None


@pytest.mark.asyncio
async def test_bootstrap_injects_data_by_type(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.bootstrap
    def seed(data: Data) -> None:
        data.counter = 7

    await kanta.open()
    assert kanta.data.counter == 7
    await kanta.close()


@pytest.mark.asyncio
async def test_bootstrap_injects_kanta(tmp_path, format_config):
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)
    seen: list[Kanta] = []

    @kanta.bootstrap
    def seed(data: Data, kanta_ref: Kanta) -> None:
        seen.append(kanta_ref)
        data.counter = 8

    await kanta.open()
    assert seen == [kanta]
    assert kanta.data.counter == 8
    await kanta.close()


@pytest.mark.asyncio
async def test_logfmt_injects_states(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt
    def resolve_users(value: str, current: DictPost) -> str | None:
        return current.get("users", {}).get(value, {}).get("name")

    await kanta.open()

    with kanta.transaction(action="create_user") as data:
        data.users["uuid-1"] = User(name="Alice")

    await kanta.close()

    assert "Alice" in caplog.text


@pytest.mark.asyncio
async def test_logfmt_injects_states_by_name(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt
    def resolve_users(value: str, prev, state: dict | None) -> str | None:
        assert prev == {}
        assert state is not None
        return state.get("users", {}).get(value, {}).get("name")

    await kanta.open()

    with kanta.transaction(action="create_user") as data:
        data.users["uuid-9"] = User(name="Carol")

    await kanta.close()

    assert "Carol" in caplog.text


@pytest.mark.asyncio
async def test_logfmt_state_name_ignores_annotation(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    # Matching by name does not check the annotation.
    @kanta.logfmt
    def resolve_users(value: str, state: int) -> str | None:
        return state.get("users", {}).get(value, {}).get("name")

    await kanta.open()

    with kanta.transaction(action="create_user") as data:
        data.users["uuid-10"] = User(name="Dave")

    await kanta.close()

    assert "Dave" in caplog.text


@pytest.mark.asyncio
async def test_logfmt_tag_takes_precedence_over_name(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt
    def check_prev(value: str, anything: DictPrev) -> str | None:
        assert anything == {}
        return None

    @kanta.logfmt
    def resolve_users(value: str, prev: DictState) -> str | None:
        # The tag wins: prev receives the current state despite its name.
        return prev.get("users", {}).get(value, {}).get("name")

    await kanta.open()

    with kanta.transaction(action="create_user") as data:
        data.users["uuid-11"] = User(name="Erin")

    await kanta.close()

    assert "Erin" in caplog.text


@pytest.mark.asyncio
async def test_logfmt_class_state_attribute(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt
    class UserLogFmt(LogFmt):
        def resolve(self, value: str, path: str) -> str | None:
            if not isinstance(value, str):
                return None
            return self.state.get("users", {}).get(value, {}).get("name")

    await kanta.open()

    with kanta.transaction(action="create_user") as data:
        data.users["uuid-12"] = User(name="Fred")

    await kanta.close()

    assert "Fred" in caplog.text


@pytest.mark.asyncio
async def test_logfmt_class_injection(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt
    class UserLogFmt(LogFmt):
        def resolve(self, value: str, path: str) -> str | None:
            if not isinstance(value, str):
                return None
            return self.current_state.get("users", {}).get(value, {}).get("name")

    await kanta.open()

    with kanta.transaction(action="create_user") as data:
        data.users["uuid-2"] = User(name="Bob")

    await kanta.close()

    assert "Bob" in caplog.text


@pytest.mark.asyncio
async def test_multiple_logfmt_chain(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt
    def resolve_a(value: str) -> str | None:
        return "A" if value == "a" else None

    @kanta.logfmt
    def resolve_b(value: str) -> str | None:
        return "B" if value == "b" else None

    await kanta.open()

    with kanta.transaction(action="create_user") as data:
        data.users["a"] = User(name="first")
        data.users["b"] = User(name="second")

    await kanta.close()

    assert "A" in caplog.text
    assert "B" in caplog.text


@pytest.mark.asyncio
async def test_logfmt_path_context(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt(path="users.uuid-1")
    def resolve_user_key(value: str) -> str | None:
        if value == "uuid-1":
            return "user-alice"
        return None

    await kanta.open()

    with kanta.transaction(action="create_user") as data:
        data.users["uuid-1"] = User(name="Alice")

    await kanta.close()

    assert "user-alice" in caplog.text


@pytest.mark.asyncio
async def test_logfmt_decorator_path_filters_calls(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt(path="counter")
    def fmt_counter(value: Any) -> str | None:
        if value == 1:
            return "one"
        return None

    await kanta.open()

    with kanta.transaction(action="create_user") as data:
        data.users["uuid-1"] = User(name="Alice")
        data.counter = 1

    await kanta.close()

    assert "one" in caplog.text
    assert "uuid-1" in caplog.text


@pytest.mark.asyncio
async def test_logfmt_user_path_replaces_user_display(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt(path="$user")
    def resolve_user(value: str, current: DictPost) -> str | None:
        return current.get("users", {}).get(value, {}).get("name")

    await kanta.open()

    with kanta.transaction(action="create_user", user="uuid-1") as data:
        data.users["uuid-1"] = User(name="Alice")

    await kanta.close()

    assert "by Alice" in caplog.text


@pytest.mark.asyncio
async def test_logfmt_non_string_value(tmp_path, format_config, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kanta.transaction")
    path = tmp_path / "test.db"
    kanta = make_kanta(path, Data, format_config)

    @kanta.logfmt
    def fmt_count(value: Any, path: str) -> str | None:
        if path == "counter" and value == 1:
            return "one"
        return None

    await kanta.open()

    with kanta.transaction(action="inc") as data:
        data.counter = 1

    await kanta.close()

    assert "one" in caplog.text


@pytest.mark.asyncio
async def test_fatal_error_injects_kanta_and_error(
    tmp_path, format_config, monkeypatch
):
    import asyncio

    path = tmp_path / "test.db"
    errors: list[DatabaseError] = []
    kantas: list[Kanta] = []
    signaled = asyncio.Event()

    kanta = make_kanta(path, Data, format_config, flush_interval=0.01)

    @kanta.fatal_error
    def on_fatal(error: DatabaseError, kanta_ref: Kanta) -> None:
        errors.append(error)
        kantas.append(kanta_ref)
        signaled.set()

    await kanta.open()

    with kanta.transaction(action="inc") as data:
        data.counter = 1

    def fail_write(_data: bytes) -> None:
        raise OSError("simulated background write failure")

    monkeypatch.setattr(kanta._impl.file, "write", fail_write)

    await asyncio.wait_for(signaled.wait(), timeout=1.0)
    assert errors
    assert kantas == [kanta]

    await kanta.close()
