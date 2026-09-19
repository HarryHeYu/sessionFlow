"""Voyager Skill installer (roadmap Phase 5 / issue #6).

Copies the packaged SKILL.md into the skill directories of known agents
so they can route natural-language requests through voyager. The skill is
pure routing documentation — it contains no core logic and never writes
provider session directories.

Behavior on an existing, user-modified target: refuse unless --force,
in which case the previous file is backed up next to itself first.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Known agent skill roots (relative to HOME — resolved per call so tests
# and isolated machines redirect cleanly). An absent root means the agent
# is not installed and we must not fabricate its directory tree.
SKILL_AGENT_ROOTS = {
    "codex": Path(".codex") / "skills",
    "claude": Path(".claude") / "skills",
    "grok": Path(".grok") / "skills",
}

# Provider configuration with their integration capabilities
PROVIDER_CONFIG = {
    "codex": {
        "name": "Codex",
        "skill_enabled": True,
        "mcp_enabled": True,
        "has_startup_hook": False,
        "config_file": ".config/codex/config.toml",
        "mcp_config": ".config/codex/mcp.json",
    },
    "claude": {
        "name": "Claude Code",
        "skill_enabled": True,
        "mcp_enabled": True,
        "has_startup_hook": True,
        "config_file": ".claude/settings.json",
        "mcp_config": ".claude/mcp.json",
    },
    "grok": {
        "name": "Grok CLI",
        "skill_enabled": True,
        "mcp_enabled": False,
        "has_startup_hook": False,
        "config_file": None,
        "mcp_config": None,
    },
    "dsh": {
        "name": "DSH",
        "skill_enabled": True,
        "mcp_enabled": False,
        "has_startup_hook": False,
        "config_file": None,
        "mcp_config": None,
    },
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


def _agent_installed(name: str, home: Path) -> bool:
    """The agent counts as installed when its config/home root exists."""
    roots = {"codex": home / ".codex", "claude": home / ".claude",
             "grok": home / ".grok", "dsh": home / ".dsh"}
    return roots.get(name, home).is_dir()


def _find_executable(name: str) -> Optional[Path]:
    """Find executable in PATH."""
    try:
        which_result = subprocess.run(
            ["which", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if which_result.returncode == 0:
            exe_path = Path(which_result.stdout.strip())
            if exe_path.exists():
                return exe_path
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _check_mcp_support(provider: str) -> Tuple[bool, str]:
    """Check if provider supports MCP integration."""
    if PROVIDER_CONFIG[provider]["mcp_enabled"]:
        # For Codex and Claude, check if mcp-jupyter-server is available
        mcp_exe = _find_executable("mcp-jupyter-server")
        if mcp_exe:
            return True, str(mcp_exe)
        return True, "MCP enabled but mcp-jupyter-server not found in PATH"
    return False, PROVIDER_CONFIG[provider]["name"] + " does not support MCP"


def _install_codex_bootstrap(target_dir: Path) -> Tuple[str, Optional[str]]:
    """Generate Codex startup bootstrap script."""
    bootstrap_content = """# Voyager Zero-Touch Continuity Bootstrap
# Add this to your ~/.zshrc or ~/.bashrc if supported:
# export VOYAGER_PROVIDER=codex
# export VOYAGER_AUTO_STARTUP=true

# At the start of each Codex session, run:
#   eval "$(voyager bootstrap codex)"

# This will automatically discover active WorkThreads and load continuity context.
"""
    try:
        bootstrap_file = target_dir / "voyager_codex_bootstrap.sh"
        bootstrap_file.write_text(bootstrap_content, encoding="utf-8")
        return "generated", str(bootstrap_file)
    except OSError as e:
        return "error", str(e)


def _install_claude_bootstrap(target_dir: Path) -> Tuple[str, Optional[str]]:
    """Generate Claude Code startup bootstrap instructions."""
    bootstrap_content = """# Voyager Zero-Touch Continuity Bootstrap for Claude Code

# Option 1: MCP-based (preferred)
# Ensure claude mcp add has been run via: voyager integrate claude

# Option 2: Global instruction file
# Create ~/.claude/instructions.md with:
#   At the beginning of each session, call voyager_startup tool to discover
#   any active WorkThreads from other agents in this repository.

