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
    if not PROVIDER_CONFIG[provider]["mcp_enabled"]:
        return False, PROVIDER_CONFIG[provider]["name"] + " does not support MCP"
    
    # For Codex/Claude, check if Voyager MCP is actually registered
    home = Path.home()
    
    if provider == "codex":
        mcp_config = home / ".config/codex/mcp.json"
        if mcp_config.exists():
            try:
                with open(mcp_config, 'r') as f:
                    config = json.load(f)
                if "voyager" in config.get("mcpServers", {}):
                    return True, "Voyager MCP already registered"
                return True, "Codex supports MCP but Voyager not yet registered"
            except (json.JSONDecodeError, IOError):
                pass
    
    elif provider == "claude":
        mcp_config = home / ".claude/mcp.json"
        if mcp_config.exists():
            try:
                with open(mcp_config, 'r') as f:
                    config = json.load(f)
                voyager_found = any(
                    "voyager" in entry.lower() 
                    for entry in config.get("MCP_SERVERS", [])
                )
                if voyager_found:
                    return True, "Voyager MCP already registered"
                return True, "Claude supports MCP but Voyager not yet registered"
            except (json.JSONDecodeError, IOError):
                pass
    
    return True, "Provider supports MCP; Voyager not yet registered"


def _register_codex_mcp(home: Path) -> Tuple[str, Optional[str]]:
    """Register Voyager MCP in Codex config.toml or mcp.json, creating files if needed."""
    
    # Try config.toml first (newer format)
    config_file = home / ".config/codex/config.toml"
    
    # Create parent directory if it doesn't exist
    config_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Read existing content or start fresh
    content = ""
    if config_file.exists():
        try:
            content = config_file.read_text(encoding="utf-8")
        except OSError:
            pass
    
    # Check if Voyager already configured
    if "[[mcp_servers.voyager]]" not in content:
        # Append Voyager MCP config
        voyager_entry = "\n[[mcp_servers.voyager]]\ncommand = \"python\"\nargs = [\"-m\", \"voyager.mcp_server\"]\nenv = {}\n"
        
        if content and not content.rstrip().endswith("\n"):
            content += "\n"
        content += voyager_entry
        
        try:
            config_file.write_text(content, encoding="utf-8")
            return "registered", str(config_file)
        except OSError:
            pass
    
    return "up-to-date", str(config_file)


