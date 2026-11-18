"""Q1-C2 — additive `merge --repo` for NEW WorkThreads (ADR PR #55).

Invariants pinned here:
* default behaviour (no --repo) is unchanged — first usable row wins;
* explicit --repo overrides repo_root only when a thread is CREATED;
* reverse order + --repo A yields the same repo_root A in both runs;
* an existing thread keeps its repo_root; a differing --repo is a warning,
  not a rewrite;
* member set, ord, thread identity and bundle order are unchanged;
* Store / Bridge schema and BRIDGE_SCHEMA_VERSION are untouched;
* repo_source is explicit (new thread + --repo), derived (new thread, first
  usable row), or existing (reused thread keeps its stored repo_root);
* the reuse warning never names an unimplemented relink command.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from voyager.api import BRIDGE_SCHEMA_VERSION
from voyager.cli import _thread_from_merge, main
from voyager.store import Store


def _sessions(store: Store, rows):
    from voyager.model import new_event
    dummy = store.db_path.parent / "src.jsonl"
    dummy.write_text("{}\n", encoding="utf-8")
    for sid, provider, repo in rows:
        sess = {
            "id": sid, "provider": provider, "native_session_id": sid.split(":")[-1],
            "title": sid, "started_at": 1000.0, "updated_at": 1010.0,
            "cwd": repo, "repo_root": repo,
            "message_count": 1, "tool_count": 0,
            "can_resume": False, "can_fork": False, "resume_cmd": None,
            "metadata": {}, "raw_metadata": {},
        }
        evs = [{"sid": sid, "ts": 1000.0, "seq": 0, "kind": "user", "content": sid}]
        store.replace_session(sess, evs, provider, dummy)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "q1c2.db")
    yield s
    s.close()


ROWS = [("codex:1", "codex", "E:/repo/A"), ("claude:2", "claude", "E:/repo/B")]


def _args(repo=None, *, json_mode=True, title=None, goal=None):
    return SimpleNamespace(repo=repo, thread_title=title, goal=goal,
                           json=json_mode, to=None, output=None,
                           bundles_dir=None, launch=False, budget=None,
                           steal=False, mode="bundle")


def _fresh(store, order=ROWS):
    _sessions(store, order)
    return [store.session(sid)[0] for sid, _, _ in order]


# --- AC1: reverse order + --repo A -> same repo_root A both runs ----------

def test_explicit_repo_is_order_independent_for_new_thread(store, tmp_path):
    # Two separate DBs so both runs CREATE a thread (no reuse shortcut).
    dba = tmp_path / "a.db"
    sa = Store(dba); _sessions(sa, ROWS)
    rows_a = [sa.session(s)[0] for s, _, _ in ROWS]
    info_a = {}
    tid_a = _thread_from_merge(sa, rows_a, _args(repo="E:/repo/EXPLICIT"), info=info_a)
    root_a = sa.thread_get(tid_a)["repo_root"]
    assert info_a["repo_source"] == "explicit"
    sa.close()

    dbb = tmp_path / "b.db"
    sb = Store(dbb); _sessions(sb, list(reversed(ROWS)))
    rows_b = [sb.session(s)[0] for s, _, _ in reversed(ROWS)]
    info_b = {}
    tid_b = _thread_from_merge(sb, rows_b, _args(repo="E:/repo/EXPLICIT"), info=info_b)
    root_b = sb.thread_get(tid_b)["repo_root"]
    assert info_b["repo_source"] == "explicit"
    sb.close()

    assert root_a == root_b == "E:/repo/EXPLICIT"
    # members set is equal; ord follows caller order (not repo-derived)
    sa2 = Store(dba)
    assert {m["id"] for m in sa2.thread_members(tid_a)} == {"codex:1", "claude:2"}
    sa2.close()
    sb2 = Store(dbb)
    assert [m["id"] for m in sb2.thread_members(tid_b)] == ["claude:2", "codex:1"]
    sb2.close()


# --- AC2: no --repo pins current first-row behaviour ---------------------

def test_default_repo_is_first_usable_row(store):
    rows = _fresh(store)
    info = {}
    tid = _thread_from_merge(store, rows, _args(repo=None), info=info)
    assert store.thread_get(tid)["repo_root"] == "E:/repo/A"
    assert info["repo_source"] == "derived"
    assert "repo_warning" not in info


# --- AC3: --repo does not change member set / ord / identity -------------

def test_explicit_repo_preserves_member_order_and_identity(store):
    rows = _fresh(store)
    tid = _thread_from_merge(store, rows, _args(repo="E:/repo/EXPLICIT"))
    assert [m["id"] for m in store.thread_members(tid)] == ["codex:1", "claude:2"]
    # A second merge of the same set finds the SAME thread (member-set identity)
    tid2 = _thread_from_merge(store, list(reversed(rows)), _args(repo="E:/repo/EXPLICIT"))
    assert tid2 == tid


# --- AC4: reuse never rewrites repo_root; mismatch warns ----------------

def test_reuse_keeps_repo_root_and_warns(store):
    rows = _fresh(store)
    tid = _thread_from_merge(store, rows, _args(repo="E:/repo/EXPLICIT"))
    assert store.thread_get(tid)["repo_root"] == "E:/repo/EXPLICIT"

    info = {}
    tid2 = _thread_from_merge(store, rows, _args(repo="E:/repo/OTHER"), info=info)
    assert tid2 == tid
    assert store.thread_get(tid2)["repo_root"] == "E:/repo/EXPLICIT"
    assert info["repo_root"] == "E:/repo/EXPLICIT"
    assert "repo_warning" in info and "ignored" in info["repo_warning"]
    assert "relink" not in info["repo_warning"]
    assert info["thread_action"] == "updated"
    assert info["repo_source"] == "existing"   # kept from the existing thread


def test_reuse_with_matching_repo_does_not_warn(store):
    rows = _fresh(store)
    tid = _thread_from_merge(store, rows, _args(repo="E:/repo/EXPLICIT"))
    info = {}
    _thread_from_merge(store, rows, _args(repo="E:/repo/EXPLICIT"), info=info)
    assert "repo_warning" not in info
    assert info["repo_source"] == "existing"


def test_reuse_same_repo_different_path_spelling_does_not_warn(store):
    rows = _fresh(store)
    _thread_from_merge(store, rows, _args(repo="E:/repo/EXPLICIT"))
    info = {}
    _thread_from_merge(store, rows, _args(repo="e:\\repo\\explicit\\"), info=info)
    assert "repo_warning" not in info
    assert store.thread_get(store.thread_find_by_members({"codex:1", "claude:2"}))["repo_root"] == "E:/repo/EXPLICIT"


def test_explicit_repo_tilde_is_expanded(store, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    rows = [("codex:tilde", "codex", "E:/repo/OLD")]
    _sessions(store, rows)
    info = {}
    tid = _thread_from_merge(store, [store.session("codex:tilde")[0]],
                             _args(repo="~/project"), info=info)
    assert store.thread_get(tid)["repo_root"] == str(tmp_path / "project")
    assert info["repo_source"] == "explicit"


# --- AC5: single-repo, no --repo unchanged -------------------------------

def test_single_repo_no_flag_unchanged(store):
    rows = [("codex:only", "codex", "E:/repo/ONE")]
    _sessions(store, rows)
    r = [store.session("codex:only")[0]]
    info = {}
    tid = _thread_from_merge(store, r, _args(repo=None), info=info)
    assert store.thread_get(tid)["repo_root"] == "E:/repo/ONE"
    assert info["thread_action"] == "created"


# --- AC6: JSON output exposes repo_root + warning, no schema change ------

def test_cli_merge_json_exposes_repo_info(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    db = tmp_path / "cli.db"
    s = Store(db); _sessions(s, ROWS); s.close()
    out = tmp_path / "bundle.md"
    rc = main(["--db", str(db), "merge", "codex:1", "claude:2",
               "--repo", "E:/repo/EXPLICIT", "--json", "-o", str(out)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["repo_root"] == "E:/repo/EXPLICIT"
    assert payload["repo_source"] == "explicit"
    assert payload["thread_action"] == "created"
    assert BRIDGE_SCHEMA_VERSION == 1


def test_no_store_or_bridge_schema_change():
    import inspect
    import voyager.store as smod
    # The flag is read off args only; no Store method signature changed.
    sig = inspect.signature(_thread_from_merge)
    assert "info" in sig.parameters
    assert BRIDGE_SCHEMA_VERSION == 1
    assert "repo" not in inspect.signature(smod.Store.thread_create).parameters
