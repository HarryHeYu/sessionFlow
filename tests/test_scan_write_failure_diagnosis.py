"""A scan that cannot write must name the file it cannot write to (#36).

Measured on v1.4.0 (Ubuntu/WSL, cold-start acceptance of issue #29, step
11d, re-verified with full output): with ``index.db`` writable but
``index.db-shm`` still mode 444 -- SQLite creates the WAL sidecars with the
db's mode while the db was read-only -- every scan reports "attempt to
write a readonly database" without ever naming *which* file is unwritable.
The user checks the db, finds it writable, and is stuck; the stdout summary
promises a retry that cannot succeed. The fix probes write access on the
db / ``-wal`` / ``-shm`` and names the unwritable one(s). Read-only probes
only: nothing is chmodded or removed.

POSIX-only: the Windows attrib-based scenario does not poison the sidecars
there, so the test skips on nt by design.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

ID1 = "11111111-2222-3333-4444-555555555555"
ID2 = "22222222-3333-4333-8444-966666666666"


def _rollout(native_id: str, text: str) -> str:
    meta = {"timestamp": "2026-11-13T09:00:00Z", "ordinal": 0,
            "type": "session_meta",
            "payload": {"session_id": native_id, "id": native_id,
                        "timestamp": "2026-11-13T09:00:00Z",
                        "cwd": "/tmp/sfacc-work", "originator": "codex_cli",
                        "cli_version": "0.153.0", "source": "cli",
                        "model_provider": "openai"}}
    turn = {"timestamp": "2026-11-13T09:00:05Z", "ordinal": 1,
            "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}
    return (json.dumps(meta, ensure_ascii=False) + "\n"
            + json.dumps(turn, ensure_ascii=False) + "\n")


def _scan(home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home), PYTHONPATH=str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, "-m", "voyager.cli", "scan", "--platform", "codex"],
        capture_output=True, text=True, timeout=120,
        cwd=str(REPO_ROOT), env=env)


@pytest.mark.skipif(os.name == "nt",
                    reason="Windows read-only handling does not poison the "
                           "-shm sidecar; scenario is POSIX-specific")
def test_scan_names_the_unwritable_sidecar(tmp_path):
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions" / "2026" / "11" / "13"
    sessions.mkdir(parents=True)
    (sessions / f"rollout-one-{ID1}_w1.jsonl").write_text(
        _rollout(ID1, "first session"), encoding="utf-8")

    # first scan: everything writable; the index and its WAL mode are born
    r = _scan(home)
    assert r.returncode == 0, r.stderr

    db = home / ".voyager" / "index.db"
    wal, shm = db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")

    # SQLite deletes the WAL sidecars when the last connection closes
    # cleanly, so hold one open while the failing scan runs -- that is the
    # state the incident happened in (sidecars present mid-life).
    holder = sqlite3.connect(str(db))
    try:
        holder.execute("select count(*) from sessions").fetchone()
        assert shm.exists() and wal.exists()

        # make ONLY the sidecar unwritable: the db stays writable, which is
        # exactly the state a user cannot diagnose from v1.4.0's output
        shm.chmod(shm.stat().st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP
                  & ~stat.S_IWOTH)
        (sessions / f"rollout-two-{ID2}_w1.jsonl").write_text(
            _rollout(ID2, "second session"), encoding="utf-8")

        r = _scan(home)
        assert r.returncode == 0  # retry-later semantics are unchanged
        assert "could not be written" in r.stdout
        # the diagnosis: the unwritable FILE is named, not just the verdict
        assert "-shm" in (r.stdout + r.stderr)
    finally:
        holder.close()
        for f in (wal, shm):
            if f.exists():
                f.chmod(f.stat().st_mode | stat.S_IWUSR | stat.S_IWGRP
                        | stat.S_IWOTH)

    # the promise is then keepable: the next scan indexes the pending session
    r = _scan(home)
    assert r.returncode == 0, r.stderr
    assert "2 sessions" in r.stdout
