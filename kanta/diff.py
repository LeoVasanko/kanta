"""Diff computation and replay utilities.

Diff format: JSON-serializable dicts where ``$delete`` is the only command
our producer emits; added keys and changed values (scalars, lists, type
changes — lists always wholesale) are plain assignments. A dict value
assigned over a non-dict needs no ``$replace``: the consumer can see from
the old value whether to patch (old is a dict) or replace. User keys
starting with ``$`` are escaped by prepending another ``$``
(``$foo`` -> ``$$foo``); values are stored verbatim.

The consumer additionally stays compatible with jsondiff's marshaled
syntax, so it can replay diffs produced by jsondiff itself: ``$replace``,
positional ``$insert``/``$delete`` and per-index nested diffs on lists,
and jsondiff's escaping of ``$``-prefixed values.
"""

from kanta.structs import ChangeRecord
from kanta.serialization.base import ReplayResult, apply_diff, replay
from kanta.serialization.framing import LineFramer
from kanta.serialization.json import JsonSerializer

_UNCHANGED = object()


def _escape_key(key: str) -> str:
    """Escape a user key for use as a diff key (``$foo`` -> ``$$foo``)."""
    if isinstance(key, str) and key.startswith("$"):
        return "$" + key
    return key


def _diff(previous, current):
    """Compute a raw diff, or _UNCHANGED if there is no difference."""
    if isinstance(previous, dict) and isinstance(current, dict):
        result = {}
        deleted = [_escape_key(k) for k in previous if k not in current]
        if deleted:
            result["$delete"] = deleted
        for key, new_value in current.items():
            if key not in previous:
                result[_escape_key(key)] = new_value
            else:
                sub = _diff(previous[key], new_value)
                if sub is not _UNCHANGED:
                    result[_escape_key(key)] = sub
        return result if result else _UNCHANGED
    if previous == current:
        return _UNCHANGED
    return current


def diff(previous: dict, current: dict) -> dict | None:
    """Compute a marshaled diff between two state dicts.

    Returns None if there is no difference.
    """
    result = _diff(previous, current)
    return result if result is not _UNCHANGED else None


def patch(state: dict, diff: dict) -> dict:
    """Apply a marshaled diff to a state dict."""
    return apply_diff(state, diff)


# Backward-compatible JSONL replay using the default serializer.
_default_serializer = JsonSerializer()
_default_framer = LineFramer()


def replay_jsonl(
    data: bytes,
    *,
    type: type[ChangeRecord] = ChangeRecord,
) -> ReplayResult:
    """Replay database state from JSONL file data.

    This is the legacy public API that hard-codes JSON/JSONL handling.
    """
    return replay(
        data,
        framer=_default_framer,
        decode=_default_serializer.decode,
    )
