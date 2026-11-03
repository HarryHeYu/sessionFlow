"""Reap a Grok CLI that has finished quitting but will not leave the terminal.

Grok 1.0.70 on Windows logs a complete quit (``quit.terminal_restore`` through
``pager.exited``) and then blocks inside ``ExitProcess``. The parent shell is
waiting on that process, so the terminal looks frozen after a normal quit.
``quit.terminal_restore`` is logged *before* ``pager.exited``, and the session
flush has already happened by then, so terminating the process once
``pager.exited`` has been logged gives the console back without dropping the
session.

The SessionStart hook arms one detached watcher per Grok process. It only
reads log lines appended after it starts, and it only calls ``TerminateProcess``
when the same process (same pid and same creation time) is still alive after a
short grace. A clean exit during the grace is left alone. Set
``VOYAGER_GROK_EXIT_WATCH=0`` to disable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

PAGER_EXITED = "pager.exited"
DEFAULT_GRACE_S = 2.0
DEFAULT_POLL_S = 0.2


def log_path() -> Path:
    override = os.environ.get("GROK_LOG_DIR")
    if override:
        return Path(override) / "unified.jsonl"
    grok_home = os.environ.get("GROK_HOME")
    root = Path(grok_home) if grok_home else Path.home() / ".grok"
    return root / "logs" / "unified.jsonl"


def pager_exited_for_pid(line: str, pid: int) -> bool:
    """True when ``line`` is Grok's final quit record for this process."""
    line = line.strip()
    if not line or PAGER_EXITED not in line:
        return False
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return False
    if not isinstance(record, dict):
        return False
    return record.get("msg") == PAGER_EXITED and record.get("pid") == pid


def watch_until_reaped(
    pid: int,
    log_file: Path,
    *,
    grace_s: float = DEFAULT_GRACE_S,
    poll_s: float = DEFAULT_POLL_S,
    alive: Optional[Callable[[int], bool]] = None,
    identity: Optional[Callable[[int], Optional[object]]] = None,
    terminate: Optional[Callable[[int], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> str:
    """Wait until ``pid`` is gone, or reap it after ``pager.exited``.

    Returns ``gone`` (process left on its own), ``reaped`` (we terminated it),
    or ``reused`` (the pid was recycled before the grace elapsed, so it was
    not touched).

    ``alive`` / ``identity`` / ``terminate`` are injectable so a test never
    signals a real process. ``identity`` is the process creation token captured
    at start; a different token means the operating system reused the pid.
    """
    if pid <= 0:
        return "gone"
    is_alive = alive or _process_alive
    same = identity or _creation_token
    kill = terminate or _terminate
    token = same(pid)
    if token is None and not is_alive(pid):
        return "gone"

    offset = log_file.stat().st_size if log_file.is_file() else 0
    pending = ""
    seen_at: Optional[float] = None

    while True:
        if not is_alive(pid):
            return "gone"
        if same(pid) != token:
            return "reused"

        chunk, offset = _read_new(log_file, offset)
        if chunk:
            pending += chunk
            lines = pending.split("\n")
            pending = lines[-1]
            for line in lines[:-1]:
                if pager_exited_for_pid(line, pid):
                    seen_at = now()
                    break

        if seen_at is not None and now() - seen_at >= grace_s:
            if not is_alive(pid) or same(pid) != token:
                return "gone" if not is_alive(pid) else "reused"
            kill(pid)
            return "reaped"
        sleep(poll_s)


def _read_new(log_file: Path, offset: int) -> tuple:
    """Bytes appended since ``offset``. A shrink (rotation) rereads from 0."""
    try:
        size = log_file.stat().st_size
    except OSError:
        return "", offset
    if size < offset:
        offset = 0
    if size == offset:
        return "", offset
    try:
        with open(log_file, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return "", offset
    return data.decode("utf-8", errors="replace"), offset + len(data)


def arm_from_here() -> Optional[int]:
    """Start a detached watcher for the Grok that launched this hook.

    Fail-open: any problem returns ``None`` and the hook still exits 0.
    No-op unless this is Windows and ``VOYAGER_GROK_EXIT_WATCH`` is not ``0``.
    """
    if os.name != "nt":
        return None
    if os.environ.get("VOYAGER_GROK_EXIT_WATCH", "1") == "0":
        return None
    if "--watch-exit" in sys.argv:
        return None
    try:
        pid = grok_ancestor_pid()
    except Exception:
        return None
    if not pid:
        return None
    try:
        _spawn_watcher(pid)
    except Exception:
        return None
    return pid


def grok_ancestor_pid(table: Optional[dict] = None,
                      start: Optional[int] = None) -> Optional[int]:
    """Pid of the nearest ``grok.exe`` above this process.

    ``table`` maps pid to ``(parent_pid, exe_basename)`` and exists so the
    walk can be tested without a live process snapshot.
    """
    if table is None:
        table = _process_table()
    pid = os.getpid() if start is None else start
    seen = set()
    while pid and pid not in seen:
        seen.add(pid)
        row = table.get(pid)
        if row is None:
            return None
        parent, name = row
        if str(name).lower() == "grok.exe":
            return pid
        pid = parent
    return None


def _spawn_watcher(pid: int) -> None:
    """Start the watcher outside this process's job.

    Grok runs the SessionStart hook inside a job object and tears that job
    down when the hook returns. A normal child dies with the hook, which is
    before the user ever quits, so it never gets to reap. ``CREATE_BREAKAWAY_FROM_JOB``
    is refused from that job (access denied). ``Win32_Process.Create`` goes
    through the WMI service, and the watcher comes back with a parent that is
    not this hook.
    """
    script = str(Path(__file__).resolve())
    command = subprocess.list2cmdline(
        [sys.executable, script, "--watch-exit", str(int(pid))])
    ps = (
        "$cmd = @'\n" + command + "\n'@\n"
        "Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{ CommandLine = $cmd } | Out-Null\n"
    )
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=8,
        check=False,
        **kwargs,
    )
    if completed.returncode == 0:
        return
    # A launcher that is not inside a hook job can still supervise directly.
    subprocess.Popen(
        [sys.executable, script, "--watch-exit", str(int(pid))],
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
    )


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "--watch-exit":
        try:
            pid = int(argv[1])
        except ValueError:
            return 0
        try:
            outcome = watch_until_reaped(pid, log_path())
        except Exception:
            return 0
        _note(pid, outcome)
        return 0
    return 0


def _note(pid: int, outcome: str) -> None:
    if outcome not in ("reaped", "reused"):
        return
    path = Path.home() / ".voyager" / "logs" / "grok-exit-watch.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "pid": pid,
                "outcome": outcome,
                "ts": time.time(),
            }) + "\n")
    except OSError:
        pass


# -- Windows process queries -------------------------------------------------

def _process_table() -> dict:
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    ERROR_NO_MORE_FILES = 18

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32FirstW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snap or snap == invalid:
        raise OSError("CreateToolhelp32Snapshot failed")
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID),
                str(entry.szExeFile),
            )
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
            if not ok and ctypes.get_last_error() != ERROR_NO_MORE_FILES:
                break
    finally:
        kernel32.CloseHandle(snap)
    return table


def _creation_token(pid: int):
    """Process creation FILETIME, or ``None`` when the pid is gone."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        unused = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(unused),
            ctypes.byref(unused), ctypes.byref(unused),
        ):
            return None
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def _process_alive(pid: int) -> bool:
    return _creation_token(pid) is not None


def _terminate(pid: int) -> None:
    if os.name != "nt":
        return
    import ctypes

    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    sys.exit(main())
