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


# --- continuation preview on the page ---------------------------------------
# The Core already compiles this; the page is a consumer like any other (O3).


@pytest.fixture
def two_threads(tmp_path):
    path = tmp_path / "two.db"
    store = Store(path)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    repo = "E:/repo/two"
    ids = {}
    for prov, tag, title, ts in (("codex", "a", "Task one", 1000.0),
                                 ("claude", "b", "Task two", 2000.0)):
        sid = "%s:%s" % (prov, tag)
        store.replace_session(
            new_session(id=sid, provider=prov, native_session_id=tag, title=title,
                        started_at=ts, updated_at=ts, repo_root=repo, cwd=repo,
                        message_count=2, can_resume=True),
            [new_event(sid=sid, ts=ts, seq=1, kind="user", content=title),
             new_event(sid=sid, ts=ts + 1, seq=2, kind="assistant",
                       content=title + ": still open")],
            prov, src)
        tid = store.thread_create(repo_root=repo, title=title, goal=title + " goal")
        store.thread_attach(tid, sid)
        ids[title] = tid
    yield store, ids, repo
    store.close()


def test_page_shows_a_preview_per_workthread(two_threads):
    store, ids, repo = two_threads
    data = dash.build(store, repo=repo)
    previews = data["previews"]
    assert set(previews) == set(ids.values()), "one preview per thread"
    for tid, pv in previews.items():
        assert pv["selected"], "the sessions it would continue from"
        assert pv["tokens"], "the context budget it would cost"
        assert "still open" in pv["bundle"], "open items survive into the preview"


def test_page_states_multi_thread_ambiguity_and_selects_nothing(two_threads):
    """A list of active threads must not read as a default."""
    store, ids, repo = two_threads
    data = dash.build(store, repo=repo)
    html = dash.render_html(data)

    assert data["ambiguous_threads"], "the page knows two are active"
    assert "nothing is selected for you" in html
    assert ids["Task one"] in html and ids["Task two"] in html, (
        "both are named so the reader can choose"
    )
    # The prompt links to the preview section rather than to a default choice.
    assert "Ambiguity" not in html.split("nothing is selected")[0][-200:]
    assert "voyager continue --thread" in html


def test_preview_is_read_only(two_threads):
    """Building the page must not lease, attach or write a bundle."""
    import sqlite3

    store, ids, repo = two_threads
    before = {}
    con = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    for table in ("thread_leases", "thread_pending"):
        try:
            before[table] = con.execute(
                "select count(*) from " + table).fetchone()[0]
        except sqlite3.Error:
            before[table] = None
    con.close()

    dash.build(store, repo=repo)

    con = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    for table, n in before.items():
        if n is None:
            continue
        assert con.execute(
            "select count(*) from " + table).fetchone()[0] == n, (
            "a preview wrote into " + table
        )
    con.close()


def test_preview_shows_the_exact_command_to_run(two_threads):
    store, ids, repo = two_threads
    html = dash.render_html(dash.build(store, repo=repo))
    for tid in ids.values():
        assert "voyager continue --thread %s" % tid in html


def test_bundle_is_rendered_as_text_not_markup(world):
    """Session content is arbitrary text; the bundle quotes what an agent said."""
    store, tid = world
    html = dash.render_html(dash.build(store))
    assert "<pre" in html, "the preview is inside a <pre>"
    # the XSS payload already used by the escaping test must not appear raw
    assert "<img src=x onerror" not in html


def test_many_sessions_do_not_grow_the_page(tmp_path):
    """A big index must not become a big page: previews are bounded."""
    store = Store(tmp_path / "many.db")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    repo = str(tmp_path / "proj")
    for i in range(120):
        sid = "codex:m%d" % i
        store.replace_session(
            new_session(id=sid, provider="codex", native_session_id="m%d" % i,
                        title="s%d" % i, started_at=1000 + i, updated_at=1000 + i,
                        repo_root=repo, cwd=repo, message_count=1,
                        can_resume=True),
            [new_event(sid=sid, ts=1000 + i, seq=1, kind="user",
                       content="task %d" % i)],
            "codex", src)
        store.thread_create(repo_root=repo, title="t%d" % i, goal="g%d" % i)
    html = dash.render_html(dash.build(store, repo=repo))
    # one preview per shown thread, and the page is still a small file
    assert len(html) < 200_000, "a 120-session index should not become a huge page"
    assert html.count("<div class='panel'") >= 1
    store.close()


# --- Session Explorer (P22) -------------------------------------------------

