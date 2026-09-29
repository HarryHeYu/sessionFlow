"""Background-subprocess suppression tests (Windows popup fix).

Contracts:
- background_subprocess_kwargs() carries CREATE_NO_WINDOW on Windows and is
  empty elsewhere;
- the background git probes (get_git_snapshot) pass those kwargs through,
  with capture/returncode/timeout semantics unchanged.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from voyager.continuity import get_git_snapshot
from voyager.winsubprocess import background_subprocess_kwargs


def test_kwargs_carry_create_no_window_only_on_windows():
    kwargs = background_subprocess_kwargs()
    if os.name == "nt":
        assert kwargs == {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        assert kwargs == {}


def test_get_git_snapshot_passes_background_kwargs(tmp_path, monkeypatch):
    """Every git probe inside get_git_snapshot must run windowless on
    Windows, while the returned snapshot semantics stay unchanged."""
    recorded = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        recorded.append(kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("voyager.continuity.subprocess.run", spy)

    snap = get_git_snapshot(str(tmp_path / "definitely-not-a-repo"))

    assert len(recorded) >= 1                      # the probes actually ran
    if os.name == "nt":
        assert all(k.get("creationflags") == subprocess.CREATE_NO_WINDOW
                   for k in recorded)
    else:
        assert all("creationflags" not in k for k in recorded)
    # semantics unchanged: non-repo degrades to the same snapshot shape
    assert snap["is_git"] is False
    assert snap["branch"] == "" and snap["commit"] == ""
    assert snap["dirty_count"] == 0


def test_get_git_snapshot_output_unchanged_on_real_repo(tmp_path):
    """The suppression must not alter what the snapshot reports."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "silent_branch", str(repo)],
                   capture_output=True, timeout=30)
    (repo / "w.txt").write_text("dirty\n", encoding="utf-8")

    snap = get_git_snapshot(str(repo))

    assert snap["is_git"] is True
    assert snap["branch"] == "silent_branch"
    assert snap["dirty_count"] == 1
