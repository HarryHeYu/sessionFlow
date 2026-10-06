"""`voyager demo` — the synthetic first-run experience.

Guards the newcomer path: the demo seeds a synthetic index (never the real
one), search finds the story, the thread carries all four agents, and every
command the verb prints back to the user is a real, working invocation.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from voyager import demo


def test_demo_builds_synthetic_index(tmp_path):
    index = tmp_path / "demo.db"          # explicit path: build is callable
                                          # with any location
    tid = demo.build(index)
    assert index.exists()
    from voyager.store import Store
    s = Store(index)
    try:
        assert s.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 4
        assert s.q("SELECT COUNT(*) n FROM events")[0]["n"] >= 10
        members = [m["provider"] for m in s.live_thread_members(tid)]
        assert sorted(members) == ["claude", "codex", "dsh", "zcode"]
        # the story's decision is searchable
        assert s.search("JWT refresh token")
        assert s.search("authentication")
    finally:
        s.close()


def test_demo_index_is_separate_from_the_real_one(tmp_path, monkeypatch):
    import voyager.store as store_mod
    real = tmp_path / "real-index.db"
    monkeypatch.setattr(store_mod, "default_db_path", lambda: real)
    index = demo.default_index()
    assert index == real.parent / "demo.db"
    assert index != real


def test_printed_commands_are_real_invocations(tmp_path, monkeypatch, capsys):
    """Every command the verb suggests must actually work on the seeded
    index — a newcomer must not be handed a broken line."""
    import voyager.store as store_mod
    monkeypatch.setattr(store_mod, "default_db_path",
                        lambda: tmp_path / "index.db")
    index = demo.default_index()
    demo.build(index)
    demo.run()
    out = capsys.readouterr().out
    assert str(index) in out
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("voyager "):
            # shlex keeps quoted phrases ("JWT refresh token") as one token;
            # strip the display quotes around paths/queries, and run from the
            # repo root (the conftest chdir'd us into an isolated dir)
            import shlex
            argv = [tok.strip('"') for tok in shlex.split(s, posix=False)]
            rc = subprocess.run(
                [sys.executable, "-m", "voyager.cli"] + argv[1:],
                capture_output=True, text=True, encoding="utf-8",
                cwd=str(Path(__file__).resolve().parent.parent), timeout=120)
            assert rc.returncode == 0, f"printed command failed: {s}\n{rc.stderr}"
