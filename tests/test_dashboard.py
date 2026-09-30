"""The local dashboard: one self-contained page, and nothing surprising in it.

Two properties matter and are easy to lose: it must not reach for the network
(no CDN, no fonts, no telemetry -- a local dashboard that phones home is not a
local dashboard), and everything it prints must be escaped, because session
content is arbitrary text that ends up inside HTML.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from voyager import dashboard as dash
from voyager.model import new_event, new_session
from voyager.store import Store


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "index.db"
    store = Store(path)
    tid = store.thread_create(repo_root="E:/repo", title="Panel test", goal="see it")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    sess = new_session(id="codex:p1", provider="codex", native_session_id="p1",
                       title="p1", started_at=time.time() - 10,
                       updated_at=time.time(), repo_root="E:/repo", cwd="E:/repo")
    store.replace_session(sess, [
        new_event(sid="codex:p1", seq=1, kind="user", ts=time.time() - 9,
                  content="do the thing"),
        new_event(sid="codex:p1", seq=2, kind="assistant", ts=time.time() - 8,
                  content="did the thing"),
    ], "codex", src)
    store.thread_attach(tid, "codex:p1")
    yield store, tid
    store.close()


def test_build_has_every_panel(world):
    store, tid = world
    data = dash.build(store, repo="E:/repo")
    for key in ("generated_at", "projects", "recent", "health", "checkpoints",
                "focus_thread", "focus"):
        assert key in data, key
    assert data["focus_thread"] == tid
    assert data["recent"], "the seeded turns should appear"


def test_the_page_is_self_contained(world):
    """No network, by construction."""
    store, _ = world
    page = dash.render_html(dash.build(store))
    for pattern in (r"https?://", r"src\s*=\s*['\"]//", r"@import"):
        assert not re.search(pattern, page), pattern
    assert "<script src" not in page
    assert page.count("<script") == 1, "one inline script, no libraries"


def test_the_page_declares_utf8(world):
    store, _ = world
    page = dash.render_html(dash.build(store))
    assert "charset='utf-8'" in page or 'charset="utf-8"' in page


def test_session_content_is_escaped(world):
    """Session text is arbitrary; it must never become markup."""
    store, tid = world
    src = store.db_path.parent / "x.jsonl"
    src.write_text("{}", encoding="utf-8")
    hostile = "<script>alert('x')</script> & <b>bold</b>"
    sess = new_session(id="codex:evil", provider="codex", native_session_id="evil",
                       title="evil", started_at=time.time(), updated_at=time.time(),
                       repo_root="E:/repo", cwd="E:/repo")
    store.replace_session(sess, [
        new_event(sid="codex:evil", seq=1, kind="assistant", ts=time.time(),
                  content=hostile)], "codex", src)
    store.thread_attach(tid, "codex:evil")

    page = dash.render_html(dash.build(store))
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "&amp;" in page


def test_a_hostile_thread_title_is_escaped(world):
    store, _ = world
    store.thread_create(repo_root="E:/x", title="<img src=x onerror=alert(1)>",
                        goal="g")
    page = dash.render_html(dash.build(store))
    assert "<img src=x" not in page
    assert "&lt;img" in page


def test_build_never_raises_on_an_empty_index(tmp_path):
    store = Store(tmp_path / "empty.db")
    try:
        data = dash.build(store)
        assert data["projects"] == []
        page = dash.render_html(data)
        assert "no projects indexed yet" in page
    finally:
        store.close()


def test_json_view_is_serialisable(world):
    store, _ = world
    json.dumps(dash.build(store), ensure_ascii=False, default=str)


def test_write_creates_the_file(world, tmp_path):
    store, _ = world
    out = dash.write(store, tmp_path / "sub" / "dash.html")
    assert out.is_file()
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_the_dashboard_reports_the_same_provider_states_as_doctor(world):
    """The panel must not tell a different story from the CLI."""
    from voyager.doctor import run as doctor_run

    store, _ = world
    data = dash.build(store)
    health = doctor_run()
    for p, v in health["providers"].items():
        assert data["health"]["providers"][p]["state"] == v["state"], p


def test_the_focus_falls_back_to_the_most_recent_thread(tmp_path):
    store = Store(tmp_path / "f.db")
    try:
        old = store.thread_create(repo_root="E:/a", title="old", goal="g")
        store.con.execute("UPDATE threads SET updated_at=? WHERE id=?",
                          (time.time() - 1000, old))
        new = store.thread_create(repo_root="E:/b", title="new", goal="g")
        store.con.commit()
        data = dash.build(store)
        assert data["focus_thread"] == new
    finally:
        store.close()
