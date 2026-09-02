"""Tests for our own diff producer/consumer and jsondiff compatibility.

jsondiff is a dev dependency used only here, to verify that:

- jsondiff.patch(..., marshal=True) can apply patches produced by
  compute_diff (our format is a subset of jsondiff's marshaled syntax);
- apply_diff can apply patches produced by jsondiff.diff(..., marshal=True),
  including positional $insert/$delete list edits and per-index nested diffs.
"""

import jsondiff
import pytest

from kanta.diff import compute_diff, patch_state
from kanta.logging import format_diff
from kanta.serialization.base import apply_diff

# --- Producer: compute_diff ------------------------------------------------


def test_no_diff():
    assert compute_diff({"a": 1}, {"a": 1}) is None
    assert compute_diff({}, {}) is None


def test_simple_diff():
    diff = compute_diff({"a": 1}, {"a": 2})
    assert diff is not None
    assert diff == {"a": 2}


def test_nested_diff():
    diff = compute_diff({"x": {"y": 1}}, {"x": {"y": 2}})
    assert diff == {"x": {"y": 2}}


def test_key_added():
    assert compute_diff({"a": 1}, {"a": 1, "b": 2}) == {"b": 2}


def test_key_removed():
    assert compute_diff({"a": 1, "b": 2}, {"a": 1}) == {"$delete": ["b"]}


def test_last_key_removed_is_delete_not_replace():
    # jsondiff's minimal-diff search emits {"$replace": {}} here; we emit
    # what actually happened: the key was deleted.
    assert compute_diff({"a": 1}, {}) == {"$delete": ["a"]}
    assert compute_diff({"x": {"y": 1}}, {"x": {}}) == {"x": {"$delete": ["y"]}}


def test_list_changes_are_full_assignment():
    # No $insert/$delete positional edits: lists are replaced wholesale.
    assert compute_diff({"l": [1, 2]}, {"l": [1, 2, 3]}) == {"l": [1, 2, 3]}
    assert compute_diff({"l": [1, 2, 3]}, {"l": [1, 3]}) == {"l": [1, 3]}
    assert compute_diff({"l": [1]}, {"l": []}) == {"l": []}


def test_list_with_unchanged_prefix_is_full_assignment():
    diff = compute_diff({"l": ["a", "b", "c"]}, {"l": ["a", "x", "b", "c"]})
    assert diff == {"l": ["a", "x", "b", "c"]}


def test_type_changes_are_full_assignment():
    assert compute_diff({"a": {"x": 1}}, {"a": [1]}) == {"a": [1]}
    # A dict replacing a non-dict is a plain assignment too: the consumer
    # sees from the old value whether to patch (dict) or replace.
    assert compute_diff({"a": [1]}, {"a": {"x": 1}}) == {"a": {"x": 1}}
    assert compute_diff({"a": 1}, {"a": None}) == {"a": None}


def test_new_dict_value_assigned_wholesale():
    assert compute_diff({}, {"a": {"x": 1}}) == {"a": {"x": 1}}


def test_dollar_keys_escaped():
    assert compute_diff({}, {"$weird": 1}) == {"$$weird": 1}
    assert compute_diff({"$weird": 1}, {"$weird": 2}) == {"$$weird": 2}
    assert compute_diff({"$weird": 1}, {}) == {"$delete": ["$$weird"]}


def test_dollar_values_not_escaped():
    # Only keys are escaped; values are stored verbatim, even "$delete".
    assert compute_diff({"s": 1}, {"s": "$y"}) == {"s": "$y"}
    assert compute_diff({"s": 1}, {"s": "$delete"}) == {"s": "$delete"}
    assert compute_diff({}, {"o": {"s": "$y", "l": ["$z"]}}) == {
        "o": {"s": "$y", "l": ["$z"]}
    }


# --- Consumer: apply_diff / patch_state -------------------------------------


def test_patch_state_delegates():
    assert patch_state({"a": 1}, {"a": 2}) == {"a": 2}


