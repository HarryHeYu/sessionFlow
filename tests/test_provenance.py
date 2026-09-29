"""Provenance: structural origin classification and NULL-only enrichment.

The point of these tests is that provenance is decided from provider structure,
never from message text -- so every case below is built from the structural
coordinates the providers actually emit.
"""
from __future__ import annotations

import json

from voyager.model import new_event, new_session
from voyager.provenance import classify, coverage, enrich
from voyager.store import Store


def _codex(typ, payload=None, metadata=None):
    rec = {"type": typ, "payload": payload or {}}
    if metadata is not None:
        rec["metadata"] = metadata
    return rec


# --- Codex ------------------------------------------------------------------

def test_codex_real_prompt_is_human_only_when_the_session_is_user_facing():
    rec = _codex("response_item", {"type": "message", "role": "user"},
                 {"user_input_order": 0, "client_authored": False})
    assert classify("codex", rec, {"source": "vscode"}) == "human"
    assert classify("codex", rec, {"source": "cli"}) == "human"


def test_codex_subagent_pseudo_user_is_not_human():
    rec = _codex("response_item", {"type": "message", "role": "user"},
                 {"user_input_order": 0})
    assert classify("codex", rec, {"source": {"subagent": {"other": "guardian"}}}) \
        != "human"
    assert classify("codex", rec, {"source": {"subagent": {"other": "guardian"}}}) \
        == "provider_synthetic"


def test_codex_agents_injection_is_bootstrap():
    # injected blocks carry no metadata at all
    rec = _codex("response_item", {"type": "message", "role": "user"})
    assert classify("codex", rec, {"source": "vscode"}) == "provider_bootstrap"


def test_codex_developer_records_are_injected_context():
    skills = _codex("response_item", {"type": "message", "role": "developer"})
    hookctx = _codex("response_item", {"type": "message", "role": "developer"},
                     {"client_authored": False})
    assert classify("codex", skills) == "provider_bootstrap"
    assert classify("codex", hookctx) == "provider_bootstrap"
    assert classify("codex", hookctx) != "human"


def test_codex_turn_aborted_is_provider_system():
    assert classify("codex", _codex("event_msg", {"type": "turn_aborted"})) \
        == "provider_system"


# --- Claude -----------------------------------------------------------------

def test_claude_real_user_is_human():
    rec = {"type": "user", "isSidechain": False, "userType": "external",
           "message": {"role": "user", "content": "please continue"}}
    assert classify("claude", rec) == "human"


class TestClaudeSidechain:
    """`isSidechain` is decisive: a spawned subagent's user-role record is
    provider-generated delegation, never a human turn.  Getting this wrong
    inflates `human_user_turns`, which feeds the session band and L1 selection.

    Note: no `isSidechain=true` record exists in this machine's Claude history,
    so these pin the rule from Claude's schema, not from an observed sample.
    """

    def test_mainline_user_is_human(self):
        rec = {"type": "user", "isSidechain": False, "isMeta": False,
               "userType": "external",
               "message": {"role": "user", "content": "please continue"}}
        assert classify("claude", rec) == "human"

    def test_sidechain_is_not_human(self):
        rec = {"type": "user", "isSidechain": True, "userType": "external",
               "message": {"role": "user", "content": "you are a subagent"}}
        assert classify("claude", rec) != "human"
        assert classify("claude", rec) == "provider_synthetic"

    def test_sidechain_with_user_shaped_payload_is_not_human(self):
        """The payload looks like an ordinary user message -- only the flag says
        otherwise, which is exactly why the flag must be read."""
        rec = {
            "type": "user", "isSidechain": True, "isMeta": False,
            "userType": "external", "entrypoint": "cli",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "continue the work"}],
            },
        }
        assert classify("claude", rec) != "human"

    def test_meta_record_is_not_human(self):
        rec = {"type": "user", "isMeta": True, "userType": "external",
               "message": {"role": "user",
                           "content": [{"type": "text", "text": "x"}]}}
        assert classify("claude", rec) != "human"
        assert classify("claude", rec) == "provider_system"

    def test_tool_result_is_not_human(self):
        rec = {"type": "user", "userType": "external",
               "message": {"role": "user",
                           "content": [{"type": "tool_result", "content": "ok"}]}}
        assert classify("claude", rec) != "human"
        assert classify("claude", rec) == "provider_system"

    def test_historical_human_case_does_not_regress(self):
        """The shapes actually present in this machine's Claude history must
        still classify as human."""
        for rec in (
            {"type": "user", "isSidechain": False, "userType": "external",
             "message": {"role": "user", "content": "hello"}},
            {"type": "user", "isSidechain": False, "userType": "external",
             "entrypoint": "cli",
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "继续"}]}},
        ):
            assert classify("claude", rec) == "human"


def test_claude_meta_record_is_not_human():
    rec = {"type": "user", "isMeta": True, "userType": "external",
           "message": {"role": "user", "content": [{"type": "text", "text": "x"}]}}
    assert classify("claude", rec) == "provider_system"


def test_claude_tool_result_on_a_user_record_is_not_a_user_turn():
    rec = {"type": "user", "userType": "external",
           "message": {"role": "user",
                       "content": [{"type": "tool_result", "content": "ok"}]}}
    assert classify("claude", rec) != "human"


