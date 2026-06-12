"""Database change logging with pretty-printed diffs.

Provides a logger for JSONL database changes that formats diffs
in a human-readable path.notation style with color coding.
"""

import logging
import re
import sys
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("kanta.changes")

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


def _format_value(
    value: Any, max_len: int = 60, resolver: Callable[[str], str] | None = None
) -> str:
    """Format a value for display, truncating if needed."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        value = _UNSAFE_CHARS.sub("", value)
        if resolver is not None:
            resolved = resolver(value)
            if resolved != value:
                return resolved
        if len(value) > max_len:
            return value[: max_len - 3] + "..."
        return value
    if isinstance(value, dict):
        if not value:
            return "{}"
        all_true = all(v is True for v in value.values())
        parts = []
        for k, v in value.items():
            key_display = resolver(k) if resolver is not None else k
            if all_true:
                parts.append(key_display)
            else:
                val_display = _format_value(v, max_len=30, resolver=resolver)
                parts.append(f"{key_display}: {val_display}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        parts = [_format_value(v, max_len=30, resolver=resolver) for v in value]
        return "[" + ", ".join(parts) + "]"
    text = str(value)
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


def _format_path(path: list[str], resolver: Callable[[str], str] | None = None) -> str:
    """Format a path as dot notation with prefix in dark grey, final in default."""
    if not path:
        return ""
    if resolver is not None:
        path = [resolver(p) for p in path]
    if len(path) == 1:
        return f"{_PATH_FINAL}{path[0]}{_RESET}"
    prefix = ".".join(path[:-1])
    final = path[-1]
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
    resolver: Callable[[str], str] | None = None,
) -> list[str]:
    """Format a single change as one or more lines."""

    def fmt_value(v: Any, child_path: list[str]) -> str:
        return _format_value(v, resolver=resolver)

    formatted_path = list(path)
    if resolver is not None:
        formatted_path = [resolver(p) for p in formatted_path]

    if change_type == "delete":
        if len(formatted_path) == 1:
            return [f"  {_DELETE}{formatted_path[0]} ✗{_RESET}"]
        prefix = ".".join(formatted_path[:-1])
        final = formatted_path[-1]
        return [f"  {_PATH_PREFIX}{prefix}.{_RESET}{_DELETE}{final} ✗{_RESET}"]

    if change_type == "add":
        if isinstance(value, dict) and value:
            lines = []
            if len(formatted_path) == 1:
                lines.append(f"  {_ADD}{formatted_path[0]}{_RESET} {_SEP}={_RESET}")
            else:
                prefix = ".".join(formatted_path[:-1])
                final = formatted_path[-1]
                lines.append(
                    f"  {_PATH_PREFIX}{prefix}.{_RESET}{_ADD}{final}{_RESET} {_SEP}={_RESET}"
                )
            formatted_items = []
            for k, v in value.items():
                k_display = resolver(k) if resolver is not None else k
                v_str = fmt_value(v, path + [k])
                formatted_items.append((k_display, v_str))
            max_key_len = max(len(k) for k, _ in formatted_items)
            field_width = max(max_key_len, 12)
            for k_display, v_str in formatted_items:
                padding = " " * (field_width - len(k_display))
                lines.append(f"    {k_display}{_SEP}:{_RESET}{padding} {v_str}")
            return lines
        else:
            value_str = fmt_value(value, path)
            if len(formatted_path) == 1:
                return [
                    f"  {_ADD}{formatted_path[0]}{_RESET} {_SEP}={_RESET} {value_str}"
                ]
            prefix = ".".join(formatted_path[:-1])
            final = formatted_path[-1]
            return [
                f"  {_PATH_PREFIX}{prefix}.{_RESET}{_ADD}{final}{_RESET} {_SEP}={_RESET} {value_str}"
            ]

    value_str = fmt_value(value, path)
    path_str = _format_path(path, resolver=resolver)
    return [f"  {path_str} {_SEP}={_RESET} {value_str}"]


def format_diff(
    diff: dict,
    previous: dict | None = None,
    resolver: Callable[[str], str] | None = None,
) -> list[str]:
    """Format a JSON diff as human-readable lines.

    Args:
        diff: The JSON diff dict.
        previous: The previous state dict (for determining add vs update).
        resolver: Optional callable to resolve path components (e.g. UUID→name).

    Returns a list of formatted lines (without newlines).
    """
    changes: list[tuple[str, list[str], Any]] = []
    _collect_changes(diff, [], changes, previous)
    if not changes:
        return []
    lines = []
    for change_type, path, value in changes:
        lines.extend(_format_change_lines(change_type, path, value, resolver))
    return lines


def format_action_header(action: str, user_display: str | None = None) -> str:
    """Format the action header line."""
    action_str = f"{_ACTION}{action}{_RESET}"
    if user_display:
        user_str = f"{_USER}{user_display}{_RESET}"
        return f"{action_str} by {user_str}"
    return action_str


def log_change(
    action: str,
    diff: dict,
    user_display: str | None = None,
    previous: dict | None = None,
    resolver: Callable[[str], str] | None = None,
) -> None:
    """Log a database change with pretty-printed diff.

    Args:
        action: The action name (e.g., "login", "admin:delete_user").
        diff: The JSON diff dict.
        user_display: Optional display name of the user who performed the action.
        previous: The previous state dict (for determining add vs update).
        resolver: Optional callable to resolve path components (e.g. UUID→name).
    """
    header = format_action_header(action, user_display)
    diff_lines = format_diff(diff, previous, resolver)

    if not diff_lines:
        logger.info(header)
        return

    if len(diff_lines) == 1:
        logger.info(f"{header}{diff_lines[0]}")
    else:
        logger.info(header)
        for line in diff_lines:
            logger.info(line)


def configure_logging() -> None:
    """Configure the database logger to output to stderr without prefix."""
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
