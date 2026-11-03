"""Reap a Grok CLI that has finished quitting but will not leave the terminal.

Grok 1.0.70 on Windows draws the "Finishing session" footer, restores the
console to cooked mode, and then blocks inside ``ExitProcess``. The parent
shell waits on that process, so the terminal looks frozen. The quit log line
``pager.exited`` is not always flushed before the block, so a watcher that
only tails the log never fires.

The Windows launcher runs Grok in *this* console and reaps it when either:

* ``pager.exited`` or ``quit.terminal_restore`` is appended for this pid, or
* the visible window is Grok's quit screen (``Resume this session with:``
  and no composer), or
* the console has been in the raw TUI mode and then stays cooked, and every
  child still alive was already there during that TUI (the MCP servers).

A command that starts when the console is cooked keeps a new child, so that
path is not reaped. Set ``VOYAGER_GROK_EXIT_WATCH=0`` to disable the reap and only
wait. The launcher must not start a second console: a detached ``python.exe``
is a visible window, which is the popup this used to cause.
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
QUIT_LOG_MSGS = frozenset(("pager.exited", "quit.terminal_restore"))
ENABLE_LINE_INPUT = 0x0002
DEFAULT_GRACE_S = 2.0
DEFAULT_POLL_S = 0.2
# Long-lived helpers Grok keeps for the whole session. They are not a tool
# the user is waiting on, so they must not block the post-quit reap.
_LONG_LIVED_MARKERS = ("voyager.mcp_server", "grok_exit_watch.py", "grok_session_start.py")
# A child still this young when the console becomes cooked was just started
# for that cooked stretch (a shell tool). Older children were already there
# through the TUI — MCP servers, not the command the user is watching.
HELPER_AGE_S = 1.0


def log_path() -> Path:
    override = os.environ.get("GROK_LOG_DIR")
    if override:
        return Path(override) / "unified.jsonl"
    grok_home = os.environ.get("GROK_HOME")
    root = Path(grok_home) if grok_home else Path.home() / ".grok"
    return root / "logs" / "unified.jsonl"


def _log_msg_for_pid(line: str, pid: int, msgs: frozenset) -> bool:
    line = line.strip()
    if not line or '"pid"' not in line:
        return False
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return False
    if not isinstance(record, dict):
        return False
    return record.get("pid") == pid and record.get("msg") in msgs


def pager_exited_for_pid(line: str, pid: int) -> bool:
    """True when ``line`` is Grok's final quit record for this process."""
    return _log_msg_for_pid(line, pid, frozenset((PAGER_EXITED,)))


def quit_signaled_for_pid(line: str, pid: int) -> bool:
    """True when the terminal has been handed back or the quit has finished."""
    return _log_msg_for_pid(line, pid, QUIT_LOG_MSGS)


class ReapState:
    """Cooked-console and quit-log timers for one supervised Grok."""

    def __init__(self) -> None:
        self.saw_raw = False
        self.cooked_since: Optional[float] = None
        self.quit_since: Optional[float] = None

    def update(
        self,
        *,
        now: float,
        cooked: bool,
        tool_child: bool,
        quit_logged: bool,
        footer: bool = False,
        console_grace: float = DEFAULT_GRACE_S,
        quit_grace: float = 0.5,
    ) -> Optional[str]:
        """Return ``"reap"`` when this process should be terminated.

        ``footer`` is the quit screen Grok draws before it blocks. It has to
        stay up for ``quit_grace``; a one-off match must not reap.
        """
        if quit_logged or footer:
            if self.quit_since is None:
                self.quit_since = now
        elif self.quit_since is not None:
            self.quit_since = None
        if self.quit_since is not None and now - self.quit_since >= quit_grace:
            return "reap"
        if not cooked:
            self.saw_raw = True
            self.cooked_since = None
            return None
        # Still starting, or a tool is using the terminal. Do not reap.
        if not self.saw_raw or tool_child:
            self.cooked_since = None
            return None
        if self.cooked_since is None:
            self.cooked_since = now
        if now - self.cooked_since >= console_grace:
            return "reap"
        return None


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


class ChildRoles:
    """Which direct children are session helpers, and which are a live tool.

    Grok keeps MCP servers as children for the whole session, including the
    hung quit. Those must not block the reap. A process that appears when the
    console is cooked — or is still brand new at that moment — is a tool, and
    it blocks the reap until it exits.
    """

    def __init__(self) -> None:
        self.role = {}
        self.seen = {}
        self.saw_raw = False

    def tool_running(self, *, now: float, cooked: bool, children) -> bool:
        """``children`` is ``(pid, is_tool)``. Returns whether a tool is live.

        ``is_tool`` is ignored for a process that has already been alive for
        ``HELPER_AGE_S``: that one was here before this cooked stretch.
        """
        alive = {pid for pid, _is_tool in children}
        for pid in list(self.role):
            if pid not in alive:
                self.role.pop(pid, None)
                self.seen.pop(pid, None)
        if not cooked:
            self.saw_raw = True
        for pid, is_tool in children:
            if pid not in self.role:
                self.seen[pid] = now
                if cooked and self.saw_raw:
                    self.role[pid] = "tool" if is_tool else "helper"
                else:
                    self.role[pid] = "pending"
                continue
            if self.role[pid] != "pending":
                continue
            aged = now - self.seen[pid] >= HELPER_AGE_S
            if aged or not is_tool:
                self.role[pid] = "helper"
            elif cooked and self.saw_raw:
                self.role[pid] = "tool"
        if not (cooked and self.saw_raw):
            return False
        return any(self.role.get(pid) == "tool" for pid, _is_tool in children)


