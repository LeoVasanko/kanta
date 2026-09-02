"""Tests for LockedFile low-level behaviors."""

from kanta.exceptions import FileLockError
from kanta.filelock import LockedFile


def test_replace_content_rewrites_in_place(tmp_path):
    path = tmp_path / "data.kantadb"
    path.write_bytes(b"original content here")

    f = LockedFile()
    f.open(path)
    try:
        f.replace_content(b"new")
        assert f.size() == 3
        f.write(b"!")
    finally:
        f.close()

    assert path.read_bytes() == b"new!"


def test_replace_content_keeps_lock(tmp_path):
    path = tmp_path / "data.kantadb"
    path.write_bytes(b"abc")

    f = LockedFile()
    f.open(path)
    try:
        f.replace_content(b"xyz")
        other = LockedFile()
        try:
            other.open(path)
            raise AssertionError("second open should fail while lock is held")
        except FileLockError:
            pass
    finally:
        f.close()


def test_replace_content_grow_and_shrink(tmp_path):
    path = tmp_path / "data.kantadb"
    path.write_bytes(b"x" * 100)

    f = LockedFile()
    f.open(path)
    try:
        f.replace_content(b"")
        assert f.size() == 0
        f.replace_content(b"y" * 200)
        assert f.size() == 200
    finally:
        f.close()

    assert path.read_bytes() == b"y" * 200
