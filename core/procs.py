"""Process liveness, without side effects, on Windows and POSIX.

``os.kill(pid, 0)`` is the POSIX idiom, but on Windows ``os.kill`` with any
signal other than CTRL_C/CTRL_BREAK calls ``TerminateProcess`` — probing a PID
that way would kill it. Windows is asked through ``OpenProcess`` instead.

A PID alone does not name a process for long: Windows hands a freed PID out again
quickly. ``process_start_time`` tells two processes with the same PID apart (#355).
"""
from __future__ import annotations

import os

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_PROC_ROOT = "/proc"


def pid_alive(pid: int | None) -> bool:
    """True when ``pid`` names a running process (ours counts)."""
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists but belongs to someone else.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def process_start_time(pid: int | None) -> str | None:
    """An opaque, stable mark of when ``pid``'s current process started; ``None`` when it cannot be read.

    Two processes that share a PID one after the other get different marks. ``None`` means unknown (the process
    is gone, access is denied, or the OS is neither Windows nor Linux): the caller then has only the PID.
    - Windows: the creation time from ``GetProcessTimes`` (a FILETIME, 100 ns units).
    - Linux: ``starttime``, field 22 of ``/proc/<pid>/stat`` (clock ticks since boot).
    """
    if not pid or pid <= 0:
        return None
    try:
        if os.name == "nt":
            return _start_time_windows(pid)
        return _start_time_linux(pid)
    except (OSError, ValueError, AttributeError):
        return None


def _start_time_linux(pid: int) -> str | None:
    try:
        with open(f"{_PROC_ROOT}/{pid}/stat", encoding="ascii", errors="replace") as f:
            stat = f.read()
    except OSError:
        return None
    # The command name (field 2) is in parentheses and may hold spaces or ')': count from the last ')'.
    fields = stat[stat.rfind(")") + 1:].split()
    return fields[19] if len(fields) > 19 else None   # fields[0] is field 3 (state), so field 22 is fields[19]


def _start_time_windows(pid: int, k32=None) -> str | None:
    import ctypes
    from ctypes import wintypes

    if k32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
    finally:
        k32.CloseHandle(handle)


__all__ = ["pid_alive", "process_start_time"]