# Option 3: CLAUDE.md at project root
# Add to your project's CLAUDE.md:
#   Before starting work, check for continuation context via:
#   voyager_startup --provider claude
"""
    try:
        bootstrap_file = target_dir / "voyager_claude_bootstrap.md"
        bootstrap_file.write_text(bootstrap_content, encoding="utf-8")
        return "generated", str(bootstrap_file)
    except OSError as e:
        return "error", str(e)


def _install_grok_bootstrap(target_dir: Path) -> Tuple[str, Optional[str]]:
    """Generate Grok CLI bootstrap instructions (best-effort)."""
    bootstrap_content = """# Voyager Startup Continuity for Grok CLI (Best-Effort)

# Grok CLI does not provide a reliable startup hook mechanism.
# Best available options:

# 1. Skill-based: Ensure SKILL.md is installed via: voyager integrate grok
#    Then instruct Grok to use Voyager for cross-session continuity.

# 2. Manual workflow: At the start of each Grok session, ask:
#    "Check Voyager for any active WorkThreads in this repo and load context"

# Note: This is marked as best-effort rather than verified zero-touch.
"""
    try:
        bootstrap_file = target_dir / "voyager_grok_bootstrap.md"
        bootstrap_file.write_text(bootstrap_content, encoding="utf-8")
        return "generated", str(bootstrap_file)
    except OSError as e:
        return "error", str(e)


def _install_dsh_bootstrap(target_dir: Path) -> Tuple[str, Optional[str]]:
    """Generate DSH bootstrap instructions (best-effort)."""
    bootstrap_content = """# Voyager Startup Continuity for DSH (Best-Effort)

# DSH platform characteristics:
# - Provides session resume capability: dsh --resume
# - Storage format: zstd-compressed JSONL in ~/.dsh/sessions
# - Limited startup hook availability

