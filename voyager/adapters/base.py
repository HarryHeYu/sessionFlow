"""Adapter registry and shared helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..model import new_session

# provider name -> module-level `scan(store_changed_check) -> iterable of
# (session, events, source_path, extra_sources)` factories are registered by
# each adapter module via `register`.


class Adapter:
    provider: str = ""
    can_resume: bool = False
    can_fork: bool = False

    def discover(self) -> List[Path]:
        """Return source artifacts (files) to parse."""
        raise NotImplementedError

    def parse(self, source: Path) -> Optional[dict]:
        """Parse one source artifact.

        Returns {"session": dict, "events": [dict], "extra_sources": [Path]}
        or None if the artifact carries no usable session.
        """
        raise NotImplementedError


_REGISTRY: Dict[str, Adapter] = {}


def register(adapter: Adapter) -> None:
    _REGISTRY[adapter.provider] = adapter


def get_adapter(provider: str) -> Optional[Adapter]:
    return _REGISTRY.get(provider)


def all_adapters() -> List[Adapter]:
    return list(_REGISTRY.values())


def enabled_adapters(names: Optional[List[str]] = None) -> List[Adapter]:
    if names:
        return [a for a in _REGISTRY.values() if a.provider in names]
    return all_adapters()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_git_cache: Dict[str, Optional[dict]] = {}


def git_info(cwd: Optional[str]) -> dict:
    """Resolve repo identity for a cwd. Priority: remote URL > git root > cwd.

    Results are cached per-cwd (scans hit the same workspaces repeatedly).
    """
    if not cwd:
        return {"repo_root": None, "remote": None, "branch": None, "commit": None}
    key = str(cwd)
    if key in _git_cache:
        return _git_cache[key]
    out = {"repo_root": None, "remote": None, "branch": None, "commit": None}
    try:
        r = subprocess.run(
            ["git", "-C", key, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            root = r.stdout.strip()
            out["repo_root"] = root
            b = subprocess.run(
                ["git", "-C", key, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            if b.returncode == 0:
                out["branch"] = b.stdout.strip() or None
            c = subprocess.run(
                ["git", "-C", key, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            if c.returncode == 0:
                out["commit"] = c.stdout.strip() or None
            rem = subprocess.run(
                ["git", "-C", key, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10,
            )
            if rem.returncode == 0:
                out["remote"] = rem.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    _git_cache[key] = out
    return out


def finish_session(session: dict, git: Optional[dict] = None) -> dict:
    """Fill repo identity: provider-supplied data wins over live `git` probing."""
    git = git or {}
    s = new_session(**{k: v for k, v in session.items() if not k.startswith("_")})
    s["_files"] = session.get("_files") or []   # consumed by the store
    if not s.get("repo_root"):
        s["repo_root"] = git.get("repo_root")
    if not s.get("git_remote"):
        s["git_remote"] = git.get("remote")
    if not s.get("git_branch"):
        s["git_branch"] = git.get("branch")
    if not s.get("git_commit"):
        s["git_commit"] = git.get("commit")
    return s
