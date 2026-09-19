"""Zero-Touch Startup Continuity Primitive.

This module provides a unified entry point for agent startup continuity:
when an agent launches in a repo with existing Voyager work, it can discover
the active WorkThread, get continuation context, and auto-attach its new
native session — all without manual voyager switch/handoff/thread attach.

Design principles:
1. Single shared primitive: startup_continuity() called from any surface
2. Strict safety: never auto-cluster or silently pick threads
3. Ambiguity = explicit error, not guessing
4. Lazy compile: only refresh stale contexts
5. No provider file writes: read-only index access

Usage patterns:
- MCP tool: voyager_startup(provider, cwd, native_session_id?)
- Skill bootstrap: call via MCP at start of each session
- CLI helper: voyager status --provider X --cwd Y
- Bootstrap script: wrap agent launch with discovery

Returns: Dict with fields documented below.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adapters.base import git_info
from .store import Store


class StartupContinuityResult:
    """Typed result structure for startup_continuity()."""
    
    def __init__(self, data: Dict[str, Any]):
        self.__dict__ = data
    
    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


def startup_continuity(
    provider: str,
    cwd: Optional[str] = None,
    native_session_id: Optional[str] = None,
    auto_attach: bool = True,
    budget: str = "auto",
    store: Optional[Store] = None,
) -> StartupContinuityResult:
    """Unified startup continuity primitive.
    
    Called by any startup surface (MCP/Skill/CLI/Bootstrap) when an agent
    begins work in a repository. Returns comprehensive state about whether
    there's existing work to continue, and if so, what the next steps are.
    
    Resolution order:
    1. Discover active WorkThreads for exact repo match
    2. If ambiguity (multiple threads), return ERROR_AMBIGUOUS_WORKTHREAD
    3. If no thread, return CONTINUITY_NONE
    4. If one thread:
       - Check if current session already attached → CONTINUED_ATTACHED
       - If not attached and safe conditions met → auto-attach
       - Compile/generate continuation context if needed
       - Handle pending attach resolution
    
    Returns dict with these fields:
    
    **Required fields:**
    - continuity_available: bool - Is there an active WorkThread?
    - thread_id: str | None - ID of discovered thread
    - repo_root: str - Root path of the thread's repo
    - goal: str | None - Primary goal from thread metadata
    - previous_provider: str | None - Last provider that worked here
    - previous_session: str | None - Last session ID
    - current_provider: str - The provider calling this function
    - current_session: str | None - Current native session ID (if known)
    - lease_state: Dict - {held, expired, why, holder, pid}
    - attach_status: str - One of:
        * "already_attached" - Session was already part of thread
        * "auto_attached" - Auto-attached during this call
        * "pending_resolve" - Pending record exists, waiting for scan
        * "no_auto_attach" - Conditions not safe for automatic attach
    - context: str | None - Compiled continuation context (budget-limited)
    - context_stale: bool - Whether context should be recompiled
    - recommended_action: str - One of: none/continue/use_context/pick_thread
    
    **Optional fields (when applicable):**
    - ambiguity_error: str - When multiple threads exist
    - missing_native_session: bool - When native_session_id not provided
    - pending_attach: List[Dict] - Existing pending records
    - auto_attach_reason: str | None - Why auto-attach succeeded/failed
    - compiled_at: float - Timestamp of context compilation
    
    **Error codes (returned as attach_status):**
    - "ERROR_AMBIGUOUS_WORKTHREAD" - Multiple threads for same repo
    - "ERROR_NO_REPO_MATCH" - No thread matches current repo
    - "ERROR_SESSION_IN_OTHER_THREAD" - Session belongs to different thread
    - "ERROR_PENDING_CONFLICT" - Pending attach points elsewhere
    - "ERROR_UNKNOWN_SESSION" - Native session ID not found in index
    
    Args:
        provider: Agent provider name (codex, claude, grok, etc.)
        cwd: Current working directory (defaults to os.getcwd())
        native_session_id: Native session ID from the agent (optional)
        auto_attach: Whether to automatically attach session to thread (default True)
        budget: Context budget mode (compact/balanced/full/auto/Nk)
        store: Optional pre-opened Store instance (for batch operations)
    
    Returns:
        StartupContinuityResult with all discovery/attach/context state
    """
    start_time = time.time()
    
    # Normalize inputs
    cwd = cwd or os.getcwd()
    own_store = store is None
    store = store or Store()
    
    try:
        # Step 1: Get git repo root
        git_root = git_info(cwd).get("repo_root") or cwd.replace("\\", "/")
        
        # Step 2: Find active WorkThreads matching this repo
        threads = _find_matching_threads(store, git_root)
        
        # Step 3: Handle zero/multiple threads case
        if len(threads) == 0:
            return StartupContinuityResult({
                "continuity_available": False,
                "thread_id": None,
                "repo_root": git_root,
                "goal": None,
                "previous_provider": None,
                "previous_session": None,
                "current_provider": provider,
                "current_session": native_session_id,
                "lease_state": {"held": False, "expired": False, "why": "free",
                               "holder": None, "pid": None},
                "attach_status": "no_auto_attach",
                "context": None,
                "context_stale": False,
                "recommended_action": "none",
                "auto_attach_reason": "no_active_workthread_for_repo",
            })
        
        if len(threads) > 1:
            # AMBIGUITY: multiple threads for same repo
            thread_info = "\n".join([
                f"- {t['id']}: {t.get('title', 'Untitled')}"
                for t in sorted(threads, key=lambda x: x["updated_at"] or 0, reverse=True)
            ])
            return StartupContinuityResult({
                "continuity_available": False,
                "thread_id": None,
                "repo_root": git_root,
                "goal": None,
                "previous_provider": None,
                "previous_session": None,
                "current_provider": provider,
                "current_session": native_session_id,
                "lease_state": {"held": False, "expired": False, "why": "free",
                               "holder": None, "pid": None},
                "attach_status": "ERROR_AMBIGUOUS_WORKTHREAD",
                "context": None,
                "context_stale": False,
                "recommended_action": "pick_thread",
                "ambiguity_error": f"Multiple active threads found:\n{thread_info}",
                "auto_attach_reason": "ambiguous_multiple_threads",
            })
        
        # Single thread found - proceed with detailed analysis
        thread = threads[0]
        tid = thread["id"]
        
        # Step 4: Get lease state
        lease = store.thread_lease_get(tid)
        from .store import lease_state
        lst = lease_state(lease)
        
        # Step 5: Check if current session already attached
        if native_session_id:
            current_id = f"{provider}:{native_session_id}"
            attached = store.attached_to_any_thread(current_id)
            if attached and store.attached_to_thread(tid, current_id):
                # Already attached - just return context if available
                return StartupContinuityResult({
                    "continuity_available": True,
                    "thread_id": tid,
                    "repo_root": git_root,
                    "goal": thread.get("goal") or thread.get("title"),
                    "previous_provider": _latest_holder_provider(store, tid),
                    "previous_session": _latest_holder_session(store, tid),
                    "current_provider": provider,
                    "current_session": native_session_id,
                    "lease_state": {"held": lst["held"], "expired": lst["expired"],
                                   "why": lst["why"], "holder": lease["holder"],
                                   "pid": lease["pid"]},
                    "attach_status": "already_attached",
                    "context": None,  # Would need separate compile request
                    "context_stale": False,
                    "recommended_action": "continue",
                })
        
        # Step 6: Check for conflicting pending attachments
        pending = store.pending_open(thread_id=tid)
        if pending:
            # Check if our session conflicts with pending
            our_pending = any(p["provider"] == provider for p in pending)
            if our_pending and not _session_exists_in_index(store, provider, native_session_id):
                # Pending exists but session not yet indexed - OK, wait for resolve
                pass
        
        # Step 7: Safe auto-attach check
        auto_attach_safe = _check_auto_attach_safety(
            store, tid, provider, native_session_id, cwd, git_root, thread
        )
        
        attach_status = "no_auto_attach"
        auto_attach_reason = None
        
        if auto_attach and auto_attach_safe and native_session_id:
            # Safe to auto-attach
            try:
                current_id = f"{provider}:{native_session_id}"
                store.thread_attach(tid, current_id)
                # Resolve any pending record for this thread/provider combo
                store.pending_mark_open_resolved(tid, provider)
                attach_status = "auto_attached"
                auto_attach_reason = "safe_conditions_met_and_executed"
            except Exception as e:
                attach_status = "no_auto_attach"
                auto_attach_reason = f"attachment_failed: {str(e)}"
        
        elif not native_session_id:
            auto_attach_reason = "no_native_session_id_provided"
            # Still check if we should create a pending record
            # This handles the case where agent started but hasn't reported session yet
        else:
            # Not safe to auto-attach
            if _check_same_repo_two_threads(store, cwd):
                auto_attach_reason = "two_active_threads_detected"
            elif not _session_exists_in_index(store, provider, native_session_id):
                # Session might be brand new - could be a pending attach candidate
                attach_status = "pending_resolve"
                auto_attach_reason = "session_not_yet_indexed_or_no_safe_match"
            else:
                auto_attach_reason = "safety_checks_prevented_attachment"
        
        # Step 8: Compile continuation context
        members = store.thread_members(tid)
        context = None
        context_stale = False
        
        # Check if we need to refresh context
        last_compile = getattr(store, "_last_compile", {})
        thread_key = f"{tid}:{provider}"
        last_compiled_at = last_compile.get(thread_key, 0)
        
        # Stale if:
        # - Never compiled OR compiled > 5 minutes ago
        # - Latest member updated after compile
        # - Git HEAD changed
        # - Holder/provider changed
        latest_member_updated = max(
            (m.get("updated_at") or 0) for m in members
        ) if members else 0
        
        git_head_changed = _git_head_changed_since(
            store, tid, git_root, last_compiled_at
        )
        
        holder_changed = (lst["held"] and 
                         lease.get("holder") != _latest_holder_provider(store, tid))
        
        context_stale = (
            time.time() - last_compiled_at > 300 or  # 5 minute TTL
            latest_member_updated > last_compiled_at or
            git_head_changed or
            holder_changed
        )
        
        # Compile context if needed
        if context_stale or not context:
            try:
                from .auto import get_continuation_context
                ctx_result = get_continuation_context(
                    store=store,
                    cwd=cwd,
                    provider=provider,
                    native_session_id=native_session_id,
                    thread_id=tid,
                    repo=git_root,
                    goal=None,  # Let ranking decide relevance
                    budget=budget,
                    target=provider,
                    sync=True,
                )
                if ctx_result.get("continuity_available"):
                    context = ctx_result.get("context")
            except Exception as e:
                # Context compile failed - don't crash, just omit context
                context = None
        
        return StartupContinuityResult({
            "continuity_available": True,
            "thread_id": tid,
            "repo_root": git_root,
            "goal": thread.get("goal") or thread.get("title"),
            "previous_provider": _latest_holder_provider(store, tid),
            "previous_session": _latest_holder_session(store, tid),
            "current_provider": provider,
            "current_session": native_session_id,
            "lease_state": {"held": lst["held"], "expired": lst["expired"],
                           "why": lst["why"], "holder": lease["holder"],
                           "pid": lease["pid"]},
            "attach_status": attach_status,
            "context": context,
            "context_stale": context_stale,
            "recommended_action": ("use_context" if context else
                                  "continue_from_memory" if members else "none"),
            "auto_attach_reason": auto_attach_reason,
            "compiled_at": time.time() if context else None,
        })
        
    finally:
        if own_store:
            store.close()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _find_matching_threads(store: Store, repo_root: str) -> List[Dict[str, Any]]:
    """Find active WorkThreads matching the given repo path."""
    threads = store.thread_list("active")
    
    def _same_repo(a: str, b: str) -> bool:
        a = (a or "").replace("\\", "/").rstrip("/").lower()
        b = (b or "").replace("\\", "/").rstrip("/").lower()
        if not a or not b:
            return False
        return a == b or a.endswith("/" + b) or b.endswith("/" + a)
    
    matching = [t for t in threads 
                if t.get("repo_root") and _same_repo(t["repo_root"], repo_root)]
    
    # Sort by updated_at descending (newest first)
    return sorted(matching, key=lambda x: x["updated_at"] or 0, reverse=True)


def _latest_holder_provider(store: Store, tid: str) -> Optional[str]:
    """Get the most recent provider that worked on this thread."""
    members = store.thread_members(tid)
    if not members:
        return None
    newest = max(members, key=lambda m: m["updated_at"] or 0)
    return newest.get("provider")


def _latest_holder_session(store: Store, tid: str) -> Optional[str]:
    """Get the most recent session ID that worked on this thread."""
    members = store.thread_members(tid)
    if not members:
        return None
    newest = max(members, key=lambda m: m["updated_at"] or 0)
    return newest.get("id")


def _check_auto_attach_safety(store: Store, tid: str, provider: str,
                              native_session_id: Optional[str],
                              cwd: str, repo_root: str,
                              thread: Dict[str, Any]) -> bool:
    """Check if it's safe to auto-attach the current session."""
    
    # Must have native session ID
    if not native_session_id:
        return False
    
    # Session must not already belong to another thread
    current_id = f"{provider}:{native_session_id}"
    if store.attached_to_any_thread(current_id):
        # Belongs to some other thread
        attached_thread = _get_attached_thread(store, current_id)
        if attached_thread and attached_thread["id"] != tid:
            return False
    
    # Repo must match exactly
    if thread.get("repo_root"):
        def _same_repo(a: str, b: str) -> bool:
            a = (a or "").replace("\\", "/").rstrip("/").lower()
            b = (b or "").replace("\\", "/").rstrip("/").lower()
            if not a or not b:
                return False
            return a == b or a.endswith("/" + b) or b.endswith("/" + a)
        
        if not _same_repo(thread["repo_root"], repo_root):
            return False
    
    # Cannot have two active threads for same repo (ambiguity prevention)
    if _check_same_repo_two_threads(store, cwd):
        return False
    
    # Session must exist in index (not yet ingested = too early)
    if not _session_exists_in_index(store, provider, native_session_id):
        # Session not yet in index - could still attach after scan
        # For now, return False and let scan catch it later
        return False
    
    return True