# Recommendations:
# 1. Install Voyager Skill: voyager integrate dsh
# 2. Use manual invocation at session start:
#    echo $(voyager_current --provider dsh --cwd $PWD) | grok
# 
# Status: best-effort due to platform limitations.
"""
    try:
        bootstrap_file = target_dir / "voyager_dsh_bootstrap.md"
        bootstrap_file.write_text(bootstrap_content, encoding="utf-8")
        return "generated", str(bootstrap_file)
    except OSError as e:
        return "error", str(e)


def install_integration(provider: str, force: bool = False,
                        home: Optional[Path] = None) -> Dict[str, Any]:
    """Install complete integration for a provider.
    
    Returns status dict with fields:
      {provider, status: installed|skipped|error|success,
       skill: status|path, mcp: status, bootstrap: status|path,
       warnings, verification}
    """
    home = home or Path.home()
    result: Dict[str, Any] = {"provider": provider, "status": "pending"}
    
    if provider not in PROVIDER_CONFIG:
        result.update({"status": "error", 
                       "warnings": [f"Unknown provider: {provider}"]})
        return result
    
    if not _agent_installed(provider, home):
        result.update({"status": "skipped",
                       "warnings": [f"{provider} not installed ({home / ('.' + provider)})"]})
        return result
    
    # Step 1: Install Skill
    skill_root = home / SKILL_AGENT_ROOTS.get(provider, Path(f".{provider}/skills"))
    target = skill_root / SKILL_REL
    
    try:
        src_text = skill_source().read_text(encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        
        if target.exists():
            current = target.read_text(encoding="utf-8")
            if current != src_text:
                if not force:
                    result.update({
                        "status": "refused",
                        "skill": {"status": "refused", "path": str(target)},
                        "warnings": ["Existing skill file modified; use --force to update"]
                    })
                    return result
                
                backup = target.with_name(
                    f"SKILL.md.bak-{time.strftime('%Y%m%d-%H%M%S')}")
                shutil.copy2(target, backup)
        
        shutil.copy2(skill_source(), target)
        result["skill"] = {"status": "installed", "path": str(target)}
    except Exception as e:
        result["skill"] = {"status": "error", "path": str(target), "error": str(e)}
        result["status"] = "error"
        return result
    
    # Step 2: Check MCP support
    mcp_support, mcp_msg = _check_mcp_support(provider)
    if mcp_support:
        result["mcp"] = {"status": "available", "message": mcp_msg}
    else:
        result["mcp"] = {"status": "unsupported", "message": mcp_msg}
    
    # Step 3: Generate bootstrap
    bootstrap_funcs = {
        "codex": _install_codex_bootstrap,
        "claude": _install_claude_bootstrap,
        "grok": _install_grok_bootstrap,
        "dsh": _install_dsh_bootstrap,
    }
    
    bootstrap_func = bootstrap_funcs.get(provider)
    if bootstrap_func:
        bs_status, bs_path = bootstrap_func(target.parent)
        result["bootstrap"] = {"status": bs_status, "path": bs_path}
    
    # Step 4: Verify installation
    verified_parts = []
    if result["skill"]["status"] in ("installed", "up-to-date"):
        verified_parts.append("skill")
    if result["mcp"]["status"] in ("available", "enabled"):
        verified_parts.append("mcp")
    if result.get("bootstrap", {}).get("status") == "generated":
        verified_parts.append("bootstrap")
    
    result["verification"] = ", ".join(verified_parts) if verified_parts else "none"
    result["status"] = "success" if verified_parts else "partial"
    
    return result


def uninstall_integration(provider: str, home: Optional[Path] = None) -> Dict[str, Any]:
    """Remove integration for a provider."""
    home = home or Path.home()
    result: Dict[str, Any] = {"provider": provider, "status": "removed"}
    
    if provider not in PROVIDER_CONFIG:
        result.update({"status": "error", "warnings": [f"Unknown provider: {provider}"]})
        return result
    
    skill_root = home / SKILL_AGENT_ROOTS.get(provider, Path(f".{provider}/skills"))
    target = skill_root / SKILL_REL
    
    try:
        if target.exists():
            target.unlink()
            result["skill"] = {"status": "removed", "path": str(target)}
        else:
            result["skill"] = {"status": "not-found", "path": str(target)}
    except OSError as e:
        result["skill"] = {"status": "error", "path": str(target), "error": str(e)}
        result["status"] = "error"
    
    return result


def check_integration_status(providers: Optional[List[str]] = None,
                              home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Check integration status for specified providers or all known ones."""
    home = home or Path.home()
    targets = providers or list(PROVIDER_CONFIG.keys())
    results = []
    
    for provider in targets:
        status = {
            "provider": provider,
            "installed": PROVIDER_CONFIG[provider]["name"],
            "skill": {"available": False, "installed": False, "path": None},
            "mcp": {"available": False, "enabled": False},
            "bootstrap": {"available": False},
            "auto_attach": False,
        }
        
        # Check skill installation
        skill_root = home / SKILL_AGENT_ROOTS.get(provider, Path(f".{provider}/skills"))
        target = skill_root / SKILL_REL
        
        if skill_root.exists() and target.exists():
            status["skill"]["available"] = True
            status["skill"]["installed"] = True
            status["skill"]["path"] = str(target)
        
        # Check MCP support
        if PROVIDER_CONFIG[provider]["mcp_enabled"]:
            status["mcp"]["available"] = True
            if _find_executable("mcp-jupyter-server"):
                status["mcp"]["enabled"] = True
        
        # Check bootstrap files
        boot_marker = target.parent / f"voyager_{provider}_bootstrap.*"
        if boot_marker.parent.exists():
            boot_files = list(boot_marker.parent.glob("voyager_*_bootstrap.*"))
            if boot_files:
                status["bootstrap"]["available"] = True
                status["bootstrap"]["files"] = [str(f) for f in boot_files[:3]]
        
        # Check auto-attach capability
        if status["skill"]["installed"] and status["mcp"]["enabled"]:
            status["auto_attach"] = True
        
        results.append(status)
    
    return results


def install_skills(agent: Optional[str] = None, force: bool = False,
                   home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Legacy wrapper maintaining backward-compatible return format."""
    results = []
    home = home or Path.home()
    
    # Resolve which agents to process (backward compatible)
    if agent:
        roots = resolve_roots(agent, home=home)
    else:
        roots = resolve_roots(home=home)
    
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
            src_text = skill_source().read_text(encoding="utf-8")
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
                shutil.copy2(skill_source(), target)
                results.append({"agent": name, "status": "updated",
                                "path": str(target),
                                "backup": str(backup)})
            else:
                shutil.copy2(skill_source(), target)
                results.append({"agent": name, "status": "installed",
                                "path": str(target)})
        except OSError as e:
            results.append({"agent": name, "status": "error",
                            "path": str(target), "error": str(e)})
    return results
