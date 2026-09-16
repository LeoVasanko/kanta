"""Cross-platform locked file for the database (no separate .lock files).

Unix:    open() + fcntl.flock (advisory, cooperative among processes that flock).
Windows: CreateFileW with FILE_SHARE_READ (OS-enforced, allows readers, blocks writers).

A single file descriptor is opened once for both reading and writing.
The lock is acquired atomically (on Windows) or immediately after open (on Unix),
and the same descriptor is used for the lifetime of the process: first to read
the existing content, then to append new writes.
"""

import logging
import os
import sys
from pathlib import Path

from kanta.exceptions import FileLockError

_logger = logging.getLogger("kanta")


def _fatal(msg: str, *, db_path: Path | None = None) -> None:
    """Log a fatal error and raise a typed exception."""
    _logger.critical(msg)
    raise FileLockError(msg, db_path=db_path)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_BEGIN = 0
    _FILE_END = 2
    _ERROR_SHARING_VIOLATION = 32
    _INVALID_FILE_SIZE = 0xFFFFFFFF

    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.GetFileSize.restype = wintypes.DWORD
    _kernel32.GetFileSize.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.SetFilePointer.restype = wintypes.DWORD
    _kernel32.SetFilePointer.argtypes = [
        wintypes.HANDLE,
        wintypes.LONG,
        ctypes.POINTER(wintypes.LONG),
        wintypes.DWORD,
    ]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.SetEndOfFile.restype = wintypes.BOOL
    _kernel32.SetEndOfFile.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]

    def _is_invalid_handle(handle) -> bool:
        return ctypes.c_void_p(handle).value == ctypes.c_void_p(-1).value

else:
    import fcntl


