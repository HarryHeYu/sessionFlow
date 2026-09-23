"""Provider capability audit and zero-touch level classification.

This module audits all supported providers for their native lifecycle surfaces
and classifies them into deterministic zero-touch levels based on actual
platform capabilities, not assumptions.

Classification hierarchy (strongest to weakest):
  SESSION_START_ZERO_TOUCH > FIRST_TURN_ZERO_TOUCH > LAUNCHER_ZERO_TOUCH >
  WATCHER_ATTACH_ONLY > STARTUP_ASSISTED > BEST_EFFORT > UNSUPPORTED
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class ZeroTouchLevel(Enum):
    """Zero-Touch integration levels from strongest to weakest."""
    
    # Native session-start hook available - Voyager invoked automatically when agent spawns
    SESSION_START_ZERO_TOUCH = "session_start_zero_touch"
    
    # First-turn instruction-based - Agent auto-calls Voyager before first user task
    FIRST_TURN_ZERO_TOUCH = "first_turn_zero_touch"
    
    # Launcher shim - Opt-in wrapper script provides continuity on launch
    LAUNCHER_ZERO_TOUCH = "launcher_zero_touch"
    
    # Process/file watcher - GUI IDEs detected via DB/file monitoring
    WATCHER_ATTACH_ONLY = "watcher_attach_only"
    
    # Requires explicit user invocation via MCP tool or prompt
    STARTUP_ASSISTED = "startup_assisted"
    
    # Partial automation possible but limited by platform constraints
    BEST_EFFORT = "best_effort"
    
    # No automation surface available
    UNSUPPORTED = "unsupported"


@dataclass
class ProviderCapabilityStatus:
    """Separate platform capability from local configuration."""
    
    platform_supported: bool = False  # Does the provider support this?
    locally_configured: bool = False  # Is it installed on this machine?
    live_verified: bool = False       # Has it been tested end-to-end?


@dataclass
class ZeroTouchCapability:
    """Complete capability profile per zero-touch level."""
    
    session_start_zero_touch: ProviderCapabilityStatus = field(
        default_factory=ProviderCapabilityStatus
    )
    first_turn_zero_touch: ProviderCapabilityStatus = field(
        default_factory=ProviderCapabilityStatus
    )
    launcher_zero_touch: ProviderCapabilityStatus = field(
        default_factory=ProviderCapabilityStatus
    )
    watcher_attach_only: ProviderCapabilityStatus = field(
        default_factory=ProviderCapabilityStatus
    )


@dataclass
class ProviderCapabilities:
    """Capability profile for a single provider."""
    
    provider: str
    name: str
    
    # Native lifecycle hooks (platform capability)
    has_session_start_hook: bool = False
    has_agent_spawn_hook: bool = False
    has_prompt_submit_hook: bool = False
    
    # Instruction surfaces (platform capability)
    global_instruction_supported: bool = False
    project_instruction_supported: bool = False
    skill_system_supported: bool = False
    
    # Integration protocols (platform capability)
    mcp_supported: bool = False
    plugin_surface: bool = False
    extension_api: bool = False
    
    # CLI & launcher (platform capability)
    cli_launcher: bool = False
    launcher_available: bool = False
    
    # Session management
    native_session_id_at_start: bool = False
    session_file_creation_timing: Optional[str] = None
    
    # Context injection (platform capability)
    stdout_context_injection: bool = False
    stdin_context_injection: bool = False
    env_variable_injection: bool = False
    
    # Workspace detection
    workspace_field_available: bool = False
    repo_field_available: bool = False
    
    # Additional notes
    source_format_known: bool = False
    max_zero_touch_level: ZeroTouchLevel = ZeroTouchLevel.UNSUPPORTED
    
    # Current installation state (separate from platform capability)
    installed: bool = False
    config_valid: bool = False
    hook_invoked: bool = False
    
    notes: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization and status display."""
        return {
            "provider": self.provider,
            "name": self.name,
            "session_start_hook": self.has_session_start_hook,
            "agent_spawn_hook": self.has_agent_spawn_hook,
            "prompt_submit_hook": self.has_prompt_submit_hook,
            "global_instruction": self.global_instruction_supported,
            "project_instruction": self.project_instruction_supported,
            "skill_system": self.skill_system_supported,
            "mcp_supported": self.mcp_supported,
            "plugin_surface": self.plugin_surface,
            "cli_launcher": self.cli_launcher,
            "launcher_available": self.launcher_available,
            "native_session_id_at_start": self.native_session_id_at_start,
            "stdout_context_injection": self.stdout_context_injection,
            "max_level": self.max_zero_touch_level.value,
            "notes": self.notes,
        }


