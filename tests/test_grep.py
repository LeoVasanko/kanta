"""Tests for structural ``--grep`` matching and match highlighting."""

from datetime import UTC, datetime

from kanta.__main__ import main
from kanta.grep import (
    GrepPattern,
    _Entry,
    _find_spans,
    _match_path,
    _match_value,
    evaluate,
    mark_spans,
)
from kanta.logging import _USER_PATH
from kanta.serialization import JsonSerializer
from kanta.serialization.framing import LineFramer
from kanta.structs import ChangeRecord, Snapshot
from kanta.tty import strip_ansi

TS = datetime(2026, 1, 1, tzinfo=UTC)

MARK = "\x1b[48;5;220m"
UNMARK = "\x1b[49m"


def test_match_path_whole_elements_anywhere():
    assert _match_path("users", ["users"]) == frozenset({0})
    assert _match_path("users", ["data", "users", "alice"]) == frozenset({1})
    # Partial element matches require explicit wildcards.
    assert _match_path("users", ["foousers"]) is None
    assert _match_path("us*rs", ["foousers"]) is None
    assert _match_path("*users", ["foousers"]) == frozenset({0})


def test_match_path_contiguous_element_sequence():
    assert _match_path("alice.email", ["users", "alice", "email"]) == frozenset({1, 2})
    # The sequence must be contiguous.
    assert _match_path("users.email", ["users", "alice", "email"]) is None
    assert _match_path("users.*.email", ["users", "alice", "email"]) == frozenset(
        {0, 1, 2}
    )
    # Matching is case-insensitive and empty terms match without marking.
    assert _match_path("USERS", ["users"]) == frozenset({0})
    assert _match_path("", ["anything"]) == frozenset()


def test_match_value_substring_for_strings_full_for_scalars():
    assert _match_value("lice", "alice@example.com", "alice@example.com")
    assert not _match_value("bob", "alice@example.com", "alice@example.com")
    # Booleans, numbers and null match only in full.
    assert _match_value("true", True, "true")
    assert not _match_value("tru", True, "true")
    assert _match_value("42", 42, "42")
    assert not _match_value("4", 42, "42")
    assert _match_value("null", None, "null")
    # Wildcards match the whole value text of any type.
    assert _match_value("tru*", True, "true")
    assert _match_value("4*", 42, "42")
    # Containers do not match; their leaves are matched individually.
    assert not _match_value("email", {"email": "a@b.c"}, '{"email": "a@b.c"}')
    # An empty term matches anything.
    assert _match_value("", True, "true")


def test_mark_spans_merges_overlaps_and_adjacents():
    assert mark_spans("alice@example.com", [(0, 5), (3, 11)]) == (
        f"{MARK}alice@examp{UNMARK}le.com"
    )
    assert mark_spans("aab", [(0, 1), (1, 3)]) == f"{MARK}aab{UNMARK}"
    assert mark_spans("ab", [(1, 1), (2, 2)]) == "ab"


def test_mark_spans_offsets_into_styled_text():
    styled = "\x1b[32mal\x1b[0mlice"
    # A span crossing an escape sequence wraps each escape-free run.
    assert mark_spans(styled, [(0, 3)]) == (
        f"\x1b[32m{MARK}al{UNMARK}\x1b[0m{MARK}l{UNMARK}ice"
    )
    # A span covering everything wraps each escape-free run separately.
    assert mark_spans(styled, [(0, 6)]) == (
        f"\x1b[32m{MARK}al{UNMARK}\x1b[0m{MARK}lice{UNMARK}"
    )
    assert mark_spans(styled, []) == styled


def test_find_spans_case_insensitive_occurrences():
    assert _find_spans("Alice likes ALICE", "alice") == [(0, 5), (12, 17)]
    assert _find_spans("nope", "alice") == []
    assert _find_spans("anything", "") == []


