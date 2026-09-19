"""Voyager Skill installer (roadmap Phase 5 / issue #6).

Copies the packaged SKILL.md into the skill directories of known agents
so they can route natural-language requests through voyager. The skill is
pure routing documentation — it contains no core logic and never writes
provider session directories.

Behavior on an existing, user-modified target: refuse unless --force,
in which case the previous file is backed up next to itself first.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Known agent skill roots (relative to HOME — resolved per call so tests
# and isolated machines redirect cleanly). An absent root means the agent
# is not installed and we must not fabricate its directory tree.
SKILL_AGENT_ROOTS = {
    "codex": Path(".codex") / "skills",
    "claude": Path(".claude") / "skills",
    "grok": Path(".grok") / "skills",
}

SKILL_REL = Path("voyager") / "SKILL.md"


def skill_roots(home: Path) -> Dict[str, Path]:
    return {name: home / rel for name, rel in SKILL_AGENT_ROOTS.items()}


def skill_source() -> Path:
    """The packaged SKILL.md (single source of truth)."""
    return Path(__file__).parent / "SKILL.md"


def resolve_roots(agent: Optional[str] = None,
                  home: Optional[Path] = None) -> Dict[str, Path]:
    """Skill roots for the requested agents, or all known ones."""
    home = home or Path.home()
    roots = skill_roots(home)
    if agent:
        if agent not in roots:
            return {agent: None}  # type: ignore[dict-item]
        return {agent: roots[agent]}
    return roots


def install_skills(agent: Optional[str] = None, force: bool = False,
                   home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Install the packaged skill into known agent skill dirs.

    Returns one status dict per requested agent:
      {agent, status: installed|up-to-date|updated|refused|skipped|
                      unknown-agent|error, path}
    """
    src = skill_source()
    src_text = src.read_text(encoding="utf-8")
    home = home or Path.home()
    roots = resolve_roots(agent, home=home)
    results: List[Dict[str, Any]] = []

    for name, root in roots.items():
        if root is None:
            results.append({"agent": name, "status": "unknown-agent",
                            "path": "install manually into "
                                    "<agent skill dir>/voyager/SKILL.md"})
            continue
        if not _agent_installed(name, home):
            results.append({"agent": name, "status": "skipped",
                            "path": "agent not installed "
                                    f"({root})"})
            continue
        target = root / SKILL_REL
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                current = target.read_text(encoding="utf-8")
                if current == src_text:
                    results.append({"agent": name, "status": "up-to-date",
                                    "path": str(target)})
                    continue
                if not force:
                    results.append({"agent": name, "status": "refused",
                                    "path": str(target)})
                    continue
                backup = target.with_name(
                    "SKILL.md.bak-" + time.strftime("%Y%m%d-%H%M%S"))
                shutil.copy2(target, backup)
                shutil.copy2(src, target)
                results.append({"agent": name, "status": "updated",
                                "path": str(target),
                                "backup": str(backup)})
            else:
                shutil.copy2(src, target)
                results.append({"agent": name, "status": "installed",
                                "path": str(target)})
        except OSError as e:
            results.append({"agent": name, "status": "error",
                            "path": str(target), "error": str(e)})
    return results


def _agent_installed(name: str, home: Path) -> bool:
    """The agent counts as installed when its config/home root exists."""
    roots = {"codex": home / ".codex", "claude": home / ".claude",
             "grok": home / ".grok"}
    return roots.get(name, home).is_dir()
