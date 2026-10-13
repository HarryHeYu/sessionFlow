"""The benchmark harness must measure what its task names say.

``scan_inc`` and ``scan_touch`` are only incremental if the fingerprints seeded
by the untimed setup survive into the measured pass.  A ``:memory:`` database
cannot carry them: ``_scan_setup`` closes its ``Store`` before the measured pass
opens a new one, and the database is discarded with it.

Measured on the cached ``codex_src10k`` dataset (10,000 sessions / 100,000
events), same machine, same run:

    scan_inc, db=":memory:"   wall=80.5 s  cpu=68.3 s  git_calls=10000
                              execute=330018  txn=10000   <- a COLD scan
    scan_inc, file-backed     wall=18.8 s  cpu=17.4 s  git_calls=0
                              execute=40019   txn=0       <- the real thing

So a ``:memory:`` scan job does not just mis-measure the incremental path; it
reports a number 4.3x too large and attributes a full re-parse to it.
"""

from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent


def _bench():
    spec = importlib.util.spec_from_file_location(
        "voyager_benchmark", REPO / "scripts" / "benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_jobs_are_file_backed(tmp_path):
    jobs = _bench().scan_jobs(
        tmp_path, tmp_path / "src",
        ("scan_initial", "scan_inc", "scan_touch", "scan_force"), "scan10k")

    assert [j[0] for j in jobs] == [
        "scan_initial", "scan_inc", "scan_touch", "scan_force"]

    for task, db, _src, _repo, _thread in jobs:
        assert db != ":memory:", (
            "%s is not file-backed, so the fingerprints seeded by the untimed "
            "setup cannot reach the measured pass and the task silently "
            "measures a cold scan" % task)


def test_scan_jobs_start_from_a_fresh_database(tmp_path):
    stale = tmp_path / "scan10k_scan_inc.db"
    stale.write_bytes(b"an index from a previous run")

    jobs = _bench().scan_jobs(tmp_path, tmp_path / "src", ("scan_inc",), "scan10k")

    assert jobs[0][1] == str(stale)
    assert not stale.exists(), (
        "a previous run's index was reused, so scan_initial would not be a "
        "cold scan and scan_inc would not be the one that pays for the writes")


def test_scan_jobs_leave_the_source_tree_alone(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    jobs = _bench().scan_jobs(tmp_path, src, ("scan_inc",), "scan10k")
    assert jobs[0][2] == str(src), "the job must point at the source tree"


def test_overview_task_pins_a_window_wide_enough_for_the_dataset(tmp_path):
    """`api.overview` defaults to a 48-hour window; the datasets age.

    With the default, the same task measured `q=2806` on 2026-10-02 (the 10k
    dataset was fresh, and the documented `OVERVIEW_RECENT_SESSIONS_N1` fired)
    and `q=4` on 2026-10-13 (the same dataset was 258 h old, every session fell
    outside the window, and the loop never ran).  A number that changes with
    the calendar cannot be compared across runs — which is exactly how a live
    N+1 gets mistaken for a fixed one.
    """
    from voyager.model import new_event, new_session
    from voyager.store import Store

    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}\n", encoding="utf-8")

    store = Store(db)
    old = new_session(
        id="codex:old", provider="codex", native_session_id="old",
        title="work from last month", started_at=1757000000.0,
        updated_at=1757000100.0, cwd="E:/proj/demo", repo_root="E:/proj/demo",
        message_count=1, tool_count=0, can_resume=True,
        resume_cmd="codex resume old")
    store.replace_session(
        old,
        [new_event(sid="codex:old", ts=1757000100.0, seq=1, kind="user",
                   role="user", content="hello")],
        "codex", src)
    store.close()

    out = _bench()._task_overview(str(db), None, None)

    assert out["recent_sessions"], (
        "the overview task returned no sessions: every session in an aged "
        "dataset falls outside the default 48-hour window, so the N+1 loop "
        "never runs and the task silently measures an empty loop")