def test_pattern_parse_forms():
    bare = GrepPattern.parse("alice")
    assert bare.term == "alice" and bare.path_term is None

    pair = GrepPattern.parse("users.alice.age=30")
    assert pair.term is None
    assert pair.path_term == "users.alice.age"
    assert pair.value_term == "30"

    value_only = GrepPattern.parse("=alice@example.com")
    assert value_only.path_term == "" and value_only.value_term == "alice@example.com"

    path_only = GrepPattern.parse("users.alice=")
    assert path_only.path_term == "users.alice" and path_only.value_term == ""

    # Split on the first '=' only; the value may contain '='.
    multi = GrepPattern.parse("key=a=b")
    assert multi.path_term == "key" and multi.value_term == "a=b"


def _patterns(*raws: str) -> list[GrepPattern]:
    return [GrepPattern.parse(raw) for raw in raws]


def test_evaluate_bare_term_against_path_value_action_user():
    record = ChangeRecord(
        ts=TS, a="create_user", u="admin", diff={"users": {"alice": {"age": 30}}}
    )
    assert evaluate(record, {}, _patterns("users.alice"))
    assert evaluate(record, {}, _patterns("30"))
    assert evaluate(record, {}, _patterns("CREATE_user"))
    assert evaluate(record, {}, _patterns("admin"))
    assert evaluate(record, {}, _patterns("age=30"))
    assert not evaluate(record, {}, _patterns("bob"))
    assert not evaluate(record, {}, _patterns("3"))  # not a full number match


def test_evaluate_unescapes_dollar_keys():
    record = ChangeRecord(ts=TS, a="set", diff={"$$config": 5})
    assert evaluate(record, {}, _patterns("$config=5"))


def test_evaluate_path_value_form_requires_same_line():
    record = ChangeRecord(ts=TS, a="set", diff={"a": {"x": 1}, "b": {"y": 2}})
    previous = {"a": {"x": 0}, "b": {"y": 0}}
    assert evaluate(record, previous, _patterns("a.x=1"))
    # 'a' matches one line's path, '2' another line's value: no match.
    assert not evaluate(record, previous, _patterns("a=2"))


def test_evaluate_all_patterns_same_record_any_line():
    record = ChangeRecord(ts=TS, a="set", diff={"a": {"x": 1}, "b": {"y": 2}})
    previous = {"a": {"x": 0}, "b": {"y": 0}}
    assert evaluate(record, previous, _patterns("a.x", "=2"))
    assert not evaluate(record, previous, _patterns("a.x", "=2", "missing"))


def test_evaluate_matches_deleted_content_by_previous_value():
    record = ChangeRecord(ts=TS, a="delete_user", diff={"users": {"$delete": "alice"}})
    previous = {"users": {"alice": {"email": "alice@example.com"}}}
    assert evaluate(record, previous, _patterns("alice@example.com"))
    assert evaluate(record, previous, _patterns("users.alice.email"))
    assert not evaluate(record, {}, _patterns("alice@example.com"))


def test_highlighter_marks_exactly_the_matched_regions():
    record = ChangeRecord(
        ts=TS,
        a="set",
        diff={
            "users": {"alice": {"email": "same@x.com"}, "bob": {"email": "same@x.com"}}
        },
    )
    previous = {
        "users": {"alice": {"email": "old@x.com"}, "bob": {"email": "same@x.com"}}
    }
    hl = evaluate(record, previous, _patterns("users.alice.email"))
    assert hl is not None
    # Path-side match: the matched elements are lit, the value is not.
    assert hl.path("alice", "users.alice") == f"{MARK}alice{UNMARK}"
    assert hl.value("same@x.com", "users.alice.email") == "same@x.com"
    # Bob's identical value is a different path: untouched.
    assert hl.path("bob", "users.bob") == "bob"
    assert hl.value("same@x.com", "users.bob.email") == "same@x.com"

    hl = evaluate(record, previous, _patterns("=same@x.com"))
    assert hl is not None
    # Both lines genuinely match the value: both are marked.
    assert hl.value("same@x.com", "users.alice.email") == f"{MARK}same@x.com{UNMARK}"
    assert hl.value("same@x.com", "users.bob.email") == f"{MARK}same@x.com{UNMARK}"
    assert hl.path("alice", "users.alice") == "alice"