class LockedFile:
    """A file opened for read+write with an optional exclusive lock.

    Usage::

        f = LockedFile()
        f.open(path)                    # open + lock (read+write)
        f.open(path, readonly=True)     # open read-only without locking
        content = f.read()              # read entire content
        f.write(data)                   # append data (seeks to end first)
        f.close()                       # release lock + close fd

    Unix:    fcntl.flock (advisory) — read-only callers that don't flock are unaffected.
    Windows: CreateFileW with FILE_SHARE_READ — OS blocks other writers.
    """

    def __init__(self) -> None:
        self._fd: int | None = None  # Unix fd or Windows HANDLE

    def open(self, path: Path, *, create: bool = False, readonly: bool = False) -> None:
        """Open *path* and optionally acquire an exclusive lock.

        Args:
            path:   File to open and lock.
            create: If True, create the file if it doesn't exist (bootstrap).
            readonly: If True, open read-only without acquiring a lock.

        Raises:
            FileLockError: If the file is locked by another process or not found.
        """
        if self._fd is not None:
            return  # Already open (idempotent)

        if sys.platform == "win32":
            self._open_win32(path, create, readonly)
        else:
            self._open_unix(path, create, readonly)

    def open_and_read(
        self, path: Path, create: bool = False, readonly: bool = False
    ) -> bytes:
        """Open *path* and read all content.

        Combined operation for efficient use with asyncio.to_thread().
        """
        self.open(path, create=create, readonly=readonly)
        return self.read()

    def read(self) -> bytes:
        """Read the entire file content from the beginning."""
        if self._fd is None:
            raise RuntimeError("LockedFile.read() called on a closed file")

        if sys.platform == "win32":
            return self._read_win32()
        else:
            return self._read_unix()

    def write(self, data: bytes) -> None:
        """Append *data* to the end of the file."""
        if self._fd is None:
            raise RuntimeError("LockedFile.write() called on a closed file")

        if sys.platform == "win32":
            self._write_win32(data)
        else:
            self._write_unix(data)

    def size(self) -> int:
        """Return current file size in bytes."""
        if self._fd is None:
            raise RuntimeError("LockedFile.size() called on a closed file")

        if sys.platform == "win32":
            size = _kernel32.GetFileSize(self._fd, None)
            if size == _INVALID_FILE_SIZE:
                raise OSError(
                    f"GetFileSize failed: Windows error {ctypes.get_last_error()}"
                )
            return int(size)

        current = os.lseek(self._fd, 0, os.SEEK_CUR)
        end = os.lseek(self._fd, 0, os.SEEK_END)
        os.lseek(self._fd, current, os.SEEK_SET)
        return end

    def replace_content(self, data: bytes) -> None:
        """Atomically-ish rewrite the file's content in place, lock retained.

        Seeks to the start, truncates, writes *data* and fsyncs, all on the
        already-locked descriptor. The path is never unlinked or renamed, so
        no other process can observe a missing file or acquire its own lock.
        Used by database rotation.
        """
        if self._fd is None:
            raise RuntimeError("LockedFile.replace_content() called on a closed file")

        if sys.platform == "win32":
            self._replace_content_win32(data)
        else:
            self._replace_content_unix(data)

    def close(self) -> None:
        """Release the lock and close the file."""
        if self._fd is None:
            return
        if sys.platform == "win32":
            _kernel32.CloseHandle(self._fd)
        else:
            os.close(self._fd)
        self._fd = None

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    # -- Unix ----------------------------------------------------------------

    def _open_unix(self, path: Path, create: bool, readonly: bool) -> None:
        if readonly:
            flags = os.O_RDONLY
        else:
            flags = os.O_RDWR | (os.O_CREAT if create else 0)
        try:
            fd = os.open(path, flags, 0o666)
        except FileNotFoundError:
            _fatal(f"Database file not found: {path.resolve()}", db_path=path)
        if not readonly:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                _fatal(
                    f"{path.resolve()}: database already locked by another instance",
                    db_path=path,
                )
        self._fd = fd

    def _read_unix(self) -> bytes:
        os.lseek(self._fd, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(self._fd, 1 << 20)  # 1 MiB
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def _write_unix(self, data: bytes) -> None:
        os.lseek(self._fd, 0, os.SEEK_END)
        os.write(self._fd, data)

    def _replace_content_unix(self, data: bytes) -> None:
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.ftruncate(self._fd, 0)
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]
        os.fdatasync(self._fd)

    # -- Windows -------------------------------------------------------------

    def _open_win32(self, path: Path, create: bool, readonly: bool) -> None:
        if readonly:
            disposition = _OPEN_EXISTING
            access = _GENERIC_READ
            share = _FILE_SHARE_READ | _FILE_SHARE_WRITE
        else:
            disposition = _OPEN_ALWAYS if create else _OPEN_EXISTING
            access = _GENERIC_READ | _GENERIC_WRITE
            share = _FILE_SHARE_READ
        handle = _kernel32.CreateFileW(
            str(path),
            access,
            share,
            None,
            disposition,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if _is_invalid_handle(handle):
            err = ctypes.get_last_error()
            if err == _ERROR_SHARING_VIOLATION:
                _fatal(
                    f"{path.resolve()}: database already locked by another instance",
                    db_path=path,
                )
            _fatal(
                f"Failed to open database {path.resolve()}: Windows error {err}",
                db_path=path,
            )
        self._fd = handle

    def _read_win32(self) -> bytes:
        _kernel32.SetFilePointer(self._fd, 0, None, _FILE_BEGIN)
        size = _kernel32.GetFileSize(self._fd, None)
        if size == _INVALID_FILE_SIZE:
            raise OSError(
                f"GetFileSize failed: Windows error {ctypes.get_last_error()}"
            )
        if size == 0:
            return b""
        buf = ctypes.create_string_buffer(size)
        bytes_read = wintypes.DWORD()
        ok = _kernel32.ReadFile(self._fd, buf, size, ctypes.byref(bytes_read), None)
        if not ok:
            raise OSError(f"ReadFile failed: Windows error {ctypes.get_last_error()}")
        return buf.raw[: bytes_read.value]

    def _write_win32(self, data: bytes) -> None:
        _kernel32.SetFilePointer(self._fd, 0, None, _FILE_END)
        written = wintypes.DWORD()
        ok = _kernel32.WriteFile(
            self._fd,
            data,
            len(data),
            ctypes.byref(written),
            None,
        )
        if not ok:
            raise OSError(f"WriteFile failed: Windows error {ctypes.get_last_error()}")

    def _replace_content_win32(self, data: bytes) -> None:
        _kernel32.SetFilePointer(self._fd, 0, None, _FILE_BEGIN)
        written = wintypes.DWORD()
        ok = _kernel32.WriteFile(
            self._fd,
            data,
            len(data),
            ctypes.byref(written),
            None,
        )
        if not ok:
            raise OSError(f"WriteFile failed: Windows error {ctypes.get_last_error()}")
        if not _kernel32.SetEndOfFile(self._fd):
            raise OSError(
                f"SetEndOfFile failed: Windows error {ctypes.get_last_error()}"
            )
        if not _kernel32.FlushFileBuffers(self._fd):
            raise OSError(
                f"FlushFileBuffers failed: Windows error {ctypes.get_last_error()}"
            )
