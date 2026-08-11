"""Database change logging with pretty-printed diffs.

All change-related output is described by a :class:`LogEvent` and dispatched
through :func:`emit_event`, which runs any registered ``logemit`` callbacks
and falls back to :func:`default_emit` for the built-in formatting.  Diff
output is formatted in a human-readable path notation style with color
coding; see :mod:`kanta.tty` for the color palette and line builder.
"""

import logging
import re
import sys
from collections.abc import Callable, Iterable
from typing import Any

import msgspec

from kanta.tty import Line, displaywidth

transaction_logger = logging.getLogger("kanta.transaction")
bootstrap_logger = logging.getLogger("kanta.bootstrap")
migration_logger = logging.getLogger("kanta.migration")

_logger = logging.getLogger(__name__)

# Pattern to match control characters and bidirectional overrides
_UNSAFE_CHARS = re.compile(
    r"[\x00-\x1f\x7f-\x9f"
    r"\u200e\u200f"
    r"\u202a-\u202e"
    r"\u2066-\u2069"
    r"]"
)

# Metadata path used when formatting the transaction actor.
_USER_PATH = "$user"


class LogEvent(msgspec.Struct, kw_only=True):
    """All state describing one loggable event, passed to logemit callbacks.

    ``kind`` is ``"change"`` (transaction, bootstrap, or migration diff),
    ``"created"`` (database file created), ``"opened"`` (database file
    opened), ``"migrated"`` (migration summary), or ``"aborted"``
    (transaction rolled back).  ``logger`` and ``level`` are Kanta's
    preferred destination; a callback may use them, log elsewhere, or not
    log at all.

    The event is mutable: a callback may modify it before returning a truthy
    value to pass it on, affecting later callbacks and the built-in fallback.
    """

    kind: str
    logger: logging.Logger
    level: int = logging.INFO
    kanta: Any = None
    action: str | None = None
    user: str | None = None
    extra: Any = None
    error: BaseException | None = None
    diff: dict = msgspec.field(default_factory=dict)
    previous: dict | None = None
    current: dict | None = None
    logfmt: Callable[[Any, str], str | None] | None = None
    show_diff: bool = True
    filename: str | None = None
    from_version: int | None = None
    to_version: int | None = None
    migrations: list[str] = msgspec.field(default_factory=list)
    _header: str | None = None
    _diff_lines: list[str] | None = None

    @property
    def header(self) -> str:
        """The default one-line header for this event, built on first access.

        Covers every event kind: ``"<action>[ <extra>][ by <user>]"`` for
        changes, ``"<action>[ <extra>][ by <user>] transaction aborted:
        <error>"`` for aborts, and the ``🛢️ <filename> <verb>`` file
        summaries (created / opened / migrated).
        """
        if self._header is None:
            self._header = self._build_header()
        return self._header

    @header.setter
    def header(self, value: str) -> None:
        """Override the header, keeping the default diff routing.

        A logemit callback can restyle the header and return a truthy value:
        :func:`default_emit` then logs this header instead of building one.
        """
        self._header = value

    def _build_header(self) -> str:
        if self.kind == "created":
            return f"🛢️ {self.filename} created"
        if self.kind == "opened":
            return f"🛢️ {self.filename} opened"
        if self.kind == "migrated":
            migrations = ", ".join(self.migrations)
            return (
                f"🛢️ {self.filename} migrated "
                f"v{self.from_version} -> v{self.to_version}: {migrations}"
            )
        if self.kind == "change":
            return format_action_header(self.action or "", self.user, self.extra)
        line = Line().action(self.action or "")
        if self.extra:
            line(" ").target(self.extra)
        if self.user:
            line(" by ").user(self.user)
        line(f" transaction aborted: {self.error}")
        return str(line)

    @property
    def diff_lines(self) -> list[str]:
        """Pretty-printed diff lines, built on first access and cached."""
        if self._diff_lines is None:
            self._diff_lines = format_diff(self.diff, self.previous, self.logfmt)
        return self._diff_lines