def test_highlighter_merges_overlapping_needles_from_different_patterns():
    record = ChangeRecord(ts=TS, a="set", diff={"email": "alice@example.com"})
    hl = evaluate(record, {}, _patterns("alice", "lice@exam"))
    assert hl is not None
    assert hl.value("alice@example.com", "email") == (
        f"{MARK}alice@exam{UNMARK}ple.com"
    )


def test_highlighter_marks_delete_marker_on_removed_content_match():
    record = ChangeRecord(ts=TS, a="delete_user", diff={"users": {"$delete": "bob"}})
    previous = {"users": {"bob": {"email": "bob@example.com"}}}
    hl = evaluate(record, previous, _patterns("bob@example.com"))
    assert hl is not None
    # The matched content is gone: the deletion marker is lit, not the path.
    assert hl.path("users", "users") == "users"
    assert hl.path("bob", "users.bob") == "bob"
    assert hl.delete("✗", "users.bob") == f"{MARK}✗{UNMARK}"
    assert hl.delete("✗", "users.alice") == "✗"

    # A path-side match lights the genuinely matched elements of the anchor,
    # and the marker for the element below it.
    hl = evaluate(record, previous, _patterns("users.bob.email"))
    assert hl is not None
    assert hl.path("users", "users") == f"{MARK}users{UNMARK}"
    assert hl.path("bob", "users.bob") == f"{MARK}bob{UNMARK}"
    assert hl.delete("✗", "users.bob") == f"{MARK}✗{UNMARK}"

    # A path-side match of one displayed element lights only that element.
    hl = evaluate(record, previous, _patterns("bob"))
    assert hl is not None
    assert hl.path("users", "users") == "users"
    assert hl.path("bob", "users.bob") == f"{MARK}bob{UNMARK}"


def test_highlighter_meta_marks_action_and_user():
    record = ChangeRecord(ts=TS, a="create_user", u="admin", diff={"x": 1})
    hl = evaluate(record, {}, _patterns("create", "ADM"))
    assert hl is not None
    assert hl.meta("create_user", "action") == f"{MARK}create{UNMARK}_user"
    assert hl.meta("admin", "user") == f"{MARK}adm{UNMARK}in"
    assert hl.meta("extra", "other") == "extra"


def test_entry_dataclass_holds_anchor_for_deletes():
    entry = _Entry(["a", "b"], 1, "1", anchor=["a"])
    assert entry.anchor == ["a"]


def _write_db(path, changes, state=None):
    serializer = JsonSerializer()
    framer = LineFramer()
    snapshot = Snapshot(ts=TS, v=1, state=state or {})
    data = framer.frame_snapshot(serializer.encode(snapshot), record_offset=0)
    for change in changes:
        data += framer.frame_change(serializer.encode(change), record_offset=0)
    path.write_bytes(data)


def _sample_changes():
    return [
        ChangeRecord(
            ts=TS,
            a="create_alice",
            u="admin",
            diff={
                "users": {
                    "alice": {"email": "alice@example.com", "admin": True},
                }
            },
        ),
        ChangeRecord(
            ts=TS,
            a="update_alice",
            u="admin",
            diff={"users": {"alice": {"age": 30}}},
        ),
        ChangeRecord(
            ts=TS,
            a="create_bob",
            u="bob",
            diff={"users": {"bob": {"email": "bob@example.com"}}},
        ),
    ]


def _run_cli(tmp_path, capsys, monkeypatch, changes, *args, color=False):
    if color:
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.delenv("FORCE_COLOR", raising=False)
    path = tmp_path / "test.kantadb"
    _write_db(path, changes)
    code = main([str(path), *args])
    assert code == 0
    return capsys.readouterr().err