def test_apply_scalar_and_add():
    assert apply_diff({"a": 1}, {"a": 2, "b": 3}) == {"a": 2, "b": 3}


def test_apply_delete():
    assert apply_diff({"a": 1, "b": 2}, {"$delete": ["b"]}) == {"a": 1}
    assert apply_diff({"a": 1}, {"$delete": ["a"]}) == {}


def test_apply_replace():
    assert apply_diff({"a": 1, "b": 2}, {"$replace": {"c": 3}}) == {"c": 3}
    assert apply_diff({"x": {"a": 1}}, {"x": {"$replace": [1]}}) == {"x": [1]}


def test_apply_nested_delete():
    diff = {"x": {"$delete": ["y"]}}
    assert apply_diff({"x": {"y": 2, "z": 3}}, diff) == {"x": {"z": 3}}


def test_apply_list_insert():
    diff = {"l": {"$insert": [[1, "x"]]}}
    assert apply_diff({"l": ["a", "b"]}, diff) == {"l": ["a", "x", "b"]}


def test_apply_list_delete():
    diff = {"l": {"$delete": [1]}}
    assert apply_diff({"l": ["a", "b", "c"]}, diff) == {"l": ["a", "c"]}


def test_apply_list_delete_multiple_positions():
    # jsondiff emits positions in descending order for sequential pops.
    diff = {"l": {"$delete": [4, 2, 0]}}
    assert apply_diff({"l": [0, 1, 2, 3, 4]}, diff) == {"l": [1, 3]}


def test_apply_list_insert_and_delete():
    diff = {"l": {"$insert": [[0, 9], [2, 8], [4, 7]], "$delete": [2, 0]}}
    assert apply_diff({"l": [0, 1, 2, 3]}, diff) == {"l": [9, 1, 8, 3, 7]}


def test_apply_list_per_index_nested_diff():
    diff = {"l": {"1": {"y": 3}}}
    state = {"l": [{"x": 1}, {"y": 2}]}
    assert apply_diff(state, diff) == {"l": [{"x": 1}, {"y": 3}]}


def test_apply_escaped_keys_and_values():
    assert apply_diff({}, {"$$weird": 1}) == {"$weird": 1}
    assert apply_diff({"$weird": 1}, {"$delete": ["$$weird"]}) == {}
    # jsondiff escapes $-values as "$$.."; those are unescaped on apply.
    assert apply_diff({"s": 1}, {"s": "$$y"}) == {"s": "$y"}
    assert apply_diff({"s": 1}, {"s": "$$delete"}) == {"s": "$delete"}
    # Our own producer stores values verbatim; single-$ stays as-is.
    assert apply_diff({"s": 1}, {"s": "$y"}) == {"s": "$y"}
    assert apply_diff({"s": 1}, {"s": "$delete"}) == {"s": "$delete"}
    assert apply_diff({}, {"o": {"s": "$$y", "l": ["$$z"]}}) == {
        "o": {"s": "$y", "l": ["$z"]}
    }


def test_apply_empty_diff():
    assert apply_diff({"a": 1}, {}) == {"a": 1}


def test_apply_diff_on_missing_state():
    assert apply_diff({}, {"a": {"b": 1}}) == {"a": {"b": 1}}


def test_apply_bare_dict_replaces_non_dict():
    # Our own producer emits no $replace; a dict over a non-dict old value
    # is a wholesale replacement.
    assert apply_diff({"a": [1, 2]}, {"a": {"x": 1}}) == {"a": {"x": 1}}
    assert apply_diff({"a": 5}, {"a": {"x": 1}}) == {"a": {"x": 1}}
    assert apply_diff({"a": None}, {"a": {"x": 1}}) == {"a": {"x": 1}}


def test_apply_list_patch_still_works_on_lists():
    # jsondiff-style per-index diff keeps list-op semantics on list state.
    assert apply_diff({"l": [1, 2]}, {"l": {"1": 9}}) == {"l": [1, 9]}


# --- jsondiff compatibility, both directions --------------------------------

