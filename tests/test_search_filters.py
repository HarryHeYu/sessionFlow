"""Search filters: narrow where to look, never what the phrase means.

The search itself is deliberately dumb -- the user's text is one quoted FTS5
phrase, because `pytest -q` and `"unbalanced` are ordinary text to a human and
operators to FTS5.  Filters are the structured half: provider, repo, time range,
event kind, tool, file, and provenance.

Two properties matter and are tested here: a filter can only ever *narrow* a
result, and combining filters is a conjunction.
"""

from __future__ import annotations

import json
import time

import pytest

from voyager.cli import _parse_when, main
from voyager.model import new_event, new_session
from voyager.store import Store

NOW = 1_790_000_000.0


@pytest.fixture
def world(tmp_path):
    """Two sessions, different providers, repos, kinds and provenance."""
    path = tmp_path / "index.db"
    store = Store(path)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")

    def seed(sid, provider, repo, events):
        sess = new_session(id=sid, provider=provider, native_session_id=sid,
                           title=sid, started_at=NOW - 1000, updated_at=NOW,
                           repo_root=repo, cwd=repo)
        store.replace_session(sess, events, provider, src)

    seed("codex:a", "codex", "E:/alpha", [
        new_event(sid="codex:a", seq=1, kind="user", ts=NOW - 900,
                  content="find the needle", origin="human"),
        new_event(sid="codex:a", seq=2, kind="assistant", ts=NOW - 800,
                  content="needle found"),
        new_event(sid="codex:a", seq=3, kind="tool_call", ts=NOW - 700,
                  content="grep needle", tool_name="shell",
                  file_path="src/needle.py"),
    ])
    seed("claude:b", "claude", "E:/beta", [
        new_event(sid="claude:b", seq=1, kind="user", ts=NOW - 600,
                  content="a needle in beta", origin="provider_bootstrap"),
        new_event(sid="claude:b", seq=2, kind="assistant", ts=NOW - 500,
                  content="needle handled"),
    ])
    yield store
    store.close()


# --- each filter narrows ----------------------------------------------------

def test_no_filters_is_the_superset(world):
    all_hits = world.search("needle", limit=50)
    for f in ({"providers": ["codex"]}, {"repo": "alpha"}, {"kinds": ["user"]},
              {"human_only": True}, {"tool": "shell"}, {"file": "needle.py"}):
        narrowed = world.search("needle", limit=50, filters=f)
        assert len(narrowed) <= len(all_hits), f
        assert narrowed, f


def test_provider_filter(world):
    rows = world.search("needle", limit=50, filters={"providers": ["codex"]})
    assert rows and {r["provider"] for r in rows} == {"codex"}


def test_repo_filter(world):
    rows = world.search("needle", limit=50, filters={"repo": "beta"})
    assert rows and all("beta" in (r["repo_root"] or "") for r in rows)


def test_kind_filter(world):
    rows = world.search("needle", limit=50, filters={"kinds": ["assistant"]})
    assert rows and {r["_kind"] for r in rows} == {"assistant"}


def test_human_only_excludes_injected_turns(world):
    """The provenance filter is the point: a provider's own bootstrap text is a
    user-role record that no human wrote."""
    all_rows = world.search("needle", limit=50)
    human = world.search("needle", limit=50, filters={"human_only": True})
    assert human, "the human turn must survive"
    assert all(r["_origin"] == "human" for r in human)
    assert len(human) < len(all_rows)


def test_origin_filter(world):
    rows = world.search("needle", limit=50,
                        filters={"origins": ["provider_bootstrap"]})
    assert rows and all(r["_origin"] == "provider_bootstrap" for r in rows)


def test_tool_and_file_filters(world):
    rows = world.search("needle", limit=50, filters={"tool": "shell"})
    assert rows and all("shell" in (r["_tool"] or "") for r in rows)
    rows = world.search("needle", limit=50, filters={"file": "needle.py"})
    assert rows and all("needle.py" in (r["_file"] or "") for r in rows)


def test_time_range_filters(world):
    recent = world.search("needle", limit=50, filters={"since": NOW - 750})
    assert recent and all(r["_ts"] >= NOW - 750 for r in recent)
    older = world.search("needle", limit=50, filters={"until": NOW - 750})
    assert older and all(r["_ts"] <= NOW - 750 for r in older)
    # the two windows are disjoint by timestamp, which is what matters (both
    # hits come from the same session, so comparing sids would prove nothing)
    assert max(r["_ts"] for r in older) <= NOW - 750
    assert min(r["_ts"] for r in recent) >= NOW - 750


def test_filters_are_a_conjunction(world):
    both = world.search("needle", limit=50,
                        filters={"providers": ["codex"], "kinds": ["user"]})
    assert both
    assert {r["provider"] for r in both} == {"codex"}
    assert {r["_kind"] for r in both} == {"user"}
    assert len(both) <= len(world.search("needle", limit=50,
                                         filters={"providers": ["codex"]}))


def test_a_filter_that_matches_nothing_returns_nothing(world):
    assert world.search("needle", limit=50,
                        filters={"providers": ["grok"]}) == []


def test_the_phrase_semantics_are_unchanged_by_filters(world):
    """A filter changes where we look, never what the text means: the same query
    returns the same matches, restricted."""
    plain = world.search("needle", limit=50)
    filtered = world.search("needle", limit=50, filters={"providers": ["codex"]})
    assert filtered, "the query still matches"
    assert {r["_sid"] for r in filtered} <= {r["_sid"] for r in plain}
    # and a multi-word phrase is still one phrase: it does not become an OR
    two = world.search("needle handled", limit=50)
    assert all("needle" in (r["snippet"] or "") for r in two)


# --- time parsing -----------------------------------------------------------

def test_parse_when_accepts_dates_and_relative_days():
    assert _parse_when(None) is None
    assert _parse_when("") is None
    assert abs(_parse_when("7d") - (time.time() - 7 * 86400)) < 5
    day = _parse_when("2026-09-30")
    assert day is not None and 1_700_000_000 < day < 1_900_000_000


def test_parse_when_rejects_nonsense():
    with pytest.raises(ValueError):
        _parse_when("nonsense")


# --- the CLI ----------------------------------------------------------------

def test_cli_filters_and_json(tmp_path, capsys):
    path = tmp_path / "cli.db"
    store = Store(path)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    sess = new_session(id="codex:z", provider="codex", native_session_id="z",
                       title="z", started_at=NOW, updated_at=NOW,
                       repo_root="E:/z", cwd="E:/z")
    store.replace_session(sess, [
        new_event(sid="codex:z", seq=1, kind="user", ts=NOW,
                  content="zzz needle", origin="human")], "codex", src)
    store.close()

    capsys.readouterr()
    assert main(["--db", str(path), "search", "needle",
                 "--provider", "codex", "--human-only", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload and payload[0]["provider"] == "codex"


def test_cli_reports_a_bad_time_instead_of_crashing(tmp_path, capsys):
    path = tmp_path / "cli2.db"
    Store(path).close()
    rc = main(["--db", str(path), "search", "x", "--since", "nonsense"])
    out = capsys.readouterr()
    assert rc == 2
    assert "unrecognised time" in out.err
