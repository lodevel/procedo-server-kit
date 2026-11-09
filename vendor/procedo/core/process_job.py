"""Child processes that never outlive the work that started them.

``subprocess`` leaves a child's own children running when the child is killed, and
leaves all of them running when this process dies. A venv build is such a tree:
``python -m venv`` goes through the venv launcher (two processes) and runs
``ensurepip`` (two more), and ``pip`` runs through the new environment's launcher.
Left running, they keep files open in a folder nobody will finish, and the next
start cannot clear it.

A :class:`ProcessJob` holds every process it starts and everything those start.
:meth:`ProcessJob.stop` ends them all and waits until they are gone. On Windows they
are a kill-on-close job: the operating system ends them when the job's last handle
closes — when the job is closed, and when this process exits, however it exits.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping

_WINDOWS = os.name == "nt"

#: How long :meth:`ProcessJob.stop` waits for the ended processes to be gone.
_GONE_TIMEOUT_S = 10.0


class Stopped(RuntimeError):
    """The job was stopped: what it was running did not finish."""


if _WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _k32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _k32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    _ntdll.NtResumeProcess.restype = ctypes.c_long  # NTSTATUS: 0 is success

    _CREATE_SUSPENDED = 0x00000004
    _CREATE_NO_WINDOW = 0x08000000
    _SET_QUOTA, _TERMINATE, _SUSPEND_RESUME = 0x0100, 0x0001, 0x0800  # process access rights
    _KILL_ON_JOB_CLOSE = 0x2000
    _BASIC_ACCOUNTING, _EXTENDED_LIMITS = 1, 9  # JOBOBJECTINFOCLASS

    class _BasicLimits(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [
            ("user_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("min_ws", ctypes.c_size_t),
            ("max_ws", ctypes.c_size_t),
            ("processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class _Limits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [
            ("basic", _BasicLimits),
            ("io", ctypes.c_uint64 * 6),
            ("memory", ctypes.c_size_t * 4),
        ]

    class _Accounting(ctypes.Structure):  # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        _fields_ = [
            ("times", ctypes.c_int64 * 4),
            ("page_faults", wintypes.DWORD),
            ("total", wintypes.DWORD),
            ("active", wintypes.DWORD),
            ("terminated", wintypes.DWORD),
        ]


class ProcessJob:
    """The processes :meth:`run` starts, and every process they start. A context manager:
    leaving it ends whatever is still running."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = False
        self._groups: list[int] = []  # POSIX: one process group per process started
        self._handle = None
        if _WINDOWS:
            self._handle = _k32.CreateJobObjectW(None, None)
            if not self._handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _Limits()
            limits.basic.flags = _KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(
                self._handle, _EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                error = ctypes.get_last_error()
                _k32.CloseHandle(self._handle)
                raise ctypes.WinError(error)

    def __enter__(self) -> ProcessJob:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: float,
        env: Mapping[str, str] | None = None,
        encoding: str | None = None,
    ) -> subprocess.CompletedProcess:
        """``subprocess.run(cmd, capture_output=True, text=True)``, inside this job.

        Raises:
            Stopped: the job is stopped, or was stopped while *cmd* ran.
            subprocess.TimeoutExpired: past *timeout*; the job was stopped first.
        """
        with self._lock:  # a stop() waits until the new process is in the job
            if self._stopped:
                raise Stopped(f"stopped before {cmd[0]} started")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=env,
                encoding=encoding,
                **(
                    {"creationflags": _CREATE_SUSPENDED | _CREATE_NO_WINDOW}
                    if _WINDOWS
                    else {"start_new_session": True}
                ),
            )
            self._adopt(proc)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            proc.communicate()
            raise
        if self._stopped:
            raise Stopped(f"stopped while {cmd[0]} ran")
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    def check_stopped(self) -> None:
        """Refuse publication after cancellation, even when the last process has ended."""
        with self._lock:
            if self._stopped:
                raise Stopped("the setup was stopped")

    def publish(self, callback: Callable[[], None]) -> None:
        """Commit completion atomically with respect to a stop from another thread."""
        with self._lock:
            if self._stopped:
                raise Stopped("the setup was stopped")
            callback()

    def _adopt(self, proc: subprocess.Popen) -> None:
        """Into the job BEFORE it runs: started suspended, so nothing it starts escapes."""
        if not _WINDOWS:
            self._groups.append(proc.pid)
            return
        process = _k32.OpenProcess(_SET_QUOTA | _TERMINATE | _SUSPEND_RESUME, False, proc.pid)
        if not process:
            error = ctypes.get_last_error()
            proc.kill()  # suspended and unreachable: it never runs outside the job
            proc.communicate()
            raise ctypes.WinError(error)
        try:
            if not _k32.AssignProcessToJobObject(self._handle, process):
                error = ctypes.get_last_error()
                _k32.TerminateProcess(process, 1)
                proc.communicate()
                raise ctypes.WinError(error)
            status = _ntdll.NtResumeProcess(process)
            if status:
                _k32.TerminateProcess(process, 1)
                proc.communicate()
                raise OSError(
                    f"could not resume {proc.args[0]} (NTSTATUS {status & 0xFFFFFFFF:#010x})"
                )
        finally:
            _k32.CloseHandle(process)

    def stop(self) -> None:
        """End every process of the job and wait until they are gone. Every :meth:`run`
        after this raises :class:`Stopped`, and so does the one in flight."""
        with self._lock:
            self._stopped = True
            if not _WINDOWS:
                for group in self._groups:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except OSError:
                        pass  # already gone
                return
            if self._handle:
                _k32.TerminateJobObject(self._handle, 1)
                deadline = time.monotonic() + _GONE_TIMEOUT_S
                while self._active() and time.monotonic() < deadline:
                    time.sleep(0.02)

    def _active(self) -> int:
        info = _Accounting()
        if not _k32.QueryInformationJobObject(
            self._handle, _BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            return 0
        return info.active

    def close(self) -> None:
        """End whatever is still running and let the job go."""
        self.stop()
        with self._lock:
            if self._handle:
                _k32.CloseHandle(self._handle)
                self._handle = None
