"""Windows directory sharing: writers keep their evidence root in place."""

from __future__ import annotations

import ctypes
import sys
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


@contextmanager
def _directory_handle(root: Path, access: int, sharing: int):
    if sys.platform != "win32":
        yield
        return
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(root), access, sharing, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        close(handle)


def writer_guard(root: Path):
    """Keep a directory from being renamed while this writer uses paths below it."""
    return _directory_handle(root, 1, 3)  # list directory; share read/write, deny delete


def rename_guard(root: Path):
    """Exclude writer handles until the rename and configuration switch finish."""
    return _directory_handle(root, 0x10000, 7)  # DELETE; share read/write/delete


def tree_write(method):
    """Protect filesystem mutations also made directly by index/outbox helpers; the
    tree's open operation forgets its listings after each one."""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            with self.write_guard():
                return method(self, *args, **kwargs)
        finally:
            self.written(*args)

    return guarded


def store_write(method):
    """Hold one root handle across a complete operation and all its metadata writes."""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._writing_root():
            return method(self, *args, **kwargs)

    return guarded
