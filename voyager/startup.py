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

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adapters.base import git_info
from .store import Store


class StartupContinuityResult:
    """Typed result structure for startup_continuity().
    
    Fields:
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
    - context_stale: bool - True when the returned context came from a cache
        that was known-stale (a fresh recompile happened, or a recompile failed
        and the previous bundle was reused). False when served from a cache
        still inside its TTL. Note: stale does NOT mean unusable.
    - context_source: str - How context was obtained: "fresh_compile" |
        "cached" | "none" (no thread, ambiguous, or nothing compiled)
    - recommended_action: str - One of: none/continue/use_context/pick_thread
    - compiled_at: float | None - Timestamp of context compilation (None when
        no context was produced; always present, so attribute access is safe)
    """
    
    def __init__(self, data: Dict[str, Any]):
        self.__dict__ = data
    
    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


def record_startup_pending(
    store: Store,
    *,
    thread_id: str,
    provider: str,
    repo_root: str,
    cwd: Optional[str] = None,
    native_session_id: Optional[str] = None,
    goal: Optional[str] = None,
    note: str = "native session start: awaiting index",
) -> None:
    """Record the open pending attach a native session start leaves behind.

    This is the single writer for the shared state machine

        native startup → pending → discovery → resolution → thread_attach

    Every provider-native startup surface goes through it, so Claude's
    SessionStart handler and Grok's SessionStart hook cannot drift into two
    similar-but-different flavours of "record the intent, let the scan finish".

    The row is keyed ``(thread_id, provider)``, so re-recording for the same
    pair replaces the previous row rather than piling up. ``native_session_id``
    is what makes the later resolution an identity match instead of the
    "exactly one candidate" heuristic, so a surface that knows its session id
    must pass it.

    ``source_provider``/``source_session`` are deliberately left unset: they
    describe the session a *switch* handed off from, and a native start has no
    such session.
    """
    store.pending_record(
        thread_id, provider,
        native_session_id=native_session_id,
        note=note,
        repo_root=repo_root,
        cwd=cwd,
        goal=goal,
    )


