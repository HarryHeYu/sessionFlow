"""CLI tests: scan (incl. idempotency + pruning), list/show/search/repo,
export, continue, brief and the guards around session resolution.

Everything runs through ``voyager.cli.main`` with an explicit ``--db`` in
tmp_path — the real index at ~/.voyager/index.db is never touched, and no
agent process is ever launched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voyager.cli import main

CODEX_NATIVE = "11111111-2222-3333-4444-555555555555"
ZCODE_NATIVE = "sess_z9"


# --- scan ------------------------------------------------------------------

def test_scan_indexes_codex_fixture(adapter_of, patch_paths, codex_fixture,
                                    tmp_path, capsys):
    patch_paths(adapter_of("codex"), SESSIONS_DIR=codex_fixture)
    db = tmp_path / "index.db"
    assert main(["--db", str(db), "scan", "--platform", "codex"]) == 0
    out = capsys.readouterr().out
    assert "1 sessions, 5 events" in out
    assert "codex: 1 sessions indexed" in out


def test_scan_is_idempotent(adapter_of, patch_paths, codex_fixture, tmp_path,
                            capsys):
    patch_paths(adapter_of("codex"), SESSIONS_DIR=codex_fixture)
    db = tmp_path / "index.db"
    main(["--db", str(db), "scan", "--platform", "codex"])
    capsys.readouterr()
    assert main(["--db", str(db), "scan", "--platform", "codex"]) == 0
    out = capsys.readouterr().out
    assert "1 sessions, 5 events" in out      # no duplication
    assert "unchanged: 1" in out              # source fingerprint honoured

    data = json.loads(_run(capsys, ["--db", str(db), "stats"]))
    assert data == {"sessions": 1, "events": 5, "by_provider": {"codex": 1}}


def test_scan_prunes_sessions_whose_source_vanished(adapter_of, patch_paths,
                                                    codex_fixture, tmp_path,
                                                    capsys):
    ad = adapter_of("codex")
    patch_paths(ad, SESSIONS_DIR=codex_fixture)
    db = tmp_path / "index.db"
    main(["--db", str(db), "scan", "--platform", "codex"])
    capsys.readouterr()

    for f in list(ad.discover()):
        f.unlink()          # the agent deleted its session file
    assert main(["--db", str(db), "scan", "--platform", "codex"]) == 0
    out = capsys.readouterr().out
    assert "pruned 1 vanished session(s)" in out
    assert json.loads(_run(capsys, ["--db", str(db), "stats"]))["sessions"] == 0


# --- read commands ---------------------------------------------------------

def _run(capsys, argv):
    """Run the CLI and return its stdout (asserting a clean exit)."""
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 0, f"{argv} exited {rc}"
    return out


def test_list_shows_both_sessions(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "list"])
    assert CODEX_NATIVE in out and ZCODE_NATIVE in out
    assert "fix the parser" in out and "2 session(s)" in out


def test_list_json_and_platform_filter(indexed_store, capsys):
    rows = json.loads(_run(capsys, ["--db", str(indexed_store.db_path),
                                    "list", "--json"]))
    assert {r["provider"] for r in rows} == {"codex", "zcode"}
    only = json.loads(_run(capsys, ["--db", str(indexed_store.db_path),
                                    "list", "--platform", "zcode", "--json"]))
    assert len(only) == 1 and only[0]["native_id"] == ZCODE_NATIVE
    none = _run(capsys, ["--db", str(indexed_store.db_path), "list",
                         "--repo", "no-such-repo"])
    assert "no sessions" in none


def test_show_renders_timeline(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "show",
                        CODEX_NATIVE[:12]])
    assert "codex:11111111-2222-3333-4444-555555555555" in out
    assert "USER" in out and "ASST" in out and "TOOL>" in out
    assert "python -m pytest -q" in out
    assert "exit=0" in out
    assert "resume  : codex resume" in out


def test_show_json(indexed_store, capsys):
    data = json.loads(_run(capsys, ["--db", str(indexed_store.db_path), "show",
                                    CODEX_NATIVE, "--json"]))
    assert data["session"]["native_id"] == CODEX_NATIVE
    assert len(data["events"]) == 6


def test_show_ambiguous_prefix_lists_candidates(indexed_store, capsys):
    with pytest.raises(SystemExit) as e:
        main(["--db", str(indexed_store.db_path), "show", ""])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "matches 2 sessions" in err and "use a longer prefix" in err


def test_show_missing_session_exits_2(indexed_store, capsys):
    with pytest.raises(SystemExit) as e:
        main(["--db", str(indexed_store.db_path), "show", "nope-nope"])
    assert e.value.code == 2
    assert "session not found" in capsys.readouterr().err


def test_search_finds_events_across_sessions(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "search", "pytest"])
    assert CODEX_NATIVE in out and "1 hit(s)" in out
    assert "no matches" in _run(capsys, ["--db", str(indexed_store.db_path),
                                         "search", "zzzznotfound"])


def test_search_json_and_cjk_substring(indexed_store, capsys):
    hits = json.loads(_run(capsys, ["--db", str(indexed_store.db_path),
                                    "search", "pytest", "--json"]))
    assert hits[0]["native_id"] == CODEX_NATIVE
    # trigram FTS must match CJK substrings, not just whole tokens
    cjk = json.loads(_run(capsys, ["--db", str(indexed_store.db_path),
                                   "search", "架构图", "--json"]))
    assert cjk and cjk[0]["provider"] == "zcode"


def test_search_survives_fts5_operator_characters(indexed_store, capsys):
    """'-', ':' and quotes are text to a user, syntax to FTS5 — never an error."""
    hits = json.loads(_run(capsys, ["--db", str(indexed_store.db_path),
                                    "search", "pytest -q", "--json"]))
    assert hits and hits[0]["native_id"] == CODEX_NATIVE
    assert "no matches" in _run(capsys, ["--db", str(indexed_store.db_path),
                                         "search", "zzz-no-such-text"])
    assert "no matches" in _run(capsys, ["--db", str(indexed_store.db_path),
                                         "search", '"unbalanced'])
    assert "search error" not in capsys.readouterr().err


def test_repo_timeline_groups_by_repo(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "repo", "demo"])
    assert "E:/proj/demo" in out and "fix the parser" in out
    assert "no sessions matched repo pattern" in _run(
        capsys, ["--db", str(indexed_store.db_path), "repo", "nope"])


def test_stats(indexed_store, capsys):
    data = json.loads(_run(capsys, ["--db", str(indexed_store.db_path), "stats"]))
    assert data["sessions"] == 2 and data["events"] == 7
    assert data["by_provider"] == {"codex": 1, "zcode": 1}


def test_db_flag_accepted_on_either_side(indexed_store, capsys):
    """`voyager --db X stats` and `voyager stats --db X` must both work."""
    before = json.loads(_run(capsys, ["--db", str(indexed_store.db_path), "stats"]))
    after = json.loads(_run(capsys, ["stats", "--db", str(indexed_store.db_path)]))
    assert before == after


def test_default_db_never_points_at_the_real_index(_never_touch_the_real_index):
    """Guard test for the safety net itself (see conftest)."""
    import voyager.store as store_mod

    assert store_mod.default_db_path() == _never_touch_the_real_index
    assert store_mod.Store().db_path == _never_touch_the_real_index
    assert ".voyager" not in str(_never_touch_the_real_index)


def test_files_reports_absent_file_history(indexed_store, capsys):
    """Codex persists no file history: say so instead of printing nothing."""
    out = _run(capsys, ["--db", str(indexed_store.db_path), "files",
                        CODEX_NATIVE])
    assert "no file history recorded" in out


def test_scan_claude_then_files_lists_backups(adapter_of, patch_paths,
                                              claude_fixture, tmp_path, capsys):
    """End-to-end: Claude project JSONL -> index -> `voyager files`."""
    patch_paths(adapter_of("claude"), PROJECTS_DIR=claude_fixture / "projects")
    db = tmp_path / "index.db"
    assert main(["--db", str(db), "scan", "--platform", "claude"]) == 0
    assert "claude: 1 sessions indexed" in capsys.readouterr().out

    out = _run(capsys, ["--db", str(db), "files", "cla-1111"])
    assert "E:/proj/demo/a.py" in out
    assert "(backup: hash1@v1)" in out


# --- export / continue / brief --------------------------------------------

def test_export_command_writes_both_formats(indexed_store, tmp_path, capsys):
    md = tmp_path / "out.md"
    js = tmp_path / "out.json"
    _run(capsys, ["--db", str(indexed_store.db_path), "export", CODEX_NATIVE,
                  "--format", "md", "-o", str(md)])
    _run(capsys, ["--db", str(indexed_store.db_path), "export", CODEX_NATIVE,
                  "--format", "json", "-o", str(js)])
    assert md.read_text(encoding="utf-8").startswith("# fix the parser")
    assert json.loads(js.read_text(encoding="utf-8"))["session"]["provider"] == "codex"


def test_continue_prints_native_resume_for_newest(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "continue"])
    assert "latest session: [codex]" in out
    assert f"$ codex resume {CODEX_NATIVE}" in out
    assert "add --launch to start it now" in out


def test_continue_falls_back_to_handoff_without_native_resume(
        indexed_store, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    out = _run(capsys, ["--db", str(indexed_store.db_path), "continue",
                        ZCODE_NATIVE])
    assert "native resume unsupported for 'zcode'" in out
    pkg = tmp_path / "handoff-zcode-sess_z9.md"
    assert pkg.is_file(), "fallback must leave a context package behind"
    assert "给 README 加个架构图" in pkg.read_text(encoding="utf-8")
    assert "$ claude" in out          # default fallback target


def test_continue_repo_filter(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "continue",
                        "--repo", "voyager"])
    assert "latest session: [zcode]" in out
    assert "native resume unsupported" in out
    # the fallback package goes to the cwd (a tmp dir, see conftest), never
    # into the checkout
    pkg = Path("handoff-zcode-sess_z9.md")
    assert pkg.is_file() and "架构图" in pkg.read_text(encoding="utf-8")


def test_continue_on_empty_index_is_not_an_error(tmp_path, capsys):
    rc = main(["--db", str(tmp_path / "empty.db"), "continue"])
    assert rc == 1
    assert "no sessions to continue" in capsys.readouterr().out


def test_brief_digest(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "brief",
                        "--hours", "1000000"])
    assert "agent activity, last 1000000.0h (2 sessions" in out
    assert "fix the parser" in out and "给 README 加个架构图" in out
    assert "(2 msgs / 1 tools)" in out
    empty = _run(capsys, ["--db", str(indexed_store.db_path), "brief",
                          "--hours", "0.000001"])
    assert "no sessions updated in the last" in empty
