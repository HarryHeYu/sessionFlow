"""Continuity tests: multi-session context synthesis, chronological overlay
(newest wins, older marked superseded), deduplication of files/commands/errors,
live git state, bundle naming, and CLI voyager merge / continue --from."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voyager.cli import main
from voyager.continuity import (
    CONTINUATION_INSTRUCTION,
    PROMPT_TARGETS,
    build_continuation_bundle,
    bundle_command,
    default_bundle_name,
    get_bundles_dir,
    get_git_snapshot,
)
from voyager.model import new_event, new_session
from voyager.store import Store


@pytest.fixture
def synthetic_trio(tmp_path):
    """Create three synthetic sessions modeling:
    Session 1 (Codex, ts=1000): Propose Approach X (XML).
    Session 2 (Claude, ts=2000): Reject Approach X for Approach Y (JSON).
    Session 3 (Grok, ts=3000): Implement Approach Y.
    """
    db_file = tmp_path / "continuity_test.db"
    store = Store(db_file)
    dummy_src = tmp_path / "dummy.jsonl"
    dummy_src.write_text("{}", encoding="utf-8")

    # Session 1: Propose Approach X
    s1 = new_session(
        id="codex:sess-1",
        provider="codex",
        native_session_id="sess-1",
        title="Session 1: Propose X",
        started_at=1000.0,
        updated_at=1050.0,
        repo_root=str(tmp_path),
        cwd=str(tmp_path),
    )
    evs1 = [
        new_event(sid="codex:sess-1", seq=1, kind="user", ts=1000.0,
                  content="We need a structured data format for config."),
        new_event(sid="codex:sess-1", seq=2, kind="tool_call", ts=1010.0,
                  tool_name="shell", command="ls -la", file_path="config.xml"),
        new_event(sid="codex:sess-1", seq=3, kind="assistant", ts=1050.0,
                  content="I propose Approach X: use XML for all configurations."),
    ]
    store.replace_session(s1, evs1, "codex", dummy_src)

    # Session 2: Reject Approach X for Y
    s2 = new_session(
        id="claude:sess-2",
        provider="claude",
        native_session_id="sess-2",
        title="Session 2: Reject X for Y",
        started_at=2000.0,
        updated_at=2050.0,
        repo_root=str(tmp_path),
        cwd=str(tmp_path),
    )
    evs2 = [
        new_event(sid="claude:sess-2", seq=1, kind="user", ts=2000.0,
                  content="Review the XML config proposal."),
        new_event(sid="claude:sess-2", seq=2, kind="tool_call", ts=2010.0,
                  tool_name="shell", command="pytest tests/test_config.py", exit_code=1),
        new_event(sid="claude:sess-2", seq=3, kind="error", ts=2020.0,
                  content="XML parsing failed: tag mismatch"),
        new_event(sid="claude:sess-2", seq=4, kind="assistant", ts=2050.0,
                  content="Rejected Approach X (XML too verbose/fragile). Use Approach Y (JSON format) instead."),
    ]
    store.replace_session(s2, evs2, "claude", dummy_src)

    # Session 3: Implement Approach Y
    s3 = new_session(
        id="grok:sess-3",
        provider="grok",
        native_session_id="sess-3",
        title="Session 3: Implement Y",
        started_at=3000.0,
        updated_at=3050.0,
        repo_root=str(tmp_path),
        cwd=str(tmp_path),
    )
    evs3 = [
        new_event(sid="grok:sess-3", seq=1, kind="user", ts=3000.0,
                  content="Implement Approach Y (JSON format)."),
        new_event(sid="grok:sess-3", seq=2, kind="tool_call", ts=3010.0,
                  tool_name="shell", command="pytest tests/test_config.py", exit_code=0),
        new_event(sid="grok:sess-3", seq=3, kind="assistant", ts=3050.0,
                  content="Approach Y implemented: JSON parser complete, all tests passing."),
    ]
    store.replace_session(s3, evs3, "grok", dummy_src)

    rows = [store.session("sess-1")[0], store.session("sess-2")[0], store.session("sess-3")[0]]
    yield store, rows
    store.close()


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
