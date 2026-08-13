"""Module-level CLI for reading a kantadb file and printing its change log."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import importlib.util
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

import msgspec

from kanta import Kanta
from kanta.callbacks import InjectionContext
from kanta.exceptions import DatabaseError, DataIntegrityError, ReplayError
from kanta.logging import LogEvent, emit_event, migration_logger
from kanta.replaylog import (
    RangeNotFoundError,
    Selection,
    SnapshotEvent,
    _snapshot_lines,
    end_of_file,
    record_change_event,
    record_label,
    replay_events,
    scan_events,
    select,
)
from kanta.serialization import Serializer, dict_to_struct, struct_to_dict
from kanta.structs import ChangeRecord, Snapshot
from kanta.tty import Line

EXIT_SUCCESS = 0
EXIT_GENERIC = 1
EXIT_RANGE_ERROR = 2
EXIT_PARSE_ERROR = 10
EXIT_MIGRATION_ERROR = 20
EXIT_VALIDATION_ERROR = 21

_logger = logging.getLogger(__name__)


class _CliError(Exception):
    """A user-facing error message paired with a process exit code."""

    def __init__(self, message: str, code: int = EXIT_GENERIC) -> None:
        self.code = code
        super().__init__(message)


def _import_dotted(path: str) -> Any:
    """Import ``module.submodule.Attr`` and return the attribute."""
    if "." not in path:
        raise ValueError(f"dotted path must contain a dot: {path!r}")
    module_name, attr_name = path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        raise ImportError(f"{path!r} not found in {module_name!r}") from exc


def _import_kanta_object(path: str) -> Any:
    """Import a Kanta object by module path.

    If ``path`` names an importable module, look up an object named
    ``kanta`` in it; otherwise treat ``path`` as ``module.attr`` referring
    directly to the object.
    """
    try:
        spec = importlib.util.find_spec(path)
    except ImportError:
        spec = None
    if spec is not None:
        module = importlib.import_module(path)
        try:
            return getattr(module, "kanta")
        except AttributeError as exc:
            raise ImportError(
                f"no 'kanta' object found in module {path!r}"
            ) from exc
    return _import_dotted(path)


def _format_ts(dt) -> str:
    """Return a local-looking timestamp without a timezone offset or microseconds."""
    return dt.replace(tzinfo=None, microsecond=0).isoformat(sep=" ")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kanta",
        description="Read a kantadb file and print each change record to the console.",
    )
    parser.add_argument(
        "file",
        help="Path to the kantadb file, or '-' to read from stdin.",
    )
    parser.add_argument(
        "-d",
        "--data",
        metavar="MOD",
        help="Dotted path to the root data type (e.g. myapp.models.Data).",
    )
    parser.add_argument(
        "-m",
        "--migrations",
        metavar="MOD",
        help="Dotted path to the migrations module (e.g. myapp.migrations).",
    )
    parser.add_argument(
        "-k",
        "--kanta",
        metavar="MOD",
        help=(
            "Module path to an existing Kanta object to use: either a module"
            " containing an object named 'kanta' (e.g. myapp.db) or a dotted"
            " path to the object itself (e.g. myapp.db.kanta).  Its type,"
            " migrations, and logfmt/logemit callbacks are used.  Cannot be"
            " combined with -d or -m."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Write the final replayed state as JSON to this file, or '-' for stdout.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress normal change/snapshot logs; only print warnings and errors.",
    )
    parser.add_argument(
        "-r",
        "--range",
        help=(
            "Python-style range to process.  Units: plain number = change index,"
            " lN = line number, sN = snapshot, vN = version.  Negative snapshot"
            " values count from the end (s-1 is the last snapshot).  Use ':' for"
            " half-open ranges and '..' for inclusive end ranges.  Examples:"
            " '2:5', '2..5', 'l10:l20', 's1:s3', 'v0:v2', 's-1:', ':-1', '-1'."
        ),
    )
    args = parser.parse_args(argv)
    if args.kanta and (args.data or args.migrations):
        parser.error("-k/--kanta cannot be used together with -d or -m")
    return args


def _print_change_log(
    label: str,
    record: ChangeRecord,
    previous: dict[str, Any],
    current: dict[str, Any],
    kanta: Kanta[Any],
) -> None:
    """Log a single change record to stderr.

    The record is dispatched as a :class:`LogEvent` through the Kanta
    object's logemit handlers; the CLI's own rendering (with the ``l<N>``
    label and timestamp) is the fallback when no handler claims the event.
    """
    event = record_change_event(record, previous, current, kanta)

    def render(ev) -> None:
        ts = _format_ts(record.ts)
        lines = ev.diff_lines
        if not lines:
            print(f"{label} {ts} {ev.header}", file=sys.stderr)
        elif len(lines) == 1:
            print(f"{label} {ts} {ev.header}{lines[0]}", file=sys.stderr)
        else:
            print(f"{label} {ts} {ev.header}", file=sys.stderr)
            for line in lines:
                print(line, file=sys.stderr)
            print(file=sys.stderr)

    emit_event(
        event,
        kanta._impl.callback_registry.logemit_handlers,
        fallback=render,
    )


def _format_size(n: int) -> str:
    """Return a human-readable byte size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} kB"
    return f"{n / (1024 * 1024):.1f} MB"


