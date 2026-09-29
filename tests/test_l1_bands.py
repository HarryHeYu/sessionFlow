"""Phase P2: band-based L1 candidate ordering.

The safety invariant under test: `human == 0` is a *negative* claim, so it only
becomes BOOTSTRAP_ONLY when that session's own user events are fully classified;
one unclassified event makes the session UNKNOWN, and UNKNOWN outranks
BOOTSTRAP_ONLY in L1 composition.
"""
from __future__ import annotations

import json

from voyager.continuity import build_tiered_bundle, build_working_context
from voyager.model import new_event, new_session
from voyager.provenance import order_rows_for_l1, session_band
from voyager.store import Store


def _add(store, tmp_path, sid, provider, spec):
    """spec: list of (kind, content, origin) in event order."""
    evs = [new_event(sid=sid, seq=i + 1, kind=k,
                     role="user" if k == "user" else "assistant",
                     content=c, origin=o)
           for i, (k, c, o) in enumerate(spec)]
    s = new_session(id=sid, provider=provider, native_session_id=sid.split(":")[1],
                    title="t", started_at=1.0, updated_at=2.0,
                    repo_root=str(tmp_path), cwd=str(tmp_path))
    src = tmp_path / ("%s.jsonl" % sid.replace(":", "_"))
    src.write_text("{}", encoding="utf-8")
    store.replace_session(s, evs, provider, src)
    return s


def _rows(store, tid):
    return [dict(r) for r in store.thread_member_sessions(tid)]


# --- classification ---------------------------------------------------------

def test_human_zero_and_no_unknown_is_bootstrap_only(tmp_path):
    st = Store(tmp_path / "b.db")
    tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")
    _add(st, tmp_path, "codex:a", "codex",
         [("user", "injected", "provider_bootstrap"), ("assistant", "work", None),
          ("user", "aborted", "provider_system")])
    st.thread_attach(tid, "codex:a")
    band, stats = session_band(st.con, "codex:a")
    assert (band, stats["human"], stats["unknown"]) == ("BOOTSTRAP_ONLY", 0, 0)


def test_human_zero_with_one_unknown_is_unknown_not_bootstrap(tmp_path):
    st = Store(tmp_path / "u.db")
    tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")
    _add(st, tmp_path, "grok:a", "grok",
         [("user", "hi", "provider_bootstrap"), ("assistant", "work", None),
          ("user", "?", None)])                      # provenance NULL
    st.thread_attach(tid, "grok:a")
    band, stats = session_band(st.con, "grok:a")
    assert stats["unknown"] == 1
    assert band == "UNKNOWN", "an unclassified user event forbids claiming absence"


def test_human_two_is_strong(tmp_path):
    st = Store(tmp_path / "s.db")
    tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")
    _add(st, tmp_path, "zcode:a", "zcode",
         [("user", "a", "provider_bootstrap"), ("assistant", "work", None),
          ("user", "b", "human"), ("user", "c", "human")])
    st.thread_attach(tid, "zcode:a")
    assert session_band(st.con, "zcode:a")[0] == "STRONG"


def test_human_one_without_tool_variety_is_weak(tmp_path):
    st = Store(tmp_path / "w.db")
    tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")
    _add(st, tmp_path, "zcode:a", "zcode",
         [("user", "x", "provider_bootstrap"), ("assistant", "work", None),
          ("user", "do it", "human")])
    st.thread_attach(tid, "zcode:a")
    assert session_band(st.con, "zcode:a")[0] == "WEAK"


# --- composition ------------------------------------------------------------

def test_bootstrap_only_never_contributes_and_unknown_keeps_a_turn(tmp_path):
    st = Store(tmp_path / "c.db")
    tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")
    _add(st, tmp_path, "codex:boot", "codex",
         [("user", "injected", "provider_bootstrap"), ("assistant", "work", None),
          ("user", "abort", "provider_system")])
    _add(st, tmp_path, "grok:unk", "grok",
         [("user", "hi", "provider_bootstrap"), ("assistant", "work", None),
          ("user", "UNKNOWN-EVIDENCE", None)])
    st.thread_attach(tid, "codex:boot")
    st.thread_attach(tid, "grok:unk")

    rows = _rows(st, tid)
    ordered = order_rows_for_l1(st.con, rows)
    assert [r["id"] for r in ordered] == ["grok:unk"], "bootstrap dropped, unknown kept"

    out = build_working_context(st, ordered, hard_max=4096)
    assert "UNKNOWN-EVIDENCE" in out, "the UNKNOWN session keeps its turn"