def quit_footer_visible(lines) -> bool:
    """The visible window is Grok's quit screen, not the live composer.

    The quit screen ends with ``Resume this session with:``. The same words
    inside a chat still have the composer (``❯``) on the bottom rows, and
    that must not be reaped.
    """
    tail = [str(ln) for ln in list(lines)[-12:]]
    text = "\n".join(tail)
    if "Resume this session with:" not in text:
        return False
    # The live composer owns the bottom of the window. A quit replaces it.
    return not any(("❯" in ln or "╭" in ln) for ln in tail)


def is_tool_child(name: str, command_line: Optional[str]) -> bool:
    """A direct child that is using the terminal for a Grok tool call.

    Long-lived helpers (the MCP server, this supervisor's hook scripts) do
    not count: they are alive for the whole session, including the hung quit.
    """
    text = "{0} {1}".format(name or "", command_line or "").lower()
    if any(marker in text for marker in _LONG_LIVED_MARKERS):
        return False
    if (name or "").lower() in ("conhost.exe", "grok.exe"):
        return False
    return True


def supervise_loop(
    pid: int,
    log_file: Path,
    *,
    cooked: Callable[[], bool],
    tool_child: Callable[[int], bool],
    alive: Callable[[int], bool],
    terminate: Callable[[int], None],
    footer: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    poll_s: float = DEFAULT_POLL_S,
    console_grace: float = DEFAULT_GRACE_S,
    quit_grace: float = 0.5,
    reap_enabled: bool = True,
) -> str:
    """Wait until ``pid`` exits, or reap it once the terminal is free.

    ``cooked`` and ``tool_child`` are injected so a test never touches the
    real console or a real process tree.
    """
    state = ReapState()
    offset = log_file.stat().st_size if log_file.is_file() else 0
    pending = ""
    quit_logged = False
    while alive(pid):
        chunk, offset = _read_new(log_file, offset)
        if chunk:
            pending += chunk
            lines = pending.split("\n")
            pending = lines[-1]
            for line in lines[:-1]:
                if quit_signaled_for_pid(line, pid):
                    quit_logged = True
                    break
        if reap_enabled:
            decision = state.update(
                now=now(),
                cooked=cooked(),
                tool_child=tool_child(pid),
                quit_logged=quit_logged,
                footer=footer(),
                console_grace=console_grace,
                quit_grace=quit_grace,
            )
            if decision == "reap" and alive(pid):
                terminate(pid)
                return "reaped"
        sleep(poll_s)
    return "gone"


def _prepare_launch() -> None:
    """Write the continuation rule, then discover the active thread.

    The ``grok.exe`` shim does not run a batch file, so this is the only
    place that work happens. The ``.cmd`` fallback sets
    ``VOYAGER_LAUNCHER_RUNNING`` after doing it itself; skip in that case.
    A failure here must not stop the CLI from starting.
    """
    if os.environ.get("VOYAGER_LAUNCHER_RUNNING"):
        return
    os.environ["VOYAGER_LAUNCHER_RUNNING"] = "1"
    cwd = os.getcwd()
    try:
        from voyager.integrations.grok_native import write_context_rules
        write_context_rules(cwd=cwd)
    except Exception:
        pass
    try:
        from voyager.launcher import prelaunch
        prelaunch(provider="grok", cwd=cwd)
    except Exception:
        pass


def supervise(argv: list, log_file: Optional[Path] = None) -> int:
    """Run Grok in this console and reap it if the quit never returns."""
    if not argv:
        return 2
    if os.name == "nt":
        _prepare_launch()
    # Ignore Ctrl+C only after the child exists. SetConsoleCtrlHandler(NULL,
    # TRUE) is inherited at CreateProcess time; doing it first would make
    # Grok ignore Ctrl+C as well.
    proc = subprocess.Popen(argv)
    if os.name == "nt":
        _ignore_ctrl_c()
    token = _creation_token(proc.pid)
    reap = os.environ.get("VOYAGER_GROK_EXIT_WATCH", "1") != "0"
    roles = ChildRoles()

    def _terminate_if_same(pid: int) -> None:
        # A recycled pid must not be killed. No token means we cannot tell.
        if token is None or _creation_token(pid) != token:
            return
        _terminate(pid)
    try:
        outcome = supervise_loop(
            proc.pid,
            log_file or log_path(),
            cooked=_console_is_cooked,
            tool_child=lambda pid: _tool_child_for_session(pid, roles),
            footer=_quit_footer_on_console,
            alive=lambda pid: proc.poll() is None,
            terminate=_terminate_if_same,
            reap_enabled=reap,
        )
    except Exception:
        outcome = "error"
    if proc.poll() is None and outcome == "reaped":
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    code = proc.poll()
    _note(proc.pid, outcome)
    if code is None:
        return 0
    return int(code)


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--supervise":
        return supervise(argv[1:])
    if len(argv) >= 2 and argv[0] == "--watch-exit":
        # Kept so a watcher started by an older hook still exits instead of
        # sitting in a console window. It no longer reaps: the launcher does.
        return 0
    return 0