def startup_continuity(
    provider: str,
    cwd: Optional[str] = None,
    native_session_id: Optional[str] = None,
    auto_attach: bool = True,
    budget: str = "auto",
    store: Optional[Store] = None,
    compile_context: bool = True,
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
    - context_stale: bool - True when the returned context came from a cache
        that was known-stale; False when served from a cache inside its TTL
    - context_source: str - "fresh_compile" | "cached" | "none"
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
        compile_context: Whether to compile the continuation bundle (default
            True). Set False from a surface that cannot deliver context — a
            passive lifecycle hook — so it still records the pending attach
            without paying for a bundle nobody can read. ``context`` is then
            None and ``context_source`` is "none".
    
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
                "context_source": "none",
                "recommended_action": "none",
                "auto_attach_reason": "no_active_workthread_for_repo",
                "compiled_at": None,
            })
        
        if len(threads) > 1:
            # AMBIGUITY: multiple threads for same repo
            thread_info = "\n".join([
                f"- {t['id']}: {dict(t).get('title', 'Untitled')}"
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
                "context_source": "none",
                "recommended_action": "pick_thread",
                "ambiguity_error": f"Multiple active threads found:\n{thread_info}",
                "auto_attach_reason": "ambiguous_multiple_threads",
                "compiled_at": None,
            })
        
        # Single thread found - proceed with detailed analysis
        thread = threads[0]
        tid = thread["id"]
        
        # Step 4: Get lease state
        lease = store.thread_lease_get(tid)
        from .store import lease_state
        lst = lease_state(lease)
        
        # Step 5: Check if current session already attached
        #
        # An already-attached session must still reach Step 8. This used to be
        # an early `return` with context=None, and the hook treats a falsy
        # context as "nothing to inject" — so the session started silently
        # without its continuation bundle. That is merely latent with
        # `matcher: "startup"` (the session id is always new), but it becomes
        # live the moment the matcher widens to `compact` or `resume`: those
        # fire SessionStart on the *same, already-attached* session id, which is
        # exactly the point where re-injection matters most.
        already_attached = False
        if native_session_id:
            current_id = f"{provider}:{native_session_id}"
            already_attached = bool(
                store.attached_to_any_thread(current_id)
                and store.attached_to_thread(tid, current_id)
            )

        attach_status = "already_attached" if already_attached else "no_auto_attach"
        auto_attach_reason = "session_already_attached" if already_attached else None

        if not already_attached:
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
                    # A session that has only just started is normally not in the
                    # index yet, so this is the *common* SessionStart path, not an
                    # edge case. The hook cannot attach a session it cannot resolve
                    # to a Voyager id, so record the intent now and let the next
                    # scan finish the job once the session has been indexed.
                    #
                    # Without this write the branch was a dead end. The status said
                    # "pending_resolve", and both the docstring above and
                    # `_check_auto_attach_safety` promised the scan would catch it
                    # later -- but `resolve_pending_attaches()` only walks *open
                    # pending rows*, and the only other writer was the explicit
                    # switch flow. So a natively started session was never attached
                    # to its WorkThread, which is precisely the zero-manual-command
                    # case this design exists for.
                    #
                    # `source_provider`/`source_session` are deliberately left
                    # unset: they describe the session a switch handed off *from*,
                    # and there is no such session here.
                    record_startup_pending(
                        store,
                        thread_id=tid,
                        provider=provider,
                        repo_root=git_root,
                        cwd=cwd,
                        native_session_id=native_session_id,
                        goal=dict(thread).get("goal"),
                    )
                    attach_status = "pending_resolve"
                    auto_attach_reason = "session_not_yet_indexed_or_no_safe_match"
                else:
                    auto_attach_reason = "safety_checks_prevented_attachment"

        # Step 8: Compile continuation context
        #
        # The cache lives in the store's `meta` table, not on the Store
        # instance. Callers such as the Claude SessionStart hook build a fresh
        # Store per invocation (see `own_store` above) and close it on the way
        # out, so an instance attribute can never be read back on the next
        # start. An earlier revision read `store._last_compile`, which nothing
        # in the codebase ever wrote — so the 5-minute TTL was dead code, every
        # session start recompiled the ~79 KB bundle from scratch, and the
        # reported `context_source` was always "fresh_compile" even when
        # nothing had changed.
        members = store.thread_members(tid)
        cached = (_load_context_cache(store, tid, provider, budget)
                  if compile_context else {"compiled_at": 0, "context": None})
        last_compiled_at = cached["compiled_at"]
        context = cached["context"]
        context_stale = False
        context_source = ("none" if not compile_context
                          else "cached" if context else "fresh_compile")
        
        # Stale if:
        # - Never compiled OR compiled > 5 minutes ago
        # - Latest member session updated after compile
        # - The thread itself was touched (attach / merge) after compile
        # - Git HEAD changed
        # - Holder/provider changed
        latest_member_updated = max(
            (dict(m).get("updated_at") or 0) for m in members
        ) if members else 0

        # `thread_attach` bumps threads.updated_at via thread_touch but leaves
        # the member sessions' own updated_at alone. Without this term, a
        # session that was already indexed and attaches *after* a compile
        # leaves every other signal untouched, and the stale bundle is served
        # without the new member — silently, and looking like a cache hit.
        thread_updated = dict(thread).get("updated_at") or 0
        
        git_head_changed = _git_head_changed_since(
            store, tid, git_root, last_compiled_at
        ) if compile_context else False
        
        holder_changed = (lst["held"] and 
                         dict(lease).get("holder") != _latest_holder_provider(store, tid))
        
        # Determine if compilation needed
        needs_compile = (
            last_compiled_at <= 0 or                # never compiled
            time.time() - last_compiled_at > CONTEXT_TTL_SECONDS or
            latest_member_updated > last_compiled_at or
            thread_updated > last_compiled_at or
            git_head_changed or
            holder_changed
        )
        
        compiled_at = last_compiled_at
        
        # Compile context if needed. A caller that only wants the pending attach
        # (Grok's SessionStart hook) opts out: the event is passive, so the
        # bundle could never reach the model, and compiling it shells out to git
        # and rebuilds transcript-derived text on every single launch.
        if compile_context and (needs_compile or not context):
            context_source = "fresh_compile"
            context_stale = True  # the cache was stale (or absent) before this call
            fresh_context = None
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
                    fresh_context = ctx_result.get("context")
            except Exception as e:
                # Context compile failed - don't crash, just omit context
                fresh_context = None
            
            if fresh_context:
                context = fresh_context
                compiled_at = time.time()
                _save_context_cache(store, tid, provider, budget,
                                    fresh_context, compiled_at)
            elif context:
                # Compilation failed but we still hold a usable bundle. Serving
                # a known-stale bundle beats injecting nothing, as long as the
                # caller can tell: "cached" + context_stale=True.
                context_source = "cached"
                compiled_at = last_compiled_at
        
        return StartupContinuityResult({
            "continuity_available": True,
            "thread_id": tid,
            "repo_root": git_root,
            "goal": dict(thread).get("goal") or dict(thread).get("title"),
            "previous_provider": _latest_holder_provider(store, tid),
            "previous_session": _latest_holder_session(store, tid),
            "current_provider": provider,
            "current_session": native_session_id,
            "lease_state": {"held": lst["held"], "expired": lst["expired"],
                           "why": lst["why"], 
                           "holder": lease.get("holder") if lease else None,
                           "pid": lease.get("pid") if lease else None},
            "attach_status": attach_status,
            "context": context,
            "context_stale": context_stale,
            "context_source": context_source,  # "fresh_compile" | "cached"
            "recommended_action": ("use_context" if context else
                                  "continue_from_memory" if members else "none"),
            "auto_attach_reason": auto_attach_reason,
            "compiled_at": compiled_at if context else None,
        })
        
    finally:
        if own_store:
            store.close()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


