"""The canonical provider capability matrix.

One source of truth for what sessionFlow can do with each provider. The README,
`voyager doctor` and `voyager integrate status` all read this, so the docs cannot
drift away from the code.

Two things are kept separate, because conflating them is how a project ends up
claiming live verification it never did:

  DECLARED -- what the provider supports, as established by reading its
              documented contract and by the code in this repo.  A ceiling.
  EVIDENCE -- what has actually happened on this machine: is the provider
              installed, is a hook registered, has that hook been observed
              firing, has a bare "continue" resumed a WorkThread.

A cell resolves to the weaker of the two, and the six states are ordered:

  NOT_FOUND_IN_CURRENT_AUDIT  <  SUPPORTED  <  CONFIGURED  <  UNIT_VERIFIED
                              <  LIVE_VERIFIED  <  ZERO_TOUCH_LIVE_VERIFIED

``SUPPORTED`` means "the provider offers this and Voyager implements it"; it never
means "seen working". Only evidence can lift a cell above CONFIGURED, and no
cell is promoted without a recorded reason.

Usage:
------
1. Collect runtime evidence: ``from voyager.capability_matrix import summary; summary()``
2. Generate full matrix: ``from voyager.capability_matrix import matrix; matrix()``
3. Check individual provider: ``from voyager.capability_matrix import provider_state; provider_state('codex')``

This module is imported, not run: the command-line view is
``voyager verify [--matrix] [--json]`` (O6 removed the old
``python -m voyager.capability_matrix`` entry point, which duplicated it).

All outputs feed into:
- ``voyager doctor --json``: Provider health status
- ``voyager integrate status``: Integration installation state
- README.md: Supported platforms table
- ROADMAP.md: Verification milestones
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --- the vocabulary ---------------------------------------------------------

NOT_FOUND = "NOT_FOUND_IN_CURRENT_AUDIT"
SUPPORTED = "SUPPORTED"
CONFIGURED = "CONFIGURED"
UNIT_VERIFIED = "UNIT_VERIFIED"
LIVE_VERIFIED = "LIVE_VERIFIED"
ZERO_TOUCH_LIVE_VERIFIED = "ZERO_TOUCH_LIVE_VERIFIED"

STATE_ORDER = {
    NOT_FOUND: 0,
    SUPPORTED: 1,
    CONFIGURED: 2,
    UNIT_VERIFIED: 3,
    LIVE_VERIFIED: 4,
    ZERO_TOUCH_LIVE_VERIFIED: 5,
}

#: The dimensions every provider is measured against.
DIMENSIONS: Tuple[str, ...] = (
    "session_discovery",
    "session_indexing",
    "native_resume",
    "startup_hook",
    "dynamic_context_injection",
    "native_session_id_at_start",
    "native_auto_attach",
    "live_zero_touch_continuity",
    "transcript_import",
    "tool_event_support",
    "cwd_repo_discovery",
    "context_budget",
    "truncation_behavior",
)

PROVIDERS: Tuple[str, ...] = (
    "codex", "claude", "grok", "zcode", "cursor", "kiro", "antigravity", "dsh",
)

# --- what each provider supports (the ceiling) -----------------------------
#
# Every entry is (state, note).  The note is the evidence for the ceiling and is
# surfaced by `doctor`, so a reader can check the claim instead of trusting it.

_ADAPTER = "adapter in voyager/adapters/"
_HANDLER = "startup handler in voyager/integrations/"


def _all(state: str, note: str) -> Dict[str, Tuple[str, str]]:
    return {d: (state, note) for d in DIMENSIONS}


DECLARED: Dict[str, Dict[str, Tuple[str, str]]] = {
    "codex": {
        "session_discovery": (LIVE_VERIFIED, _ADAPTER + "; rollout JSONL"),
        "session_indexing": (LIVE_VERIFIED, _ADAPTER),
        "native_resume": (SUPPORTED, "`codex resume` is documented"),
        "startup_hook": (LIVE_VERIFIED, "hooks.json SessionStart, observed firing"),
        "dynamic_context_injection": (LIVE_VERIFIED, "hookSpecificOutput.additionalContext"),
        "native_session_id_at_start": (LIVE_VERIFIED, "payload session_id"),
        "native_auto_attach": (LIVE_VERIFIED, "pending-attach resolved on start"),
        "live_zero_touch_continuity": (ZERO_TOUCH_LIVE_VERIFIED,
                                       "2026-09-28: bare 继续 resumed the WorkThread"),
        "transcript_import": (LIVE_VERIFIED, _ADAPTER),
        "tool_event_support": (SUPPORTED, "tool events present in rollouts"),
        "cwd_repo_discovery": (LIVE_VERIFIED, "payload cwd + rollout fallback"),
        "context_budget": (LIVE_VERIFIED, "10,000 chars measured; window built at 7,600"),
        "truncation_behavior": (LIVE_VERIFIED, "measured: middle elision above 10,000"),
    },
    "claude": {
        "session_discovery": (LIVE_VERIFIED, _ADAPTER + "; project JSONL"),
        "session_indexing": (LIVE_VERIFIED, _ADAPTER),
        "native_resume": (SUPPORTED, "`claude --resume` is documented"),
        "startup_hook": (LIVE_VERIFIED, "settings.json SessionStart, observed firing"),
        "dynamic_context_injection": (LIVE_VERIFIED, "hookSpecificOutput.additionalContext"),
        "native_session_id_at_start": (LIVE_VERIFIED, "payload session_id"),
        "native_auto_attach": (LIVE_VERIFIED, "pending-attach resolved on start"),
        "live_zero_touch_continuity": (ZERO_TOUCH_LIVE_VERIFIED,
                                       "2026-09-24/25: cross-provider leg ran live"),
        "transcript_import": (LIVE_VERIFIED, _ADAPTER),
        "tool_event_support": (SUPPORTED, "tool_use / tool_result records"),
        "cwd_repo_discovery": (LIVE_VERIFIED, "payload cwd"),
        "context_budget": (LIVE_VERIFIED, "10,000 chars; window built at 7,600"),
        "truncation_behavior": (SUPPORTED, "documented cap; not triggered since the budget fix"),
    },
    "grok": {
        "session_discovery": (LIVE_VERIFIED, _ADAPTER + "; chat_history.jsonl"),
        "session_indexing": (LIVE_VERIFIED, _ADAPTER),
        "native_resume": (SUPPORTED, "`grok -r` is documented"),
        "startup_hook": (LIVE_VERIFIED, "rules-writer hook, observed firing"),
        "dynamic_context_injection": (LIVE_VERIFIED, "rules file written before launch"),
        "native_session_id_at_start": (LIVE_VERIFIED, "session id recorded on start"),
        "native_auto_attach": (LIVE_VERIFIED, "pending-attach resolved on start"),
        "live_zero_touch_continuity": (ZERO_TOUCH_LIVE_VERIFIED,
                                       "2026-09-25: sentinel written in Claude was recovered"),
        "transcript_import": (LIVE_VERIFIED, _ADAPTER),
        "tool_event_support": (SUPPORTED, "tool records present"),
        "cwd_repo_discovery": (LIVE_VERIFIED, "cwd resolved at start"),
        "context_budget": (SUPPORTED, "documented budget; not measured here"),
        "truncation_behavior": (NOT_FOUND, "no measurement recorded"),
    },
    "zcode": {
        "session_discovery": (LIVE_VERIFIED, _ADAPTER + "; SQLite"),
        "session_indexing": (LIVE_VERIFIED, _ADAPTER),
        "native_resume": (SUPPORTED, "desktop resume is documented"),
        "startup_hook": (UNIT_VERIFIED, _HANDLER + "; registered; not yet observed firing"),
        "dynamic_context_injection": (UNIT_VERIFIED, "envelope driven end to end with a real payload"),
        "native_session_id_at_start": (UNIT_VERIFIED, "payload session_id"),
        "native_auto_attach": (UNIT_VERIFIED, "shared core path exercised in tests"),
        "live_zero_touch_continuity": (NOT_FOUND, "LIVE_VERIFICATION_BLOCKED_BY_PROVIDER_UI"),
        "transcript_import": (LIVE_VERIFIED, _ADAPTER),
        "tool_event_support": (SUPPORTED, "file events stay in raw"),
        "cwd_repo_discovery": (UNIT_VERIFIED, "payload cwd"),
        "context_budget": (UNIT_VERIFIED, "shared cap; no documented provider limit"),
        "truncation_behavior": (NOT_FOUND, "not measured; ZCode reports maxOutputBytes"),
    },
    "cursor": {
        "session_discovery": (LIVE_VERIFIED, _ADAPTER + "; state.vscdb"),
        "session_indexing": (LIVE_VERIFIED, _ADAPTER),
        "native_resume": (NOT_FOUND, "no resume surface found; IDE only"),
        "startup_hook": (UNIT_VERIFIED, _HANDLER + "; hooks.json registered; not yet fired"),
        "dynamic_context_injection": (UNIT_VERIFIED, "top-level additional_context verified"),
        "native_session_id_at_start": (UNIT_VERIFIED, "payload session_id / conversation_id"),
        "native_auto_attach": (UNIT_VERIFIED, "shared core path exercised in tests"),
        "live_zero_touch_continuity": (NOT_FOUND, "LIVE_VERIFICATION_BLOCKED_BY_PROVIDER_UI"),
        "transcript_import": (LIVE_VERIFIED, _ADAPTER),
        "tool_event_support": (SUPPORTED, "tool data in raw"),
        "cwd_repo_discovery": (UNIT_VERIFIED, "workspace_roots (no cwd on sessionStart)"),
        "context_budget": (UNIT_VERIFIED, "shared cap; Cursor documents no limit"),
        "truncation_behavior": (NOT_FOUND, "not measured"),
    },
    "kiro": {
        "session_discovery": (LIVE_VERIFIED, _ADAPTER + "; workspace-session JSON"),
        "session_indexing": (LIVE_VERIFIED, _ADAPTER),
        "native_resume": (NOT_FOUND, "sessions are not persisted across restarts"),
        "startup_hook": (UNIT_VERIFIED, _HANDLER + "; hooks are project-scoped, so none installed globally"),
        "dynamic_context_injection": (UNIT_VERIFIED, "plain-text stdout contract verified"),
        "native_session_id_at_start": (UNIT_VERIFIED, "payload session id, if present"),
        "native_auto_attach": (UNIT_VERIFIED, "shared core path exercised in tests"),
        "live_zero_touch_continuity": (NOT_FOUND, "LIVE_VERIFICATION_BLOCKED_BY_PROVIDER_UI"),
        "transcript_import": (LIVE_VERIFIED, _ADAPTER),
        "tool_event_support": (SUPPORTED, "tool records present"),
        "cwd_repo_discovery": (UNIT_VERIFIED, "project root of the command hook"),
        "context_budget": (UNIT_VERIFIED, "shared cap; Kiro documents no limit"),
        "truncation_behavior": (NOT_FOUND, "not measured"),
    },
    "antigravity": {
        "session_discovery": (LIVE_VERIFIED, _ADAPTER + "; conversation SQLite"),
        "session_indexing": (LIVE_VERIFIED, _ADAPTER),
        "native_resume": (NOT_FOUND, "no resume surface found"),
        "startup_hook": (UNIT_VERIFIED, _HANDLER + "; PreInvocation, registered globally"),
        "dynamic_context_injection": (UNIT_VERIFIED, "injectSteps.ephemeralMessage verified"),
        "native_session_id_at_start": (UNIT_VERIFIED, "payload conversationId"),
        "native_auto_attach": (UNIT_VERIFIED, "shared core path exercised in tests"),
        "live_zero_touch_continuity": (NOT_FOUND, "LIVE_VERIFICATION_BLOCKED_BY_PROVIDER_UI"),
        "transcript_import": (LIVE_VERIFIED, _ADAPTER),
        "tool_event_support": (SUPPORTED, "tool records present"),
        "cwd_repo_discovery": (UNIT_VERIFIED, "workspacePaths"),
        "context_budget": (UNIT_VERIFIED, "shared cap; no documented limit"),
        "truncation_behavior": (NOT_FOUND, "not measured"),
    },
    "dsh": {
        "session_discovery": (LIVE_VERIFIED, _ADAPTER + "; zstd JSONL"),
        "session_indexing": (LIVE_VERIFIED, _ADAPTER),
        "native_resume": (SUPPORTED, "`dsh --resume` is documented"),
        "startup_hook": (NOT_FOUND, "no native startup surface; profiles/plugins/ACP only"),
        "dynamic_context_injection": (NOT_FOUND, "no startup surface to inject into"),
        "native_session_id_at_start": (NOT_FOUND, "no startup surface"),
        "native_auto_attach": (NOT_FOUND, "no startup surface"),
        "live_zero_touch_continuity": (NOT_FOUND, "no startup surface"),
        "transcript_import": (LIVE_VERIFIED, _ADAPTER),
        "tool_event_support": (SUPPORTED, "tool records present"),
        "cwd_repo_discovery": (LIVE_VERIFIED, "session records carry cwd"),
        "context_budget": (NOT_FOUND, "no injection path"),
        "truncation_behavior": (NOT_FOUND, "no injection path"),
    },
}

#: Where each provider keeps its hook registration, if it has one.
HOOK_CONFIG_PATHS: Dict[str, str] = {
    "codex": "~/.codex/hooks.json",
    "claude": "~/.claude/settings.json",
    "zcode": "~/.zcode/cli/config.json",
    "cursor": "~/.cursor/hooks.json",
    "kiro": ".kiro/hooks/<id>.json",          # project-scoped, not global
    "antigravity": "~/.gemini/config/hooks.json",
}

#: Where each provider's own session data lives, for source-path checks.
SOURCE_PATHS: Dict[str, str] = {
    "codex": "~/.codex/sessions",
    "claude": "~/.claude/projects",
    "grok": "~/.grok",
    "zcode": "~/.zcode/cli/db",
    "cursor": "~/.cursor",            # config/data root on Windows
    "kiro": "~/.kiro",
    "antigravity": "~/.gemini/antigravity",
    "dsh": "~/.dsh/sessions",
}

#: Per-provider context budget declarations (roadmap Phase 4 / issue #5).
# Each provider declares its maximum safe payload size, transport limits,
# and truncation/spill behaviors. These override the global defaults.
PROVIDER_CONTEXT_BUDGETS: Dict[str, Dict[str, Any]] = {
    "codex": {
        "startup_context_budget": 7600,      # chars before Codex additionalContext cap
        "transport_limit": 8000,             # max bytes per hookSpecificOutput
        "truncation_behavior": "middle_elision",  # measured: middle cut above 10k
        "supports_spill_pointer": False,     # no spill path in hooks.json
        "native_session_id_at_start": True,
        "auto_budget_target": "balanced",    # map to preset
    },
    "claude": {
        "startup_context_budget": 7600,      # chars before claude cap
        "transport_limit": 10000,            # additionalContext ceiling
        "truncation_behavior": "header_elision",  # drops header sections first
        "supports_spill_pointer": True,      # can use spilled path pointer
        "native_session_id_at_start": True,
        "auto_budget_target": "balanced",
    },
    "grok": {
        "startup_context_budget": 5000,      # rules file size constraint
        "transport_limit": 5500,
        "truncation_behavior": "section_deduplication",
        "supports_spill_pointer": False,
        "native_session_id_at_start": True,
        "auto_budget_target": "compact",
    },
    "zcode": {
        "startup_context_budget": 8000,      # shared cap; ZCode reports maxOutputBytes
        "transport_limit": 10000,
        "truncation_behavior": "section_dropping",
        "supports_spill_pointer": True,
        "native_session_id_at_start": True,
        "auto_budget_target": "balanced",
    },
    "cursor": {
        "startup_context_budget": 8000,      # Cursor documents no limit
        "transport_limit": 12000,
        "truncation_behavior": "section_dropping",
        "supports_spill_pointer": True,
        "native_session_id_at_start": True,
        "auto_budget_target": "full",
    },
    "kiro": {
        "startup_context_budget": 8000,      # Kiro documents no limit
        "transport_limit": 12000,
        "truncation_behavior": "section_dropping",
        "supports_spill_pointer": True,
        "native_session_id_at_start": True,
        "auto_budget_target": "full",
    },
    "antigravity": {
        "startup_context_budget": 8000,      # no documented limit
        "transport_limit": 12000,
        "truncation_behavior": "section_dropping",
        "supports_spill_pointer": True,
        "native_session_id_at_start": True,
        "auto_budget_target": "full",
    },
    "dsh": {
        "startup_context_budget": None,      # no injection path
        "transport_limit": None,
        "truncation_behavior": None,
        "supports_spill_pointer": False,
        "native_session_id_at_start": False,
        "auto_budget_target": None,
    },
}


# --- runtime evidence ------------------------------------------------------

@dataclass
class Evidence:
    """What has actually been observed on this machine."""

    installed: bool = False
    hook_registered: bool = False
    hook_fired: bool = False
    zero_touch_observed: bool = False
    last_trigger: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def _log_dir() -> Path:
    import os
    override = os.environ.get("VOYAGER_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".voyager" / "logs"


def collect_evidence(provider: str) -> Evidence:
    """Read the machine state for one provider. Best effort, never raises.

    Runtime evidence (hook fired, zero-touch observed) comes **only** from the
    append-only ``verification_events`` table managed by
    :mod:`voyager.verification_harness`.  No hardcoded sets, no log-file
    grepping, no "adapter exists = live" shortcuts -- if the evidence table has
    no rows for a provider, that provider is ``UNIT_VERIFIED`` at best, which
    is the honest answer ("code exists, not yet observed on this machine").

    ``installed`` and ``hook_registered`` are still checked on the filesystem,
    because those describe *configuration* state, not *runtime* state.
    """
    ev = Evidence()
    try:
        src = SOURCE_PATHS.get(provider)
        ev.installed = bool(src) and Path(src).expanduser().exists()
    except Exception:
        ev.installed = False
    try:
        cfg = HOOK_CONFIG_PATHS.get(provider)
        ev.hook_registered = bool(cfg) and not cfg.startswith(".") and Path(cfg).expanduser().exists()
    except Exception:
        ev.hook_registered = False

    # Query the evidence table for runtime observations.  This is the sole
    # source of hook_fired / zero_touch_observed -- no hardcoded sets, no log
    # grepping.  If the table does not exist or has no events for this
    # provider, the evidence stays at its defaults (all False), which means
    # the ceiling is UNIT_VERIFIED.  That is correct.
    try:
        from .verification_harness import _connect, observed_state, has_evidence_table
        con = _connect()
        if con is not None:
            try:
                if has_evidence_table(con):
                    obs = observed_state(con, provider)
                    state = obs.get("observed_state")
                    if state == ZERO_TOUCH_LIVE_VERIFIED:
                        ev.zero_touch_observed = True
                        ev.hook_fired = True
                    elif state == LIVE_VERIFIED:
                        ev.hook_fired = True
                    ev.last_trigger = obs.get("last_live_event")
            finally:
                try:
                    con.close()
                except Exception:
                    pass
    except Exception:
        pass  # verification_harness not available or DB inaccessible

    return ev


# --- resolution ------------------------------------------------------------

def _weaker(a: str, b: str) -> str:
    return a if STATE_ORDER.get(a, 0) <= STATE_ORDER.get(b, 0) else b


def evidence_ceiling(ev: Evidence) -> str:
    """The highest state the machine evidence can justify.

    The floor is UNIT_VERIFIED, not CONFIGURED: a handler that exists and is
    covered by tests is *verified at the unit level* whether or not this machine
    happens to have the provider installed.  Evidence below that only ever lowers
    a LIVE / ZERO_TOUCH claim, never a code-level one.
    """
    if ev.zero_touch_observed:
        return ZERO_TOUCH_LIVE_VERIFIED
    if ev.hook_fired:
        return LIVE_VERIFIED
    return UNIT_VERIFIED


def adapter_can_resume(provider: str) -> Optional[bool]:
    """What the adapter itself says, or None when there is no adapter.

    The adapter is the authority on native resume: it is the code that would
    have to build the command.  When it sets ``can_resume = False`` the
    provider cannot be resumed no matter what a hand-written table claims, and
    reporting SUPPORTED tells the user to run a command that does not exist.
    """
    try:
        from voyager.adapters import load_all
        from voyager.adapters.base import get_adapter
    except Exception:  # noqa: BLE001 - a broken import must not break the matrix
        return None
    try:
        # Importing the adapter modules is what registers them; without this
        # the registry is empty and every provider looks like "no adapter".
        load_all()
        adapter = get_adapter(provider)
    except Exception:  # noqa: BLE001
        return None
    if adapter is None:
        return None
    return bool(getattr(adapter, "can_resume", False))


def resolve_cell(provider: str, dimension: str, ev: Optional[Evidence] = None) -> Tuple[str, str]:
    """The actual state of one cell, with the reason it is not higher."""
    declared = DECLARED.get(provider, {}).get(dimension, (NOT_FOUND, "no entry"))
    state, note = declared
    ev = ev or collect_evidence(provider)

    # Native resume is a claim about code, not about this machine: if the
    # adapter says it cannot resume, no declaration may outrank that.  This is
    # checked before the evidence ceiling because it is not an evidence
    # question at all -- see the zcode entry, which declared SUPPORTED while
    # its adapter sets can_resume=False.
    if dimension == "native_resume":
        can = adapter_can_resume(provider)
        if can is False and STATE_ORDER.get(state, 0) > STATE_ORDER.get(NOT_FOUND, 0):
            return (NOT_FOUND,
                    "the %s adapter sets can_resume=False; declared %s (%s)"
                    % (provider, state, note))

    # Dimensions that describe data on disk do not depend on a hook firing.
    machine_dependent = dimension in (
        "startup_hook", "dynamic_context_injection", "native_session_id_at_start",
        "native_auto_attach", "live_zero_touch_continuity",
    )
    if machine_dependent:
        ceiling = evidence_ceiling(ev)
        if STATE_ORDER.get(state, 0) > STATE_ORDER.get(ceiling, 0):
            reason = "declared %s; on this machine: %s" % (state, _explain(ev, ceiling))
            return (ceiling, reason)
    return (state, note)


def _explain(ev: Evidence, ceiling: str) -> str:
    if ceiling == UNIT_VERIFIED:
        return "hook registered, covered by tests, provider not yet observed firing"
    if ceiling == LIVE_VERIFIED:
        return "hook observed firing"
    if ceiling == CONFIGURED:
        return "provider installed, hook not registered"
    if ceiling == ZERO_TOUCH_LIVE_VERIFIED:
        return "a bare continue resumed the WorkThread with no Voyager command"
    return "not observed on this machine"


def matrix(providers: Optional[List[str]] = None) -> Dict[str, Dict[str, Dict[str, str]]]:
    """The resolved matrix: {provider: {dimension: {state, note}}}."""
    out: Dict[str, Dict[str, Dict[str, str]]] = {}
    for p in (providers or PROVIDERS):
        ev = collect_evidence(p)
        out[p] = {}
        for d in DIMENSIONS:
            state, note = resolve_cell(p, d, ev)
            out[p][d] = {"state": state, "note": note}
    return out


def provider_state(provider: str, ev: Optional[Evidence] = None) -> str:
    """One headline state for a provider.

    The headline is the continuity a user actually gets: ZERO_TOUCH_LIVE_VERIFIED
    when a bare continue was observed resuming the thread, LIVE_VERIFIED when the
    hook was seen firing, otherwise whatever the startup-hook dimension supports
    -- **capped at the evidence ceiling**.  A provider whose code declares
    LIVE_VERIFIED but has no evidence in the table reports UNIT_VERIFIED,
    because "the handler exists and is tested" is all the evidence supports.

    A provider with no startup surface at all reports NOT_FOUND -- it is not
    silently folded into SUPPORTED.
    """
    ev = ev or collect_evidence(provider)
    declared = DECLARED.get(provider, {}).get("startup_hook", (NOT_FOUND, ""))[0]
    if declared == NOT_FOUND:
        return NOT_FOUND
    if ev.zero_touch_observed:
        return ZERO_TOUCH_LIVE_VERIFIED
    if ev.hook_fired:
        return LIVE_VERIFIED
    # No live evidence: cap at the evidence ceiling (UNIT_VERIFIED when no
    # evidence table or no events).  The declared ceiling may be higher
    # (LIVE_VERIFIED / ZERO_TOUCH_LIVE_VERIFIED), but without evidence the
    # honest answer is the ceiling the evidence can justify.
    ceiling = evidence_ceiling(ev)
    if STATE_ORDER.get(declared, 0) > STATE_ORDER.get(ceiling, 0):
        return ceiling
    return declared


def summary() -> Dict[str, Any]:
    """What `doctor` prints and `--json` emits."""
    providers = {}
    for p in PROVIDERS:
        ev = collect_evidence(p)
        providers[p] = {
            "state": provider_state(p, ev),
            "installed": ev.installed,
            "hook_registered": ev.hook_registered,
            "hook_fired": ev.hook_fired,
            "last_trigger": ev.last_trigger,
            "context_budget": PROVIDER_CONTEXT_BUDGETS.get(p, {}).get("startup_context_budget"),
            "auto_budget_target": PROVIDER_CONTEXT_BUDGETS.get(p, {}).get("auto_budget_target"),
        }
    return {"providers": providers,
            "dimensions": list(DIMENSIONS),
            "states": list(STATE_ORDER)}


# --- provider-aware budget helpers -----------------------------------------

def provider_context_budget(provider: str) -> Optional[int]:
    """Get the declared startup context budget (in chars) for a provider.

    Returns None if provider has no injection path (e.g., DSH).
    """
    return PROVIDER_CONTEXT_BUDGETS.get(provider, {}).get("startup_context_budget")


def provider_transport_limit(provider: str) -> Optional[int]:
    """Get the transport limit (max bytes per hook payload) for a provider."""
    return PROVIDER_CONTEXT_BUDGETS.get(provider, {}).get("transport_limit")


def provider_truncation_behavior(provider: str) -> Optional[str]:
    """Get the truncation behavior strategy for a provider.

    Strategies: "middle_elision", "header_elision", "section_deduplication", "section_dropping"
    """
    return PROVIDER_CONTEXT_BUDGETS.get(provider, {}).get("truncation_behavior")


def provider_supports_spill_pointer(provider: str) -> bool:
    """Does this provider support spill pointer mechanism?"""
    return PROVIDER_CONTEXT_BUDGETS.get(provider, {}).get("supports_spill_pointer", False)


def provider_auto_budget_target(provider: str) -> Optional[str]:
    """Get the auto-budget target preset name for a provider.

    Maps to: "compact" | "balanced" | "full" | None
    """
    return PROVIDER_CONTEXT_BUDGETS.get(provider, {}).get("auto_budget_target")


def resolve_provider_budget(provider: str, user_specified: Optional[str] = None) -> int:
    """Resolve the actual token budget for a given provider.

    Priority order:
    1. User-specified budget overrides everything
    2. Provider-specific startup_context_budget converted to tokens
    3. Global default (AUTO_BUDGET_TOKENS from budget.py)

    Returns token count (chars/4 estimate).
    """
    from .budget import BUDGET_PRESETS, AUTO_BUDGET_TOKENS

    # 1. If user explicitly specified, use that
    if user_specified and user_specified.lower() != "auto":
        from .budget import parse_budget
        parsed = parse_budget(user_specified)
        if parsed:
            return parsed

    # 2. Try provider-specific budget
    char_budget = provider_context_budget(provider)
    if char_budget:
        # Convert chars to tokens using standard estimate (chars/4)
        return max(1, char_budget // 4)

    # 3. Fallback to global default
    auto_target = provider_auto_budget_target(provider)
    if auto_target and auto_target in BUDGET_PRESETS:
        return BUDGET_PRESETS[auto_target]

    return AUTO_BUDGET_TOKENS
