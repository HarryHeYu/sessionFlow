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
        "config_file": ".codex/config.toml",
        "mcp_config": ".codex/config.toml",
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
        # Grok has a native SessionStart hook (live-verified 2026-09-25); this
        # flag was False only because the hook had not been implemented yet.
        "has_startup_hook": True,
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
    """Find executable in PATH using platform-independent method."""
    import shutil
    which_path = shutil.which(name)
    if which_path:
        exe_path = Path(which_path)
        if exe_path.exists():
            return exe_path
    return None


def _check_mcp_support(provider: str, home: Optional[Path] = None) -> Tuple[bool, str]:
    """Check if provider supports MCP integration.

    `home` must be threaded through from the caller.  This used to hardcode
    `Path.home()`, which silently ignored the CLI's `--home` flag, so every
    `--home`-scoped run reported on the *real* user profile instead of the one
    it was pointed at.
    """
    if not PROVIDER_CONFIG[provider]["mcp_enabled"]:
        return False, PROVIDER_CONFIG[provider]["name"] + " does not support MCP"
    
    # For Codex/Claude, check if Voyager MCP is actually registered
    home = home or Path.home()
    
    if provider == "codex":
        mcp_config = home / ".codex/config.toml"
        if mcp_config.exists():
            try:
                content = mcp_config.read_text(encoding="utf-8")
                # Check for Voyager in TOML format [mcp_servers.voyager]
                if "[mcp_servers.voyager]" in content:
                    return True, "Voyager MCP already registered"
                return True, "Codex supports MCP but Voyager not yet registered"
            except OSError:
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
    """Register Voyager MCP in Codex config.toml, creating file if needed."""
    
    config_file = home / ".codex/config.toml"
    
    # Create parent directory if it doesn't exist
    config_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Read existing content or start fresh
    content = ""
    if config_file.exists():
        try:
            content = config_file.read_text(encoding="utf-8")
        except OSError:
            pass
    
    # Check if Voyager already configured (TOML table format)
    if "[mcp_servers.voyager]" not in content:
        # Append Voyager MCP config using proper TOML syntax
        voyager_entry = "\n[mcp_servers.voyager]\ncommand = \"python\"\nargs = [\"-m\", \"voyager.mcp_server\"]\nenv = {}\n"
        
        if content and not content.rstrip().endswith("\n"):
            content += "\n"
        content += voyager_entry
        
        try:
            config_file.write_text(content, encoding="utf-8")
            return "registered", str(config_file)
        except OSError:
            pass
    
    return "up-to-date", str(config_file)


def _is_real_home(home: Path) -> bool:
    """True when `home` is the profile the provider CLIs would write to.

    Provider CLIs (`claude mcp add`, ...) have no `--home` of their own: they
    write wherever the provider is configured to look, which is the real
    profile.  So they may only be used when that is what the caller asked for;
    otherwise `integrate install --home <scratch>` would mutate the real
    profile and spawn a provider process as a side effect of a config write.
    """
    try:
        return Path(home).resolve() == Path.home().resolve()
    except OSError:
        return False