@pytest.fixture
def detail_world(tmp_path):
    """A session with tool calls, an empty one, a hostile one, a long one."""
    path = tmp_path / "d.db"
    store = Store(path)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    repo = "E:/repo/detail"
    XSS = "<img src=x onerror=alert(1)><script>alert(2)</script>"
    LONG = "y" * 9000

    def put(prov, tag, title, ts, evs):
        sid = "%s:%s" % (prov, tag)
        store.replace_session(
            new_session(id=sid, provider=prov, native_session_id=tag, title=title,
                        started_at=ts, updated_at=ts, repo_root=repo, cwd=repo,
                        message_count=len(evs), can_resume=True), evs, prov, src)
        return sid

    full = put("codex", "full", "Fix quoting", 1000, [
        new_event(sid="codex:full", ts=1000, seq=0, kind="user",
                  content="修复 CSV 引号"),
        new_event(sid="codex:full", ts=1001, seq=1, kind="assistant",
                  content="checking"),
        new_event(sid="codex:full", ts=1002, seq=2, kind="tool_call",
                  tool_name="exec", command="pytest -q", tool_call_id="t1"),
        new_event(sid="codex:full", ts=1003, seq=3, kind="tool_result",
                  role="tool", tool_call_id="t1", exit_code=1,
                  tool_output="1 failed"),
    ])
    empty = put("dsh", "empty", "no content", 2000, [])
    evil = put("claude", "evil", XSS, 3000, [
        new_event(sid="claude:evil", ts=3000, seq=0, kind="user", content=XSS),
        new_event(sid="claude:evil", ts=3001, seq=1, kind="assistant", content=LONG),
    ])
    tid = store.thread_create(repo_root=repo, title="T", goal="g")
    store.thread_attach(tid, full)
    store.thread_attach(tid, evil)
    yield store, repo, {"full": full, "empty": empty, "evil": evil, "tid": tid}
    store.close()


def test_every_session_is_reachable_even_an_empty_one(detail_world):
    """An empty session has no user/assistant text, so the recent panel omits it
    -- which used to make it unreachable from the page entirely."""
    store, repo, ids = detail_world
    data = dash.build(store, repo=repo)
    assert set(data["details"]) >= set(ids.values()) - {ids["tid"]}, (
        "the empty session must still be openable"
    )


def test_detail_carries_the_whole_conversation(detail_world):
    store, repo, ids = detail_world
    data = dash.build(store, repo=repo)
    d = data["details"][ids["full"]]
    assert [t["kind"] for t in d["turns"]] == ["user", "assistant"]
    assert d["turns"][0]["text"] == "修复 CSV 引号", "non-ASCII intact"
    assert d["provider"] == "codex"
    assert d["repo"] == "E:/repo/detail"
    assert d["threads"] == [ids["tid"]]


def test_tool_results_are_joined_to_their_calls(detail_world):
    """The exit code must be shown next to the command, or a failed call reads
    as a successful one."""
    store, repo, ids = detail_world
    data = dash.build(store, repo=repo)
    tools = data["details"][ids["full"]]["tools"]
    assert len(tools) == 1
    assert tools[0]["command"] == "pytest -q"
    assert tools[0]["exit_code"] == 1, "exit code joined by call id"
    assert tools[0]["output"] == "1 failed"


def test_long_bodies_are_truncated_and_flagged(detail_world):
    store, repo, ids = detail_world
    data = dash.build(store, repo=repo)
    turns = data["details"][ids["evil"]]["turns"]
    long_one = [t for t in turns if t["text"].startswith("yyy")]
    assert long_one and long_one[0]["truncated"], "must say it is truncated"
    assert len(long_one[0]["text"]) <= dash.DETAIL_BODY + 1


def test_session_detail_escapes_everything(detail_world):
    store, repo, ids = detail_world
    html = dash.render_html(dash.build(store, repo=repo))
    assert "<img src=x onerror" not in html
    assert "<script>alert" not in html
    assert "&lt;img" in html


def test_commands_are_not_rendered_as_controls(detail_world):
    """A tool call shown as a button would be remote code in a local file."""
    store, repo, ids = detail_world
    html = dash.render_html(dash.build(store, repo=repo))
    assert "<button" not in html
    assert "javascript:" not in html
    assert "onclick" not in html


def test_search_states_its_own_scope(detail_world):
    store, repo, ids = detail_world
    html = dash.render_html(dash.build(store, repo=repo))
    # a snapshot filter must not be described as whole-index search
    assert "snapshot" in html
    assert "voyager search &lt;term&gt;" in html
    assert "id='qnote'" in html, "a place for the no-result explanation"


def test_session_id_is_queryable_and_turns_link_to_detail(detail_world):
    store, repo, ids = detail_world
    html = dash.render_html(dash.build(store, repo=repo))
    assert "data-sid=" in html
    assert "href='#s-%s'" % ids["full"] in html
    assert "id='s-%s'" % ids["full"] in html


def test_page_size_is_flat_as_the_index_grows(tmp_path):
    """1000 sessions must not become a 1000-session page."""
    store = Store(tmp_path / "big.db")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    repo = str(tmp_path / "proj")
    for i in range(300):
        sid = "codex:b%d" % i
        store.replace_session(
            new_session(id=sid, provider="codex", native_session_id="b%d" % i,
                        title="s%d" % i, started_at=1000 + i,
                        updated_at=1000 + i, repo_root=repo, cwd=repo,
                        message_count=2, can_resume=True),
            [new_event(sid=sid, ts=1000 + i, seq=0, kind="user",
                       content="task %d" % i),
             new_event(sid=sid, ts=1000 + i, seq=1, kind="assistant",
                       content="done %d" % i)],
            "codex", src)
    html = dash.render_html(dash.build(store, repo=repo))
    assert len(html) < 400_000, "page should stay a single small file"
    assert html.count("<div class='panel'") >= 1
    store.close()
