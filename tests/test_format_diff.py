from kanta.logging import format_diff
from kanta.tty import ESC, colors

_ADD = f"{ESC}{colors.add}m"
_DELETE = f"{ESC}{colors.delete}m"


def test_add():
    lines = format_diff({"name": "Alice"}, previous={})
    assert any("name" in line for line in lines)


def test_add_path_is_green():
    lines = format_diff({"name": "Alice"}, previous={})
    assert any(_ADD in line for line in lines)


def test_nested_add_path_final_element_is_green():
    lines = format_diff({"users": {"alice": 1}}, previous={"users": {}})
    assert any(_ADD in line and "alice" in line for line in lines)


def test_update_path_not_colored_as_add():
    lines = format_diff({"name": "Bob"}, previous={"name": "Alice"})
    assert lines
    assert all(_ADD not in line for line in lines)


def test_delete_path_not_colored_as_add():
    lines = format_diff({"$delete": ["old_key"]}, previous={"old_key": 1})
    assert any(_DELETE in line for line in lines)
    assert all(_ADD not in line for line in lines)


def test_update():
    lines = format_diff({"name": "Bob"}, previous={"name": "Alice"})
    assert any("Bob" in line for line in lines)


def test_delete():
    lines = format_diff({"$delete": ["old_key"]}, previous={"old_key": 1})
    assert any("old_key" in line for line in lines)


def test_logfmt():
    lines = format_diff(
        {"users": {"uuid-1": {"name": "Alice"}}},
        previous={},
        logfmt=lambda value, path: "Alice" if value == "uuid-1" else None,
    )
    assert any("Alice" in line for line in lines)


def test_logfmt_uses_path_context():
    lines = format_diff(
        {
            "users": {"uuid-1": {"name": "Alice"}},
            "groups": {"uuid-1": {"name": "Admins"}},
        },
        previous={},
        logfmt=lambda value, path: (
            "User Alice" if path.startswith("users.") and value == "uuid-1" else None
        ),
    )
    assert any("User Alice" in line for line in lines)
    assert any("uuid-1" in line for line in lines)


def test_logfmt_formats_non_string_value():
    lines = format_diff(
        {"count": 42},
        previous={},
        logfmt=lambda value, path: "forty-two" if value == 42 else None,
    )
    assert any("forty-two" in line for line in lines)