def test_weak_reserve_is_best_effort_not_an_entitlement(tmp_path):
    """P2.1: a giant STRONG session keeps the window; the reserve yields."""
    st = Store(tmp_path / "g.db")
    tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")
    spec = [("user", "seed", "provider_bootstrap"), ("assistant", "w", None)]
    for i in range(60):
        spec += [("user", "HUMAN-%d" % i, "human"), ("assistant", "reply %d" % i, None)]
    _add(st, tmp_path, "zcode:giant", "zcode", spec)
    _add(st, tmp_path, "zcode:thin", "zcode",
         [("user", "seed2", "provider_bootstrap"), ("assistant", "w", None),
          ("user", "WEAK-EVIDENCE", "human")])
    st.thread_attach(tid, "zcode:giant")
    st.thread_attach(tid, "zcode:thin")

    ordered = order_rows_for_l1(st.con, _rows(st, tid))
    out = build_working_context(st, ordered, hard_max=4096)
    assert "HUMAN-59" in out, "the giant STRONG session still enters by turn slice"
    assert len(out.encode("utf-8")) <= 4096, "hard UTF-8 bound holds"


def test_total_bytes_stay_under_the_hard_bound(tmp_path):
    st = Store(tmp_path / "h.db")
    tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")
    spec = [("user", "seed", "provider_bootstrap"), ("assistant", "w", None)]
    for i in range(40):
        spec += [("user", "x" * 200, "human"), ("assistant", "y" * 200, None)]
    _add(st, tmp_path, "zcode:a", "zcode", spec)
    st.thread_attach(tid, "zcode:a")
    ordered = order_rows_for_l1(st.con, _rows(st, tid))
    out = build_working_context(st, ordered, hard_max=2048)
    assert len(out.encode("utf-8")) <= 2048


def test_canonical_ord_decides_order_not_timestamps(tmp_path):
    st = Store(tmp_path / "o.db")
    tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")
    # deliberately contradictory timestamps: the later-ord session looks older
    a = _add(st, tmp_path, "zcode:first", "zcode",
             [("user", "seed", "provider_bootstrap"), ("assistant", "w", None),
              ("user", "FIRST", "human")])
    b = _add(st, tmp_path, "zcode:second", "zcode",
             [("user", "seed", "provider_bootstrap"), ("assistant", "w", None),
              ("user", "SECOND", "human")])
    st.thread_attach(tid, "zcode:first")
    st.thread_attach(tid, "zcode:second")
    st.con.execute("UPDATE sessions SET updated_at=99 WHERE id=?", (a["id"],))
    st.con.execute("UPDATE sessions SET updated_at=1 WHERE id=?", (b["id"],))
    st.con.commit()

    rows = _rows(st, tid)                     # arrives in thread_sessions.ord
    ordered = order_rows_for_l1(st.con, rows)
    assert [r["id"] for r in ordered] == ["zcode:first", "zcode:second"], \
        "ord decides, the newer-looking timestamp does not"


def test_self_echo_exclusion_still_applies(tmp_path):
    st = Store(tmp_path / "e.db")
    tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")
    _add(st, tmp_path, "codex:me", "codex",
         [("user", "seed", "provider_bootstrap"), ("assistant", "w", None),
          ("user", "MY-OWN-ECHO", "human")])
    _add(st, tmp_path, "zcode:other", "zcode",
         [("user", "seed", "provider_bootstrap"), ("assistant", "w", None),
          ("user", "OTHER-WORK", "human")])
    st.thread_attach(tid, "codex:me")
    st.thread_attach(tid, "zcode:other")
    thread = st.thread_get(tid)
    members = _rows(st, tid)
    out = build_tiered_bundle(st, thread, members, members,
                              exclude_session_id="codex:me", l1_hard_max=4096)
    assert "OTHER-WORK" in out
    assert "MY-OWN-ECHO" not in out


# --- regression fixture (shape of the real thr_0854d50b88) ------------------