def test_cli_grep_filters_records(tmp_path, capsys, monkeypatch):
    """Only matching change records are printed."""
    err = _run_cli(tmp_path, capsys, monkeypatch, _sample_changes(), "--grep", "alice")
    assert "create_alice" in err
    assert "update_alice" in err
    assert "create_bob" not in err
    assert "bob@example.com" not in err


def test_cli_grep_prints_entire_transaction(tmp_path, capsys, monkeypatch):
    """A match prints the whole record, not just the matching line."""
    err = _run_cli(
        tmp_path, capsys, monkeypatch, _sample_changes(), "--grep", "users.alice.email"
    )
    assert "create_alice" in err
    # Non-matching lines of the same record are printed too.
    assert "admin" in err and "true" in err
    # The update record does not contain the path.
    assert "update_alice" not in err


def test_cli_grep_repeated_patterns_must_all_match(tmp_path, capsys, monkeypatch):
    """Repeated --grep options are ANDed within the same record."""
    err = _run_cli(
        tmp_path,
        capsys,
        monkeypatch,
        _sample_changes(),
        "--grep",
        "alice",
        "--grep",
        "=alice@example.com",
    )
    assert "create_alice" in err
    # update_alice matches 'alice' but has no email value.
    assert "update_alice" not in err

    err = _run_cli(
        tmp_path,
        capsys,
        monkeypatch,
        _sample_changes(),
        "--grep",
        "alice",
        "--grep",
        "bob",
    )
    assert "create_alice" not in err
    assert "create_bob" not in err


def test_cli_grep_no_match_exits_zero(tmp_path, capsys, monkeypatch):
    """No matching records is not an error; snapshot lines still print."""
    err = _run_cli(tmp_path, capsys, monkeypatch, _sample_changes(), "--grep", "nobody")
    assert "snapshot s0" in err
    assert "create_alice" not in err
    assert "create_bob" not in err


def test_cli_grep_path_value_forms(tmp_path, capsys, monkeypatch):
    """The 'path=value', 'path=' and '=value' forms restrict the match side."""
    err = _run_cli(
        tmp_path, capsys, monkeypatch, _sample_changes(), "--grep", "users.*.admin=true"
    )
    assert "create_alice" in err
    assert "create_bob" not in err

    err = _run_cli(
        tmp_path, capsys, monkeypatch, _sample_changes(), "--grep", "users.alice.age="
    )
    assert "update_alice" in err
    assert "create_alice" not in err

    err = _run_cli(
        tmp_path, capsys, monkeypatch, _sample_changes(), "--grep", "=bob@example.com"
    )
    assert "create_bob" in err
    assert "create_alice" not in err


def test_cli_grep_path_elements_match_in_full(tmp_path, capsys, monkeypatch):
    """'users' does not match a 'foousers' element; 'us*' does."""
    changes = [ChangeRecord(ts=TS, a="trap", diff={"foousers": {"note": "x"}})]
    err = _run_cli(tmp_path, capsys, monkeypatch, changes, "--grep", "users")
    assert "trap" not in err

    err = _run_cli(tmp_path, capsys, monkeypatch, changes, "--grep", "*users")
    assert "trap" in err


def _shared_email_changes():
    return [
        ChangeRecord(
            ts=TS,
            a="create_alice",
            u="admin",
            diff={"users": {"alice": {"email": "shared@example.com", "admin": True}}},
        ),
        ChangeRecord(
            ts=TS,
            a="create_bob",
            u="admin",
            diff={"users": {"bob": {"email": "shared@example.com"}}},
        ),
        ChangeRecord(
            ts=TS,
            a="delete_bob",
            u="admin",
            diff={"users": {"$delete": "bob"}},
        ),
    ]


def _lines_with(err: str, text: str) -> list[str]:
    return [line for line in err.splitlines() if text in strip_ansi(line)]