def _find_venv_site_packages(start: Path) -> list[Path]:
    """Return site-packages dirs of ``.venv`` directories from *start* to parents."""
    py_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    found: list[Path] = []
    for parent in [start, *start.parents]:
        venv = parent / ".venv"
        if not venv.is_dir():
            continue
        site_packages = venv / "lib" / py_dir / "site-packages"
        if site_packages.is_dir():
            found.append(site_packages)
            continue
        # Windows layout
        win_site = venv / "Lib" / "site-packages"
        if win_site.is_dir():
            found.append(win_site)
    return found


@contextlib.contextmanager
def _extra_import_paths():
    """Temporarily add current dir and nearby venv site-packages to ``sys.path``.

    The current directory is inserted first, then local ``.venv`` site-packages,
    then any parent ``.venv`` site-packages.  Only paths that were not already
    present are added, and only those added paths are removed on exit.
    """
    paths_to_add = [str(Path.cwd())]
    paths_to_add.extend(str(p) for p in _find_venv_site_packages(Path.cwd()))
    added: list[str] = []
    for path in reversed(paths_to_add):
        if path not in sys.path:
            sys.path.insert(0, path)
            added.append(path)
    try:
        yield
    finally:
        for path in added:
            if path in sys.path:
                sys.path.remove(path)


def _print_snapshot_indicator(
    label: str,
    snap: Snapshot,
    index: int,
    serializer: Serializer,
) -> None:
    """Print a snapshot indicator line to stderr.

    ``snapshot s<N>`` is rendered in bright white; the version, optional mtime
    and data size are printed in normal and dark colors respectively.
    """
    ts = _format_ts(snap.ts)
    line = Line().snapshot("snapshot").snapshot(f" s{index}")
    line.target(f" v{snap.v}")
    if snap.m is not None:
        line.target(f" {_format_ts(snap.m)}")
    size = len(serializer.encode(snap.state))
    line.path_prefix(f" {_format_size(size)}")
    print(f"{label} {ts} {line}", file=sys.stderr)


