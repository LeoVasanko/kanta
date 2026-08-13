"""Tests for the ``python -m kanta`` CLI output formatting."""

import sys
from datetime import UTC, datetime

from kanta.__main__ import _extra_import_paths, _format_ts, main
from kanta.serialization import JsonSerializer
from kanta.serialization.framing import LineFramer
from kanta.structs import ChangeRecord, Snapshot


def test_format_ts_strips_microseconds():
    """Timestamps are rendered without microsecond precision."""
    dt = datetime(2026, 8, 12, 10, 6, 52, 375398, tzinfo=UTC)
    assert _format_ts(dt) == "2026-08-12 10:06:52"


def test_extra_import_paths_are_temporary(tmp_path, monkeypatch):
    """CWD and nearby venv site-packages are added only for the import block."""
    parent_dir = tmp_path / "parent"
    cwd = parent_dir / "child"
    venv_site = (
        cwd
        / ".venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    venv_site.mkdir(parents=True)
    parent_venv_site = (
        parent_dir
        / ".venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    parent_venv_site.mkdir(parents=True)

    monkeypatch.chdir(cwd)
    cwd_str = str(cwd)
    venv = str(venv_site)
    parent_venv = str(parent_venv_site)

    before = sys.path.copy()
    with _extra_import_paths():
        during = sys.path.copy()
        assert cwd_str in during
        assert venv in during
        assert parent_venv in during
        assert during.index(cwd_str) < during.index(venv) < during.index(parent_venv)
    assert sys.path == before


def test_cli_snapshot_line_format(tmp_path, capsys):
    """Snapshot lines are timestamped and colored with metadata."""
    path = tmp_path / "test.kantadb"
    ts = datetime(2026, 8, 12, 10, 6, 52, 375398, tzinfo=UTC)
    mtime = datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC)
    serializer = JsonSerializer()
    framer = LineFramer()

    snapshot = Snapshot(ts=ts, v=1, m=mtime, state={"counter": 5})
    change = ChangeRecord(ts=ts, a="inc", v=1, u="user1", diff={"counter": 6})
    data = (
        framer.frame_snapshot(serializer.encode(snapshot), record_offset=0)
        + framer.frame_change(serializer.encode(change), record_offset=0)
    )
    path.write_bytes(data)

    code = main([str(path)])
    assert code == 0

    err = capsys.readouterr().err
    # No microsecond precision anywhere.
    assert "10:06:52" in err
    assert "10:06:52.375398" not in err

    # Snapshot line: bright white snapshot/sN, white version/mtime, dark size.
    assert "\x1b[97msnapshot s0" in err
    assert "\x1b[38;5;250m v1 2026-08-12 09:00:00" in err
    assert "\x1b[38;5;242m 13 B" in err