def test_cli_highlight_marks_exactly_the_matched_regions(tmp_path, capsys, monkeypatch):
    """A path-side match lights only the matched elements, not the value."""
    err = _run_cli(
        tmp_path,
        capsys,
        monkeypatch,
        _shared_email_changes(),
        "--grep",
        "users.alice.email",
        color=True,
    )
    (alice_line,) = _lines_with(err, "shared@example.com")
    assert f"{MARK}alice{UNMARK}" in alice_line
    assert f"{MARK}email{UNMARK}" in alice_line
    # The value itself did not match, so it is not highlighted.
    assert f"{MARK}shared@example.com{UNMARK}" not in alice_line
    (header_line,) = _lines_with(err, "users =")
    assert f"{MARK}users{UNMARK}" in header_line
    assert "create_bob" not in err


def test_cli_highlight_value_matches_on_all_matching_lines(
    tmp_path, capsys, monkeypatch
):
    """A value-side match lights the value on every line that genuinely matched."""
    err = _run_cli(
        tmp_path,
        capsys,
        monkeypatch,
        _shared_email_changes(),
        "--grep",
        "=shared@example.com",
        color=True,
    )
    lines = _lines_with(err, "shared@example.com")
    assert len(lines) == 2  # alice's and bob's records both matched
    for line in lines:
        assert f"{MARK}shared@example.com{UNMARK}" in line
    # The deletion of bob matched by its previous (removed) value: the
    # deletion marker is lit, not the deleted path.
    (delete_line,) = _lines_with(err, "✗")
    assert f"{MARK}✗{UNMARK}" in delete_line
    assert f"{MARK}bob{UNMARK}" not in delete_line
    assert f"{MARK}users{UNMARK}" not in delete_line


def test_cli_highlight_merges_overlapping_matches(tmp_path, capsys, monkeypatch):
    """Overlapping matches from different patterns form one continuous mark."""
    err = _run_cli(
        tmp_path,
        capsys,
        monkeypatch,
        _shared_email_changes(),
        "--grep",
        "shared",
        "--grep",
        "red@exam",
        color=True,
    )
    lines = _lines_with(err, "shared@example.com")
    assert len(lines) == 2  # both records genuinely match both patterns
    for line in lines:
        assert f"{MARK}shared@exam{UNMARK}ple.com" in line


def test_cli_highlight_full_scalar_and_header_fields(tmp_path, capsys, monkeypatch):
    """Scalars are marked in full; matched action/user substrings are marked."""
    err = _run_cli(
        tmp_path,
        capsys,
        monkeypatch,
        _shared_email_changes(),
        "--grep",
        "admin=true",
        "--grep",
        "create_al",
        color=True,
    )
    (line,) = _lines_with(err, "shared@example.com")
    assert f"{MARK}true{UNMARK}" in line
    (header,) = _lines_with(err, "create_alice")
    assert f"{MARK}create_al{UNMARK}ice" in header
    # Only the record matching both patterns is printed.
    assert "create_bob" not in err
    assert "delete_bob" not in err


def test_evaluate_matches_prettified_and_raw_value_forms():
    """With logfmt, both the raw and the prettified form of a value match."""
    record = ChangeRecord(ts=TS, a="set", diff={"when": 1767225600})

    def logfmt(value, path):
        return "2026-01-01" if value == 1767225600 else None

    # The prettified form matches; the needle is located in the display text.
    hl = evaluate(record, {}, _patterns("2026"), logfmt=logfmt)
    assert hl is not None
    assert hl.value("2026-01-01", "when") == f"{MARK}2026{UNMARK}-01-01"
    # The raw form matches too; its needle is absent from the displayed
    # text, so the whole displayed value is marked.
    hl = evaluate(record, {}, _patterns("1767225600"), logfmt=logfmt)
    assert hl is not None
    assert hl.value("2026-01-01", "when") == f"{MARK}2026-01-01{UNMARK}"
    # Without logfmt the raw value is matched and marked precisely.
    hl = evaluate(record, {}, _patterns("1767225600"))
    assert hl.value("1767225600", "when") == f"{MARK}1767225600{UNMARK}"
    # Neither form matches.
    assert evaluate(record, {}, _patterns("1999"), logfmt=logfmt) is None