async def _log_migration(
    kanta: Kanta[Any],
    filename: Path,
    result,
    previous_version: int,
    quiet: bool,
) -> None:
    """Log an applied migration through the Kanta instance's callbacks.

    Routes to the object's logmigr callbacks when registered (like
    :meth:`KantaImpl._handle_migration_log`); otherwise emits a ``migrated``
    event through its logemit handlers, falling back to a stderr line.
    """
    registry = kanta._impl.callback_registry
    if registry.has("logmigr"):
        try:
            await registry.invoke(
                "logmigr",
                InjectionContext(kanta=kanta, migration_result=result),
            )
        except Exception:
            _logger.exception("logmigr callback failed")
        return
    if quiet:
        return
    descriptions = [
        f"{m.name} ({m.description})" for m in result.migrations if m.changed
    ]
    emit_event(
        LogEvent(
            kind="migrated",
            logger=migration_logger,
            kanta=kanta,
            filename=str(filename),
            from_version=previous_version,
            to_version=result.version,
            migrations=descriptions,
        ),
        registry.logemit_handlers,
        fallback=lambda ev: print(ev.header, file=sys.stderr),
    )


def _get_kanta(
    args: argparse.Namespace, filename: Path
) -> tuple[Kanta[Any], bool]:
    """Return the Kanta instance to work with, and whether the CLI owns it.

    With ``-k`` the existing object is used as-is (and never closed by us);
    otherwise an instance is constructed with an empty dict state.
    """
    if args.kanta:
        try:
            obj = _import_kanta_object(args.kanta)
        except (ImportError, ValueError) as exc:
            raise _CliError(f"Invalid --kanta value: {exc}") from exc
        if not isinstance(obj, Kanta):
            raise _CliError(
                f"Invalid --kanta value: {args.kanta!r} is not a Kanta object"
            )
        return obj, False
    try:
        return Kanta(filename, {}, type=dict, migrations=args.migrations), True
    except Exception as exc:
        if args.migrations:
            raise _CliError(
                f"Migration error: {exc}", EXIT_MIGRATION_ERROR
            ) from exc
        raise _CliError(f"Failed to initialize database: {exc}") from exc