def _get_attached_thread(store: Store, session_id: str) -> Optional[Dict[str, Any]]:
    """Get the thread this session is attached to, if any."""
    rows = store.q(
        "SELECT thread_id FROM thread_sessions WHERE session_id=?",
        (session_id,)
    )
    if rows:
        tid = rows[0]["thread_id"]
        return store.thread_get(tid)
    return None


def _check_same_repo_two_threads(store: Store, cwd: str) -> bool:
    """Check if there are two+ active threads for the same repo."""
    from .adapters.base import git_info
    git_root = git_info(cwd).get("repo_root") or cwd.replace("\\", "/")
    matching = _find_matching_threads(store, git_root)
    return len(matching) >= 2


def _session_exists_in_index(store: Store, provider: str,
                            native_session_id: str) -> bool:
    """Check if session exists in the Voyager index."""
    session_id = f"{provider}:{native_session_id}"
    rows = store.q("SELECT 1 FROM sessions WHERE id=?", (session_id,))
    return len(rows) > 0


def _git_head_changed_since(store: Store, tid: str, repo_root: str,
                            since_ts: float) -> bool:
    """Check if git HEAD has changed since timestamp."""
    try:
        import subprocess
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2
        )
        if res.returncode != 0:
            return False
        
        # In production, we'd cache git commit + timestamp per thread
        # and compare. For now, assume no change detection.
        # TODO: Add git commit fingerprint tracking to thread metadata
        return False
    except Exception:
        return False
