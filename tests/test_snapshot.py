from kanta.snapshot import SnapshotState


def test_no_write_below_min_diffs():
    class FakeFile:
        def __init__(self):
            self.written = []
            self.is_open = True

        def write(self, data: bytes):
            self.written.append(data)

    ss = SnapshotState(min_diffs=10)
    ss.record_changes(5)
    f = FakeFile()
    ss.maybe_write(f, 1, {"x": 1})
    assert len(f.written) == 0


def test_force_writes():
    class FakeFile:
        def __init__(self):
            self.written = []
            self.is_open = True

        def write(self, data: bytes):
            self.written.append(data)

    ss = SnapshotState(min_diffs=10)
    ss.record_changes(15)
    ss.request_force()
    f = FakeFile()
    ss.maybe_write(f, 1, {"x": 1})
    assert len(f.written) == 1


def test_force_bypasses_min_diffs():
    class FakeFile:
        def __init__(self):
            self.written = []
            self.is_open = True

        def write(self, data: bytes):
            self.written.append(data)

    ss = SnapshotState(min_diffs=100)
    ss.record_changes(5)
    ss.request_force()
    f = FakeFile()
    ss.maybe_write(f, 1, {"x": 1})
    assert len(f.written) == 1
