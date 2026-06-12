from kanta import compute_diff


def test_no_diff():
    assert compute_diff({"a": 1}, {"a": 1}) is None


def test_simple_diff():
    diff = compute_diff({"a": 1}, {"a": 2})
    assert diff is not None
    assert diff == {"a": 2}


def test_nested_diff():
    diff = compute_diff({"x": {"y": 1}}, {"x": {"y": 2}})
    assert diff == {"x": {"y": 2}}
