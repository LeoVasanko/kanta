from kanta import format_diff


def test_add():
    lines = format_diff({"name": "Alice"}, previous={})
    assert any("name" in line for line in lines)


def test_update():
    lines = format_diff({"name": "Bob"}, previous={"name": "Alice"})
    assert any("Bob" in line for line in lines)


def test_delete():
    lines = format_diff({"$delete": ["old_key"]}, previous={"old_key": 1})
    assert any("old_key" in line for line in lines)


def test_resolver():
    lines = format_diff(
        {"users": {"uuid-1": {"name": "Alice"}}},
        previous={},
        resolver=lambda x: "Alice" if x == "uuid-1" else x,
    )
    assert any("Alice" in line for line in lines)