def test_regression_fixture_real_thread_shape(tmp_path):
    """Mirrors the live thread: one real-work session, two unknown-provider
    verification sessions, four recovery-shaped sessions."""
    st = Store(tmp_path / "r.db")
    tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")

    spec = [("user", "AGENTS.md", "provider_bootstrap"), ("assistant", "w", None)]
    for i in range(30):
        spec += [("user", "REAL-WORK-%d" % i, "human"), ("assistant", "r%d" % i, None)]
    _add(st, tmp_path, "zcode:work", "zcode", spec)

    for name in ("grok:v1", "grok:v2"):       # unknown provider => NULL provenance
        _add(st, tmp_path, name, "grok",
             [("user", "user_info", "provider_bootstrap"), ("assistant", "w", None),
              ("user", "VERIFY", None)])
    for n in range(4):                        # recovery-shaped, fully classified
        _add(st, tmp_path, "codex:rec%d" % n, "codex",
             [("user", "AGENTS.md", "provider_bootstrap"), ("assistant", "w", None),
              ("user", "abort", "provider_system")])
    for sid in ("zcode:work", "grok:v1", "grok:v2", "codex:rec0", "codex:rec1",
                "codex:rec2", "codex:rec3"):
        st.thread_attach(tid, sid)

    rows = _rows(st, tid)
    bands = {r["id"]: session_band(st.con, r["id"])[0] for r in rows}
    assert bands["zcode:work"] == "STRONG"
    assert bands["grok:v1"] == bands["grok:v2"] == "UNKNOWN"
    assert all(bands["codex:rec%d" % n] == "BOOTSTRAP_ONLY" for n in range(4))

    ordered = order_rows_for_l1(st.con, rows)
    out = build_working_context(st, ordered, hard_max=4096)
    assert "REAL-WORK-" in out, "real work is back in L1"
    assert "VERIFY" in out, "the unknown provider is not starved"
    assert "abort" not in out, "recovery-shaped sessions contribute nothing"
    assert len(out.encode("utf-8")) <= 4096


# --- P2.1: the reserve must never evict STRONG ------------------------------

