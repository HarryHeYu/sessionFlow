"""Continuity tests: multi-session context synthesis, chronological overlay
(newest wins, older marked superseded), deduplication of files/commands/errors,
live git state, bundle naming, and CLI voyager merge / continue --from."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voyager.cli import main
from voyager.continuity import (
    CONTEXT_FORMAT_TIERED,
    CONTEXT_FORMAT_VERSION,
    CONTINUATION_INSTRUCTION,
    L1_TRUNCATION_MARKER,
    PROMPT_TARGETS,
    build_continuation_bundle,
    build_thread_state,
    build_tiered_bundle,
    build_working_context,
    bundle_command,
    default_bundle_name,
    get_bundles_dir,
    get_git_snapshot,
)
from voyager.model import new_event, new_session
from voyager.store import Store


def test_synthesis_chronological_overlay_and_superseding(synthetic_trio):
    """Test the core overlay rule: newest session's 'where we stopped' is current state;
    older conclusions appear under 'Prior assistant conclusions (may be superseded)'
    and are not confused with open/active options.
    """
    store, rows = synthetic_trio
    bundle = build_continuation_bundle(store, rows, live_git=False)

    assert bundle.startswith("# Continuation Bundle")

    # 1. Goal section
    assert "## Goal" in bundle
    assert "We need a structured data format for config." in bundle
    assert "Implement Approach Y (JSON format)." in bundle

    # 2. Current verified state
    assert "## Current verified state" in bundle
    assert "active session: `grok` `sess-3`" in bundle
    assert "Approach Y implemented: JSON parser complete" in bundle

    # 3. Prior conclusions section: must record superseded older proposals with source ids
    assert "## Prior assistant conclusions (may be superseded)" in bundle
    assert "`[codex:sess-1]`" in bundle
    assert "I propose Approach X" in bundle
    assert "`[claude:sess-2]`" in bundle
    assert "Rejected Approach X" in bundle

    # Concat-regression test: Approach X must NOT appear in Current verified state
    state_section = bundle.split("## Current verified state")[1].split("##")[0]
    assert "Approach Y" in state_section
    assert "Approach X" not in state_section

    # 4. Files touched & commands
    assert "## Files touched across sessions" in bundle
    assert "`config.xml`" in bundle

    assert "## Commands executed" in bundle
    # pytest should be deduplicated in the commands section
    cmds_section = bundle.split("## Commands executed")[1].split("##")[0]
    assert cmds_section.count("pytest tests/test_config.py") == 1

    # 5. Errors encountered
    assert "## Errors encountered" in bundle
    assert "XML parsing failed: tag mismatch" in bundle

    # 6. Evidence & Provenance
    assert "## Evidence & Provenance" in bundle
    assert "Compiled from 3 session(s):" in bundle
    assert "**codex** `sess-1`" in bundle
    assert "**claude** `sess-2`" in bundle
    assert "**grok** `sess-3`" in bundle


def test_synthesis_explicit_goal_override(synthetic_trio):
    store, rows = synthetic_trio
    bundle = build_continuation_bundle(store, rows, goal="Refactor JSON serializer", live_git=False)
    assert "## Goal" in bundle
    assert "**Primary user goal:** Refactor JSON serializer" in bundle


def test_bundle_naming():
    s1 = {"provider": "codex", "native_id": "1111-2222-3333"}
    assert default_bundle_name([s1]).startswith("bundle-codex-1111-2222-3333")

    s2 = {"provider": "claude", "native_id": "aaaa-bbbb"}
    name = default_bundle_name([s1, s2])
    assert "bundle-merged-2sessions-claude-codex-" in name


def test_bundle_command_targets(tmp_path):
    bundle_file = tmp_path / "bundle.md"
    for target in PROMPT_TARGETS:
        argv = bundle_command(target, bundle_file)
        assert argv[0] == PROMPT_TARGETS[target]
        assert argv[1].startswith(CONTINUATION_INSTRUCTION)
        assert str(bundle_file.resolve()) in argv[1]
    assert bundle_command("unknown", bundle_file) is None


def test_cli_merge_command(synthetic_trio, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    store, rows = synthetic_trio
    out = tmp_path / "merged_out.md"
    rc = main(["--db", str(store.db_path), "merge", "sess-1", "sess-2", "sess-3",
               "--to", "claude", "-o", str(out)])
    printed = capsys.readouterr().out
    assert rc == 0
    assert out.is_file()
    content = out.read_text(encoding="utf-8")
    assert "Approach Y implemented" in content
    assert "continuation bundle:" in printed
    assert '$ claude "<continuation prompt>"' in printed


def test_cli_continue_from_command(synthetic_trio, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    store, rows = synthetic_trio
    out = tmp_path / "cont_out.md"
    rc = main(["--db", str(store.db_path), "continue", "--from", "sess-1,sess-2,sess-3",
               "--to", "codex", "-o", str(out)])
    printed = capsys.readouterr().out
    assert rc == 0
    assert out.is_file()
    content = out.read_text(encoding="utf-8")
    assert "Approach Y implemented" in content
    assert "continuation bundle:" in printed
    assert '$ codex "<continuation prompt>"' in printed


def test_default_bundle_directory_is_in_home(synthetic_trio, capsys, monkeypatch):
    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    store, rows = synthetic_trio
    rc = main(["--db", str(store.db_path), "merge", "sess-1", "sess-2"])
    printed = capsys.readouterr().out
    assert rc == 0
    # Must save into bundles dir under HOME, not cwd
    bundles_dir = get_bundles_dir()
    assert str(bundles_dir) in printed
    assert "next: pick a target agent" in printed


# ---------------------------------------------------------------------------
# Phase 1b — index freshness / automatic sync
# ---------------------------------------------------------------------------

def _codex_rollout_fixture(tmp_path, monkeypatch):
    """A synthetic codex rollout + patched adapter path; returns (file, sid)."""
    import voyager.adapters.codex as codex_mod
    d = tmp_path / "codex" / "sessions" / "2026" / "09" / "18"
    d.mkdir(parents=True)
    f = d / "rollout-2026-09-18T10-00-00-11111111-2222-3333-4444-555555555555_w.jsonl"
    lines = [
        json.dumps({"timestamp": "2026-09-18T10:00:00Z", "ordinal": 0,
                    "type": "session_meta",
                    "payload": {"session_id": "11111111-2222-3333-4444-555555555555",
                                "timestamp": "2026-09-18T10:00:00Z",
                                "cwd": str(tmp_path / "repo"),
                                "originator": "codex_vscode"}}),
        json.dumps({"timestamp": "2026-09-18T10:00:05Z", "ordinal": 1,
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text",
                                             "text": "phase1b marker ALPHA"}]}}),
        json.dumps({"timestamp": "2026-09-18T10:00:10Z", "ordinal": 2,
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": "done ALPHA"}]}}),
    ]
    f.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", d.parent.parent.parent)
    return f, "codex:11111111-2222-3333-4444-555555555555"


def test_phase1b_merge_sees_new_content_after_change(
        tmp_path, monkeypatch, capsys):
    import os as _os
    import re as _re
    import time as _time
    monkeypatch.delenv("VOYAGER_NO_SYNC", raising=False)
    """Two merges around a content change: the second bundle MUST contain
    the new message; unchanged round must not re-parse (idempotent);
    provider file must never be written to."""
    f, sid = _codex_rollout_fixture(tmp_path, monkeypatch)
    before_bytes = f.read_bytes()
    db = tmp_path / "p1b.db"
    out1 = tmp_path / "b1.md"

    # round 1: scan + merge
    rc = main(["--db", str(db), "scan"])
    assert rc == 0
    rc = main(["--db", str(db), "merge", sid, "-o", str(out1)])
    assert rc == 0
    b1 = out1.read_text(encoding="utf-8")
    assert "phase1b marker ALPHA" in b1

    # append a new message (content + mtime change)
    _time.sleep(0.02)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("\n" + json.dumps({
            "timestamp": "2026-09-18T10:05:00Z", "ordinal": 3,
            "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text",
                                     "text": "phase1b marker BETA"}]}}))
    _os.utime(f, (_time.time(), _time.time()))

    # round 2: merge again (handoff/merge pre-scan must refresh internally)
    out2 = tmp_path / "b2.md"
    rc = main(["--db", str(db), "merge", sid, "-o", str(out2)])
    assert rc == 0
    b2 = out2.read_text(encoding="utf-8")
    assert "phase1b marker BETA" in b2, "fresh scan must pick up new content"
    printed = capsys.readouterr().out
    assert "freshness:" in printed

    # round 3: nothing changed -> freshness reports 0 changed sources
    rc = main(["--db", str(db), "continue", sid, "--to", "claude",
               "-o", str(tmp_path / "b3.md")])
    assert rc == 0
    printed = capsys.readouterr().out
    assert _re.search(r"freshness: scanned in \d+\.\d+s, 0 source\(s\) changed",
                      printed), printed

    # provider file never written by voyager (only our own append touched it)
    assert b"phase1b marker BETA" in f.read_bytes()
    assert "phase1b marker BETA" in f.read_text(encoding="utf-8")


def test_phase1b_scan_is_idempotent(tmp_path, monkeypatch):
    import io
    import re as _re
    from contextlib import redirect_stdout
    monkeypatch.delenv("VOYAGER_NO_SYNC", raising=False)
    f, sid = _codex_rollout_fixture(tmp_path, monkeypatch)
    db = tmp_path / "p1b_idem.db"
    main(["--db", str(db), "scan"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["--db", str(db), "scan"])
    assert rc == 0
    assert "0 new/refreshed" in buf.getvalue()


# --- L0: authoritative WorkThread state (Phase C / Step A) ------------------

L0_MARKER = "...[truncated]"


def _l0_thread(**over):
    row = {
        "id": "thr_l0test",
        "repo_root": "E:/code/repo",
        "title": "Continuity Engine build-out",
        "goal": "complete the roadmap",
        "status": "active",
        "created_at": 1.0,
        "updated_at": 2.0,
    }
    row.update(over)
    return row


def _l0_member(sid, provider, ts):
    return {"id": sid, "provider": provider, "started_at": ts, "updated_at": ts}


def _l0_line(out, name):
    return [ln for ln in out.splitlines() if ln.startswith(name + ": ")][0]


class TestL0ThreadState:
    """L0-core projects the WorkThread's own fields; it does not read sessions.

    The tier split exists so that a session's temporary task cannot overwrite the
    thread's actual goal, which is why that negative case is the first test.
    """

    def test_goal_survives_a_session_working_on_something_else(self):
        out = build_thread_state(
            _l0_thread(), [_l0_member("claude:aaa", "claude", 10.0)])
        assert _l0_line(out, "goal") == "goal: complete the roadmap"
        # Nothing session-derived may become the authority for `goal`.
        assert "fix test count" not in out
        assert out.count("goal:") == 1

    def test_missing_field_reads_unknown_and_is_never_inferred(self):
        assert _l0_line(build_thread_state(_l0_thread(goal=None), ()),
                        "goal") == "goal: unknown"
        assert _l0_line(build_thread_state(_l0_thread(goal="   "), ()),
                        "goal") == "goal: unknown"
        # An absent member list is reported mechanically, not guessed at.
        out = build_thread_state(_l0_thread(), ())
        assert _l0_line(out, "members") == "members: 0"
        assert _l0_line(out, "latest_session_id") == "latest_session_id: unknown"

    def test_long_field_is_truncated_with_an_explicit_marker(self):
        goal = "x" * 5000
        line = _l0_line(
            build_thread_state(_l0_thread(goal=goal), (), max_field_chars=100),
            "goal")
        assert line.endswith(L0_MARKER)
        assert len(line) == len("goal: ") + 100
        # deterministic: the same over-long value clips to the same thing
        assert line == _l0_line(
            build_thread_state(_l0_thread(goal=goal), (), max_field_chars=100),
            "goal")

    def test_same_input_is_byte_identical_regardless_of_member_order(self):
        members = [_l0_member("claude:bbb", "claude", 30.0),
                   _l0_member("grok:ccc", "grok", 20.0)]
        first = build_thread_state(_l0_thread(), members)
        assert first == build_thread_state(_l0_thread(), members)
        assert first == build_thread_state(_l0_thread(), list(reversed(members)))
        assert _l0_line(first, "latest_session_id") == \
            "latest_session_id: claude:bbb"
        assert _l0_line(first, "latest_session_provider") == \
            "latest_session_provider: claude"
        assert first.startswith(
            "[WorkThread]\ncontext_format_version: %d\n" % CONTEXT_FORMAT_VERSION)

    def test_build_does_not_mutate_its_inputs(self, tmp_path):
        db = tmp_path / "l0.db"
        store = Store(db)
        tid = store.thread_create(repo_root=str(tmp_path), title="t", goal="g")
        src = tmp_path / "s.jsonl"
        src.write_text("{}", encoding="utf-8")
        session = new_session(
            id="claude:l0", provider="claude", native_session_id="l0", title="s",
            started_at=10.0, updated_at=20.0,
            repo_root=str(tmp_path), cwd=str(tmp_path))
        store.replace_session(
            session,
            [new_event(sid="claude:l0", seq=1, kind="user", ts=10.0,
                       content="hello")],
            "claude", src)
        store.thread_attach(tid, "claude:l0")

        thread_row = store.thread_get(tid)
        members = store.thread_member_sessions(tid)
        thread_before = dict(thread_row)
        members_before = [dict(m) for m in members]
        stats_before = store.stats()

        out = build_thread_state(thread_row, members)

        assert "goal: g" in out
        assert dict(store.thread_get(tid)) == thread_before
        assert [dict(m) for m in store.thread_member_sessions(tid)] == \
            members_before
        assert store.stats() == stats_before


# --- Step B: tiered bundle skeleton ----------------------------------------

class TestTieredBundleSkeleton:
    """Step B pins the format and the composition boundary. Nothing shrinks yet.

    The payload is the flat builder's own output rather than a re-implementation,
    so there is no second set of session/event/git/budget formatting to drift
    away while L1 is still being designed.
    """

    def _fixture(self, tmp_path):
        store = Store(tmp_path / "tiered.db")
        tid = store.thread_create(repo_root=str(tmp_path),
                                  title="Continuity Engine build-out",
                                  goal="complete the roadmap")
        src = tmp_path / "s.jsonl"
        src.write_text("{}", encoding="utf-8")
        session = new_session(
            id="claude:tb", provider="claude", native_session_id="tb",
            title="Session tb", started_at=10.0, updated_at=20.0,
            repo_root=str(tmp_path), cwd=str(tmp_path))
        store.replace_session(
            session,
            [new_event(sid="claude:tb", seq=1, kind="user", ts=10.0,
                       content="Ship the tiered bundle skeleton."),
             new_event(sid="claude:tb", seq=2, kind="tool_call", ts=15.0,
                       tool_name="shell", command="pytest -q",
                       file_path="voyager/continuity.py"),
             new_event(sid="claude:tb", seq=3, kind="error", ts=16.0,
                       content="boom: TypeError")],
            "claude", src)
        store.thread_attach(tid, "claude:tb")
        return (store, store.thread_get(tid), store.thread_member_sessions(tid),
                [dict(r) for r in store.sessions()])

    def test_marker_and_authoritative_l0(self, tmp_path):
        store, thread, members, rows = self._fixture(tmp_path)
        out = build_tiered_bundle(store, thread, members, rows, live_git=False)
        assert out.startswith("format: %s\n" % CONTEXT_FORMAT_TIERED)
        assert "[L0 Thread State]" in out
        assert "goal: complete the roadmap" in out
        assert "context_format_version: %d" % CONTEXT_FORMAT_VERSION in out
        assert "[L1 Compatibility Payload]" in out

    def test_payload_is_byte_identical_to_the_flat_builder(self, tmp_path):
        store, thread, members, rows = self._fixture(tmp_path)
        out = build_tiered_bundle(store, thread, members, rows, live_git=False)
        payload = out.split("[L1 Compatibility Payload]\n", 1)[1]
        assert payload == build_continuation_bundle(store, rows, live_git=False)
        # ...and it still carries the evidence the flat bundle carried
        assert "pytest -q" in payload
        assert "boom: TypeError" in payload

    def test_same_input_is_byte_identical(self, tmp_path):
        store, thread, members, rows = self._fixture(tmp_path)
        assert (build_tiered_bundle(store, thread, members, rows, live_git=False)
                == build_tiered_bundle(store, thread, members, rows,
                                       live_git=False))

    def test_tiered_build_does_not_mutate_state(self, tmp_path):
        store, thread, members, rows = self._fixture(tmp_path)
        stats_before = store.stats()
        thread_before = dict(store.thread_get(thread["id"]))
        build_tiered_bundle(store, thread, members, rows, live_git=False)
        assert store.stats() == stats_before
        assert dict(store.thread_get(thread["id"])) == thread_before


# --- Step C: bounded L1 active working context ------------------------------

class TestL1WorkingContext:
    """Step C pins deterministic L1 selection: canonical event order, complete
    turns, hard byte budget, recency-only priority.  No importance scoring."""

    def _add(self, store, tmp_path, sid, provider, events, updated_at=2.0):
        src = tmp_path / (sid.replace(":", "_") + ".jsonl")
        src.write_text("{}", encoding="utf-8")
        session = new_session(
            id=sid, provider=provider, native_session_id=sid.split(":", 1)[1],
            title="Session " + sid, started_at=1.0, updated_at=updated_at,
            repo_root=str(tmp_path), cwd=str(tmp_path))
        store.replace_session(session, events, provider, src)

    def _rows(self, store, sids):
        by_id = {r["id"]: dict(r) for r in store.sessions()}
        return [by_id[sid] for sid in sids]

    def _header_len(self, store):
        # with no sessions the document is exactly the fixed banner
        return len(build_working_context(store, [], hard_max=10 ** 6)
                   .encode("utf-8"))

    def test_selection_follows_canonical_order_not_timestamps(self, tmp_path):
        """Edge case: timestamps contradicting the canonical order lose.

        The canonical-old session carries the biggest timestamps and the
        canonical-new session's own events run ts backwards against seq --
        neither may move what counts as newest.
        """
        store = Store(tmp_path / "l1.db")
        self._add(store, tmp_path, "codex:old", "codex",
                  [new_event(sid="codex:old", seq=1, kind="user", ts=900.0,
                             content="old task"),
                   new_event(sid="codex:old", seq=2, kind="assistant", ts=950.0,
                             content="old conclusion " + "P" * 6000)],
                  updated_at=9999.0)
        self._add(store, tmp_path, "codex:new", "codex",
                  [new_event(sid="codex:new", seq=1, kind="user", ts=120.0,
                             content="new task"),
                   new_event(sid="codex:new", seq=2, kind="assistant", ts=100.0,
                             content="new conclusion")],
                  updated_at=1.0)
        rows = self._rows(store, ["codex:old", "codex:new"])

        out = build_working_context(store, rows, hard_max=4096)

        # newest = last in canonical order, not max(ts) and not updated_at
        assert "[codex:new #1] user: new task" in out
        assert "[codex:new #2] assistant: new conclusion" in out
        assert "old conclusion" not in out
        assert "old task" not in out
        assert len(out.encode("utf-8")) <= 4096

    def test_tightest_budget_still_keeps_the_newest_event(self, tmp_path):
        store = Store(tmp_path / "l1.db")
        self._add(store, tmp_path, "grok:t", "grok",
                  [new_event(sid="grok:t", seq=1, kind="user", ts=1.0,
                             content="kick off " + "A" * 2000),
                   new_event(sid="grok:t", seq=2, kind="assistant", ts=2.0,
                             content="B" * 2000 + "FINAL-STATE")])
        rows = self._rows(store, ["grok:t"])
        base = self._header_len(store)

        out = build_working_context(store, rows, hard_max=base + 200)

        assert "FINAL-STATE" in out           # the newest evidence survives
        assert L1_TRUNCATION_MARKER in out    # the drop is declared, not silent
        assert "kick off" not in out          # older events of the turn are gone
        assert len(out.encode("utf-8")) <= base + 200

    def test_newest_turn_over_budget_is_truncated_deterministically(
            self, tmp_path):
        """Edge case: a newest turn that alone exceeds the hard max."""
        store = Store(tmp_path / "l1.db")
        self._add(store, tmp_path, "codex:k", "codex",
                  [new_event(sid="codex:k", seq=1, kind="user", ts=1.0,
                             content="kick off"),
                   new_event(sid="codex:k", seq=2, kind="assistant", ts=2.0,
                             content="Q" * 20000)])
        rows = self._rows(store, ["codex:k"])

        first = build_working_context(store, rows, hard_max=2000)
        second = build_working_context(store, rows, hard_max=2000)

        assert first == second
        assert len(first.encode("utf-8")) <= 2000
        assert L1_TRUNCATION_MARKER in first
        assert "kick off" not in first
        assert first.endswith("Q" * 50)       # the tail, not the head, survives

    def test_complete_turns_are_never_cut_in_half(self, tmp_path):
        store = Store(tmp_path / "l1.db")
        self._add(store, tmp_path, "zcode:s1", "zcode",
                  [new_event(sid="zcode:s1", seq=1, kind="user", ts=1.0,
                             content="first task"),
                   new_event(sid="zcode:s1", seq=2, kind="assistant", ts=2.0,
                             content="X" * 3000)])
        self._add(store, tmp_path, "zcode:s2", "zcode",
                  [new_event(sid="zcode:s2", seq=1, kind="user", ts=3.0,
                             content="second task"),
                   new_event(sid="zcode:s2", seq=2, kind="assistant", ts=4.0,
                             content="second done")])
        self._add(store, tmp_path, "zcode:s3", "zcode",
                  [new_event(sid="zcode:s3", seq=1, kind="user", ts=5.0,
                             content="third task"),
                   new_event(sid="zcode:s3", seq=2, kind="assistant", ts=6.0,
                             content="third done")])
        rows = self._rows(store, ["zcode:s1", "zcode:s2", "zcode:s3"])
        base = self._header_len(store)

        out = build_working_context(store, rows, hard_max=base + 1000)

        # the two newest turns are present whole: every line of each
        assert out.count("[zcode:") == 4
        assert "[zcode:s2 #1] user: second task" in out
        assert "[zcode:s2 #2] assistant: second done" in out
        assert "[zcode:s3 #1] user: third task" in out
        assert "[zcode:s3 #2] assistant: third done" in out
        # the oldest turn is dropped whole -- recency only, no partial packing
        assert "first task" not in out
        assert "X" * 100 not in out
        assert len(out.encode("utf-8")) <= base + 1000

    def test_hard_byte_bound_holds_across_budgets(self, tmp_path):
        store = Store(tmp_path / "l1.db")
        self._add(store, tmp_path, "codex:a", "codex",
                  [new_event(sid="codex:a", seq=1, kind="user", ts=1.0,
                             content="task a"),
                   new_event(sid="codex:a", seq=2, kind="assistant", ts=2.0,
                             content="done a")])
        self._add(store, tmp_path, "codex:b", "codex",
                  [new_event(sid="codex:b", seq=1, kind="user", ts=3.0,
                             content="task b"),
                   new_event(sid="codex:b", seq=2, kind="assistant", ts=4.0,
                             content="done b")])
        self._add(store, tmp_path, "codex:c", "codex",
                  [new_event(sid="codex:c", seq=1, kind="user", ts=5.0,
                             content="task c"),
                   new_event(sid="codex:c", seq=2, kind="assistant", ts=6.0,
                             content="M" * 1500)])
        rows = self._rows(store, ["codex:a", "codex:b", "codex:c"])

        for hard_max in (500, 1500, 5000, 100000):
            out = build_working_context(store, rows, hard_max=hard_max)
            assert len(out.encode("utf-8")) <= hard_max, hard_max
        # below the fixed banner the bound cannot be honored at all
        with pytest.raises(ValueError):
            build_working_context(store, rows, hard_max=10)

    def test_hundred_sessions_stay_bounded_and_l2_stays_out(self, tmp_path):
        store = Store(tmp_path / "l1.db")
        for i in range(100):
            self._add(store, tmp_path, "codex:s%d" % i, "codex",
                      [new_event(sid="codex:s%d" % i, seq=1, kind="user",
                                 ts=float(i), content="task %d" % i),
                       new_event(sid="codex:s%d" % i, seq=2, kind="assistant",
                                 ts=float(i) + 0.5, content="done %d" % i)])
        rows = self._rows(store, ["codex:s%d" % i for i in range(100)])

        out = build_working_context(store, rows, hard_max=4096)

        assert len(out.encode("utf-8")) <= 4096
        assert "[codex:s99 #2] assistant: done 99" in out   # newest survives
        assert "task 0" not in out                          # oldest is L2, not inlined
        assert "task 70" in out                             # a recent window is

    def test_build_does_not_mutate_the_store(self, tmp_path):
        store = Store(tmp_path / "l1.db")
        tid = store.thread_create(repo_root=str(tmp_path), title="t", goal="g")
        for sid in ("claude:m1", "grok:m2"):
            self._add(store, tmp_path, sid, sid.split(":", 1)[0],
                      [new_event(sid=sid, seq=1, kind="user", ts=1.0,
                                 content="hello from " + sid),
                       new_event(sid=sid, seq=2, kind="assistant", ts=2.0,
                                 content="done")])
            store.thread_attach(tid, sid)
        rows = self._rows(store, ["claude:m1", "grok:m2"])

        stats_before = store.stats()
        sessions_before = {r["id"]: dict(r) for r in store.sessions()}
        events_before = {sid: [dict(e) for e in store.events(sid)]
                         for sid in ("claude:m1", "grok:m2")}
        thread_before = dict(store.thread_get(tid))

        build_working_context(store, rows, hard_max=10 ** 6)

        assert store.stats() == stats_before
        assert {r["id"]: dict(r) for r in store.sessions()} == sessions_before
        assert {sid: [dict(e) for e in store.events(sid)]
                for sid in ("claude:m1", "grok:m2")} == events_before
        assert dict(store.thread_get(tid)) == thread_before

    def test_empty_and_renderless_inputs(self, tmp_path):
        store = Store(tmp_path / "l1.db")
        empty = build_working_context(store, [], hard_max=5000)
        assert empty.startswith("[L1 Active Working Context]")
        assert len(empty.encode("utf-8")) <= 5000
        assert empty == build_working_context(store, [], hard_max=5000)

        # events with nothing to show render nothing: the document stays
        # exactly the banner
        self._add(store, tmp_path, "codex:e", "codex",
                  [new_event(sid="codex:e", seq=1, kind="meta", ts=1.0,
                             content=None)])
        assert build_working_context(
            store, self._rows(store, ["codex:e"]), hard_max=5000) == empty

        # command-only events render their command; a missing seq reads "-"
        self._add(store, tmp_path, "codex:c", "codex",
                  [new_event(sid="codex:c", seq=None, kind="tool_call", ts=2.0,
                             tool_name="shell", command="pytest -q"),
                   new_event(sid="codex:c", seq=3, kind="user", ts=3.0,
                             content=None),
                   new_event(sid="codex:c", seq=4, kind="assistant", ts=4.0,
                             content="orphan conclusion")])
        out = build_working_context(
            store, self._rows(store, ["codex:c"]), hard_max=5000)
        assert "[codex:c #-] tool_call: pytest -q" in out
        assert "[codex:c #4] assistant: orphan conclusion" in out
        # an empty user line vanishes, but it still splits the turn: the
        # assistant conclusion below it is its own turn, not the command's
        assert "[codex:c #3] user:" not in out