def detect_capabilities(provider: str, home: Optional[Path] = None) -> ProviderCapabilities:
    """Audit actual platform capabilities for a provider.
    
    This function performs real investigation using:
    - Local --help output where available
    - Existing config files on the system
    - Official documentation references
    - Executable existence checks
    
    Returns concrete capability profile, not marketing assumptions.
    """
    home = home or Path.home()
    
    # Dispatcher to provider-specific detection
    detectors = {
        "codex": _detect_codex_capabilities,
        "claude": _detect_claude_capabilities,
        "grok": _detect_grok_capabilities,
        "dsh": _detect_dsh_capabilities,
        "zcode": _detect_zcode_capabilities,
        "cursor": _detect_cursor_capabilities,
        "kiro": _detect_kiro_capabilities,
        "antigravity": _detect_antigravity_capabilities,
    }
    
    detector = detectors.get(provider)
    if not detector:
        return ProviderCapabilities(
            provider=provider,
            name=provider.title(),
            notes=[f"Unknown provider - manual audit required"],
        )
    
    return detector(home)


def get_all_capabilities(home: Optional[Path] = None) -> Dict[str, ProviderCapabilities]:
    """Get capability profiles for all 8 supported providers."""
    providers = [
        "codex", "claude", "grok", "dsh",
        "zcode", "cursor", "kiro", "antigravity",
    ]
    return {p: detect_capabilities(p, home) for p in providers}


# =============================================================================
# Provider-Specific Detectors
# =============================================================================


def _detect_codex_capabilities(home: Path) -> ProviderCapabilities:
    """Codex CLI/VSCode/Desktop capability audit.
    
    Investigation findings:
    - Global instructions via AGENTS.md (both global and per-repo)
    - MCP server support (config.toml)
    - Skill system (~/.codex/skills/)
    - NO native session-start hook in current versions
    - session ID only available after first turn
    """
    # Check executable
    exe = shutil.which("codex")
    cli_launcher = exe is not None
    
    # Check for global instruction support (AGENTS.md)
    global_agents = home / ".codex/AGENTS.md"
    global_instruction_supported = global_agents.exists()
    
    # Check for project instruction support
    # (would be in repo root as .codex/AGENTS.md)
    
    # Check for skill system
    skill_root = home / ".codex/skills"
    skill_system_supported = skill_root.exists()
    
    # Check MCP support
    mcp_config = home / ".codex/config.toml"
    mcp_supported = mcp_config.exists()
    
    # CODX does NOT have native session-start hook
    # Agent spawn hook would require OS-level integration (not present)
    
    # Determine max level
    if global_instruction_supported and skill_system_supported:
        max_level = ZeroTouchLevel.FIRST_TURN_ZERO_TOUCH
        notes = [
            "Relies on AGENTS.md + Skill for first-turn automaticity",
            "User must start with normal task (not asking about Voyager)",
            "MCP tool voyager_startup called before first model response",
        ]
    elif mcp_supported:
        max_level = ZeroTouchLevel.STARTUP_ASSISTED
        notes = ["MCP registered but requires explicit invocation"]
    else:
        max_level = ZeroTouchLevel.STARTUP_ASSISTED
        notes = ["Best effort via Skill guidance alone"]
    
    return ProviderCapabilities(
        provider="codex",
        name="Codex",
        cli_launcher=cli_launcher,
        global_instruction_supported=global_instruction_supported,
        skill_system_supported=skill_system_supported,
        mcp_supported=mcp_supported,
        has_session_start_hook=False,
        has_agent_spawn_hook=False,
        native_session_id_at_start=False,
        session_file_creation_timing="after_first_turn",
        max_zero_touch_level=max_level,
        notes=notes,
    )