COMPAT_CASES = [
    ("scalar change", {"a": 1}, {"a": 2}),
    ("key add", {"a": 1}, {"a": 1, "b": 2}),
    ("key remove", {"a": 1, "b": 2}, {"a": 1}),
    ("last key removed", {"a": 1}, {}),
    ("nested delete", {"a": {"x": 1, "y": 2}}, {"a": {"x": 1}}),
    ("nested mixed", {"a": {"x": 1, "y": 2}}, {"a": {"x": 9, "z": 3}}),
    ("list append", {"l": [1, 2]}, {"l": [1, 2, 3]}),
    ("list insert mid", {"l": [1, 2, 3]}, {"l": [1, 9, 2, 3]}),
    ("list remove mid", {"l": [1, 2, 3]}, {"l": [1, 3]}),
    ("list remove many", {"l": [0, 1, 2, 3, 4]}, {"l": [1, 3]}),
    ("list replace all", {"l": [1, 2]}, {"l": [3, 4]}),
    ("list insert+delete", {"l": [0, 1, 2, 3]}, {"l": [9, 1, 8, 3, 7]}),
    ("dict in list", {"l": [{"x": 1}, {"y": 2}]}, {"l": [{"x": 1}, {"y": 3}]}),
    ("list to empty", {"l": [1]}, {"l": []}),
    ("type change dict->list", {"a": {"x": 1}}, {"a": [1]}),
    ("type change list->dict", {"a": [1]}, {"a": {"x": 1}}),
    ("dollar key", {"$k": 1, "b": 1}, {"$k": 2}),
    ("dollar value", {"s": "$x"}, {"s": "$y"}),
    (
        "deep nesting",
        {"a": {"b": {"c": {"d": 1, "e": 2}}}},
        {"a": {"b": {"c": {"d": 9}}}},
    ),
]


# jsondiff.patch cannot apply our patches for "type change list->dict"
# (we emit a bare dict where jsondiff needs $replace) and "dollar value"
# (we do not escape "$"-prefixed values; jsondiff.patch would strip the
# "$"), so those cases are excluded from this direction.
JSONDIFF_APPLIES_CASES = [
    c for c in COMPAT_CASES if c[0] not in {"type change list->dict", "dollar value"}
]


@pytest.mark.parametrize(
    "name,old,new", JSONDIFF_APPLIES_CASES, ids=[c[0] for c in JSONDIFF_APPLIES_CASES]
)
def test_jsondiff_applies_our_patches(name, old, new):
    diff = compute_diff(old, new)
    assert diff is not None
    assert jsondiff.patch(old, diff, marshal=True) == new


@pytest.mark.parametrize("name,old,new", COMPAT_CASES, ids=[c[0] for c in COMPAT_CASES])
def test_we_apply_jsondiff_patches(name, old, new):
    diff = jsondiff.diff(old, new, marshal=True)
    assert apply_diff(old, diff) == new


@pytest.mark.parametrize("name,old,new", COMPAT_CASES, ids=[c[0] for c in COMPAT_CASES])
def test_our_own_round_trip(name, old, new):
    diff = compute_diff(old, new)
    assert diff is not None
    assert apply_diff(old, diff) == new


def test_no_diff_means_equal_states():
    for _name, old, new in COMPAT_CASES:
        assert compute_diff(old, new) is not None  # cases really differ
    assert compute_diff({"a": [1, {"b": "$x"}]}, {"a": [1, {"b": "$x"}]}) is None


# --- Logging ----------------------------------------------------------------


def test_format_diff_list_edit_shows_whole_list():
    diff = jsondiff.diff({"l": [1, 2, 3]}, {"l": [1, 9, 3]}, marshal=True)
    lines = format_diff(diff, previous={"l": [1, 2, 3]})
    text = "\n".join(lines)
    assert "$insert" not in text
    assert "[1, 9, 3]" in text


def test_format_diff_unescapes_dollar_keys():
    lines = format_diff({"$$weird": 1}, previous={})
    assert any("$weird" in line and "$$weird" not in line for line in lines)
