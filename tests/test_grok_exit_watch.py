"""Grok's Windows quit leaves the process alive after ``pager.exited``.

The watcher may terminate a process only after that line is appended for the
same pid, the grace has elapsed, and the pid still names the same process.
Anything earlier, or a recycled pid, must be left alone.
"""

from __future__ import annotations

import json

import pytest

from voyager.integrations.grok_exit_watch import (
    ChildRoles,
    ReapState,
    grok_ancestor_pid,
    is_tool_child,
    pager_exited_for_pid,
    quit_signaled_for_pid,
    supervise_loop,
    watch_until_reaped,
)


PID = 4242


def _line(pid, msg, **extra):
    record = {"pid": pid, "msg": msg}
    record.update(extra)
    return json.dumps(record)


def test_pager_exited_matches_only_that_pid_and_message():
    assert pager_exited_for_pid(_line(PID, "pager.exited"), PID)
    assert not pager_exited_for_pid(_line(PID + 1, "pager.exited"), PID)
    assert not pager_exited_for_pid(_line(PID, "quit.terminal_restore"), PID)
    assert not pager_exited_for_pid(_line(str(PID), "pager.exited"), PID)
    assert not pager_exited_for_pid("not json pager.exited", PID)
    # A pid that merely contains the digits of another pid is not a match.
    assert not pager_exited_for_pid(_line(int(str(PID) + "0"), "pager.exited"), PID)


def test_ancestor_walk_stops_at_grok_exe():
    table = {
        10: (9, "python.exe"),
        9: (8, "cmd.exe"),
        8: (7, "powershell.exe"),
        7: (4, "grok.exe"),
        4: (1, "explorer.exe"),
    }
    assert grok_ancestor_pid(table, start=10) == 7
    assert grok_ancestor_pid(table, start=4) is None


def test_ancestor_walk_does_not_loop():
    table = {1: (2, "cmd.exe"), 2: (1, "powershell.exe")}
    assert grok_ancestor_pid(table, start=1) is None


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.n = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.n += 1
        if self.n > 40:
            raise AssertionError("watcher did not finish")


def test_old_pager_exited_does_not_reap(tmp_path):
    log = tmp_path / "unified.jsonl"
    log.write_text(_line(PID, "pager.exited") + "\n", encoding="utf-8")
    clock = _Clock()
    killed = []

    def alive(_pid):
        return clock.n < 3

    outcome = watch_until_reaped(
        PID, log, grace_s=2.0, poll_s=0.2,
        alive=alive, identity=lambda _pid: "born",
        terminate=lambda pid: killed.append(pid),
        sleep=clock.sleep, now=clock.now,
    )
    assert outcome == "gone"
    assert killed == []


def test_reaps_only_after_grace_following_a_new_pager_exited(tmp_path):
    log = tmp_path / "unified.jsonl"
    log.write_text(_line(PID, "quit.started") + "\n", encoding="utf-8")
    clock = _Clock()
    killed = []

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.n == 1:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(_line(99, "pager.exited") + "\n")
                fh.write(_line(PID, "pager.exited") + "\n")

    outcome = watch_until_reaped(
        PID, log, grace_s=2.0, poll_s=0.5,
        alive=lambda _pid: True, identity=lambda _pid: "born",
        terminate=lambda pid: killed.append(pid),
        sleep=sleep, now=clock.now,
    )
    assert outcome == "reaped"
    assert killed == [PID]
    # one poll to observe the line, then grace (2.0) at 0.5s steps: 4 more
    assert clock.t >= 2.0


def test_clean_exit_during_grace_is_not_killed(tmp_path):
    log = tmp_path / "unified.jsonl"
    log.write_text("", encoding="utf-8")
    clock = _Clock()
    killed = []
    state = {"alive": True}

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.n == 1:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(_line(PID, "pager.exited") + "\n")
        if clock.t >= 0.5:
            state["alive"] = False

    outcome = watch_until_reaped(
        PID, log, grace_s=2.0, poll_s=0.5,
        alive=lambda _pid: state["alive"], identity=lambda _pid: "born",
        terminate=lambda pid: killed.append(pid),
        sleep=sleep, now=clock.now,
    )
    assert outcome == "gone"
    assert killed == []


def test_recycled_pid_is_not_killed(tmp_path):
    log = tmp_path / "unified.jsonl"
    log.write_text("", encoding="utf-8")
    clock = _Clock()
    killed = []
    token = {"value": "born"}

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.n == 1:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(_line(PID, "pager.exited") + "\n")
        if clock.t >= 2.0:
            token["value"] = "someone-else"

    outcome = watch_until_reaped(
        PID, log, grace_s=2.0, poll_s=0.5,
        alive=lambda _pid: True, identity=lambda _pid: token["value"],
        terminate=lambda pid: killed.append(pid),
        sleep=sleep, now=clock.now,
    )
    assert outcome == "reused"
    assert killed == []


