"""Grok's Windows quit leaves the process alive after ``pager.exited``.

The watcher may terminate a process only after that line is appended for the
same pid, the grace has elapsed, and the pid still names the same process.
Anything earlier, or a recycled pid, must be left alone.
"""

from __future__ import annotations

import json

import pytest

from voyager.integrations.grok_exit_watch import (
    arm_from_here,
    grok_ancestor_pid,
    pager_exited_for_pid,
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


def test_arm_is_off_when_disabled(monkeypatch):
    monkeypatch.setenv("VOYAGER_GROK_EXIT_WATCH", "0")
    assert arm_from_here() is None


def test_arm_spawns_the_ancestor_on_windows(monkeypatch):
    monkeypatch.setattr("voyager.integrations.grok_exit_watch.os.name", "nt")
    monkeypatch.delenv("VOYAGER_GROK_EXIT_WATCH", raising=False)
    monkeypatch.setattr(
        "voyager.integrations.grok_exit_watch.grok_ancestor_pid", lambda: 77)
    seen = []
    monkeypatch.setattr(
        "voyager.integrations.grok_exit_watch._spawn_watcher",
        lambda pid: seen.append(pid))
    assert arm_from_here() == 77
    assert seen == [77]


def test_spawn_uses_wmi_so_the_hook_job_cannot_kill_it(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        class Done:
            returncode = 0
        return Done()

    monkeypatch.setattr(
        "voyager.integrations.grok_exit_watch.subprocess.run", fake_run)
    monkeypatch.setattr(
        "voyager.integrations.grok_exit_watch.subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("child spawn")))
    from voyager.integrations.grok_exit_watch import _spawn_watcher
    _spawn_watcher(77)
    assert calls, "watcher was not started"
    argv, kwargs = calls[0]
    assert argv[0] == "powershell"
    assert "--watch-exit 77" in argv[-1]
    assert kwargs.get("timeout") == 8


def test_arm_is_off_off_windows(monkeypatch):
    monkeypatch.setattr("voyager.integrations.grok_exit_watch.os.name", "posix")
    monkeypatch.delenv("VOYAGER_GROK_EXIT_WATCH", raising=False)
    assert arm_from_here() is None


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows process snapshot")
def test_live_process_snapshot_sees_this_process():
    from voyager.integrations.grok_exit_watch import _creation_token, _process_table

    table = _process_table()
    assert __import__("os").getpid() in table
    assert _creation_token(__import__("os").getpid()) is not None
    assert _creation_token(2**31 - 1) is None
