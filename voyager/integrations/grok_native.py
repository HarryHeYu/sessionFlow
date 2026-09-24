"""Grok CLI native startup-continuity surfaces.

Grok's CLI exposes two *native* mechanisms that make zero-touch continuity
possible without asking the user to run a voyager command or paste context.
Both were verified against a real Grok CLI (v1.0.41) rather than inferred from
documentation:

* **``SessionStart`` hooks** — ``~/.grok/hooks/*.json`` fires on every session
  start with ``GROK_SESSION_ID`` and ``GROK_WORKSPACE_ROOT`` in the process
  environment. ``SessionStart`` is a *passive* event (its stdout is ignored, and
  ``UserPromptSubmit`` discards ``additionalContext`` too), so a hook cannot
  inject text — but it can have side effects. That is exactly what the pending
  attach needs: record the intent with the native id, let the next scan bind the
  session to its WorkThread.

* **rules files** — every ``*.md`` directly inside ``$GROK_HOME/rules/`` (and
  any ``[paths] extra_rule_dirs`` entry) is loaded into the system prompt at
  session start, before the first turn, regardless of folder trust or
  ``.gitignore``. This is the only verified way to get continuation context into
  an *interactive* Grok session, because ``--rules`` needs a command line and
  ``--prompt-file`` is single-turn only.

The launcher writes the rules file before ``exec``-ing the real binary; the
SessionStart hook records the pending. Both go through
:func:`voyager.startup.startup_continuity`, so Claude and Grok share one state
machine instead of growing two similar-but-different startup semantics.

The rules file lives **outside the repository** on purpose: the compiled bundle
carries transcript text, and writing that into the repo would put conversation
content into a tracked-or-untracked project file.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ..store import Store
from ..startup import startup_continuity

#: Name of the generated rule file inside the Grok rules directory.
RULES_FILENAME = "voyager-continuation.md"

#: Header marker so a stale file is recognisable and never mistaken for a
#: hand-written rule.
RULES_MARKER = "<!-- voyager:continuation-context -->"


def grok_home(home: Optional[Path] = None) -> Path:
    """Resolve the Grok config directory.

    ``home`` is the *user* home, matching the rest of the integration layer
    (``GrokIntegration(home=...)``); the ``.grok`` segment is appended here.
    An explicit ``home`` wins over ``$GROK_HOME`` so tests stay isolated.
    """
    if home is not None:
        return Path(home) / ".grok"
    override = os.environ.get("GROK_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grok"


def rules_dir(home: Optional[Path] = None) -> Path:
    """The Grok rules directory Voyager writes into."""
    return grok_home(home) / "rules"


def context_rules_path(home: Optional[Path] = None) -> Path:
    """Absolute path of the generated continuation rule file."""
    return rules_dir(home) / RULES_FILENAME


def _header(repo_root: str, thread_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{RULES_MARKER}\n"
        "# Voyager continuation context (auto-generated)\n"
        "\n"
        f"- Repository: `{repo_root}`\n"
        f"- WorkThread: `{thread_id}`\n"
        f"- Generated: {stamp}\n"
        "\n"
        "This file is written by the Voyager launcher and loaded by Grok as a\n"
        "rule. It carries the continuation context for the WorkThread above.\n"
        "**If your working directory is not that repository, ignore this file\n"
        "entirely.**\n"
        "\n"
        "---\n"
        "\n"
    )


def clear_context_rules(home: Optional[Path] = None) -> bool:
    """Remove the generated rule file. Returns True when a file was removed."""
    path = context_rules_path(home)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def write_context_rules(
    cwd: str,
    home: Optional[Path] = None,
    store: Optional[Store] = None,
    clear: bool = True,
) -> Dict[str, Any]:
    """Compile the continuation context for ``cwd`` into Grok's rules dir.

    Called by the launcher *before* the real binary starts, so the rule is on
    disk by the time Grok reads its rules. When there is no active WorkThread
    the file is removed instead of written, so a launch in an unrelated repo
    cannot inherit the previous repo's context.

    ``clear`` separates the two callers by what they actually know:

    * ``clear=True`` (default) is the *launch* semantics. The caller is about to
      start a Grok session in ``cwd``, so "no thread here" means "this session
      must not inherit any context" and the stale file has to go.
    * ``clear=False`` is the *sync* semantics. A background sync has no opinion
      about where the next session will start -- ``voyager watch`` runs from the
      Startup folder, so its cwd is not the repo the user is in -- and deleting
      the rule there would wipe the file a handoff depends on, once per
      interval. A sync may refresh the file, never remove it.

    Returns a small status dict; never raises for the normal "nothing to do"
    paths, because a launcher must not break the agent it is wrapping.
    """
    path = context_rules_path(home)
    try:
        result = startup_continuity(provider="grok", cwd=cwd, store=store)
    except Exception as exc:  # launcher must never block the agent
        return {"status": "error", "message": str(exc), "path": str(path)}

    thread_id = getattr(result, "thread_id", None)
    context = getattr(result, "context", None)

    if not getattr(result, "continuity_available", False) or not context:
        # A sync (`clear=False`) reaches this branch for every repo that has no
        # WorkThread -- including the cwd of a background watcher, which is not
        # the repo the user is in. It must not delete a rule another surface
        # wrote for the repo the next session will actually start in.
        removed = clear_context_rules(home) if clear else False
        return {
            "status": "no_thread",
            "thread": None,
            "path": str(path),
            "cleared": removed,
        }

    repo_root = getattr(result, "repo_root", None) or cwd
    body = _header(str(repo_root), str(thread_id)) + context
    if not body.endswith("\n"):
        body += "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "message": str(exc), "path": str(path)}

    return {
        "status": "written",
        "thread": thread_id,
        "path": str(path),
        "chars": len(body),
        "context_source": getattr(result, "context_source", None),
    }


def session_start(
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    home: Optional[Path] = None,
    store: Optional[Store] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Grok ``SessionStart`` hook body: bind this native session to its thread.

    Grok hands the hook ``GROK_SESSION_ID`` and ``GROK_WORKSPACE_ROOT``, so the
    recorder already knows which session it is waiting for — the same position
    Claude's SessionStart handler is in. That is what makes the later resolution
    an identity match rather than a uniqueness guess.

    ``home`` is accepted for symmetry with the other entry points and to keep
    the signature testable; this path writes no files.
    """
    env = os.environ if env is None else env
    session_id = session_id or env.get("GROK_SESSION_ID")
    cwd = (
        cwd
        or env.get("GROK_WORKSPACE_ROOT")
        or env.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )

    try:
        result = startup_continuity(
            provider="grok", cwd=cwd, native_session_id=session_id,
            store=store,
            # SessionStart is passive: its stdout is ignored, so a compiled
            # bundle could never reach the model. Compiling it here would add
            # git calls and transcript-derived text to every single launch.
            compile_context=False,
        )
    except Exception as exc:  # a hook must fail open, never block the session
        return {
            "status": "error",
            "message": str(exc),
            "session_id": session_id,
            "cwd": cwd,
            "pending_recorded": False,
        }

    attach_status = getattr(result, "attach_status", None)
    return {
        "status": "ok",
        "session_id": session_id,
        "cwd": cwd,
        "thread": getattr(result, "thread_id", None),
        "attach_status": attach_status,
        # "pending_resolve" is the branch that leaves the open row behind.
        "pending_recorded": attach_status == "pending_resolve",
        # Always "none" here: a passive event has no way to deliver a bundle, so
        # this path must never pay to compile one.
        "context_source": getattr(result, "context_source", None),
    }
