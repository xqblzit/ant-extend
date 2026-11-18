"""Small standard-library portability layer for macOS and Windows."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import subprocess
import time


def lock_fd(fd: int, blocking=True, timeout=3):
    if os.name != "nt":
        import fcntl
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(fd, flags)
        return
    import msvcrt
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if not blocking or time.monotonic() >= deadline:
                raise BlockingIOError("State is busy") from exc
            time.sleep(.025)


@contextmanager
def file_lock(path: Path, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        lock_fd(fd, blocking)
        yield
    finally:
        os.close(fd)  # Releasing the OS handle releases its lock on both platforms.


def process_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows can terminate a process. Never use it here.
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def spawn_background(arguments: list[str], env=None):
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True, env=env)
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(arguments, **options)


def replace_file(source: Path, target: Path):
    # Antivirus/indexers may briefly hold Windows destination handles.
    for delay in (0, .025, .05, .1, .2):
        if delay:
            time.sleep(delay)
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or delay == .2:
                raise
