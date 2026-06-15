"""Database change logging with pretty-printed diffs.

Provides a logger for JSONL database changes that formats diffs
in a human-readable path.notation style with color coding.
"""

import logging
import re
import sys
from collections.abc import Callable
from typing import Any

changes_logger = logging.getLogger("kanta.changes")
migration_logger = logging.getLogger("kanta.migrations")

# Pattern to match control characters and bidirectional overrides
_UNSAFE_CHARS = re.compile(
    r"[\x00-\x1f\x7f-\x9f"
    r"\u200e\u200f"
    r"\u202a-\u202e"
    r"\u2066-\u2069"
    r"]"
)

# ANSI color codes
_RESET = "\033[0m"
_SEP = "\033[38;5;242m"  # Dark grey for separators
_PATH_PREFIX = "\033[38;5;242m"  # Dark grey for path prefix
_PATH_FINAL = "\033[38;5;250m"  # Default for final element
_DELETE = "\033[1;31m"  # Red for deletions
_ADD = "\033[0;32m"  # Green for additions
_ACTION = "\033[1;34m"  # Bold blue for action name
_USER = "\033[0;34m"  # Blue for user display

# Metadata path used when formatting the transaction actor.
_USER_PATH = "$user"


def _join_path(path: str, key: str) -> str:
    """Append *key* to a dot-notation *path*."""
    if not path:
        return key
    return f"{path}.{key}"


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
            return value[: max_len - 3] + "..."
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
        text = text[: max_len - 3] + "..."
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
    path: list[str], logfmt: Callable[[Any, str], str | None] | None
) -> str:
    """Format a path as dot notation with prefix in dark grey, final in default."""
    components = _format_path_components(path, logfmt)
    if not components:
        return ""
    if len(components) == 1:
        return f"{_PATH_FINAL}{components[0]}{_RESET}"
    prefix = ".".join(components[:-1])
    final = components[-1]
    return f"{_PATH_PREFIX}{prefix}.{_RESET}{_PATH_FINAL}{final}{_RESET}"


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
    path_str = _format_path(path, logfmt=logfmt)

    if change_type == "delete":
        components = _format_path_components(path, logfmt)
        if len(components) == 1:
            return [f"  {_DELETE}{components[0]} ✗{_RESET}"]
        prefix = ".".join(components[:-1])
        final = components[-1]
        return [f"  {_PATH_PREFIX}{prefix}.{_RESET}{_DELETE}{final} ✗{_RESET}"]

    if change_type == "add":
        if isinstance(value, dict) and value:
            lines = [f"  {path_str} {_SEP}={_RESET}"]
            formatted_items = []
            base_path = ".".join(path)
            for k, v in value.items():
                key_path = _join_path(base_path, str(k))
                key_display = _format_value(k, key_path, max_len=30, logfmt=logfmt)
                v_str = _format_value(v, key_path, max_len=30, logfmt=logfmt)
                formatted_items.append((key_display, v_str))
            max_key_len = max(len(k) for k, _ in formatted_items)
            field_width = max(max_key_len, 12)
            for k_display, v_str in formatted_items:
                padding = " " * (field_width - len(k_display))
                lines.append(f"    {k_display}{_SEP}:{_RESET}{padding} {v_str}")
            return lines
        value_str = _format_value(value, ".".join(path), logfmt=logfmt)
        return [f"  {path_str} {_SEP}={_RESET} {value_str}"]

    value_str = _format_value(value, ".".join(path), logfmt=logfmt)
    return [f"  {path_str} {_SEP}={_RESET} {value_str}"]


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


def format_action_header(action: str, user: str | None = None) -> str:
    """Format the action header line."""
    action_str = f"{_ACTION}{action}{_RESET}"
    if user:
        user_str = f"{_USER}{user}{_RESET}"
        return f"{action_str} by {user_str}"
    return action_str


def log_change(
    action: str,
    diff: dict,
    user: str | None = None,
    previous: dict | None = None,
    logfmt: Callable[[Any, str], str | None] | None = None,
    *,
    logger: logging.Logger = changes_logger,
    level: int = logging.INFO,
) -> None:
    """Log a database change with pretty-printed diff.

    Args:
        action: The action name (e.g., "login", "admin:delete_user").
        diff: The JSON diff dict.
        user: Optional already-formatted user name to show in the header.
        previous: The previous state dict (for determining add vs update).
        logfmt: Optional formatter callable ``(value, path) -> str | None``.
        logger: Logger to write to. Defaults to the ``kanta.changes`` logger.
        level: Log level to use. Defaults to ``logging.INFO``.
    """
    header = format_action_header(action, user)
    diff_lines = format_diff(diff, previous, logfmt)

    if not diff_lines:
        logger.log(level, header)
        return

    if len(diff_lines) == 1:
        logger.log(level, f"{header}{diff_lines[0]}")
    else:
        logger.log(level, header)
        for line in diff_lines:
            logger.log(level, line)


def configure_logging() -> None:
    """Configure the database logger to output to stderr without prefix."""
    if not changes_logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        changes_logger.addHandler(handler)
    changes_logger.setLevel(logging.INFO)
    changes_logger.propagate = False