def emit_event(
    ev: LogEvent,
    handlers: Iterable[Callable[[LogEvent], Any]] = (),
) -> None:
    """Dispatch *ev* through registered logemit handlers.

    Each handler receives the event and may log it (or not) as it sees fit.
    A falsy return value stops the chain: the event is considered handled.
    A truthy return value passes the event — possibly modified — to the next
    handler.  When all handlers pass, :func:`default_emit` renders the event
    with the built-in formatting.

    Logging must never break functionality: a crashing handler is reported
    and the chain falls back to the built-in formatting, and a failure in
    the built-in formatting itself is reported and swallowed.
    """
    try:
        for handler in handlers:
            try:
                proceed = handler(ev)
            except Exception:
                _logger.exception("logemit callback failed, using default formatting")
                break
            if not proceed:
                return
        default_emit(ev)
    except Exception:
        _logger.exception("failed to emit %s log event", ev.kind)


def default_emit(ev: LogEvent) -> None:
    """Emit *ev* with Kanta's built-in formatting.

    Logs :attr:`LogEvent.header`; for change events the
    :attr:`LogEvent.diff_lines` body follows on the ``<logger>.diff`` child
    logger so it can be silenced or routed separately from the headers.
    This is what runs when no logemit callback handles the event; custom
    callbacks may call it to delegate events they do not care about.
    """
    if ev.kind != "change":
        ev.logger.log(ev.level, ev.header)
        return

    diff_logger = logging.getLogger(f"{ev.logger.name}.diff")
    lines = ev.diff_lines if ev.show_diff and diff_logger.isEnabledFor(ev.level) else []

    if not lines:
        ev.logger.log(ev.level, ev.header)
        return

    if len(lines) == 1:
        diff_logger.log(ev.level, f"{ev.header}{lines[0]}")
        return

    ev.logger.log(ev.level, ev.header)
    for line in lines:
        diff_logger.log(ev.level, line)


def _join_path(path: str, key: str) -> str:
    """Append *key* to a dot-notation *path*."""
    if not path:
        return key
    return f"{path}.{key}"


def _dim_ellipsis() -> str:
    """Return the truncation ellipsis in the palette's ellipsis color."""
    return str(Line().ellipsis("…"))


