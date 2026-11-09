"""Whether a process on THIS machine is alive, and when it started.

The one authority every lock, lease, claim and leftover sweep on this machine asks
before it reclaims or deletes what a process left: age never is, because a paused
writer is indistinguishable from a dead one by age alone, and the operating system
already knows the answer.

Every pid these callers record is one of the current user's own processes, which
Windows always lets that user open (``PROCESS_QUERY_LIMITED_INFORMATION``, across
elevation too); see :func:`pid_alive` for what a refusal therefore means. Qt-free,
stdlib only, importing nothing of the app (anything may import it).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_QUERY_LIMITED_INFORMATION = 0x1000
_PID_MAX = 2**31 - 1  # no OS hands out a larger pid; ctypes and os.kill overflow past it

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE, *[ctypes.POINTER(ctypes.c_ulonglong)] * 4)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)


def _open(pid: int) -> tuple[int | None, bool]:
    """A query handle on ``pid`` (close it), or ``None`` and whether Windows REFUSED it
    (the process exists but is not ours to inspect) rather than not finding it."""
    handle = _k32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    return handle, not handle and ctypes.get_last_error() == _ERROR_ACCESS_DENIED


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a LIVE process of ours on THIS machine.

    A pid Windows refuses to open is not one of ours: same-user processes are always
    openable, so refused = our process died and the pid was reused by SYSTEM or another
    user (else its lock would survive until reboot). POSIX keeps ``PermissionError`` as
    alive — an asymmetry: there a refusal is read the safe way, not as a reuse. A pid no
    OS can hand out (``<= 0`` or past ``2**31 - 1``) is not alive rather than an error.
    """
    if not 0 < pid <= _PID_MAX:
        return False
    if sys.platform == "win32":
        handle, _ = _open(pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            read = _k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(read) and code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_time(pid: int) -> int | None:
    """When ``pid`` started, in the OS's own ticks; 0 where that cannot be read; None
    when Windows refuses to open it (it exists, another user's or protected)."""
    if sys.platform == "win32":
        handle, refused = _open(pid)
        if not handle:
            return None if refused else 0
        try:
            times = [ctypes.c_ulonglong() for _ in range(4)]  # created, exited, kernel, user
            if not _k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return 0
            return times[0].value
        finally:
            _k32.CloseHandle(handle)
    try:  # Linux: field 22 of /proc/<pid>/stat, counted after the ")" that ends the name
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return 0
