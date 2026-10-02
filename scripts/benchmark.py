"""Voyager O1 performance benchmark harness (Phase O1).

Subcommands
-----------
gen-store --sessions N --per E --out DB
    Bulk-generate a synthetic index (sessions + events + FTS + one bench
    WorkThread over the first 100 sessions) mirroring the production schema.
gen-codex --sessions N --per E --out TREE
    Bulk-generate a synthetic codex rollout source tree compatible with
    voyager.adapters.codex.parse().
worker --task T --db D [--src TREE] [--repo R] [--thread TID]
    Run ONE benchmark task in a fresh process with instrumentation
    (subprocess count, Store.q count, peak RSS) and print one JSON line.
run --full [--json OUT]
    Spawn workers for the whole matrix and print a markdown table.

Datasets are cached under TEMP/voyager-bench/ and reused across runs.
Metrics per task: wall time, CPU time, peak RSS, subprocess count,
Store.q count, git calls, rows/events processed.  No subjective claims.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voyager.store import Store  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
# bench datasets live next to the repo (same drive), never on the system
# temp drive; override with VOYAGER_BENCH_ROOT when needed.
BENCH_ROOT = Path(os.environ.get("VOYAGER_BENCH_ROOT",
                                 str(REPO.drive) + "/voyager-bench"))

FTS_BODY_CAP = 600
WORDPOOL = (
    ["refactor", "lease", "WorkThread", "session", "index", "测试", "里程碑",
     "性能", "benchmark", "checkpoint", "pending", "attach", "FTS", "trigram",
     "coverage", "roadmap", "phase", "handoff", "switch", "artifact"])
PROVIDERS = ("codex", "claude", "grok", "zcode")

COUNTS = {"subprocess": 0, "q": 0, "git_calls": 0}
BG = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _words(n: int, rng: random.Random) -> str:
    return " ".join(rng.choice(WORDPOOL) for _ in range(n))


# ---------------------------------------------------------------------------
# generators
# ---------------------------------------------------------------------------

def gen_store(out: Path, sessions: int, per: int) -> None:
    """Bulk-build a synthetic index mirroring the production schema."""
    if out.exists():
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    import voyager.store as store_mod
    con = sqlite3.connect(str(out))
    con.executescript(store_mod.SCHEMA)
    rng = random.Random(42)
    now = time.time()

    srows = []
    for i in range(sessions):
        prov = PROVIDERS[i % 4]
        srows.append((
            f"{prov}:bench{i:07d}", prov, f"bench{i:07d}",
            _words(6, rng), now - (sessions - i) * 60,
            now - (sessions - i) * 60 + per * 7,
            "E:/bench/repo", "E:/bench/repo", "https://example/bench.git",
            "main", f"commit{i:07d}", "bench-model",
            per // 2, per // 4, 1, 0, None,
            json.dumps({"bench": True}), json.dumps({})))
    con.executemany(
        "INSERT INTO sessions(id, provider, native_id, title, started_at,"
        " updated_at, cwd, repo_root, git_remote, git_branch, git_commit,"
        " model, message_count, tool_count, can_resume, can_fork,"
        " resume_cmd, metadata_json, raw_metadata_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", srows)

    erows, frows = [], []
    eid = 0
    for i in range(sessions):
        sid = f"{PROVIDERS[i % 4]}:bench{i:07d}"
        base_ts = now - (sessions - i) * 60
        for j in range(per):
            eid += 1
            kind = ("user", "assistant", "tool_call", "tool_result",
                    "reasoning")[j % 5]
            body = _words(12 + (j * 7) % 40, rng)
            cmd = f"bench-cmd-{j} --session {i}" if kind == "tool_call" else None
            erows.append((
                eid, sid, base_ts + j * 3, j, kind,
                "user" if kind == "user" else
                "assistant" if kind == "assistant" else None,
                body if kind in ("user", "assistant") else None,
                "bench-tool" if kind == "tool_call" else None,
                f"call-{eid}" if kind == "tool_call" else None,
                json.dumps({"i": i, "j": j}) if kind == "tool_call" else None,
                body if kind == "tool_result" else None,
                cmd,
                body if kind == "tool_result" else None, None,
                0 if kind == "tool_result" else None,
                f"src/file{i % 97}.py" if j % 3 == 0 else None,
                None, None, None, "[]", "bench-model", None, "human", None))
            fbody = " ".join(x for x in (body, cmd, body) if x)[:FTS_BODY_CAP]
            frows.append((eid, fbody,
                          f"src/file{i % 97}.py" if j % 3 == 0 else None,
                          cmd or "", sid))
            if len(erows) >= 2000:
                con.executemany(
                    "INSERT INTO events(id, sid, ts, seq, kind, role, content,"
                    " tool_name, tool_call_id, tool_input, tool_output,"
                    " command, stdout, stderr, exit_code, file_path,"
                    " old_content, new_content, diff, files_json, model,"
                    " usage_json, origin, raw_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    erows)
                con.executemany(
                    "INSERT INTO event_fts(rowid, body, file_path, command,"
                    " sid) VALUES (?,?,?,?,?)", frows)
                erows, frows = [], []
    if erows:
        con.executemany(
            "INSERT INTO events(id, sid, ts, seq, kind, role, content,"
            " tool_name, tool_call_id, tool_input, tool_output, command,"
            " stdout, stderr, exit_code, file_path, old_content, new_content,"
            " diff, files_json, model, usage_json, origin, raw_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", erows)
    if frows:
        con.executemany(
            "INSERT INTO event_fts(rowid, body, file_path, command, sid)"
            " VALUES (?,?,?,?,?)", frows)

    con.execute(
        "INSERT INTO threads(id, repo_root, title, goal, status, created_at,"
        " updated_at) VALUES ('thr_bench0001', 'E:/bench/repo',"
        " 'benchmark thread', 'finish the benchmark', 'active', ?, ?)",
        (now, now))
    for i in range(min(100, sessions)):
        con.execute(
            "INSERT INTO thread_sessions(thread_id, session_id, ord)"
            " VALUES ('thr_bench0001', ?, ?)",
            (f"{PROVIDERS[i % 4]}:bench{i:07d}", i))
    con.commit()
    con.execute("ANALYZE")
    con.commit()
    con.close()
    print(f"gen-store: {sessions} sessions / {sessions * per} events -> {out}",
          flush=True)


def gen_codex(out: Path, sessions: int, per: int) -> None:
    """Bulk-generate a synthetic codex rollout tree compatible with parse()."""
    rng = random.Random(7)
    day = Path(out) / "2026" / "09" / "28"
    day.mkdir(parents=True, exist_ok=True)
    for i in range(sessions):
        uuid = f"{i:08x}-0000-7000-8000-{i:012x}"
        f = day / (f"rollout-2026-09-28T00-{(i // 60) % 60:02d}-"
                   f"{i % 60:02d}-00-{uuid}_w{i}.jsonl")
        rows = [{"timestamp": "2026-09-28T00:00:00.000Z",
                 "type": "session_meta",
                 "payload": {"session_id": uuid, "cwd": "E:/bench/repo",
                             "model_provider": "bench",
                             "git": {"commit_hash": f"c{i}", "branch": "main"},
                             "originator": "bench"}}]
        for j in range(per):
            kind = ("user", "assistant", "reasoning", "function_call",
                    "function_call_output")[j % 5]
            text = _words(10 + (j * 5) % 30, rng)
            if kind == "user":
                p = {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]}
            elif kind == "assistant":
                p = {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": text}]}
            elif kind == "reasoning":
                p = {"type": "reasoning", "summary": text, "model": "bench"}
            elif kind == "function_call":
                p = {"type": "function_call", "name": "shell",
                     "arguments": json.dumps({"cmd": text}),
                     "call_id": f"call-{i}-{j}"}
            else:
                p = {"type": "function_call_output", "call_id": f"call-{i}-{j}",
                     "output": json.dumps({"output": text, "exit_code": 0})}
            rows.append({"timestamp": "2026-09-28T00:00:00.000Z",
                         "type": "response_item", "payload": p})
        f.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                               for r in rows), encoding="utf-8")
    print(f"gen-codex: {sessions} rollout files / {sessions * per} events "
          f"-> {day}", flush=True)


# ---------------------------------------------------------------------------
# instrumentation
# ---------------------------------------------------------------------------

COUNTS = {"subprocess": 0, "q": 0, "git_calls": 0}
BG = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def peak_rss_bytes() -> int:
    if os.name != "nt":
        return 0

    class PME(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint),
                    ("PageFaultCount", ctypes.c_uint),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    pm = PME()
    pm.cb = ctypes.sizeof(PME)
    psapi = ctypes.windll.psapi
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PME), ctypes.c_uint]
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pm), pm.cb):
        return int(pm.PeakWorkingSetSize)
    return 0


def _install_instrumentation():
    real_popen = subprocess.Popen

    def counting_popen(*a, **kw):
        COUNTS["subprocess"] += 1
        return real_popen(*a, **kw)

    subprocess.Popen = counting_popen

    import voyager.store as store_mod
    real_store = store_mod.Store

    class CountingStore(real_store):
        def q(self, sql, *a):
            COUNTS["q"] += 1
            return super().q(sql, *a)

    store_mod.Store = CountingStore

    import voyager.api as api_mod
    api_mod.Store = CountingStore

    import voyager.adapters.base as base_mod
    real_git = base_mod.git_info

    def counting_git(cwd):
        COUNTS["git_calls"] += 1
        return real_git(cwd)

    base_mod.git_info = counting_git


# ---------------------------------------------------------------------------
# worker tasks
# ---------------------------------------------------------------------------

def _task_overview(db, repo, thread):
    import voyager.api as api
    return api.overview(db=db, repo=repo)


def _task_sessions(db, repo, thread):
    import voyager.api as api
    return api.sessions(db=db, limit=100)


def _task_thread_detail(db, repo, thread):
    import voyager.api as api
    return api.thread_detail(db=db, thread_id=thread)


def _task_search(db, repo, thread):
    import voyager.store as store_mod
    store = store_mod.Store(db)
    queries = ["benchmark", "测试 里程碑", "voyager_continue", "bench-cmd-1",
               "checkpoint attach", "性能", "roadmap phase", "FTS trigram"]
    out = 0
    for q in queries:
        out += len(store.search(q, limit=50))
    store.close()
    return {"rows": out}


def _task_summarize(db, repo, thread):
    import voyager.store as store_mod
    from voyager.thread_brief import summarize
    b = summarize(store_mod.Store(db), thread)
    return {"title": b.title, "contributions": len(b.contributions)}


def _task_compile(db, repo, thread):
    import voyager.store as store_mod
    from voyager.continuity import build_tiered_bundle
    store = store_mod.Store(db)
    t = store.thread_get(thread)
    members = store.thread_member_sessions(thread)
    rows = [dict(m) for m in members]
    out = build_tiered_bundle(store, t, members, rows, exclude_session_id=None)
    store.close()
    return {"chars": len(out)}


def _scan_task(task, db, src):
    import voyager.adapters.codex as codex_mod
    import voyager.store as store_mod
    from voyager.cli import run_scan
    store = store_mod.Store(db)
    codex_mod.SESSIONS_DIR = Path(src)
    codex_mod.STATE_DB = Path(src) / "_no_state.sqlite"
    codex_mod.THREAD_HISTORY_DB = Path(src) / "_no_history.sqlite"
    if task == "scan_initial":
        return run_scan(store, providers=["codex"], quiet=True)
    if task == "scan_inc":
        run_scan(store, providers=["codex"], quiet=True)
        return run_scan(store, providers=["codex"], quiet=True)
    if task == "scan_touch":
        run_scan(store, providers=["codex"], quiet=True)
        files = sorted(Path(src).rglob("rollout-*.jsonl"))
        step = max(1, len(files) // 20)
        for f in files[::step]:
            os.utime(f, None)
        return run_scan(store, providers=["codex"], quiet=True)
    if task == "scan_force":
        return run_scan(store, providers=["codex"], force=True, quiet=True)
    raise ValueError(task)


WORKERS = {
    "overview": lambda db, repo, thread: _task_overview(db, repo, thread),
    "sessions": lambda db, repo, thread: _task_sessions(db, repo, thread),
    "thread_detail": lambda db, repo, thread: _task_thread_detail(
        db, repo, thread),
    "search": lambda db, repo, thread: _task_search(db, repo, thread),
    "summarize": lambda db, repo, thread: _task_summarize(db, repo, thread),
    "compile": lambda db, repo, thread: _task_compile(db, repo, thread),
    "scan_initial": lambda db, repo, thread, src=None: _scan_task(
        "scan_initial", db, src),
    "scan_inc": lambda db, repo, thread, src=None: _scan_task(
        "scan_inc", db, src),
    "scan_touch": lambda db, repo, thread, src=None: _scan_task(
        "scan_touch", db, src),
    "scan_force": lambda db, repo, thread, src=None: _scan_task(
        "scan_force", db, src),
    "doctor": None,          # O4 will add it; reported as n/a
}


def _jmake(res):
    try:
        json.dumps(res)
        return res
    except TypeError:
        return str(res)


def worker(task, db, src, repo, thread):
    if WORKERS.get(task) is None:
        print(json.dumps({"task": task, "status": "n/a"}), flush=True)
        return
    _install_instrumentation()
    fn = WORKERS[task]
    t0 = time.perf_counter()
    c0 = time.process_time()
    res = fn(db=db, repo=repo, thread=thread, src=src)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - c0
    print(json.dumps({
        "task": task, "status": "ok", "wall": round(wall, 4),
        "cpu": round(cpu, 4), "peak_rss": peak_rss_bytes(),
        "subprocess": COUNTS["subprocess"], "q": COUNTS["q"],
        "git_calls": COUNTS["git_calls"], "result": _jmake(res),
    }, ensure_ascii=True), flush=True)


# ---------------------------------------------------------------------------
# parent orchestration
# ---------------------------------------------------------------------------

def _spawn_worker(task, db, src, repo, thread) -> dict:
    r = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()),
         "worker", "--task", task, "--db", str(db), "--src", str(src or ""),
         "--repo", str(repo or ""), "--thread", str(thread or "")],
        capture_output=True, text=True, timeout=3600, **BG)
    lines = [l for l in r.stdout.splitlines() if l.strip().startswith("{")]
    if not lines:
        return {"task": task, "status": "worker-failed",
                "stderr": r.stderr[-300:]}
    return json.loads(lines[-1])


def run_suite(full: bool, out_json) -> None:
    real_db = Path.home() / ".voyager" / "index.db"
    bench = BENCH_ROOT
    syn10k = bench / "syn10k.db"
    syn100k = bench / "syn100k.db"
    codex10k = bench / "codex_src10k"
    codex100k = bench / "codex_src100k"
    repo = "E:/code/voyager"
    thread = "thr_0854d50b88"
    bench_thread = "thr_bench0001"
    read_tasks = ["overview", "sessions", "thread_detail", "search",
                  "summarize", "compile"]

    jobs = []
    if real_db.exists():
        jobs += [(t, str(real_db), None, repo, thread) for t in read_tasks]
    if not syn10k.exists():
        gen_store(syn10k, 10_000, 10)
    jobs += [(t, str(syn10k), None, "E:/bench/repo", bench_thread)
             for t in read_tasks]
    if not codex10k.exists():
        gen_codex(codex10k, 10_000, 10)
    jobs += [("scan_initial", ":memory:", str(codex10k), None, None),
             ("scan_inc", ":memory:", str(codex10k), None, None),
             ("scan_touch", ":memory:", str(codex10k), None, None),
             ("scan_force", ":memory:", str(codex10k), None, None)]
    if full:
        if not syn100k.exists():
            gen_store(syn100k, 100_000, 10)
        jobs += [(t, str(syn100k), None, "E:/bench/repo", bench_thread)
                 for t in read_tasks]
        if not codex100k.exists():
            gen_codex(codex100k, 100_000, 10)
        jobs += [("scan_force", ":memory:", str(codex100k), None, None)]

    results = []
    for task, db, src, r, th in jobs:
        dbp = ":memory:" if task.startswith("scan_") else db
        res = _spawn_worker(task, dbp, src, r, th)
        results.append(res)
        print("done: %s -> wall=%s" % (task, res.get("wall")), flush=True)

    if out_json:
        out_json.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print()
    print("| task | dataset | wall s | cpu s | peak RSS MB | subprocess | q |")
    print("|---|---|---|---|---|---|---|")
    for r in results:
        ds = Path(str(r.get("db", ""))).name or r.get("src", "") or "?"
        print("| {task} | {ds} | {wall} | {cpu} | {rss} | {sp} | {q} |".format(
            task=r["task"], ds=ds, wall=r.get("wall"), cpu=r.get("cpu"),
            rss=round((r.get("peak_rss") or 0) / 1048576, 1),
            sp=r.get("subprocess"), q=r.get("q")))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen-store")
    g.add_argument("--sessions", type=int, required=True)
    g.add_argument("--per", type=int, default=10)
    g.add_argument("--out", required=True)

    g = sub.add_parser("gen-codex")
    g.add_argument("--sessions", type=int, required=True)
    g.add_argument("--per", type=int, default=10)
    g.add_argument("--out", required=True)

    g = sub.add_parser("worker")
    g.add_argument("--task", required=True)
    g.add_argument("--db", default="")
    g.add_argument("--src", default="")
    g.add_argument("--repo", default="")
    g.add_argument("--thread", default="")

    g = sub.add_parser("run")
    g.add_argument("--full", action="store_true")
    g.add_argument("--json", default="")

    a = ap.parse_args()
    if a.cmd == "gen-store":
        gen_store(Path(a.out), a.sessions, a.per)
    elif a.cmd == "gen-codex":
        gen_codex(Path(a.out), a.sessions, a.per)
    elif a.cmd == "worker":
        worker(a.task, a.db or None, a.src or None, a.repo or None,
               a.thread or None)
    elif a.cmd == "run":
        run_suite(a.full, Path(a.json) if a.json else None)


if __name__ == "__main__":
    main()