def test_evaluate_matches_prettified_user():
    """The user field matches both the raw id and the logfmt-resolved name."""

    def logfmt(value, path):
        return "Alice Admin" if path == _USER_PATH else None

    record = ChangeRecord(ts=TS, a="set", u="u123", diff={"x": 1})
    hl = evaluate(record, {}, _patterns("alice"), logfmt=logfmt)
    assert hl is not None
    assert hl.meta("Alice Admin", "user") == f"{MARK}Alice{UNMARK} Admin"
    # A raw-only user match marks the whole displayed name.
    hl = evaluate(record, {}, _patterns("u123"), logfmt=logfmt)
    assert hl is not None
    assert hl.meta("Alice Admin", "user") == f"{MARK}Alice Admin{UNMARK}"
    # The action is never prettified.
    assert hl.meta("set", "action") == "set"


def test_evaluate_without_logfmt_matches_raw_only():
    record = ChangeRecord(ts=TS, a="set", u="u123", diff={"when": 1767225600})
    assert evaluate(record, {}, _patterns("1767225600"))
    assert evaluate(record, {}, _patterns("2026")) is None
    assert evaluate(record, {}, _patterns("alice")) is None


def test_cli_grep_matches_prettified_forms_with_kanta_object(
    tmp_path, capsys, monkeypatch
):
    """End to end: a -k object's logfmt formatter doubles the match surface."""
    db_path = tmp_path / "test.kantadb"
    module = tmp_path / "dbmod.py"
    module.write_text(
        "from typing import Any\n"
        "from kanta import Kanta\n"
        f"kanta = Kanta({str(db_path)!r}, {{}}, type=dict)\n"
        "@kanta.logfmt\n"
        "def pretty(value: Any, path: str) -> str | None:\n"
        "    if path == 'when':\n"
        "        return 'Nov 3, 2025'\n"
        "    if path == '$user':\n"
        "        return 'Alice Admin'\n"
        "    return None\n"
    )
    changes = [ChangeRecord(ts=TS, a="set", u="u123", diff={"when": 1767225600})]
    kanta_args = ("-k", str(module))

    # A term matching only the prettified value finds the record.
    err = _run_cli(
        tmp_path, capsys, monkeypatch, changes, *kanta_args, "--grep", "nov", color=True
    )
    (line,) = _lines_with(err, "Nov 3, 2025")
    assert f"{MARK}Nov{UNMARK} 3, 2025" in line

    # A term matching only the raw value marks the whole prettified display.
    err = _run_cli(
        tmp_path,
        capsys,
        monkeypatch,
        changes,
        *kanta_args,
        "--grep",
        "1767225600",
        color=True,
    )
    (line,) = _lines_with(err, "Nov 3, 2025")
    assert f"{MARK}Nov 3, 2025{UNMARK}" in line

    # The prettified user matches, and the raw id marks the whole display.
    err = _run_cli(
        tmp_path,
        capsys,
        monkeypatch,
        changes,
        *kanta_args,
        "--grep",
        "alice",
        color=True,
    )
    (header,) = _lines_with(err, "Alice Admin")
    assert f"{MARK}Alice{UNMARK} Admin" in header
    err = _run_cli(
        tmp_path,
        capsys,
        monkeypatch,
        changes,
        *kanta_args,
        "--grep",
        "u123",
        color=True,
    )
    (header,) = _lines_with(err, "Alice Admin")
    assert f"{MARK}Alice Admin{UNMARK}" in header

    # Without -k there is no prettified form to match.
    err = _run_cli(tmp_path, capsys, monkeypatch, changes, "--grep", "nov")
    assert not _lines_with(err, "u123")
    err = _run_cli(tmp_path, capsys, monkeypatch, changes, "--grep", "1767225600")
    assert _lines_with(err, "u123")
