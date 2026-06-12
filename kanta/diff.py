"""Diff computation and replay utilities."""

import jsondiff

from kanta.structs import ChangeRecord
from kanta.serialization.base import ReplayResult, replay
from kanta.serialization.framing import LineFramer
from kanta.serialization.json import JsonSerializer


def compute_diff(previous: dict, current: dict) -> dict | None:
    """Compute a jsondiff patch between two dicts.

    Returns None if there is no difference.
    """
    return jsondiff.diff(previous, current, marshal=True) or None


def _apply_diff(state: dict, diff: dict) -> dict:
    """Apply a jsondiff patch manually, handling ``$replace`` and ``$delete``.

    jsondiff.patch does not handle nested ``$replace`` commands when the
    parent key is missing from the state.  This function recursively applies
    diffs, treating ``$replace`` as full replacement and ``$delete`` as
    key removal.
    """
    if not isinstance(diff, dict):
        return diff

    result = dict(state) if isinstance(state, dict) else state
    if not isinstance(result, dict):
        result = {}

    for key, value in diff.items():
        if key == "$replace":
            return value
        elif key == "$delete":
            if isinstance(value, list):
                for k in value:
                    result.pop(k, None)
            else:
                result.pop(value, None)
        elif isinstance(value, dict):
            old = result.get(key, {})
            if not isinstance(old, dict):
                old = {}
            result[key] = _apply_diff(old, value)
        else:
            result[key] = value

    return result


def patch_state(state: dict, diff: dict) -> dict:
    """Apply a jsondiff patch to a state dict.

    The diff was produced with ``marshal=True`` (string keys like
    ``"$replace"`` and ``"$delete"``) and decoded from JSON.
    """
    return _apply_diff(state, diff)


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