def test_a_split_line_is_still_seen(tmp_path):
    log = tmp_path / "unified.jsonl"
    log.write_text("", encoding="utf-8")
    clock = _Clock()
    body = _line(PID, "pager.exited") + "\n"
    parts = [body[:8], body[8:]]
    killed = []

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.n <= len(parts):
            with open(log, "ab") as fh:
                fh.write(parts[clock.n - 1].encode("utf-8"))

    outcome = watch_until_reaped(
        PID, log, grace_s=1.0, poll_s=0.5,
        alive=lambda _pid: True, identity=lambda _pid: "born",
        terminate=lambda pid: killed.append(pid),
        sleep=sleep, now=clock.now,
    )
    assert outcome == "reaped"
    assert killed == [PID]


def test_quit_terminal_restore_counts_as_quit_signal():
    assert quit_signaled_for_pid(_line(PID, "quit.terminal_restore"), PID)
    assert quit_signaled_for_pid(_line(PID, "pager.exited"), PID)
    assert not quit_signaled_for_pid(_line(PID, "quit.started"), PID)


def test_session_helpers_do_not_block_the_quit_reap():
    roles = ChildRoles()
    mcp = [(10, True), (11, True)]
    # Startup is cooked, and these processes are still young.
    assert roles.tool_running(now=0, cooked=True, children=mcp) is False
    assert roles.tool_running(now=0.2, cooked=False, children=mcp) is False
    # Quit. The same processes are now old, so they are helpers.
    assert roles.tool_running(now=2.0, cooked=True, children=mcp) is False


def test_a_shell_that_appears_as_the_console_is_cooked_blocks_the_reap():
    roles = ChildRoles()
    roles.tool_running(now=0, cooked=False, children=[])
    assert roles.tool_running(now=5, cooked=True, children=[(20, True)]) is True
    assert roles.tool_running(now=6, cooked=True, children=[]) is False


def test_a_shell_seen_for_a_moment_of_raw_mode_is_still_a_tool():
    roles = ChildRoles()
    roles.tool_running(now=0, cooked=False, children=[])
    roles.tool_running(now=5, cooked=False, children=[(21, True)])
    assert roles.tool_running(now=5.2, cooked=True, children=[(21, True)]) is True


def test_conhost_appearing_while_cooked_does_not_block():
    roles = ChildRoles()
    roles.tool_running(now=0, cooked=False, children=[])
    assert roles.tool_running(now=5, cooked=True, children=[(22, False)]) is False


def test_mcp_helper_is_not_a_tool_child():
    assert not is_tool_child(
        "python.exe", "C:\\Python314\\python.exe -m voyager.mcp_server")
    assert is_tool_child("python.exe", "C:\\Python314\\python.exe -m pytest")
    assert is_tool_child("powershell.exe", None)
    assert not is_tool_child("conhost.exe", None)


def test_cooked_console_reaps_only_after_the_tui_and_without_a_tool():
    state = ReapState()
    # Startup is cooked. That must not reap.
    assert state.update(now=0, cooked=True, tool_child=False, quit_logged=False) is None
    # TUI is raw.
    assert state.update(now=1, cooked=False, tool_child=False, quit_logged=False) is None
    # A tool holds the terminal.
    assert state.update(now=2, cooked=True, tool_child=True, quit_logged=False) is None
    assert state.update(now=10, cooked=True, tool_child=True, quit_logged=False) is None
    # Quit restored the console and no tool is running.
    assert state.update(now=11, cooked=True, tool_child=False, quit_logged=False) is None
    assert state.update(
        now=13, cooked=True, tool_child=False, quit_logged=False) == "reap"


def test_quit_log_reaps_even_while_a_helper_is_alive():
    state = ReapState()
    state.saw_raw = True
    assert state.update(
        now=0, cooked=True, tool_child=True, quit_logged=True, quit_grace=0.5,
    ) is None
    assert state.update(
        now=0.5, cooked=True, tool_child=True, quit_logged=True, quit_grace=0.5,
    ) == "reap"


def test_supervisor_reaps_when_the_console_stays_cooked(tmp_path):
    log = tmp_path / "unified.jsonl"
    log.write_text("", encoding="utf-8")
    clock = _Clock()
    killed = []
    phase = {"cooked": False, "tool": False}

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.t >= 0.5:
            phase["cooked"] = False  # TUI up
        if clock.t >= 1.0:
            phase["cooked"] = True   # quit restored the console
            phase["tool"] = False

    outcome = supervise_loop(
        PID, log,
        cooked=lambda: phase["cooked"] if clock.t < 0.5 or clock.t >= 1.0 else False,
        tool_child=lambda _pid: phase["tool"],
        alive=lambda _pid: not killed,
        terminate=lambda pid: killed.append(pid),
        sleep=sleep, now=clock.now,
        poll_s=0.5, console_grace=1.0,
    )
    assert outcome == "reaped"
    assert killed == [PID]


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows process snapshot")
def test_live_process_snapshot_sees_this_process():
    from voyager.integrations.grok_exit_watch import _creation_token, _process_table

    table = _process_table()
    assert __import__("os").getpid() in table
    assert _creation_token(__import__("os").getpid()) is not None
    assert _creation_token(2**31 - 1) is None