def _ignore_ctrl_c() -> None:
    """Let Grok handle Ctrl+C. This supervisor shares its console."""
    import ctypes
    ctypes.WinDLL("kernel32").SetConsoleCtrlHandler(None, True)


def _console_is_cooked() -> bool:
    """True when this console is back in line-input mode.

    Grok's TUI clears ``ENABLE_LINE_INPUT``. The quit path sets it again
    before the process blocks, which is the moment the shell should return.
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    handle = kernel32.GetStdHandle(0xFFFFFFF6)  # STD_INPUT_HANDLE
    mode = wintypes.DWORD()
    if not handle or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    return bool(mode.value & ENABLE_LINE_INPUT)


def _visible_console_lines() -> list:
    """Last rows of this console's visible window. Empty when there is none."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [
            ("Left", ctypes.c_short), ("Top", ctypes.c_short),
            ("Right", ctypes.c_short), ("Bottom", ctypes.c_short),
        ]

    class CSBI(ctypes.Structure):
        _fields_ = [
            ("dwSize", COORD),
            ("dwCursorPosition", COORD),
            ("wAttributes", ctypes.c_ushort),
            ("srWindow", SMALL_RECT),
            ("dwMaximumWindowSize", COORD),
        ]

    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.GetConsoleScreenBufferInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(CSBI)]
    kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
    kernel32.ReadConsoleOutputCharacterW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD, COORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.ReadConsoleOutputCharacterW.restype = wintypes.BOOL
    handle = kernel32.GetStdHandle(0xFFFFFFF5)  # STD_OUTPUT_HANDLE
    info = CSBI()
    if not handle or not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        return []
    width = max(1, int(info.dwSize.X))
    bottom = int(info.srWindow.Bottom)
    top = max(int(info.srWindow.Top), bottom - 11)
    lines = []
    for y in range(top, bottom + 1):
        buf = ctypes.create_unicode_buffer(width + 1)
        read = wintypes.DWORD()
        if not kernel32.ReadConsoleOutputCharacterW(
            handle, buf, width, COORD(0, y), ctypes.byref(read),
        ):
            return []
        lines.append(buf.value[:read.value])
    return lines


def _quit_footer_on_console() -> bool:
    try:
        return quit_footer_visible(_visible_console_lines())
    except Exception:
        return False


def _tool_child_for_session(pid: int, roles: ChildRoles) -> bool:
    """True when a direct child is a tool started for this cooked stretch.

    Children already classified are not opened again. A snapshot failure
    counts as a tool so the reap does not fire on a guess.
    """
    cooked = _console_is_cooked()
    try:
        table = _process_table()
    except Exception:
        return True
    children = []
    for child, (parent, name) in table.items():
        if parent != pid:
            continue
        if roles.role.get(child) in ("helper", "tool"):
            children.append((child, False))
            continue
        children.append((child, is_tool_child(name, _command_line(child))))
    return roles.tool_running(
        now=time.monotonic(), cooked=cooked, children=children)


def _command_line(pid: int) -> Optional[str]:
    """Process command line, or ``None`` when it cannot be read.

    A child whose command line cannot be read counts as a tool (see
    ``is_tool_child``): failing open here would reap a live command.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ProcessCommandLineInformation = 60
    STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    ntdll.NtQueryInformationProcess.argtypes = [
        ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p,
        wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
    ]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_ulong
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        needed = wintypes.ULONG(0)
        status = ntdll.NtQueryInformationProcess(
            handle, ProcessCommandLineInformation, None, 0, ctypes.byref(needed))
        if status != STATUS_INFO_LENGTH_MISMATCH or not needed.value:
            return None
        buf = ctypes.create_string_buffer(needed.value)
        status = ntdll.NtQueryInformationProcess(
            handle, ProcessCommandLineInformation, buf, needed.value,
            ctypes.byref(needed))
        if status != 0 or needed.value < 16:
            return None
        length = int.from_bytes(buf.raw[0:2], "little")
        ptr = int.from_bytes(buf.raw[8:16], "little")
        if not ptr or length <= 0:
            return None
        return ctypes.wstring_at(ptr, length // 2)
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


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