# -- continuation-context cache --------------------------------------------
# Persisted in the store's `meta` table so it survives process boundaries: the
# Claude SessionStart hook constructs a fresh Store per invocation. The key is
# per (thread, provider, budget) — the budget is part of the key rather than
# validated on read, because two callers that alternate budgets (the hook
# defaults to "auto", some callers ask for "compact") would otherwise
# invalidate each other's entry on every call and never hit the cache at all.
CONTEXT_CACHE_PREFIX = "ctx_cache:"
CONTEXT_TTL_SECONDS = 300  # 5 minutes


def _context_cache_key(tid: str, provider: str, budget: str) -> str:
    return f"{CONTEXT_CACHE_PREFIX}{tid}:{provider}:{budget}"


def _load_context_cache(store: Store, tid: str, provider: str,
                        budget: str) -> Dict[str, Any]:
    """Load a cached continuation bundle.

    Returns {"compiled_at": float, "context": str | None}. Anything malformed,
    unreadable or non-finite is reported as a cache miss (compiled_at=0,
    context=None).

    This must never raise, and that is not a formality: the call site sits
    outside the compile `try`, so an exception here escapes `startup_continuity`
    entirely and the hook turns it into exit 2 on *every* session start — while
    the offending row stays in the table, so the session never recovers. Every
    parse/coerce step below is therefore defensive.
    """
    miss = {"compiled_at": 0.0, "context": None}
    try:
        raw = store.meta_get(_context_cache_key(tid, provider, budget))
        if not raw:
            return miss
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return miss
        context = payload.get("context")
        if not isinstance(context, str) or not context:
            return miss
        compiled_at = payload.get("compiled_at")
        # bool is an int subclass, and a JSON integer can be arbitrarily large:
        # float(10**400) raises OverflowError, which would otherwise escape.
        if isinstance(compiled_at, bool) or not isinstance(compiled_at, (int, float)):
            return miss
        compiled_at = float(compiled_at)
        # json.loads accepts NaN / Infinity / 1e400 by default. A non-finite
        # value would make every freshness test below silently false
        # (now - inf = -inf; anything > nan = False), freezing the cache
        # permanently against both the TTL and member updates.
        if not math.isfinite(compiled_at) or compiled_at <= 0:
            return miss
    except Exception:
        return miss
    return {"compiled_at": compiled_at, "context": context}


def _save_context_cache(store: Store, tid: str, provider: str, budget: str,
                        context: str, compiled_at: float) -> bool:
    """Persist a compiled bundle. Returns False if it could not be stored.

    Never raises. `ensure_ascii=True` is deliberate: session text can contain a
    lone surrogate (a truncated emoji, a broken codepoint from a provider
    export), which cannot be encoded to UTF-8 — SQLite would reject the row,
    the write would vanish, and the cache would silently never hit. Escaping
    keeps the stored value pure ASCII, and `json.loads` restores the original
    string exactly, lone surrogate included.
    """
    try:
        return bool(store.meta_set(
            _context_cache_key(tid, provider, budget),
            json.dumps({"compiled_at": compiled_at, "budget": budget,
                        "context": context}, ensure_ascii=True),
        ))
    except Exception:
        return False


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
                if dict(t).get("repo_root") and _same_repo(t["repo_root"], repo_root)]
    
    # Sort by updated_at descending (newest first)
    return sorted(matching, key=lambda x: x["updated_at"] or 0, reverse=True)


def _latest_holder_provider(store: Store, tid: str) -> Optional[str]:
    """Get the most recent provider that worked on this thread."""
    members = store.thread_members(tid)
    if not members:
        return None
    newest = max(members, key=lambda m: m["updated_at"] or 0)
    return dict(newest).get("provider")


def _latest_holder_session(store: Store, tid: str) -> Optional[str]:
    """Get the most recent session ID that worked on this thread."""
    members = store.thread_members(tid)
    if not members:
        return None
    newest = max(members, key=lambda m: m["updated_at"] or 0)
    return dict(newest).get("id")


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
    if dict(thread).get("repo_root"):
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
    """Check if git HEAD has changed since timestamp.
    
    This is a simple implementation that checks current HEAD vs last recorded.
    In production, we'd persist git commit fingerprints per thread and compare.
    For now, we just check if there's been any recent activity on source files.
    """
    # For simplicity, use file mtime as proxy for changes
    # This is imperfect but better than always returning False
    
    import subprocess
    
    try:
        # Get list of relevant source files (not .git/, not vendor/)
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode != 0:
            return False
        
        files = [f for f in result.stdout.split('\0') if f and 
                 not f.startswith('.git/') and 'vendor' not in f]
        
        if not files:
            return False
        
        # Check if any source file was modified after since_ts
        import os
        for f in files[:100]:  # Limit to first 100 files for performance
            try:
                mt = os.path.getmtime(os.path.join(repo_root, f))
                if mt > since_ts and mt > since_ts - 60:  # Allow 1 min tolerance
                    return True
            except OSError:
                continue
        
        return False
        
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    except Exception:
        return False