def _format_value(
    value: Any,
    path: str,
    *,
    max_len: int = 60,
    logfmt: Callable[[Any, str], str | None] | None = None,
) -> str:
    """Format a value for display, truncating if needed."""
    if logfmt is not None:
        resolved = logfmt(value, path)
        if resolved is not None:
            return resolved

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        value = _UNSAFE_CHARS.sub("", value)
        if len(value) > max_len:
            return value[: max_len - 1] + _dim_ellipsis()
        return value
    if isinstance(value, dict):
        if not value:
            return "{}"
        all_true = all(v is True for v in value.values())
        parts = []
        for k, v in value.items():
            key_path = _join_path(path, str(k))
            key_display = _format_value(k, key_path, max_len=30, logfmt=logfmt)
            if all_true:
                parts.append(key_display)
            else:
                val_display = _format_value(v, key_path, max_len=30, logfmt=logfmt)
                parts.append(f"{key_display}: {val_display}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        parts = []
        for i, v in enumerate(value):
            item_path = _join_path(path, str(i))
            parts.append(_format_value(v, item_path, max_len=30, logfmt=logfmt))
        return "[" + ", ".join(parts) + "]"
    text = str(value)
    if len(text) > max_len:
        text = text[: max_len - 1] + _dim_ellipsis()
    return text


def _format_path_components(
    path: list[str], logfmt: Callable[[Any, str], str | None] | None
) -> list[str]:
    """Return path components after applying formatters."""
    if not path:
        return []
    result = []
    for i, component in enumerate(path):
        prefix_path = ".".join(path[: i + 1])
        display = component
        if logfmt is not None:
            resolved = logfmt(component, prefix_path)
            if resolved is not None:
                display = resolved
        result.append(display)
    return result


def _format_path(
    path: list[str],
    logfmt: Callable[[Any, str], str | None] | None,
    final_color: str = "path_final",
) -> str:
    """Format a path as dot notation with prefix in dark grey, final colored.

    *final_color* names a color in the :data:`kanta.tty.colors` palette.
    """
    components = _format_path_components(path, logfmt)
    if not components:
        return ""
    line = Line()
    if len(components) > 1:
        line.path_prefix(".".join(components[:-1]) + ".")
    getattr(line, final_color)(components[-1])
    return str(line)


def _get_nested(data: dict | None, path: list[str]) -> Any:
    """Get a nested value from a dict by path, or None if not found."""
    if data is None:
        return None
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _collect_changes(
    diff: dict,
    path: list[str],
    changes: list[tuple[str, list[str], Any]],
    previous: dict | None,
) -> None:
    """Recursively collect changes from a diff into a flat list.

    Each change is a tuple of (change_type, path, new_value).
    change_type is one of: 'add', 'update', 'delete'
    """
    if not isinstance(diff, dict):
        existed = _get_nested(previous, path) is not None
        changes.append(("update" if existed else "add", path, diff))
        return

    for key, value in diff.items():
        if key == "$delete":
            if isinstance(value, list):
                for deleted_key in value:
                    changes.append(("delete", path + [str(deleted_key)], None))
            else:
                changes.append(("delete", path + [str(value)], None))
        elif key == "$replace":
            old_collection = _get_nested(previous, path)
            old_keys = (
                set(old_collection.keys())
                if isinstance(old_collection, dict)
                else set()
            )
            new_keys = set(value.keys()) if isinstance(value, dict) else set()
            for deleted_key in old_keys - new_keys:
                changes.append(("delete", path + [str(deleted_key)], None))
            if isinstance(value, dict):
                for rkey, rval in value.items():
                    existed = rkey in old_keys
                    changes.append(
                        ("update" if existed else "add", path + [str(rkey)], rval)
                    )
            elif value or not old_keys:
                changes.append(
                    ("update" if old_collection is not None else "add", path, value)
                )
        elif isinstance(key, str) and key.startswith("$"):
            changes.append(("add", path, {key: value}))
        else:
            new_path = path + [str(key)]
            existed = _get_nested(previous, new_path) is not None
            if existed:
                _collect_changes(value, new_path, changes, previous)
            else:
                changes.append(("add", new_path, value))


def _format_change_lines(
    change_type: str,
    path: list[str],
    value: Any,
    logfmt: Callable[[Any, str], str | None] | None = None,
) -> list[str]:
    """Format a single change as one or more lines."""
    if change_type == "delete":
        components = _format_path_components(path, logfmt)
        line = Line()("  ")
        if len(components) > 1:
            line.path_prefix(".".join(components[:-1]) + ".")
        line.delete(components[-1], " ✗")
        return [str(line)]

    if change_type == "add":
        path_str = _format_path(path, logfmt, final_color="add")
        if isinstance(value, dict) and value:
            lines = [str(Line()("  ", path_str, " ").sep("="))]
            base_path = ".".join(path)
            keys = []
            for k in value:
                key_path = _join_path(base_path, str(k))
                keys.append(
                    (k, _format_value(k, key_path, max_len=30, logfmt=logfmt))
                )
            field_width = max(displaywidth(kd) for _, kd in keys)
            field_width = max(field_width, 12)
            # Each item line is "    {key:{field_width}}: {value}"; budget the
            # value so the whole line fits in 80 columns.
            value_width = max(80 - 4 - field_width - 2, 20)
            formatted_items = []
            for (k, key_display), v in zip(keys, value.values()):
                key_path = _join_path(base_path, str(k))
                v_str = _format_value(v, key_path, max_len=value_width, logfmt=logfmt)
                formatted_items.append((key_display, v_str))
            return lines + [
                str(
                    Line()("    ", k).sep(":")(
                        " " * (field_width - displaywidth(k)), " ", v
                    )
                )
                for k, v in formatted_items
            ]
        value_str = _format_value(value, ".".join(path), logfmt=logfmt)
        return [str(Line()("  ", path_str, " ").sep("=")(" ", value_str))]

    value_str = _format_value(value, ".".join(path), logfmt=logfmt)
    path_str = _format_path(path, logfmt=logfmt)
    return [str(Line()("  ", path_str, " ").sep("=")(" ", value_str))]


def format_diff(
    diff: dict,
    previous: dict | None = None,
    logfmt: Callable[[Any, str], str | None] | None = None,
) -> list[str]:
    """Format a JSON diff as human-readable lines.

    Args:
        diff: The JSON diff dict.
        previous: The previous state dict (for determining add vs update).
        logfmt: Optional formatter callable ``(value, path) -> str | None``.
            ``path`` is a dot-notation string; ``"$user"`` is used for the
            transaction actor.  If the callable returns ``None``, default
            formatting is used.

    Returns a list of formatted lines (without newlines).
    """
    changes: list[tuple[str, list[str], Any]] = []
    _collect_changes(diff, [], changes, previous)
    if not changes:
        return []
    lines = []
    for change_type, path, value in changes:
        lines.extend(_format_change_lines(change_type, path, value, logfmt))
    return lines


def format_action_header(
    action: str,
    user: str | None = None,
    extra: Any = None,
) -> str:
    """Format the default action header line."""
    line = Line().action(action)
    if extra is not None and (extra := f"{extra}"):
        line(" ").target(extra)
    if user is not None and (user := f"{user}"):
        line(" by ").user(user)
    return str(line)


def log_change(
    action: str,
    diff: dict,
    user: str | None = None,
    previous: dict | None = None,
    extra: Any = None,
    logfmt: Callable[[Any, str], str | None] | None = None,
    *,
    logger: logging.Logger = transaction_logger,
    level: int = logging.INFO,
    log_diff: bool = True,
) -> None:
    """Log a database change with the built-in formatting.

    Compatibility wrapper around :func:`emit_event` with no handlers; Kanta
    itself builds a :class:`LogEvent` and dispatches it with the registered
    logemit callbacks.

    Args:
        action: The action name (e.g., "login", "admin:delete_user").
        diff: The JSON diff dict.
        user: Optional already-formatted user name to show in the header.
        previous: The previous state dict (for determining add vs update).
        extra: Optional display-only value shown after the action in the
            header.  Anything other than ``None`` is printed str-converted
            (colored by Kanta), unless a custom logemit handler does
            something else with it.
        logfmt: Optional formatter callable ``(value, path) -> str | None``.
        logger: Logger to write to. Defaults to the ``kanta.transaction`` logger.
        level: Log level to use. Defaults to ``logging.INFO``.
        log_diff: Whether to build and emit the diff lines.  ``False`` skips
            diff formatting entirely and only the header is logged.
    """
    emit_event(
        LogEvent(
            kind="change",
            logger=logger,
            level=level,
            action=action,
            user=user,
            extra=extra,
            diff=diff,
            previous=previous,
            logfmt=logfmt,
            show_diff=log_diff,
        )
    )


def configure_logging(
    *,
    skiproot: bool = True,
    bootstrap: bool = True,
    migration: bool = True,
    transaction: bool = True,
    diff: bool = True,
    debug: bool = False,
) -> None:
    """Configure Kanta's default logging output.

    Args:
        skiproot: If ``True`` (default), attach a no-prefix stderr handler to
            the ``kanta`` logger and set ``kanta.propagate = False`` so Kanta
            output is rendered directly without propagating to the root logger.
            If ``False``, the child logger enable flags are still applied, but
            no handler is added and ``kanta`` propagation is left untouched so
            the application's root logger handles Kanta output.
        bootstrap: Whether bootstrap logs are enabled.
        migration: Whether migration logs are enabled.
        transaction: Whether transaction logs are enabled.
        diff: Whether transaction diff lines are enabled.  When ``False``,
            only transaction headers are printed and diff formatting is
            skipped.  Per transaction this is controlled by the ``logdiff``
            argument of :meth:`Kanta.transaction`.
        debug: Whether to set the ``kanta`` logger level to ``DEBUG`` instead
            of ``INFO``.  This reveals debug-level output such as migration
            diffs, which are hidden by default.

    This helper is not called automatically; applications that want Kanta's
    default output can call it, but most applications will configure logging
    themselves.
    """
    logging.getLogger("kanta.transaction.diff").disabled = not diff

    for name, enabled in (
        ("kanta.bootstrap", bootstrap),
        ("kanta.migration", migration),
        ("kanta.transaction", transaction),
    ):
        logging.getLogger(name).propagate = enabled

    if not skiproot:
        return

    target = logging.getLogger("kanta")
    target.propagate = False

    if not target.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        target.addHandler(handler)
    target.setLevel(logging.DEBUG if debug else logging.INFO)