def _register_claude_mcp(home: Path) -> Tuple[str, Optional[str]]:
    """Register Voyager MCP for Claude Code.
    
    Prefers the provider CLI when operating on the real profile (it produces a
    validated config), and falls back to writing `mcp.json` directly otherwise.
    """
    
    mcp_file = home / ".claude/mcp.json"
    
    # Only shell out when `home` really is the profile the CLI would write to.
    if _is_real_home(home):
        try:
            result = subprocess.run(
                ["claude", "mcp", "add", "voyager", "--", "python", "-m", "voyager.mcp_server"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return "registered", str(mcp_file)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            # OSError also covers Windows' WinError 193, raised when the
            # provider ships as a `.cmd`/`.bat` shim that CreateProcess cannot
            # execute directly.
            pass
    
    # CLI not available or failed - check if we can write directly
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
    
    # Add Voyager entry using verified MCP_SERVERS array format
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
    return "manual_required", "Use: claude mcp add voyager -- python -m voyager.mcp_server"


def _install_codex_bootstrap(target_dir: Path,
                             home: Optional[Path] = None) -> Tuple[str, Optional[str]]:
    """Generate Codex startup bootstrap with actual startup hook instructions."""
    # Codex doesn't have native startup hooks, so provide best-available mechanism
    # The Skill file serves as the primary guidance
    bootstrap_content = """# Voyager Startup Continuity for Codex

Status: N (no native session-start hook; first-turn / Skill guidance)

Codex CLI does not provide a native session-start hook mechanism, so there is
nothing for Voyager to register. Continuity still works, but it is the agent
following an instruction rather than the platform firing a hook.

Best available option:

1. Install Voyager Skill (already done by `voyager integrate install codex`):
   This instructs Codex to call voyager_startup at session start.

2. Configure MCP connection in ~/.codex/config.toml:
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


def _install_claude_bootstrap(target_dir: Path,
                              home: Optional[Path] = None) -> Tuple[str, Optional[str]]:
    """Generate the Claude Code bootstrap doc.

    MCP registration itself belongs to `_register_claude_mcp`; this function
    only produces the bootstrap file.  The provider CLI is consulted only when
    we are on the real profile, for the same isolation reason as there: it
    writes wherever the provider looks, not where the caller asked.
    """
    home = home or Path.home()
    mcp_file = home / ".claude/mcp.json"

    if _is_real_home(home):
        try:
            result = subprocess.run(
                ["claude", "mcp", "add", "voyager", "python", "-m", "voyager.mcp_server"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return "registered", str(mcp_file)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    
    # Fall back to manual instruction file
    bootstrap_content = """# Voyager Startup Continuity for Claude Code

Status: NATIVE_SESSIONSTART_HOOK (registered; live trigger not yet observed)

Claude Code reads `SessionStart` hooks from ~/.claude/settings.json, and
Voyager registers one there. It fires without the model having to follow an
instruction. The handler is verified end-to-end; what is NOT yet verified is
Claude Code itself firing the hook, so treat the trigger as unproven until you
have seen it in `claude --debug hooks --init-only`.

Install or repair the hook:
   voyager integrate install claude

Verify the trigger:
   claude --debug hooks --init-only
   # expect: Found 1 hook matchers in settings

MCP registration is still useful for in-session tool calls:
   claude mcp add voyager python -m voyager.mcp_server

Restart Claude Code after changing settings.json.

Note: registration is automatic; confirming the trigger is a one-time manual step.
"""
    try:
        bootstrap_file = target_dir / "voyager_claude_bootstrap.md"
        bootstrap_file.write_text(bootstrap_content, encoding="utf-8")
        return "generated", str(bootstrap_file)
    except OSError as e:
        return "error", str(e)


def _install_grok_bootstrap(target_dir: Path,
                            home: Optional[Path] = None) -> Tuple[str, Optional[str]]:
    """Generate Grok CLI bootstrap instructions (best-effort)."""
    bootstrap_content = """# Voyager Startup Continuity for Grok CLI

Status: N (no native hook; opt-in launcher shim available)

Grok CLI does not provide session-start hooks or reliable MCP integration, so
there is nothing for Voyager to register. The opt-in launcher wrapper is the
closest thing to automatic startup.

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


def _install_dsh_bootstrap(target_dir: Path,
                           home: Optional[Path] = None) -> Tuple[str, Optional[str]]:
    """Generate DSH bootstrap instructions (best-effort)."""
    bootstrap_content = """# Voyager Startup Continuity for DSH

Status: N (no native hook; launcher + session watcher available)

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
       bootstrap:installed|generated, startup_status:Y/H/A/N, auto_attach:bool,
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
        mcp_support, mcp_msg = _check_mcp_support(provider, home)

        if mcp_support and "already registered" in mcp_msg.lower():
            # Idempotent re-run.  This still has to count as *registered*:
            # otherwise a second `integrate install` reported `N` for a provider
            # that `integrate status` happily reported as `A`.
            mcp_result = {"status": "registered", "path": None, "message": mcp_msg}
        elif mcp_support and "not yet registered" in mcp_msg.lower():
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

    # Step 2b: Native lifecycle hook.  Claude Code reads `SessionStart` hooks
    # from ~/.claude/settings.json, which is the only mechanism that fires
    # without the model having to follow an instruction.  Registration here
    # used to be skipped entirely, which is why the hook had to be added by
    # hand and the CLI's own install looked like it had done nothing.
    if provider == "claude":
        from .integrations.claude import ClaudeIntegration

        hook_result = ClaudeIntegration(home=home).install()
        result["hook"] = hook_result
        if hook_result.get("status") != "installed":
            result.setdefault("warnings", []).append(
                "native SessionStart hook not installed: "
                + str(hook_result.get("message") or hook_result.get("status"))
            )
    elif provider == "grok":
        # Grok's native SessionStart hook is what records the pending attach, so
        # registering it is the same class of step as Claude's -- and the reason
        # Grok stops reporting `N`.  `install()` also writes the launcher shim,
        # which needs the real binary on PATH; both install and status key off
        # the hook files, so they cannot disagree about the letter.
        from .integrations.grok import GrokIntegration

        hook_result = GrokIntegration(home=home).install()
        result["hook"] = hook_result
        if hook_result.get("status") != "installed":
            result.setdefault("warnings", []).append(
                "native SessionStart hook not installed: "
                + str(hook_result.get("message") or hook_result.get("status"))
            )

    # Step 3: Generate startup instructions
    bootstrap_funcs = {
        "codex": _install_codex_bootstrap,
        "claude": _install_claude_bootstrap,
        "grok": _install_grok_bootstrap,
        "dsh": _install_dsh_bootstrap,
    }
    
    bootstrap_func = bootstrap_funcs.get(provider)
    if bootstrap_func:
        bs_status, bs_path = bootstrap_func(target.parent, home)
        result["bootstrap"] = {"status": bs_status, "path": bs_path}
    
    # Step 4: Determine startup status.
    #   Y = platform fires the hook automatically AND that was verified live
    #   H = native hook registered; the live trigger is not verified yet
    #   A = no hook, but MCP-assisted startup is available
    #   N = no mechanism
    #
    # This MUST agree with `check_integration_status()`.  The two used to key off
    # different fields -- install off `has_startup_hook`, status off `mcp_enabled`
    # -- so `voyager integrate install codex` reported `N` while
    # `voyager integrate status` reported `A` for the very same machine.
    hook_ok = result.get("hook", {}).get("status") == "installed"
    if hook_ok:
        # Registration is proven; the trigger firing is not, so this is
        # deliberately not "Y".
        result["startup_status"] = "H"
    elif not has_mcp:
        result["startup_status"] = "N"  # No hook and no MCP surface
    elif mcp_result["status"] == "registered":
        result["startup_status"] = "A"  # Assisted via instruction following
    else:
        result["startup_status"] = "N"  # Not registered
    
    # Step 5: Auto-attach capability (core always supports this)
    result["auto_attach"] = True
    
    # Step 6: Mark verification status
    result["verified"] = "pending_real_provider_test"
    result["status"] = "success" if result["skill"]["status"] == "installed" else "partial"
    
    return result


def uninstall_integration(provider: str, home: Optional[Path] = None) -> Dict[str, Any]:
    """Remove integration for a provider - undo everything Voyager installed."""
    home = home or Path.home()
    result: Dict[str, Any] = {"provider": provider, "status": "removed"}
    
    if provider not in PROVIDER_CONFIG:
        result.update({"status": "error", "warnings": [f"Unknown provider: {provider}"]})
        return result
    
    # Step 1: Remove Skill file
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
    
    # Step 2: Remove MCP registration where supported
    has_mcp = PROVIDER_CONFIG[provider]["mcp_enabled"]
    if has_mcp:
        mcp_result = {"status": "not-supported"}
        
        if provider == "codex":
            config_file = home / ".codex/config.toml"
            if config_file.exists():
                try:
                    content = config_file.read_text(encoding="utf-8")
                    if "[mcp_servers.voyager]" in content:
                        # Remove Voyager entry from TOML (preserve other entries)
                        lines = content.split("\n")
                        new_lines = []
                        skip_until_next_table = False
                        
                        for line in lines:
                            if "[mcp_servers.voyager]" in line:
                                skip_until_next_table = True
                                continue
                            if skip_until_next_table:
                                # Skip until we hit next table marker or EOF
                                if line.strip().startswith("["):
                                    skip_until_next_table = False
                                    new_lines.append(line)
                                continue
                            new_lines.append(line)
                        
                        new_content = "\n".join(new_lines)
                        config_file.write_text(new_content, encoding="utf-8")
                        mcp_result = {"status": "removed", "path": str(config_file)}
                    else:
                        mcp_result = {"status": "not-found", "path": str(config_file)}
                except OSError as e:
                    mcp_result = {"status": "error", "path": str(config_file), "error": str(e)}
            else:
                mcp_result = {"status": "not-found", "path": str(config_file)}
            
            result["mcp"] = mcp_result
        
        elif provider == "claude":
            mcp_file = home / ".claude/mcp.json"
            if mcp_file.exists():
                try:
                    with open(mcp_file, 'r') as f:
                        config = json.load(f)
                    
                    if "MCP_SERVERS" in config and isinstance(config["MCP_SERVERS"], list):
                        # Remove only Voyager entry (preserve others)
                        original_count = len(config["MCP_SERVERS"])
                        config["MCP_SERVERS"] = [
                            s for s in config["MCP_SERVERS"]
                            if not (isinstance(s, str) and "voyager" in s.lower())
                        ]
                        
                        # Only write if we actually removed something
                        if len(config["MCP_SERVERS"]) < original_count:
                            with open(mcp_file, 'w', encoding='utf-8') as f:
                                json.dump(config, f, indent=2)
                            result["mcp"] = {"status": "removed", "path": str(mcp_file)}
                        else:
                            result["mcp"] = {"status": "not-found", "path": str(mcp_file)}
                    else:
                        result["mcp"] = {"status": "not-found", "path": str(mcp_file)}
                except (json.JSONDecodeError, IOError) as e:
                    result["mcp"] = {"status": "error", "path": str(mcp_file), "error": str(e)}
            else:
                result["mcp"] = {"status": "not-found", "path": str(mcp_file)}
    
    # Step 2b: Remove the native lifecycle hook (Claude Code).
    if provider == "claude":
        from .integrations.claude import ClaudeIntegration

        result["hook"] = ClaudeIntegration(home=home).remove()

    # Step 3: Remove bootstrap files
    boot_marker = target.parent / f"voyager_{provider}_bootstrap.*"
    if boot_marker.parent.exists():
        boot_files = list(boot_marker.parent.glob("voyager_*_bootstrap.*"))
        removed_bootstraps = []
        for bf in boot_files:
            try:
                bf.unlink()
                removed_bootstraps.append(str(bf))
            except OSError:
                pass
        
        if removed_bootstraps:
            result["bootstrap"] = {"status": "removed", "paths": removed_bootstraps}
        else:
            result["bootstrap"] = {"status": "not-found"}
    
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
            "startup_status": "N",  # Y=verified live, H=hook registered (unverified), A=assisted, N=none
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
            mcp_support, mcp_msg = _check_mcp_support(provider, home)
            
            if provider == "codex":
                mcp_config = home / ".codex/config.toml"
                if mcp_config.exists():
                    try:
                        content = mcp_config.read_text(encoding="utf-8")
                        if "[mcp_servers.voyager]" in content:
                            status["mcp"]["registered"] = True
                            status["mcp"]["message"] = "Voyager MCP registered"
                        else:
                            status["mcp"]["message"] = "Requires manual registration"
                    except (OSError, IOError):
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
        
        # Native lifecycle hook registration (Claude Code and Grok CLI).
        status["hook"] = {"registered": False, "command": None}
        if provider == "claude":
            from .integrations.claude import ClaudeIntegration

            hook_verify = ClaudeIntegration(home=home).verify()
            status["hook"]["registered"] = bool(hook_verify["verified"])
            status["hook"]["command"] = hook_verify.get("command")
        elif provider == "grok":
            from .integrations.grok import GrokIntegration

            gi = GrokIntegration(home=home)
            checks = gi.verify().get("checks", {})
            # The letter is about the native hook, not the launcher shim: the
            # shim needs the real binary on PATH, the hook does not.
            status["hook"]["registered"] = bool(
                checks.get("hook_installed") and checks.get("hook_script"))
            status["hook"]["command"] = str(gi.hook_cmd)

        # Determine startup_status based on platform capabilities
        if status["hook"]["registered"]:
            status["startup_status"] = "H"  # registered; live trigger unverified
        elif not has_mcp:
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
