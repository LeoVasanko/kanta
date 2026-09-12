"""Tests for the ``python -m kanta`` CLI output formatting."""

import sys
from datetime import UTC, datetime

import pytest

from kanta.__main__ import (
    _extra_import_paths,
    _format_ts,
    _import_dotted,
    _import_kanta_object,
    main,
)
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


def test_extra_import_paths_ignores_other_python_versions(tmp_path, monkeypatch):
    """Only the site-packages for the running Python version is picked up."""
    current_site = (
        tmp_path
        / ".venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    other_site = tmp_path / ".venv" / "lib" / "python9.9" / "site-packages"
    current_site.mkdir(parents=True)
    other_site.mkdir(parents=True)

    monkeypatch.chdir(tmp_path)
    with _extra_import_paths():
        assert str(current_site) in sys.path
        assert str(other_site) not in sys.path


def test_cli_snapshot_line_format(tmp_path, capsys, monkeypatch):
    """Snapshot lines are timestamped and colored with metadata."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    path = tmp_path / "test.kantadb"
    ts = datetime(2026, 8, 12, 10, 6, 52, 375398, tzinfo=UTC)
    mtime = datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC)
    serializer = JsonSerializer()
    framer = LineFramer()

    snapshot = Snapshot(ts=ts, v=1, m=mtime, state={"counter": 5})
    change = ChangeRecord(ts=ts, a="inc", v=1, u="user1", diff={"counter": 6})
    data = framer.frame_snapshot(
        serializer.encode(snapshot), record_offset=0
    ) + framer.frame_change(serializer.encode(change), record_offset=0)
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


def test_cli_strips_ansi_without_color_support(tmp_path, capsys, monkeypatch):
    """Without a tty and with NO_COLOR set, output contains no ANSI codes."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    path = tmp_path / "test.kantadb"
    ts = datetime(2026, 8, 12, 10, 6, 52, 375398, tzinfo=UTC)
    serializer = JsonSerializer()
    framer = LineFramer()

    snapshot = Snapshot(ts=ts, v=1, m=None, state={"counter": 5})
    change = ChangeRecord(ts=ts, a="inc", v=1, u="user1", diff={"counter": 6})
    data = framer.frame_snapshot(
        serializer.encode(snapshot), record_offset=0
    ) + framer.frame_change(serializer.encode(change), record_offset=0)
    path.write_bytes(data)

    code = main([str(path)])
    assert code == 0

    err = capsys.readouterr().err
    assert "\x1b[" not in err
    assert "snapshot s0" in err


def test_cli_version_on_help_and_version_flag(capsys):
    """--help and --version print the installed package version."""
    import importlib.metadata

    version = importlib.metadata.version("kanta")

    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    assert f"kanta {version}" in capsys.readouterr().out

    with pytest.raises(SystemExit) as version_exit:
        main(["--version"])
    assert version_exit.value.code == 0
    assert capsys.readouterr().out.strip() == f"kanta {version}"


def test_import_dotted_from_file_path(tmp_path):
    """--data can be a filesystem path with an optional colon-separated symbol."""
    module = tmp_path / "models.py"
    module.write_text("class Data:\n    pass\n")
    result = _import_dotted(f"{module}:Data")
    assert result.__name__ == "Data"


def test_import_kanta_object_from_file_path(tmp_path):
    """--kanta can be a filesystem path; default symbol is ``kanta``."""
    module = tmp_path / "database.py"
    module.write_text("class Kanta:\n    pass\nkanta = Kanta()\n")
    result = _import_kanta_object(str(module))
    assert type(result).__name__ == "Kanta"


def test_import_kanta_object_from_file_path_with_symbol(tmp_path):
    """--kanta can be a filesystem path with an explicit colon-separated symbol."""
    module = tmp_path / "database.py"
    module.write_text("class CustomKanta:\n    pass\nmy_kanta = CustomKanta()\n")
    result = _import_kanta_object(f"{module}:my_kanta")
    assert type(result).__name__ == "CustomKanta"
