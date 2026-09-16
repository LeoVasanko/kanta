"""Module-level CLI for reading a kantadb file and printing its change log."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import importlib.metadata
import importlib.util
import sys
import tempfile
from pathlib import Path
from typing import Any

import msgspec

from kanta import Kanta
from kanta.callbacks import InjectionContext, callback_error_reporter
from kanta.exceptions import DatabaseError, DataIntegrityError, ReplayError
from kanta.grep import GrepPattern, evaluate
from kanta.logging import (
    LogEvent,
    emit_event,
    format_action_header,
    format_diff,
    migration_logger,
)
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
from kanta.tty import Line, strip_ansi, use_color

EXIT_SUCCESS = 0
EXIT_GENERIC = 1
EXIT_RANGE_ERROR = 2
EXIT_PARSE_ERROR = 10
EXIT_MIGRATION_ERROR = 20
EXIT_VALIDATION_ERROR = 21


def _print(*args: Any) -> None:
    """Print to stderr, stripping ANSI codes when the stream has no color support.

    Color detection runs per call so redirected or reassigned ``sys.stderr``
    (and environment changes) are honored; ANSI codes are stripped after
    formatting, not by formatting differently.
    """
    text = " ".join(str(arg) for arg in args)
    if not use_color():
        text = strip_ansi(text)
    print(text, file=sys.stderr)


class _CliError(Exception):
    """A user-facing error message paired with a process exit code."""

    def __init__(self, message: str, code: int = EXIT_GENERIC) -> None:
        self.code = code
        super().__init__(message)


def _import_dotted(path: str) -> Any:
    """Import ``module.submodule.Attr`` or a filesystem path and return the attribute."""
    if _is_file_path(path):
        return _import_from_file(path)
    if "." not in path:
        raise ValueError(f"dotted path must contain a dot: {path!r}")
    module_name, attr_name = path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        raise ImportError(f"{path!r} not found in {module_name!r}") from exc


def _is_file_path(path: str) -> bool:
    """Return True if *path* looks like a filesystem path rather than a dotted name."""
    return "/" in path or "\\" in path or ":" in path


def _import_from_file(path: str) -> Any:
    """Import a module or attribute from a filesystem path.

    *path* may be ``path/to/file.py`` (returns the module) or
    ``path/to/file.py:symbol`` (returns ``symbol`` from the module).
    """
    if ":" in path:
        file_path, symbol = path.rsplit(":", 1)
    else:
        file_path, symbol = path, None

    file_path = Path(file_path).resolve()
    if not file_path.exists():
        raise ImportError(f"{file_path!r} not found")
    if not file_path.is_file():
        raise ImportError(f"{file_path!r} is not a file")

    module_name = f"_kanta_cli_{file_path.stem}_{file_path.stat().st_ino}"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {file_path!r}")
    module = importlib.util.module_from_spec(spec)

    file_dir = str(file_path.parent)
    added_dir = False
    if file_dir not in sys.path:
        sys.path.insert(0, file_dir)
        added_dir = True
    try:
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        if added_dir:
            sys.path.remove(file_dir)

    if symbol is None:
        return module
    try:
        return getattr(module, symbol)
    except AttributeError as exc:
        raise ImportError(f"{symbol!r} not found in {file_path!r}") from exc


def _import_kanta_object(path: str) -> Any:
    """Import a Kanta object by module or filesystem path.

    If ``path`` names an importable module, look up an object named
    ``kanta`` in it; otherwise treat ``path`` as ``module.attr`` or
    ``path/to/file.py[:kanta]`` referring directly to the object.
    """
    if _is_file_path(path):
        if ":" in path:
            return _import_from_file(path)
        module = _import_from_file(path)
        try:
            return getattr(module, "kanta")
        except AttributeError as exc:
            raise ImportError(f"no 'kanta' object found in {path!r}") from exc
    try:
        spec = importlib.util.find_spec(path)
    except ImportError:
        spec = None
    if spec is not None:
        module = importlib.import_module(path)
        try:
            return getattr(module, "kanta")
        except AttributeError as exc:
            raise ImportError(f"no 'kanta' object found in module {path!r}") from exc
    return _import_dotted(path)


def _format_ts(dt) -> str:
    """Return a local-looking timestamp without a timezone offset or microseconds."""
    return dt.replace(tzinfo=None, microsecond=0).isoformat(sep=" ")


def _package_version() -> str:
    """Return the installed package version, or ``"unknown"`` from a source tree."""
    try:
        return importlib.metadata.version("kanta")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kanta",
        description=(
            f"kanta {_package_version()} - read a kantadb file and print each"
            " change record to the console."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    parser.add_argument(
        "file",
        help="Path to the kantadb file, or '-' to read from stdin.",
    )
    parser.add_argument(
        "-d",
        "--data",
        metavar="MOD",
        help=(
            "Dotted path or filesystem path to the root data type."
            " Examples: myapp.models.Data, myapp/models.py:Data."
        ),
    )
    parser.add_argument(
        "-m",
        "--migrations",
        metavar="MOD",
        help=(
            "Dotted path or filesystem path to the migrations module."
            " Examples: myapp.migrations, myapp/migrations.py."
        ),
    )
    parser.add_argument(
        "-k",
        "--kanta",
        metavar="MOD",
        help=(
            "Module path or filesystem path to an existing Kanta object to use."
            " Either a module containing an object named 'kanta' (e.g. myapp.db),"
            " a dotted path to the object (e.g. myapp.db.kanta), or a file path"
            " (e.g. myapp/db.py or myapp/db.py:kanta).  Its type, migrations, and"
            " logfmt/logemit callbacks are used.  Cannot be combined with -d or -m."
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
    parser.add_argument(
        "-g",
        "--grep",
        action="append",
        metavar="PATTERN",
        help=(
            "Print only change records matching PATTERN, structurally and"
            " case-insensitively; matched regions get a yellow background,"
            " and on a match the whole record is printed, not just the"
            " matching line.  Repeatable: every pattern must match somewhere"
            " in the same record, but different patterns may match different"
            " lines of it.  A bare pattern matches the action or user as a"
            " substring, a dotted path by element (each element in full,"
            " unless it uses wildcards: 'users' matches 'users' anywhere but"
            " not 'foousers', 'us*' does), or a value (strings by substring,"
            " other values in full: 'true' matches a boolean, 'tru' does"
            " not).  The 'path=value' form requires the path and the value"
            " to match within the same change line; use '=value' or 'path='"
            " to match values or paths only.  With -k, logfmt-prettified"
            " values and users match alongside the raw ones.  Examples:"
            " --grep alice, --grep 'users.*.email', --grep"
            " 'users.alice.admin=true', --grep create_user --grep"
            " '@example.com'."
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
    highlight: Any = None,
) -> None:
    """Log a single change record to stderr.

    The record is dispatched as a :class:`LogEvent` through the Kanta
    object's logemit handlers; the CLI's own rendering (with the ``l<N>``
    label and timestamp) is the fallback when no handler claims the event.
    ``highlight`` is an optional :class:`kanta.grep.GrepHighlighter` with
    the record's matched regions, applied to the fallback rendering.
    """
    event = record_change_event(record, previous, current, kanta)

    def render(ev) -> None:
        ts = _format_ts(record.ts)
        if highlight is not None:
            header = format_action_header(
                ev.action or "", ev.user, ev.extra, highlight=highlight
            )
            lines = format_diff(ev.diff, ev.previous, ev.logfmt, highlight=highlight)
        else:
            header, lines = ev.header, ev.diff_lines
        if not lines:
            _print(f"{label} {ts} {header}")
        elif len(lines) == 1:
            _print(f"{label} {ts} {header}{lines[0]}")
        else:
            _print(f"{label} {ts} {header}")
            for line in lines:
                _print(line)
            _print()

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
    _print(f"{label} {ts} {line}")


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
        await registry.invoke(
            "logmigr",
            InjectionContext(kanta=kanta, report=result),
            on_error=callback_error_reporter("logmigr"),
        )
        return
    if quiet:
        return
    descriptions = [f"{m.name} ({m.description})" for m in result.applied if m.changed]
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
        fallback=lambda ev: _print(ev.header),
    )


def _get_kanta(args: argparse.Namespace, filename: Path) -> tuple[Kanta[Any], bool]:
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
            raise _CliError(f"Migration error: {exc}", EXIT_MIGRATION_ERROR) from exc
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

        grep_patterns = [GrepPattern.parse(p) for p in args.grep or ()]

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
                _print()
        else:
            # Replay up to the range end, printing logs within the range.
            state = {}
            version = 0
            printed = False
            for event, previous, current in replay_events(events, selection.end_line):
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
                    highlight = None
                    if grep_patterns:
                        # Build the same logfmt the rendering uses, so both
                        # raw and prettified values are matched.
                        logfmt = kanta._impl.callback_registry.build_logfmt(
                            InjectionContext(
                                kanta=kanta,
                                previous_state=previous,
                                current_state=current,
                            )
                        )
                        highlight = evaluate(
                            event.record, previous, grep_patterns, logfmt=logfmt
                        )
                        if highlight is None:
                            continue
                    _print_change_log(
                        label, event.record, previous, current, kanta, highlight
                    )
                printed = True
            if printed:
                _print()

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
                await _log_migration(
                    kanta, filename, result, previous_version, args.quiet
                )

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
                _print(f"{data}")
                output_state = struct_to_dict(data, serializer=kanta._impl.serializer)
            else:
                kanta_typed = Kanta(
                    filename, data, type=data_type, migrations=args.migrations
                )
                try:
                    await kanta_typed.open(create=False, readonly=True, log=False)
                    _print(f"{data}")
                except (msgspec.ValidationError, msgspec.DecodeError) as exc:
                    raise _CliError(
                        f"Validation error: {exc}", EXIT_VALIDATION_ERROR
                    ) from exc
                except DataIntegrityError as exc:
                    raise _CliError(f"Parse error: {exc}", EXIT_PARSE_ERROR) from exc
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
                    raise _CliError(f"Failed to open {filename}: {exc}") from exc
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
        _print(exc)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
