import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from uuid import UUID

import msgspec

from kanta.kanta import Kanta
from kanta.structs import ChangeRecord, Snapshot


class User(msgspec.Struct):
    name: str = ""
    age: int = 0


class Data(msgspec.Struct):
    users: dict[str, User] = {}
    counter: int = 0


class ExoticData(msgspec.Struct, omit_defaults=False):
    uuid_values: dict[str, UUID] = {}
    uuid_keys: dict[UUID, int] = {}
    datetime_values: dict[str, datetime] = {}
    datetime_keys: dict[datetime, int] = {}
    bytes_values: dict[str, bytes] = {}
    bytes_keys: dict[bytes, int] = {}


class EvolvableDataV1(msgspec.Struct, omit_defaults=False):
    counter: int = 0


class EvolvableDataV2(msgspec.Struct, omit_defaults=False):
    counter: int = 0
    enabled: bool = True


def make_kanta(path: Path, data_or_type, format_config, **kwargs):
    _, serializer_cls = format_config
    root = data_or_type() if isinstance(data_or_type, type) else data_or_type
    return Kanta(
        str(path),
        root,
        serializer=serializer_cls(),
        **kwargs,
    )


def seed_single_change(path: Path, change: ChangeRecord, format_config) -> None:
    _, serializer_cls = format_config
    serializer = serializer_cls()
    framer = serializer.framer_cls()
    payload = serializer.encode(change)
    path.write_bytes(framer.frame_change(payload, record_offset=0))


def change_actions(path: Path, format_config) -> list[str]:
    _, serializer_cls = format_config
    serializer = serializer_cls()
    framer = serializer.framer_cls()
    actions: list[str] = []
    for is_snapshot, payload, _, _ in framer.iter_records(path.read_bytes(), 0):
        if is_snapshot:
            continue
        rec = serializer.decode(payload, type=ChangeRecord)
        actions.append(rec.a)
    return actions


def read_changes(path: Path, format_config) -> list[ChangeRecord]:
    _, serializer_cls = format_config
    serializer = serializer_cls()
    framer = serializer.framer_cls()
    records: list[ChangeRecord] = []
    for is_snapshot, payload, _, _ in framer.iter_records(path.read_bytes(), 0):
        if is_snapshot:
            continue
        records.append(serializer.decode(payload, type=ChangeRecord))
    return records


def make_migrations_module(name: str, fn_name: str, fn):
    mod = ModuleType(name)
    mod.__dict__[fn_name] = fn
    sys.modules[name] = mod
    return mod


def read_last_snapshot(path: Path, format_config) -> Snapshot | None:
    _, serializer_cls = format_config
    serializer = serializer_cls()
    framer = serializer.framer_cls()
    data = path.read_bytes()
    payload, _, _ = framer.scan_last_snapshot(data)
    if payload is None:
        return None
    return serializer.decode(payload, type=Snapshot)


def fixed_change(action: str, diff: dict, *, version: int = 0) -> ChangeRecord:
    return ChangeRecord(
        ts=datetime(2026, 1, 1, tzinfo=UTC), a=action, v=version, diff=diff
    )
