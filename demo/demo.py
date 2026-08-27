#!/usr/bin/env -S uv run
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import msgspec

from kanta import Kanta, configure_logging


filename = Path(__file__).with_name("demo.kantadb")

# For demonstration purposes, we use "original v0" and "modified v1" in this same script
# Normally your app would only have the latest supported data model


class Data(msgspec.Struct):  # type: ignore - intentionally redefined later
    users: dict[str, dict] = {}
    counter: int = 0


kanta_v0 = Kanta(filename, Data())


@kanta_v0.bootstrap
def bootstrap(data: Data) -> None:
    """Create the initial admin user."""
    data.users["userid001"] = {"name": "Alice", "role": "admin"}


# Redefinition to simulate new version
class Data(msgspec.Struct):
    users: dict[str, dict] = {}
    total: int = 0  # Replaces old counter field
    lang: str = "en"  # New field


def migrate_v1(d: dict) -> None:
    """Rename counter to total"""
    d["total"] = d["counter"]


kanta_v1 = Kanta(filename, Data(), migrations=sys.modules[__name__])


@kanta_v1.logfmt
def resolve_user(value: str, path: str, state: dict) -> str | None:
    """Resolve user ids to names from the database state itself."""
    if path != "$user" and not path.startswith("users."):
        return None
    return state.get("users", {}).get(value, {}).get("name")


async def main() -> None:
    filename.unlink(missing_ok=True)

    print("Database creation with v0 schema and basic transactions:\n")
    # Open and close automatically; you can also `await kanta.open()` instead
    async with kanta_v0 as kanta:
        with kanta.transaction(action="create", user="userid001") as data:
            data.users["userid002"] = {"name": "Bob", "role": "user"}

        with kanta.transaction(action="update", user="userid001") as data:
            data.users["userid002"]["role"] = "editor"
            data.counter = 1

        # Display-only extra string, appended after the action.
        with kanta.transaction(
            action="export", user="userid002", extra="extra info"
        ) as data:
            data.counter = 2

    print("\nA new data model, migrations and logfmt pretty names:\n")
    async with kanta_v1 as kanta:
        with kanta.transaction(
            action="update", user="userid002", extra=filename.name
        ) as data:
            data.total += 1

        try:
            with kanta.transaction(action="reset", user="userid001") as data:
                data.total = 99
                raise ValueError("simulated failure")
        except ValueError:
            print(
                f"\nReset rolled back: {data.total=} (we can always read data without tx)\n"
            )

        with kanta.transaction(action="delete", user="userid002") as data:
            del data.users["userid001"]


# Fake clock for deterministic timestamps
_now = datetime(2027, 1, 1, tzinfo=UTC)


@kanta_v0.clock
@kanta_v1.clock
def fake_clock() -> datetime:
    global _now
    _now += timedelta(hours=1)
    return _now


if __name__ == "__main__":
    configure_logging(debug=True)
    asyncio.run(main())