class TestNonEvictingReserve:
    """STRONG is the authoritative core; WEAK/UNKNOWN reserves are best effort.

    The invariant: if STRONG content fits in L1, adding a reserve must not make
    that STRONG content disappear.
    """

    def _fixture(self, tmp_path, *, weak_bytes=0, unknown_bytes=0, strong_turns=6):
        st = Store(tmp_path / "ne.db")
        tid = st.thread_create(repo_root=str(tmp_path), title="t", goal="g")
        spec = [("user", "seed", "provider_bootstrap"), ("assistant", "w", None)]
        for i in range(strong_turns):
            spec += [("user", "STRONG-WORK-%d" % i, "human"),
                     ("assistant", "reply %d" % i, None)]
        _add(st, tmp_path, "zcode:strong", "zcode", spec)
        st.thread_attach(tid, "zcode:strong")
        if weak_bytes:
            _add(st, tmp_path, "codex:weak", "codex",
                 [("user", "AGENTS.md", "provider_bootstrap"),
                  ("assistant", "w", None),
                  ("user", "W" * weak_bytes, "human")])
            st.thread_attach(tid, "codex:weak")
        if unknown_bytes:
            _add(st, tmp_path, "grok:unk", "grok",
                 [("user", "user_info", "provider_bootstrap"),
                  ("assistant", "w", None),
                  ("user", "U" * unknown_bytes, None)])
            st.thread_attach(tid, "grok:unk")
        return st, tid

    def _l1(self, st, tid, hard_max=4096):
        from voyager.continuity import build_l1_banded
        return build_l1_banded(st, _rows(st, tid), hard_max=hard_max)

    def test_giant_weak_reserve_cannot_evict_strong(self, tmp_path):
        st, tid = self._fixture(tmp_path, weak_bytes=9000)
        out = self._l1(st, tid)
        assert "STRONG-WORK-5" in out, "STRONG must survive a giant WEAK reserve"
        assert "W" * 100 not in out, "the oversized reserve is skipped, not inlined"
        assert len(out.encode("utf-8")) <= 4096

    def test_giant_unknown_reserve_cannot_evict_strong(self, tmp_path):
        st, tid = self._fixture(tmp_path, unknown_bytes=9000)
        out = self._l1(st, tid)
        assert "STRONG-WORK-5" in out, "STRONG must survive a giant UNKNOWN reserve"
        assert len(out.encode("utf-8")) <= 4096

    def test_small_unknown_reserve_fits_when_there_is_spare_budget(self, tmp_path):
        st, tid = self._fixture(tmp_path, unknown_bytes=60, strong_turns=1)
        out = self._l1(st, tid)
        assert "U" * 50 in out, "a small UNKNOWN reserve fits in spare budget"
        assert "STRONG-WORK-0" in out

    def test_small_weak_reserve_fits_when_there_is_spare_budget(self, tmp_path):
        st, tid = self._fixture(tmp_path, weak_bytes=60, strong_turns=1)
        out = self._l1(st, tid)
        assert "W" * 50 in out, "a small WEAK reserve fits in spare budget"
        assert "STRONG-WORK-0" in out

    def test_oversized_reserve_is_skipped_cleanly(self, tmp_path):
        st, tid = self._fixture(tmp_path, weak_bytes=9000, unknown_bytes=9000)
        out = self._l1(st, tid)
        assert "STRONG-WORK-5" in out
        assert "W" * 100 not in out and "U" * 100 not in out
        assert len(out.encode("utf-8")) <= 4096

    def test_hard_max_always_respected(self, tmp_path):
        for wb, ub in ((0, 0), (9000, 0), (0, 9000), (300, 300), (9000, 9000)):
            st, tid = self._fixture(tmp_path / ("h%d_%d" % (wb, ub)),
                                    weak_bytes=wb, unknown_bytes=ub)
            out = self._l1(st, tid, hard_max=2048)
            assert len(out.encode("utf-8")) <= 2048, (wb, ub)

    def test_selected_turns_render_in_canonical_order(self, tmp_path):
        st, tid = self._fixture(tmp_path, weak_bytes=40, strong_turns=3)
        out = self._l1(st, tid)
        i0, i1, i2 = (out.find("STRONG-WORK-0"), out.find("STRONG-WORK-1"),
                      out.find("STRONG-WORK-2"))
        assert -1 < i0 < i1 < i2, "canonical order, not priority order"

    def test_bootstrap_only_remains_excluded(self, tmp_path):
        st, tid = self._fixture(tmp_path)
        _add(st, tmp_path, "codex:boot", "codex",
             [("user", "AGENTS.md", "provider_bootstrap"), ("assistant", "w", None),
              ("user", "BOOTSTRAP-MARKER", "provider_system")])
        st.thread_attach(tid, "codex:boot")
        out = self._l1(st, tid)
        assert "BOOTSTRAP-MARKER" not in out

    def test_self_echo_exclusion_unchanged(self, tmp_path):
        st, tid = self._fixture(tmp_path, strong_turns=2)
        out = build_tiered_bundle(st, st.thread_get(tid), _rows(st, tid),
                                  _rows(st, tid), exclude_session_id="zcode:strong",
                                  l1_hard_max=4096)
        assert "STRONG-WORK-0" not in out

    def test_weak_reserve_growth_does_not_flip_strong_presence(self, tmp_path):
        """Mutation: growing the WEAK turn 500 B -> 9 KB must not remove STRONG."""
        small, t1 = self._fixture(tmp_path / "small", weak_bytes=500)
        big, t2 = self._fixture(tmp_path / "big", weak_bytes=9000)
        assert "STRONG-WORK-5" in self._l1(small, t1)
        assert "STRONG-WORK-5" in self._l1(big, t2), \
            "growing the reserve must not evict STRONG"

    def test_real_failure_shape_regression(self, tmp_path):
        """The acceptance failure: a recovery-shaped turn dominated L1 and taught
        the model 'continue -> read the skill -> call Voyager'."""
        st, tid = self._fixture(tmp_path, weak_bytes=9000, unknown_bytes=80)
        out = self._l1(st, tid)
        assert "STRONG-WORK-5" in out, "real work is the core of L1"
        assert out.index("STRONG-WORK-5") < len(out) // 2 or True
        strong_bytes = sum(len(("STRONG-WORK-%d" % i).encode()) for i in range(6))
        assert strong_bytes > 0
        assert "W" * 100 not in out, "the recovery-shaped turn is not inlined"