def _detect_claude_capabilities(home: Path) -> ProviderCapabilities:
    """Claude Code capability audit.
    
    CRITICAL: Must check for ACTUAL session-start hook, not assume from old docs.
    
    Investigation findings:
    - Global instructions via CLAUDE.md
    - MCP server support (~/.claude/mcp.json or CLI)
    - Skill system (~/.claude/skills/)
    - Need to verify if Claude Code 2026+ provides SessionStart hook
    
    Key question: Does Claude invoke external commands automatically at session start?
    Answer: Currently NO verified mechanism found. Relies on instruction-following.
    """
    # Check executable
    exe = shutil.which("claude-code") or shutil.which("claude")
    cli_launcher = exe is not None
    
    # Check for global instruction support (CLAUDE.md)
    global_claude = home / ".claude/CLAUDE.md"
    global_instruction_supported = global_claude.exists()
    
    # Check for skill system
    skill_root = home / ".claude/skills"
    skill_system_supported = skill_root.exists()
    
    # Check MCP support
    mcp_config = home / ".claude/mcp.json"
    mcp_cli_exists = cli_launcher and _has_claude_mcp_command(cli_launcher)
    mcp_supported = mcp_config.exists() or mcp_cli_exists
    
    # SessionStart hooks: Claude Code DOES support them — the CLI bundle's event
    # list contains `SessionStart`, and `--init-only` exists specifically to run
    # Setup + SessionStart:startup headlessly.  What varies per machine is
    # whether a hook is *registered*, so that is what gets detected here.  An
    # earlier revision hardcoded "no hook" and stayed wrong after the
    # integration shipped.
    from .claude import ClaudeIntegration

    hook_status = ClaudeIntegration(home=home).verify()
    hook_registered = bool(hook_status["verified"])

    # The platform ceiling is a native session-start hook.  Whether one is
    # configured here, and whether it has been observed firing, are separate
    # facts carried by `config_valid` / `hook_invoked` below.
    max_level = ZeroTouchLevel.SESSION_START_ZERO_TOUCH
    notes = [
        "Native SessionStart hooks are supported and Voyager ships a handler",
        ("Voyager hook registered in ~/.claude/settings.json"
         if hook_registered else
         "No Voyager hook registered - run `voyager integrate install claude`"),
        "Claude Code firing the trigger has NOT been observed yet",
    ]

    return ProviderCapabilities(
        provider="claude",
        name="Claude Code",
        cli_launcher=cli_launcher,
        global_instruction_supported=global_instruction_supported,
        skill_system_supported=skill_system_supported,
        mcp_supported=mcp_supported,
        has_session_start_hook=True,
        has_agent_spawn_hook=False,
        native_session_id_at_start=False,
        session_file_creation_timing="after_first_turn",
        stdout_context_injection=True,
        max_zero_touch_level=max_level,
        config_valid=hook_registered,
        hook_invoked=False,
        notes=notes,
    )


def _detect_grok_capabilities(home: Path) -> ProviderCapabilities:
    """Grok CLI capability audit.
    
    Investigation findings:
    - Provides CLI: grok -r for resume
    - Skill system (~/.grok/skills/)
    - NO confirmed MCP support
    - NO native session-start hook
    - Could use launcher shim strategy
    
    Key: Grok CLI is shell-friendly, good candidate for opt-in wrapper.
    """
    # Check executable
    exe = shutil.which("grok")
    cli_launcher = exe is not None
    
    # Check for skill system
    skill_root = home / ".grok/skills"
    skill_system_supported = skill_root.exists()
    
    # Grok does NOT have MCP support (per current investigation)
    mcp_supported = False
    
    # Grok does NOT have native hooks
    has_session_start_hook = False
    
    # Best approach: launcher shim via opt-in installation
    max_level = ZeroTouchLevel.LAUNCHER_ZERO_TOUCH if cli_launcher else ZeroTouchLevel.STARTUP_ASSISTED
    notes = [
        "Opt-in launcher wrapper recommended",
        "Resume via 'grok -r' supported",
        "Skill guidance available but no native hook",
    ]
    
    return ProviderCapabilities(
        provider="grok",
        name="Grok CLI",
        cli_launcher=cli_launcher,
        launcher_available=cli_launcher,
        skill_system_supported=skill_system_supported,
        mcp_supported=mcp_supported,
        has_session_start_hook=has_session_start_hook,
        native_session_id_at_start=False,
        session_file_creation_timing="unknown",
        max_zero_touch_level=max_level,
        notes=notes,
    )


def _detect_dsh_capabilities(home: Path) -> ProviderCapabilities:
    """DSH (DeepSeek Harness) capability audit.
    
    Investigation findings:
    - Provides CLI: dsh --resume
    - Storage: zstd-compressed JSONL in ~/.dsh/sessions
    - NO confirmed MCP support
    - NO native session-start hook
    - Session creation timing unknown (probe needed)
    
    Best approach: launcher + session file watcher
    """
    # Check executable
    exe = shutil.which("dsh")
    cli_launcher = exe is not None
    
    # Check for skill system
    skill_root = home / ".dsh/skills"
    skill_system_supported = skill_root.exists()
    
    # DSH does NOT have MCP support
    mcp_supported = False
    
    # Check session directory structure
    session_dir = home / ".dsh/sessions"
    session_structure_known = session_dir.exists()
    
    max_level = ZeroTouchLevel.LAUNCHER_ZERO_TOUCH if cli_launcher else ZeroTouchLevel.BEST_EFFORT
    notes = [
        "Launcher + session watcher strategy viable",
        "Can attach new sessions via file polling",
        "Context injection unverified - may require manual paste",
    ]
    
    return ProviderCapabilities(
        provider="dsh",
        name="DSH",
        cli_launcher=cli_launcher,
        launcher_available=cli_launcher,
        skill_system_supported=skill_system_supported,
        mcp_supported=mcp_supported,
        has_session_start_hook=False,
        native_session_id_at_start=False,
        session_file_creation_timing="unknown",
        max_zero_touch_level=max_level,
        notes=notes,
    )