async def _run(args: argparse.Namespace) -> int:
    cleanup_path: Path | None = None
    if args.file == "-":
        content = sys.stdin.buffer.read()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".kantadb")
        tmp.write(content)
        tmp.close()
        filename = Path(tmp.name)
        cleanup_path = filename
    else:
        filename = Path(args.file)
        if not filename.exists():
            raise _CliError(f"File not found: {filename}")
        content = filename.read_bytes()

    data_type: type[Any] | None = None
    if args.data:
        with _extra_import_paths():
            try:
                data_type = _import_dotted(args.data)
            except (ImportError, ValueError) as exc:
                raise _CliError(f"Invalid --data value: {exc}") from exc

    kanta: Kanta[Any] | None = None
    kanta_owned = False
    kanta_typed: Kanta[Any] | None = None
    try:
        with _extra_import_paths():
            kanta, kanta_owned = _get_kanta(args, filename)
        if data_type is None and args.kanta and kanta._impl.data_type is not dict:
            data_type = kanta._impl.data_type

        # Decode and validate the whole file into positioned events.
        try:
            events, change_count = scan_events(content, kanta)
        except ReplayError as exc:
            raise _CliError(str(exc), EXIT_PARSE_ERROR) from exc
        except Exception as exc:
            raise _CliError(
                f"Failed to replay records from {filename}: {exc}",
                EXIT_PARSE_ERROR,
            ) from exc

        snapshot_line_to_index = {
            line: idx for idx, line in enumerate(_snapshot_lines(events))
        }

        # Resolve -r into a line range or a single snapshot selection.
        try:
            selection = (
                select(args.range, events, change_count)
                if args.range is not None
                else Selection(0, end_of_file(events))
            )
        except RangeNotFoundError as exc:
            raise _CliError(str(exc), EXIT_RANGE_ERROR) from exc
        except ValueError as exc:
            raise _CliError(f"Invalid --range value: {exc}") from exc

        if selection.snapshot is not None:
            snap_event = selection.snapshot
            state = snap_event.snap.state
            version = snap_event.snap.v
            if not args.quiet:
                _print_snapshot_indicator(
                    record_label(snap_event.line_number, snap_event.record_index),
                    snap_event.snap,
                    snapshot_line_to_index[snap_event.line_number],
                    kanta._impl.serializer,
                )
                print(file=sys.stderr)
        else:
            # Replay up to the range end, printing logs within the range.
            state = {}
            version = 0
            printed = False
            for event, previous, current in replay_events(
                events, selection.end_line
            ):
                state = current
                version = event.version
                if event.line_number < selection.start_line or args.quiet:
                    continue
                label = record_label(event.line_number, event.record_index)
                if isinstance(event, SnapshotEvent):
                    _print_snapshot_indicator(
                        label,
                        event.snap,
                        snapshot_line_to_index[event.line_number],
                        kanta._impl.serializer,
                    )
                else:
                    assert previous is not None
                    _print_change_log(label, event.record, previous, current, kanta)
                printed = True
            if printed:
                print(file=sys.stderr)

        # Apply optional migrations to the range-end state.
        if kanta._impl.migrations is not None:
            try:
                previous_version = version
                result = kanta._impl.migrations.apply(state, version, kanta)
                version = result.version
            except Exception as exc:
                raise _CliError(
                    f"Migration error: {exc}", EXIT_MIGRATION_ERROR
                ) from exc
            if version != previous_version:
                await _log_migration(kanta, filename, result, previous_version, args.quiet)

        output_state: dict[str, Any]
        if data_type is not None:
            try:
                data = dict_to_struct(
                    state, data_type, serializer=kanta._impl.serializer
                )
            except (
                msgspec.ValidationError,
                msgspec.DecodeError,
                TypeError,
                ValueError,
            ) as exc:
                raise _CliError(
                    f"Validation error: {exc}", EXIT_VALIDATION_ERROR
                ) from exc

            if args.kanta:
                # The file was already fully decoded and validated above with
                # the object's own serializer, and its migrations were applied
                # to the state; no need to re-open through a new instance.
                print(f"{data}", file=sys.stderr)
                output_state = struct_to_dict(
                    data, serializer=kanta._impl.serializer
                )
            else:
                kanta_typed = Kanta(
                    filename, data, type=data_type, migrations=args.migrations
                )
                try:
                    await kanta_typed.open(create=False, readonly=True, log=False)
                    print(f"{data}", file=sys.stderr)
                except (msgspec.ValidationError, msgspec.DecodeError) as exc:
                    raise _CliError(
                        f"Validation error: {exc}", EXIT_VALIDATION_ERROR
                    ) from exc
                except DataIntegrityError as exc:
                    raise _CliError(
                        f"Parse error: {exc}", EXIT_PARSE_ERROR
                    ) from exc
                except DatabaseError as exc:
                    if not args.migrations or exc.cause_type == "ReplayError":
                        raise _CliError(
                            f"Parse error: {exc}", EXIT_PARSE_ERROR
                        ) from exc
                    raise _CliError(
                        f"Migration error: {exc}", EXIT_MIGRATION_ERROR
                    ) from exc
                except Exception as exc:
                    if args.migrations:
                        raise _CliError(
                            f"Migration error: {exc}", EXIT_MIGRATION_ERROR
                        ) from exc
                    raise _CliError(
                        f"Failed to open {filename}: {exc}"
                    ) from exc
                output_state = kanta_typed._impl.statedict
        else:
            output_state = state

        if args.output:
            try:
                out_bytes = msgspec.json.encode(output_state)
                if args.output == "-":
                    sys.stdout.buffer.write(out_bytes)
                else:
                    Path(args.output).write_bytes(out_bytes)
            except Exception as exc:  # pragma: no cover
                raise _CliError(f"Failed to write output: {exc}") from exc

        return EXIT_SUCCESS
    finally:
        if kanta_typed is not None:
            await kanta_typed.close()
        if kanta is not None and kanta_owned:
            await kanta.close()
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m kanta``."""
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except _CliError as exc:
        print(exc, file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