def _register_claude_mcp(home: Path) -> Tuple[str, Optional[str]]:
    """Register Voyager MCP in Claude mcp.json, creating file if needed."""
    import subprocess
    
    mcp_file = home / ".claude/mcp.json"
    
    # Try direct CLI approach first (preferred)
    try:
        result = subprocess.run(
            ["claude", "mcp", "add", "voyager", "python", "-m", "voyager.mcp_server"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return "registered", str(mcp_file)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    # Create parent directory if it doesn't exist
    mcp_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Check if already registered
    existing_config = {}
    if mcp_file.exists():
        try:
            with open(mcp_file, 'r') as f:
                existing_config = json.load(f)
            
            voyagers = [
                s for s in existing_config.get("MCP_SERVERS", [])
                if isinstance(s, str) and "voyager" in s.lower()
            ]
            
            if voyagers:
                return "up-to-date", str(mcp_file)
        except (json.JSONDecodeError, IOError):
            pass
    
    # Add Voyager entry (try different MCP formats based on Claude version)
    # Format 1: Settings format (newer Claude versions)
    if "MCP_SERVERS" not in existing_config or not isinstance(existing_config["MCP_SERVERS"], list):
        existing_config.setdefault("MCP_SERVERS", [])
    
    voyager_entry = "python -m voyager.mcp_server"
    
    # Only add if not already present
    if voyager_entry not in existing_config["MCP_SERVERS"]:
        existing_config["MCP_SERVERS"].append(voyager_entry)
    
    try:
        with open(mcp_file, 'w', encoding='utf-8') as f:
            json.dump(existing_config, f, indent=2)
        
        return "registered", str(mcp_file)
    except OSError:
        pass
    
    # If we get here, file creation failed - provide manual instructions
    return "manual_required", f"Use: claude mcp add voyager python -m voyager.mcp_server"


def _install_codex_bootstrap(target_dir: Path) -> Tuple[str, Optional[str]]:
    """Generate Codex startup bootstrap with actual startup hook instructions."""
    # Codex doesn't have native startup hooks, so provide best-available mechanism
    # The Skill file serves as the primary guidance
    bootstrap_content = """# Voyager Startup Continuity for Codex

Status: STARTUP_ASSISTED (not verified zero-touch)

Codex CLI does not provide a native session-start hook mechanism.

Best available option:

1. Install Voyager Skill (already done by `voyager integrate codex`):
   This instructs Codex to call voyager_startup at session start.

2. Configure MCP connection in ~/.config/codex/config.toml or mcp.json:
   Copy-paste the output of: voyager integrate status --json | jq '.[] | select(.provider=="codex") | .mcp'

3. In each Codex session, before asking user questions:
   Call tool: voyager_startup(provider="codex", cwd="$PWD", native_session_id="<session-id>")

The Skill.md file contains exact invocation instructions.

Note: This relies on agent following instruction - not fully automatic at runtime.
"""
    try:
        bootstrap_file = target_dir / "voyager_codex_bootstrap.md"
        bootstrap_file.write_text(bootstrap_content, encoding="utf-8")
        return "generated", str(bootstrap_file)
    except OSError as e:
        return "error", str(e)


def _install_claude_bootstrap(target_dir: Path) -> Tuple[str, Optional[str]]:
    """Install Claude MCP registration for Voyager."""
    # Attempt to register via Claude's CLI if available
    home = Path.home()
    mcp_file = home / ".claude/mcp.json"
    
    # Try direct CLI approach first
    try:
        import subprocess
        result = subprocess.run(
            ["claude", "mcp", "add", "voyager", "python", "-m", "voyager.mcp_server"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return "registered", str(mcp_file)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    # Fall back to manual instruction file
    bootstrap_content = """# Voyager Startup Continuity for Claude Code

Status: STARTUP_ASSISTED (requires manual MCP configuration)

Claude Code does not provide verified zero-touch startup hooks.

Registration steps:

1. Register Voyager MCP server:
   claude mcp add voyager python -m voyager.mcp_server

2. Verify registration:
   cat ~/.claude/mcp.json | jq '.MCP_SERVERS'

3. Restart Claude Code for changes to take effect

The Skill.md file contains exact invocation instructions for within sessions.

Note: This requires manual one-time setup; not fully automatic at runtime.
"""
    try:
        bootstrap_file = target_dir / "voyager_claude_bootstrap.md"
        bootstrap_file.write_text(bootstrap_content, encoding="utf-8")
        return "generated", str(bootstrap_file)
    except OSError as e:
        return "error", str(e)


def _install_grok_bootstrap(target_dir: Path) -> Tuple[str, Optional[str]]:
    """Generate Grok CLI bootstrap instructions (best-effort)."""
    bootstrap_content = """# Voyager Startup Continuity for Grok CLI

Status: BEST_EFFORT (no startup hooks available)

Grok CLI does not provide session-start hooks or reliable MCP integration.

Available options:

1. Skill-based guidance:
   SKILL.md is installed at ~/.grok/skills/voyager/SKILL.md
   
   Instruct Grok to call voyager_startup at start of each session.

2. Manual invocation:
   Call voyager_startup tool explicitly before starting work.

Cannot achieve zero-touch due to platform limitations.
"""
    try:
        bootstrap_file = target_dir / "voyager_grok_bootstrap.md"
        bootstrap_file.write_text(bootstrap_content, encoding="utf-8")
        return "generated", str(bootstrap_file)
    except OSError as e:
        return "error", str(e)


def _install_dsh_bootstrap(target_dir: Path) -> Tuple[str, Optional[str]]:
    """Generate DSH bootstrap instructions (best-effort)."""
    bootstrap_content = """# Voyager Startup Continuity for DSH

Status: BEST_EFFORT (platform limitations)

DSH characteristics:
- Provides session resume: dsh --resume
- Storage: zstd-compressed JSONL in ~/.dsh/sessions
- No verified startup hooks or MCP support

Best available option:

1. Install Voyager Skill at ~/.dsh/skills/voyager/SKILL.md
2. Manually invoke continuation context at session start

Cannot guarantee automatic discovery without platform support.
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
      {provider, skill:installed|up-to-date|error, mcp:registered|available/manual, 
       bootstrap:installed|generated, startup_status:Y/A/N, auto_attach:bool,
       verified:string}
    """
    home = home or Path.home()
    result: Dict[str, Any] = {"provider": provider, "status": "pending"}
    
    if provider not in PROVIDER_CONFIG:
        result.update({"status": "error", "warnings": [f"Unknown provider: {provider}"]})
        return result
    
    if not _agent_installed(provider, home):
        result.update({"status": "skipped",
                       "warnings": [f"{provider} not installed ({home / ('.' + provider)})"]})
        return result
    
    # Step 1: Install Skill (always attempted)
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
    
    # Step 2: Register MCP where supported
    has_mcp = PROVIDER_CONFIG[provider]["mcp_enabled"]
    mcp_result = {"status": "unsupported"}
    
    if has_mcp:
        mcp_support, mcp_msg = _check_mcp_support(provider)
        
        if mcp_support and "not yet registered" in mcp_msg.lower():
            # Attempt registration based on provider
            if provider == "codex":
                reg_status, reg_path = _register_codex_mcp(home)
                mcp_result = {
                    "status": "registered" if reg_status in ("registered", "up-to-date") else "manual_required",
                    "path": reg_path,
                    "message": "Voyager MCP configured" if reg_status in ("registered", "up-to-date") else mcp_msg
                }
            elif provider == "claude":
                reg_status, reg_path = _register_claude_mcp(home)
                mcp_result = {
                    "status": "registered" if reg_status in ("registered", "up-to-date") else "manual_required",
                    "path": reg_path,
                    "message": "Voyager MCP configured" if reg_status in ("registered", "up-to-date") else mcp_msg
                }
        else:
            mcp_result = {"status": "available", "message": mcp_msg}
    
    result["mcp"] = mcp_result
    
    # Step 3: Generate startup instructions
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
    
    # Step 4: Determine startup status (Y=AUTO, A=ASSISTED, N=NONE)
    if PROVIDER_CONFIG[provider]["has_startup_hook"]:
        if mcp_result["status"] == "registered":
            result["startup_status"] = "A"  # Assisted via instruction following
        else:
            result["startup_status"] = "N"  # No reliable hook
    else:
        result["startup_status"] = "N"  # Platform doesn't support hooks
    
    # Step 5: Auto-attach capability (core always supports this)
    result["auto_attach"] = True
    
    # Step 6: Mark verification status
    result["verified"] = "pending_real_provider_test"
    result["status"] = "success" if result["skill"]["status"] == "installed" else "partial"
    
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
            "mcp": {"available": False, "registered": False, "message": ""},
            "bootstrap": {"available": False, "status": None},
            "auto_attach": False,
            "startup_status": "N",  # Y=AUTO, A=ASSISTED, N=NONE
            "verified": "not_tested",
        }
        
        # Check skill installation
        skill_root = home / SKILL_AGENT_ROOTS.get(provider, Path(f".{provider}/skills"))
        target = skill_root / SKILL_REL
        
        if skill_root.exists() and target.exists():
            status["skill"]["available"] = True
            status["skill"]["installed"] = True
            status["skill"]["path"] = str(target)
        
        # Check MCP registration
        has_mcp = PROVIDER_CONFIG[provider]["mcp_enabled"]
        if has_mcp:
            status["mcp"]["available"] = True
            mcp_support, mcp_msg = _check_mcp_support(provider)
            
            if provider == "codex":
                mcp_config = home / ".config/codex/mcp.json"
                if mcp_config.exists():
                    try:
                        with open(mcp_config, 'r') as f:
                            config = json.load(f)
                        if "voyager" in config.get("mcpServers", {}):
                            status["mcp"]["registered"] = True
                            status["mcp"]["message"] = "Voyager MCP registered"
                    except (json.JSONDecodeError, IOError):
                        pass
            
            elif provider == "claude":
                mcp_file = home / ".claude/mcp.json"
                if mcp_file.exists():
                    try:
                        with open(mcp_file, 'r') as f:
                            config = json.load(f)
                        voyagers = [
                            s for s in config.get("MCP_SERVERS", [])
                            if isinstance(s, str) and "voyager" in s.lower()
                        ]
                        if voyagers:
                            status["mcp"]["registered"] = True
                            status["mcp"]["message"] = "Voyager MCP registered"
                        else:
                            status["mcp"]["message"] = "Requires manual registration"
                    except (json.JSONDecodeError, IOError):
                        pass
        
        # Check bootstrap files
        boot_marker = target.parent / f"voyager_{provider}_bootstrap.*"
        if boot_marker.parent.exists():
            boot_files = list(boot_marker.parent.glob("voyager_*_bootstrap.*"))
            if boot_files:
                status["bootstrap"]["available"] = True
                status["bootstrap"]["status"] = "instructions_generated"
        
        # Determine startup_status based on platform capabilities
        if not has_mcp:
            status["startup_status"] = "N"  # No hook support
        elif status["mcp"]["registered"]:
            status["startup_status"] = "A"  # Assisted via instruction following
        else:
            status["startup_status"] = "N"  # Not registered
        
        # Auto-attach capability (core always supports this)
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