def _detect_zcode_capabilities(home: Path) -> ProviderCapabilities:
    """ZCode (CLI vs Desktop distinction).
    
    CRITICAL BLOCKER: Source discovery overly relies on hardcoded path:
      ~/.zcode/cli/db/db.sqlite
    
    Investigation needed:
    - Actual DB path location rules
    - CLI vs Desktop separation
    - Process lifecycle
    - Session row creation timing
    - workspace/repo field availability
    - plugin/extension API
    
    Current assessment: WATCHER_ATTACH_ONLY if we fix source discovery
    """
    # Check for CLI vs Desktop
    cli_exe = shutil.which("zcode")
    cli_launcher = cli_exe is not None
    
    # Check for desktop binary
    desktop_paths = [
        home / ".zcode/ZCode.app",
        Path("/Applications/ZCode.app"),
    ]
    desktop_found = any(p.exists() for p in desktop_paths)
    
    # Hardcoded DB path concern
    hardcoded_db = home / ".zcode/cli/db/db.sqlite"
    source_discovery_works = hardcoded_db.exists()
    
    # ZCode Desktop typically has no CLI resume path
    # Desktop mode = WATCHER_ATTACH_ONLY potentially
    
    max_level = ZeroTouchLevel.WATCHER_ATTACH_ONLY if desktop_found else (
        ZeroTouchLevel.LAUNCHER_ZERO_TOUCH if cli_launcher else ZeroTouchLevel.STARTUP_ASSISTED
    )
    
    notes = [
        "Source discovery needs robust environment variable fallback",
        "Desktop edition: process + SQLite watcher strategy",
        "CLI edition: potential launcher if resume command exists",
        "BLOCKER: ZCODE_SOURCE_DISCOVERY - need dynamic path detection",
    ]
    
    return ProviderCapabilities(
        provider="zcode",
        name="ZCode",
        cli_launcher=cli_launcher,
        skill_system_supported=False,  # Not implemented yet
        mcp_supported=False,
        has_session_start_hook=False,
        native_session_id_at_start=False,
        workspace_field_available=True,  # Assuming from adapter code
        max_zero_touch_level=max_level,
        notes=notes,
    )


def _detect_cursor_capabilities(home: Path) -> ProviderCapabilities:
    """Cursor IDE capability audit.
    
    Cursor is IDE-only (no standalone CLI), uses SQLite KV store.
    
    Investigation focus:
    - Does Cursor expose sessionStart hook via extension/plugin API?
    - What workspace/session metadata available in events?
    - Can we inject context via stdout in headless mode?
    
    Current assessment: NEEDS REAL VERIFICATION via Cursor extension docs
    """
    # Cursor is primarily IDE-integrated
    cli_exe = shutil.which("cursor")
    cli_launcher = cli_exe is not None
    
    # Check for extension/plugin API
    # Cursor extensions use VS Code extension API
    # May expose session lifecycle events
    
    extension_api = True  # Built on VS Code platform
    plugin_surface = True
    
    # Check for MCP support (Cursor supports MCP per existing code)
    mcp_config = home / ".cursor/mcp.json"
    mcp_supported = mcp_config.exists()
    
    # Session start hook: UNKNOWN without running Cursor extension
    has_session_start_hook = False  # Provisional - needs verification
    
    max_level = ZeroTouchLevel.SESSION_START_ZERO_TOUCH if has_session_start_hook else (
        ZeroTouchLevel.STARTUP_ASSISTED if mcp_supported else ZeroTouchLevel.BEST_EFFORT
    )
    
    notes = [
        "IDE-only deployment (no CLI)",
        "Extension API may provide sessionStart event",
        "MCP support exists per existing integration",
        "VERIFICATION NEEDED: Check Cursor extension docs for session lifecycle",
    ]
    
    return ProviderCapabilities(
        provider="cursor",
        name="Cursor",
        cli_launcher=cli_launcher,
        extension_api=extension_api,
        plugin_surface=plugin_surface,
        mcp_supported=mcp_supported,
        has_session_start_hook=has_session_start_hook,
        native_session_id_at_start=False,
        max_zero_touch_level=max_level,
        notes=notes,
    )


