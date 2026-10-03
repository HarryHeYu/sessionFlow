"""O6 — scoped pre-compile scan (POST-1.0 §4).

``continue`` / ``handoff`` / ``merge`` must refresh the index before compiling
(D12), but they do not need to refresh *every* provider.  ``_scan_scope_for_sessions``
narrows the scan to the providers a command can actually read, and -- crucially
-- returns ``None`` (scan everything) whenever that set cannot be proven, so
the freshness guarantee can never be weakened by the optimisation.

These tests pin that contract.
"""

from __future__ import annotations

from voyager.cli import _scan_scope_for_sessions

CODEX_SID = "codex:11111111-2222-3333-4444-555555555555"
ZCODE_SID = "zcode:sess_z9"


def test_scope_of_one_indexed_session(indexed_store):
    assert _scan_scope_for_sessions(indexed_store, [CODEX_SID]) == ["codex"]


def test_scope_is_the_union_of_indexed_sessions(indexed_store):
    got = _scan_scope_for_sessions(indexed_store, [CODEX_SID, ZCODE_SID])
    assert got == ["codex", "zcode"]


def test_unresolvable_ref_falls_back_to_scan_everything(indexed_store):
    """A brand-new session is exactly what the pre-compile scan is for."""
    assert _scan_scope_for_sessions(indexed_store, ["no-such-session"]) is None


def test_one_bad_ref_among_good_ones_scans_everything(indexed_store):
    assert _scan_scope_for_sessions(indexed_store, [CODEX_SID, "no-such-session"]) is None


def test_empty_refs_scan_everything(indexed_store):
    assert _scan_scope_for_sessions(indexed_store, []) is None
    assert _scan_scope_for_sessions(indexed_store, None) is None


def test_scope_includes_thread_member_providers(indexed_store):
    """The engine compiles the whole WorkThread, so every member provider counts."""
    tid = indexed_store.thread_create(repo_root="E:/proj/demo", title="demo")
    indexed_store.thread_attach(tid, CODEX_SID)
    indexed_store.thread_attach(tid, ZCODE_SID)
    # naming only the codex session still scopes in zcode, its thread-mate
    got = _scan_scope_for_sessions(indexed_store, [CODEX_SID])
    assert got == ["codex", "zcode"]


def test_scope_accepts_a_session_id_prefix(indexed_store):
    """`store.session()` resolves prefixes, so the scope helper inherits that."""
    assert _scan_scope_for_sessions(indexed_store, ["zcode:"]) == ["zcode"]