# --- ZCode ------------------------------------------------------------------

def test_zcode_plain_user_message_is_human():
    assert classify("zcode", {"role": "user"}, {"parent_id": None}) == "human"


def test_zcode_flags_win_over_role():
    assert classify("zcode", {"role": "user", "synthetic": True}) == "provider_synthetic"
    assert classify("zcode", {"role": "user", "source": "todo_update"}) == "provider_system"


def test_zcode_subagent_child_is_not_human():
    assert classify("zcode", {"role": "user"}, {"parent_id": "ses_parent"}) \
        == "provider_synthetic"
    assert classify("zcode", {"role": "user"}, {"task_type": "subagent_child"}) \
        == "provider_synthetic"


# --- enrichment -------------------------------------------------------------

def _store_with(tmp_path, provider, records):
    store = Store(tmp_path / "prov.db")
    session = new_session(id="%s:s1" % provider, provider=provider,
                          native_session_id="s1", title="t",
                          started_at=1.0, updated_at=2.0,
                          repo_root=str(tmp_path), cwd=str(tmp_path),
                          raw_metadata={"source": "vscode"})
    events = [new_event(sid="%s:s1" % provider, seq=i + 1, kind="user", role="user",
                        content="c%d" % i, raw_event=rec)
              for i, rec in enumerate(records)]
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store.replace_session(session, events, provider, src)
    return store


def test_enrich_fills_only_nulls_and_is_idempotent(tmp_path):
    recs = [_codex("response_item", {"type": "message", "role": "user"},
                   {"user_input_order": 0}),
            _codex("response_item", {"type": "message", "role": "user"})]
    store = _store_with(tmp_path, "codex", recs)

    dry = enrich(store.con, dry_run=True)
    assert dry["classified"] == 2 and dry["applied"] == 0
    assert store.con.execute("select count(*) from events where origin is not null"
                             ).fetchone()[0] == 0, "dry run must not write"

    first = enrich(store.con, dry_run=False)
    assert first["applied"] == 2
    origins = [r[0] for r in store.con.execute("select origin from events order by seq")]
    assert origins == ["human", "provider_bootstrap"]

    # second run: nothing left to fill, state unchanged
    snapshot = store.con.execute("select id, origin from events order by id").fetchall()
    second = enrich(store.con, dry_run=False)
    assert second["applied"] == 0 and second["candidates"] == 0
    assert store.con.execute("select id, origin from events order by id").fetchall() \
        == snapshot


def test_enrich_leaves_unknown_when_the_record_is_unusable(tmp_path):
    store = _store_with(tmp_path, "codex", [None, {"type": "response_item"}])
    rep = enrich(store.con, dry_run=False)
    assert rep["applied"] == 0
    assert rep["left_unknown"] == 2
    assert store.con.execute("select count(*) from events where origin is not null"
                             ).fetchone()[0] == 0


def test_grok_has_no_classifier_and_stays_unknown(tmp_path):
    store = _store_with(tmp_path, "grok", [{"role": "user", "text": "hi"}])
    rep = enrich(store.con, dry_run=False)
    assert rep["applied"] == 0 and rep["left_unknown"] == 1


def test_coverage_report_shape(tmp_path):
    recs = [_codex("response_item", {"type": "message", "role": "user"},
                   {"user_input_order": 0}),
            _codex("response_item", {"type": "message", "role": "user"})]
    store = _store_with(tmp_path, "codex", recs)
    enrich(store.con, dry_run=False)
    rows = {r["provider"]: r for r in coverage(store.con)}
    assert rows["codex"]["user_events"] == 2
    assert rows["codex"]["enriched"] == 2
    assert rows["codex"]["human"] == 1
    assert rows["codex"]["coverage_pct"] == 100.0


# --- one stored representation for "not classified" -------------------------

def test_new_event_folds_the_unknown_sentinel_to_null():
    """A classifier's UNKNOWN must not reach the store as a literal string.

    Two encodings of the same state (`NULL` and `'unknown'`) would make
    `origin IS NOT NULL` stop meaning "classified" and drift apart over time.
    """
    from voyager.model import ORIGIN_UNKNOWN, new_event

    ev = new_event(sid="s", seq=1, kind="user", origin=ORIGIN_UNKNOWN)
    assert ev["origin"] is None, "UNKNOWN must be stored as NULL"

    ev2 = new_event(sid="s", seq=2, kind="user", origin="human")
    assert ev2["origin"] == "human", "real origins still round-trip"

    ev3 = new_event(sid="s", seq=3, kind="user", origin=None)
    assert ev3["origin"] is None


def test_a_genuinely_bad_origin_still_raises():
    import pytest as _pytest

    from voyager.model import new_event

    with _pytest.raises(ValueError):
        new_event(sid="s", seq=1, kind="user", origin="nonsense")


def test_provenance_unknown_matches_the_model_vocabulary():
    from voyager.model import ORIGIN_UNKNOWN
    from voyager.provenance import UNKNOWN

    assert UNKNOWN == ORIGIN_UNKNOWN == "unknown"
