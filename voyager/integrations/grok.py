"""Grok CLI integration implementation.

Two layers, in the order Grok's own surfaces allow:

* **Native** — a ``SessionStart`` hook records the pending attach with the
  session id Grok hands the hook, and the launcher writes the compiled
  continuation bundle into Grok's rules directory so it lands in the system
  prompt before the first turn. Verified against Grok CLI 1.0.41.
* **Launcher shim** — still installed for the pre-launch step, because nothing
  Grok exposes can inject context *at* session start: ``SessionStart`` is a
  passive event, and ``UserPromptSubmit`` discards ``additionalContext``.

The shim only helps when ``~/.voyager/bin`` precedes the real Grok binary on
``PATH``; ``verify()`` reports that, and the native hook works regardless.
"""
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional
import shutil
from .capabilities import ProviderCapabilities, ZeroTouchLevel
from . import grok_native


class GrokIntegration:
    """Grok CLI launcher integration.

    Strategy: NATIVE_HOOK_ZERO_TOUCH (SessionStart hook + rules file), with an
    opt-in launcher wrapper for the pre-launch context write.
    """

    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self._capabilities: Optional[ProviderCapabilities] = None
        self.voyager_bin = self.home / ".voyager/bin"
        self.grok_home = self.home / ".grok"
        self.hooks_dir = self.grok_home / "hooks"
        self.hook_json = self.hooks_dir / "voyager.json"
        self.hook_cmd = self.hooks_dir / "voyager-session-start.cmd"
        self.windows_shim = self.voyager_bin / "grok.cmd"

    # -- generated file bodies ---------------------------------------------

    def _hook_command(self) -> str:
        """The python invocation the SessionStart hook shim runs."""
        script = Path(__file__).with_name("grok_session_start.py").resolve()
        return '"{0}" "{1}"'.format(sys.executable, script)

    def _hook_json_body(self) -> str:
        """Grok hook definition.

        ``command`` must be a *path to an executable* relative to the JSON file.
        An inline shell command does not survive the runner: Grok executes hook
        commands through PowerShell on Windows, and a quoted inline command is
        rejected at parse time (verified: ``exit code 1`` at the first quote).
        """
        return (
            "{\n"
            '  "hooks": {\n'
            '    "SessionStart": [\n'
            "      {\n"
            '        "hooks": [\n'
            "          {\n"
            '            "type": "command",\n'
            '            "command": "voyager-session-start.cmd",\n'
            '            "timeout": 30\n'
            "          }\n"
            "        ]\n"
            "      }\n"
            "    ]\n"
            "  }\n"
            "}\n"
        )

    def _hook_cmd_body(self) -> str:
        return (
            "@echo off\r\n"
            "REM Voyager Grok SessionStart hook.\r\n"
            "REM Grok runs hook commands through PowerShell, which rejects a\r\n"
            "REM quoted inline command; a file the runner can execute directly\r\n"
            "REM avoids the quoting problem entirely.\r\n"
            f"{self._hook_command()} >nul 2>&1\r\n"
            "exit /b 0\r\n"
        )

    def _windows_shim_body(self, real_grok: str) -> str:
        return (
            "@echo off\r\n"
            "REM Voyager launcher for Grok CLI on Windows.\r\n"
            "REM Writes the continuation rule file, then starts the real CLI.\r\n"
            "setlocal\r\n"
            "if not \"%VOYAGER_LAUNCHER_RUNNING%\"==\"\" goto EXECUTE_GROK\r\n"
            "set VOYAGER_LAUNCHER_RUNNING=1\r\n"
            "voyager hook grok-context --cwd \"%CD%\" --quiet\r\n"
            "voyager launcher prelaunch --provider grok --cwd \"%CD%\" >nul 2>&1\r\n"
            ":EXECUTE_GROK\r\n"
            f"start \"\" \"{real_grok}\" %*\r\n"
            "endlocal\r\n"
        )

    # -- lifecycle ---------------------------------------------------------

    def install(self) -> Dict[str, Any]:
        """Install the Grok native hook, the rules wiring and the launcher.

        Creates:
        - ~/.grok/hooks/voyager.json          (SessionStart hook)
        - ~/.grok/hooks/voyager-session-start.cmd
        - ~/.voyager/bin/grok                 (POSIX launcher shim)
        - ~/.voyager/bin/grok.cmd             (Windows launcher shim)
        """
        real_grok = shutil.which("grok")
        if not real_grok:
            return {
                "provider": "grok",
                "status": "error",
                "message": "Grok executable not found in PATH",
            }

        # Create launcher directory
        self.voyager_bin.mkdir(parents=True, exist_ok=True)

        # Generate wrapper script
        wrapper_script = self.voyager_bin / "grok"
        script_content = f"""#!/bin/sh
# Voyager launcher for Grok CLI
# Opt-in wrapper that provides continuity on launch

real_executable="{real_grok}"

# Prevent recursion
if [ -n "$VOYAGER_LAUNCHER_RUNNING" ]; then
    exec "$real_executable" "$@"
fi

export VOYAGER_LAUNCHER_RUNNING=1

# Compile the continuation context into Grok's rules dir before launch, so it
# is in the system prompt before the first turn.
voyager hook grok-context --cwd "$PWD" --quiet || true

# Run prelaunch hook
voyager launcher prelaunch --provider grok --cwd "$PWD" || true

# Launch real grok with original arguments
exec "$real_executable" "$@"
"""
        wrapper_script.write_text(script_content, encoding="utf-8")
        wrapper_script.chmod(0o755)

        self.windows_shim.write_text(
            self._windows_shim_body(real_grok), encoding="utf-8")

        # Native SessionStart hook.
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        self.hook_json.write_text(self._hook_json_body(), encoding="utf-8")
        self.hook_cmd.write_text(self._hook_cmd_body(), encoding="utf-8")

        return {
            "provider": "grok",
            "status": "installed",
            "launcher": str(wrapper_script),
            "windows_launcher": str(self.windows_shim),
            "hook": str(self.hook_json),
            "real_executable": real_grok,
            "rules_dir": str(grok_native.rules_dir(self.home)),
            "strategy": "NATIVE_HOOK_ZERO_TOUCH",
            "notes": [
                "SessionStart hook installed; it records the pending attach "
                "using the native session id Grok provides",
                "Launcher shim writes the continuation rules file before launch",
                "Add ~/.voyager/bin to PATH prefix for the launcher to run; the "
                "native hook works without it",
            ],
        }

    def remove(self) -> Dict[str, Any]:
        """Remove Grok integration artifacts."""
        removed = []
        for path in (self.voyager_bin / "grok", self.windows_shim,
                     self.hook_json, self.hook_cmd):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(str(path))
            except OSError:
                return {"provider": "grok", "status": "error",
                        "message": "could not remove {0}".format(path)}
        # The generated rule would otherwise keep injecting stale context into
        # every future Grok session.
        grok_native.clear_context_rules(self.home)
        return {"provider": "grok", "status": "removed", "removed": removed}

    def capabilities(self) -> ProviderCapabilities:
        """Return capability profile."""
        from .capabilities import detect_capabilities
        if self._capabilities is None:
            self._capabilities = detect_capabilities("grok", self.home)
        return self._capabilities

    def launcher_on_path(self) -> bool:
        """Does ``grok`` resolve to our shim before the real binary?

        The shim only runs when ``~/.voyager/bin`` precedes the real CLI on
        ``PATH``, and ``install()`` cannot reorder the user's ``PATH`` for them.
        Reported rather than enforced: the native SessionStart hook works either
        way, and only the pre-launch context write depends on this.
        """
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            for name in ("grok.exe", "grok.cmd", "grok.bat", "grok"):
                if (Path(entry) / name).is_file():
                    return Path(entry) == self.voyager_bin
        return False

    def verify(self) -> Dict[str, Any]:
        """Verify the launcher and the native hook are correctly installed.

        Two traps this avoids, both of which made the method unusable:

        * ``stat()`` must not run unconditionally.  It raises
          ``FileNotFoundError`` when the launcher is missing — which is
          precisely the situation ``verify()`` exists to report, so the method
          crashed exactly when it was needed.
        * The execute bit is a POSIX notion.  On Windows ``chmod(0o755)``
          leaves ``st_mode`` at ``0o100666``, so testing ``st_mode & 0o111``
          reported a healthy install as broken and ``verified`` could never be
          true there.  The bit is only *required* where it means something.
        """
        launcher = self.voyager_bin / "grok"
        exists = launcher.exists()
        executable = bool(launcher.stat().st_mode & 0o111) if exists else False

        checks = {
            "launcher_exists": exists,
            "executable": executable,
            "hook_installed": self.hook_json.is_file(),
            "hook_script": self.hook_cmd.is_file(),
            "launcher_on_path": self.launcher_on_path(),
        }
        required = ["launcher_exists"] + (["executable"] if os.name == "posix" else [])

        all_ok = all(checks[k] for k in required)
        return {
            "verified": all_ok,
            "checks": checks,
            "strategy": "NATIVE_HOOK_ZERO_TOUCH" if all_ok else "ERROR",
        }
