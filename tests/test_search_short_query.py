"""Short phrases: the trigram index cannot see them, the fallback must.

FTS5's trigram tokenizer indexes 3-character windows, so MATCH on a 1- or
2-character phrase returns 0 rows *with no error*.  Two-character CJK words
("补丁", "账本") are the most common Chinese query, so "0 hits" read as "this
word is not in the index" -- reported against voyager 1.3.0 on a 469k-event
index, where the true count was 5,178.

Two properties matter: the fallback must select the same rows the index would
have selected (same source text), and it must not trade correctness for speed
(no ordering by rowid).
"""

from __future__ import annotations

import pytest

from voyager.model import new_event, new_session
from voyager.store import Store

NOW = 1_790_000_000.0


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "index.db"
    store = Store(path)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")

    def seed(sid, provider, events, repo="E:/p"):
        sess = new_session(id=sid, provider=provider, native_session_id=sid,
                           title=sid, started_at=NOW - 1000, updated_at=NOW,
                           repo_root=repo, cwd=repo)
        store.replace_session(sess, events, provider, src)

    seed("codex:a", "codex", [
        new_event(sid="codex:a", seq=1, kind="user", ts=NOW - 900,
                  content="修复补丁已完成", origin="human"),
        new_event(sid="codex:a", seq=2, kind="assistant", ts=NOW - 800,
                  content="账本对不上，需要三重闸", origin="provider_system"),
    ])
    seed("claude:b", "claude", [
        new_event(sid="claude:b", seq=1, kind="user", ts=NOW - 100,
                  content="abc def 补丁", origin="human"),
    ])
    seed("claude:c", "claude", [
        new_event(sid="claude:c", seq=1, kind="user", ts=NOW - 50,
                  content="補丁 한국 テスト", origin="human"),
    ], repo="E:/q")
    yield store
    store.close()


def test_two_character_cjk_finds_rows(world):
    rows = world.search("补丁")
    assert len(rows) == 2


def test_single_character_finds_rows(world):
    assert len(world.search("账")) == 1


def test_two_character_ascii_finds_rows(world):
    assert len(world.search("ab")) == 1


def test_three_characters_still_use_fts(world):
    """The fallback must not swallow the path that already works."""
    assert len(world.search("三重闸")) == 1
    assert len(world.search("账本对")) == 1


def test_snippet_uses_the_same_marker_shape(world):
    rows = world.search("补丁")
    assert rows, "fallback returned nothing"
    for r in rows:
        assert ">>>补丁<<<" in r["snippet"]


def test_short_results_are_newest_first(world):
    """ORDER BY rowid would return the first-inserted row and look fast.

    The newest matching event is claude:b at NOW-100; the codex one is at
    NOW-900.  A rowid-ordered LIMIT 1 would answer with the wrong session.
    """
    rows = world.search("补丁", limit=1)
    assert len(rows) == 1
    assert rows[0]["id"] == "claude:b"


def test_short_query_honours_filters(world):
    assert len(world.search("补丁", filters={"providers": ["claude"]})) == 1
    assert len(world.search("补丁", filters={"providers": ["codex"]})) == 1
    assert len(world.search("补丁", filters={"kinds": ["assistant"]})) == 0


def test_wildcards_are_literal_text(world):
    """`%` is a LIKE wildcard; the query is literal text, so it must not match."""
    assert world.search("%") == []
    assert world.search("_") == []


def test_japanese_and_korean_two_characters(world):
    """The trigram limit is about characters, not bytes or script."""
    assert len(world.search("補丁")) == 1      # Japanese kanji, 2 chars
    assert len(world.search("한국")) == 1      # Hangul, 2 chars


def test_repo_filter_narrows_short_results(world):
    """Filters must narrow the fallback exactly as they narrow FTS."""
    assert len(world.search("補丁", filters={"repo": "E:/q"})) == 1
    assert world.search("補丁", filters={"repo": "E:/p"}) == []


def test_empty_query_does_not_return_everything(world):
    assert world.search("") == []