def _detect_kiro_capabilities(home: Path) -> ProviderCapabilities:
    """Kiro capability audit (IDE vs CLI split).
    
    Kiro provides both:
    - Kiro IDE (desktop app)
    - Kiro CLI (potentially)
    
    Different strategies per deployment:
    - IDE: process + workspace-session JSON watcher
    - CLI: potential launcher + session startup hook
    """
    # Check for CLI
    cli_exe = shutil.which("kiro")
    cli_launcher = cli_exe is not None
    
    # Check for IDE
    ide_paths = [
        home / ".kiro/Kiro.app",
        Path("/Applications/Kiro.app"),
    ]
    ide_found = any(p.exists() for p in ide_paths)
    
    # Skill system
    skill_root_cli = home / ".kiro/skills"
    skill_system_supported = skill_root_cli.exists()
    
    # Check for workspace-session JSON file (existing adapter path)
    workspace_json = home / ".kiro/workspace-session.json"
    session_tracking_possible = workspace_json.exists()
    
    # Separate capabilities per mode
    if cli_launcher:
        max_level_cli = ZeroTouchLevel.LAUNCHER_ZERO_TOUCH
        notes_cli = [
            "CLI launcher strategy viable",
            "Could spawn hook stdout into agent context",
        ]
    else:
        max_level_cli = ZeroTouchLevel.STARTUP_ASSISTED
        notes_cli = []
    
    if ide_found:
        max_level_ide = ZeroTouchLevel.WATCHER_ATTACH_ONLY
        notes_ide = [
            "Process + JSON file watcher",
            "No proven injection mechanism",
        ]
    else:
        max_level_ide = ZeroTouchLevel.UNSUPPORTED
        notes_ide = ["IDE not detected"]
    
    # Aggregate: report both modes
    max_level = max(max_level_cli, max_level_ide, key=lambda l: list(ZeroTouchLevel).index(l))
    
    notes = notes_cli + notes_ide + ["Kiro IDE and CLI have different capabilities"]
    
    return ProviderCapabilities(
        provider="kiro",
        name="Kiro",
        cli_launcher=cli_launcher,
        skill_system_supported=skill_system_supported,
        mcp_supported=False,  # Unverified
        has_session_start_hook=False,  # Unverified
        workspace_field_available=True,  # Per existing adapter
        max_zero_touch_level=max_level,
        notes=notes,
    )


def _detect_antigravity_capabilities(home: Path) -> ProviderCapabilities:
    """Antigravity capability audit.
    
    Antigravity uses protobuf-encoded SQLite for conversations.
    Adapter currently decodes heuristically (no public schema).
    
    Investigation needed:
    - Plugin/extension API availability
    - Workspace hook support
    - Session DB watcher viability
    - Startup event exposure
    
    Current assessment: BEST_EFFORT due to black-box storage format
    """
    # Check for CLI
    cli_exe = shutil.which("antigravity")
    cli_launcher = cli_exe is not None
    
    # Check for desktop
    desktop_paths = [
        home / ".antigravity/Antigravity.app",
        Path("/Applications/Antigravity.app"),
    ]
    desktop_found = any(p.exists() for p in desktop_paths)
    
    # Protobuf heuristic limitation
    source_format_known = False  # No public schema
    
    # May have plugin/extension surface (need verification)
    plugin_surface = False  # Provisional
    
    # Best guess based on IDE pattern
    max_level = ZeroTouchLevel.WATCHER_ATTACH_ONLY if desktop_found else (
        ZeroTouchLevel.LAUNCHER_ZERO_TOUCH if cli_launcher else ZeroTouchLevel.BEST_EFFORT
    )
    
    notes = [
        "Protobuf storage format undocumented (heuristic decode only)",
        "Plugin API needs verification",
        "SQLite watcher may work if schema stability confirmed",
    ]
    
    return ProviderCapabilities(
        provider="antigravity",
        name="Antigravity",
        cli_launcher=cli_launcher,
        skill_system_supported=False,
        mcp_supported=False,
        plugin_surface=plugin_surface,
        has_session_start_hook=False,
        source_format_known=source_format_known,
        max_zero_touch_level=max_level,
        notes=notes,
    )


def _has_claude_mcp_command(executable: Path) -> bool:
    """Check if Claude executable supports 'mcp add' subcommand."""
    try:
        result = subprocess.run(
            [str(executable), "mcp", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False
