"""Grok zero-touch startup continuity (native surfaces).

Two contracts, both load-bearing for the Claude → Grok leg and both previously
untested:

1. **A plain Grok launch leaves a resumable pending record.** The record must be
   produced by the Grok startup entrypoint itself — not by a hand-written
   ``pending_record()`` in the test — or the test would keep passing while the
   production path stayed dead. That is exactly the hole the Claude chain had:
   the status string ``pending_resolve`` was asserted while nothing backed it.

2. **The path a plain ``grok`` takes carries the continuation context.**
   ``SessionStart`` is a *passive* event (Grok ignores its stdout) and
   ``UserPromptSubmit`` discards ``additionalContext``, so a session-start hook
   cannot inject anything. The only channel that reaches an interactive session
   is a rules file written *before* the CLI starts, which is what the installed
   launcher does. Testing ``grok "<prompt>"`` instead would prove nothing: that
   is ``--prompt-file``, a single-turn path the interactive case never uses.

Both tests are mutation-verified: deleting the pending write, or deleting the
context write from the launcher path, turns the matching test red.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from voyager.cli import main, run_scan
from voyager.integrations import grok_native
from voyager.model import new_event, new_session
from voyager.store import Store

#: A real Grok session id, in the shape the CLI hands a SessionStart hook.
GROK_SID = "01a0d3f0-48db-7a93-8aa7-a1ae3c21a9de"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "grok-native.db")
    yield s
    s.close()


@pytest.fixture(scope="module")
def git_repo(tmp_path_factory):
    """A real git repo, so `git_info()` yields the production `repo_root`."""
    repo = tmp_path_factory.mktemp("groknative") / "workrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)],
                   capture_output=True, timeout=60)
    return repo


def _seed_session(store, sid, provider, native, repo, born, content="work"):
    src = store.db_path.parent / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    s = new_session(id=sid, provider=provider, native_session_id=native,
                    title=native, started_at=born, updated_at=born, cwd=repo,
                    repo_root=repo, message_count=1)
    store.replace_session(
        s,
        [new_event(sid=sid, ts=born, seq=0, kind="user", content=content)],
        provider, src,
    )
    return sid


def _write_grok_transcript(sessions_root, repo, native, born):
    """Put a Grok transcript on disk, in the layout the adapter discovers.

    The chain under test is "the CLI wrote its own transcript, a later scan
    noticed it". Seeding the session straight into the index instead would skip
    the adapter and, worse, the grok adapter prunes every grok session it owns
    when it discovers no sources — so the seeded row would vanish mid-test.
    """
    cwd = repo.replace("\\", "/")
    sdir = sessions_root / quote(cwd, safe="") / native
    sdir.mkdir(parents=True, exist_ok=True)
    iso = datetime.fromtimestamp(born, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    (sdir / "summary.json").write_text(json.dumps({
        "info": {"id": native, "cwd": cwd},
        "session_summary": "grok continuation",
        "created_at": iso,
        "updated_at": iso,
        "num_messages": 1,
        "current_model_id": "grok-4.6",
        "git_root_dir": cwd,
    }), encoding="utf-8")
    (sdir / "chat_history.jsonl").write_text(
        json.dumps({"type": "user",
                    "content": [{"type": "text", "text": "continue"}]}) + "\n",
        encoding="utf-8",
    )
    return sdir


# ---------------------------------------------------------------------------
# 1. Grok native start → pending → discovery → attach
# ---------------------------------------------------------------------------

def test_grok_session_start_records_a_pending_the_scan_then_resolves(
        store, tmp_path, git_repo, adapter_of, patch_paths):
    """The whole chain, driven the way it actually happens.

    `session_start()` is the body of the Grok SessionStart hook; it is the only
    thing that may write the pending. The scan that later picks the session up
    runs in a different process, so the row has to survive a store restart, the
    real grok adapter has to discover the transcript, and the resolver has to be
    reached through `run_scan()`.
    """
    repo = str(git_repo)
    tid = store.thread_create(repo_root=repo, title="grok native start")

    result = grok_native.session_start(session_id=GROK_SID, cwd=repo,
                                      store=store)

    assert result["pending_recorded"] is True, result
    assert result["context_source"] == "none", (
        "a passive hook cannot deliver a bundle, so it must not compile one"
    )
    rows = store.pending_open(thread_id=tid)
    assert len(rows) == 1, "the reported pending must correspond to an open row"
    pend = rows[0]
    assert pend["provider"] == "grok"
    assert pend["native_session_id"] == GROK_SID, (
        "an identity match is what keeps the resolver off the uniqueness guess"
    )
    assert pend["status"] == "open"
    assert pend["repo_root"], "the resolver matches on repo; it must be recorded"
    # the session genuinely is not indexed yet — that is *why* it is pending
    assert store.q("SELECT 1 FROM sessions WHERE id=?",
                   ("grok:" + GROK_SID,)) == []
    store.close()   # the hook's process exits

    # a later process: Grok has written its transcript and the scan finds it
    sessions_root = tmp_path / "grok-sessions"
    _write_grok_transcript(sessions_root, repo, GROK_SID, time.time())
    store2 = Store(tmp_path / "grok-native.db")
    ad = adapter_of("grok")
    patch_paths(ad, SESSIONS_DIR=sessions_root)
    run_scan(store2, providers=["grok"], quiet=True)

    assert store2.q("SELECT 1 FROM sessions WHERE id=?",
                    ("grok:" + GROK_SID,)), "the adapter must have indexed it"
    assert store2.thread_member_ids(tid) == ["grok:" + GROK_SID]
    row = store2.q("SELECT status, resolved_sid FROM thread_pending "
                   "WHERE thread_id=?", (tid,))[0]
    assert row["status"] == "resolved"
    assert row["resolved_sid"] == "grok:" + GROK_SID


# ---------------------------------------------------------------------------
# 2. Plain `grok` carries the continuation context
# ---------------------------------------------------------------------------

def test_plain_grok_launch_path_carries_the_continuation_context(
        store, tmp_path, git_repo, monkeypatch):
    """`grok` with no arguments must still start with the WorkThread context.

    Drives the exact command the installed launcher runs, then checks that the
    launcher it installs still runs it. The second half is the part that matters
    for the interactive case: a correct writer nobody calls is not continuity.
    """
    repo = str(git_repo)
    tid = store.thread_create(repo_root=repo, title="carry the context")
    _seed_session(store, "claude:aaa", "claude", "aaa", repo, time.time(),
                  content="SENTINEL-CONTINUATION-PAYLOAD")
    store.thread_attach(tid, "claude:aaa")

    home = tmp_path / "userhome"
    rc = main(["hook", "grok-context", "--cwd", repo, "--home", str(home),
               "--db", str(store.db_path), "--quiet"])
    assert rc == 0

    rules = grok_native.context_rules_path(home)
    assert rules.is_file(), "plain `grok` would start with no context at all"
    body = rules.read_text(encoding="utf-8")
    assert grok_native.RULES_MARKER in body
    assert tid in body
    assert "SENTINEL-CONTINUATION-PAYLOAD" in body

    # A launch in an unrelated repo must not inherit this context.
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert main(["hook", "grok-context", "--cwd", str(other),
                 "--home", str(home), "--db", str(store.db_path),
                 "--quiet"]) == 0
    assert not rules.exists(), "stale context must be cleared, not injected"

    # A *sync* may refresh the rule but must never remove it. `voyager watch`
    # is started from the Startup folder, so its cwd is not the repo the user is
    # in; if a scan cleared the rule on "no thread for *my* cwd" it would delete
    # the context the handoff depends on once per interval, and the next plain
    # `grok` would start with nothing.
    #
    # `run_scan` takes no `--home`, so it resolves the rules dir from the
    # environment: pointing `GROK_HOME` at this test's home is both the
    # isolation seam and exactly what the watcher resolves against.
    monkeypatch.setenv("GROK_HOME", str(home / ".grok"))
    assert main(["hook", "grok-context", "--cwd", repo, "--home", str(home),
                 "--db", str(store.db_path), "--quiet"]) == 0
    assert rules.is_file()
    assert main(["--db", str(store.db_path), "scan",
                 "--platform", "grok"]) == 0
    assert rules.is_file(), (
        "a background sync deleted the continuation rule for a repo it was "
        "not in -- the next `grok` would start with no context"
    )

    # ...and the installed launcher must actually run the writer.
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/opt/grok/bin/grok" if name == "grok" else None,
    )
    from voyager.integrations.grok import GrokIntegration

    GrokIntegration(home=home).install()
    shim = (home / ".voyager/bin/grok").read_text(encoding="utf-8")
    win = (home / ".voyager/bin/grok.cmd").read_text(encoding="utf-8")
    assert "hook grok-context" in shim
    assert "hook grok-context" in win


# ---------------------------------------------------------------------------
# 3. A scan refreshes the rule without re-entering context compilation
# ---------------------------------------------------------------------------

def test_a_scan_refreshes_the_rule_without_re_entering_compilation(
        store, git_repo, monkeypatch):
    """The refresh belongs to the scan *command*, not to `run_scan()`.

    `run_scan()` is a core primitive that context compilation calls back into
    (`auto.get_continuation_context(sync=True)`). The Grok refresh compiles
    context itself, so doing it from inside `run_scan()` closed a cycle::

        run_scan -> write_context_rules -> startup_continuity -> compile
                 -> get_continuation_context -> run_scan -> ...

    With a stale cache that recursed until the stack ran out, so `voyager scan`
    never finished -- and neither did any Claude SessionStart that had to
    compile, because that hook reaches the same primitive.

    The rest of the suite cannot see it: conftest sets `VOYAGER_NO_SYNC`, which
    makes `get_continuation_context` step over the scan and the cycle with it.
    Counting the writer's calls catches it in milliseconds instead of by
    waiting for a scan to hang.
    """
    repo = str(git_repo)
    store.thread_create(repo_root=repo, title="scan re-entrancy")
    monkeypatch.chdir(repo)

    seen = []

    def fake_write_context_rules(**kwargs):
        seen.append(kwargs)
        return {"status": "skipped"}

    monkeypatch.setattr(
        "voyager.integrations.grok_native.write_context_rules",
        fake_write_context_rules)

    # the primitive has to stay re-entrant ...
    run_scan(store, providers=["grok"], quiet=True)
    assert seen == [], (
        "run_scan() called back into context compilation; a scan that has to "
        "compile never terminates"
    )

    # ... and the command is where the refresh happens, exactly once
    assert main(["--db", str(store.db_path), "scan",
                 "--platform", "grok"]) == 0
    assert len(seen) == 1, "the sync command must refresh the rule"
