#!/usr/bin/env python3
"""Level 2 — Agent Continuation Effectiveness harness (synthetic, no LLM).

WHY THIS EXISTS
---------------
`scripts/continuity_benchmark.py` (Core PR #64) measures *context preparation*
deterministically: are facts present, is provenance explained, is budget kept.
It deliberately cannot answer "did the next agent actually finish the work?"
This module adds that second layer without reusing or rewriting the Level 1
scorer: it builds real, small software tasks whose requirements live ONLY in
synthetic prior-agent sessions, prepares four context variants, and provides a
pluggable executor + an independent hidden-test verdict.

HONESTY / SCOPE
---------------
* No LLM, embedding, vector DB or long-term memory service is introduced.
* No real user session, index, home or repo is read or written. Every repo,
  session and test is synthetic and built under a caller-supplied workdir.
* Running a real model needs paid calls / auth / real histories. Without an
  explicit, authorised runner, this harness runs in DRY-RUN mode: it builds
  everything, prepares contexts, and reports every agent-outcome metric as
  ``LIVE_MODEL_NOT_VERIFIED``. It never invents a completion rate.
* The task "answer" is a hidden test set the incoming agent never sees; the
  benchmark verdict is that test's result, not tool-call counts.

Run:
    python scripts/agent_continuity_benchmark.py --workdir DIR
    python scripts/agent_continuity_benchmark.py --workdir DIR --out result.json
    # with an authorised agent command (LIVE MODEL — only with permission):
    python scripts/agent_continuity_benchmark.py --workdir DIR \
        --runner 'my-agent --prompt-file {prompt} --out {repo}'
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("VOYAGER_NO_SYNC", "1")

from voyager.budget import apply_budget, estimate_tokens, parse_budget  # noqa: E402
from voyager.continuity import build_continuation_bundle  # noqa: E402
from voyager.model import new_event, new_session  # noqa: E402
from voyager.store import Store  # noqa: E402

BASELINES = ("no_history", "raw_history", "latest_summary", "sessionflow_bundle")
DEFAULT_BUDGET = 4000  # equal token budget across all four variants

# Outcome metrics. With no live model these are all LIVE_MODEL_NOT_VERIFIED.
OUTCOME_METRICS = (
    "used_sessionflow",
    "found_correct_workthread",
    "restored_correct_goal",
    "fabricated_task",
    "used_historical_evidence",
    "final_code_meets_goal",
    "independent_tests_pass",
    "tool_call_count",
    "token_spend",
    "elapsed_seconds",
    "failure_type",
)


# ---------------------------------------------------------------------------
# Task construction. The requirement is ONLY in the prior sessions; the repo
# and the visible tests start green and contain no hint of the change.
# ---------------------------------------------------------------------------
def build_task(workdir: Path, task_id: str = "T1") -> Dict[str, Any]:
    """Create one synthetic software task under `workdir`.

    Returns a manifest describing the repo, the hidden tests, the prior-agent
    sessions, the goal and the correct WorkThread. No real data is touched.
    """
    workdir = Path(workdir)
    repo = workdir / "repo"
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    # Visible starting code + a green test. Nothing here reveals the task.
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tests_dir / "test_visible.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")

    # Hidden acceptance test — the incoming agent never sees this file. It is
    # stored beside the workdir, not inside the repo.
    hidden = workdir / "hidden_tests"
    hidden.mkdir(parents=True, exist_ok=True)
    hidden_test = hidden / "test_hidden.py"
    hidden_test.write_text(
        "from calc import add\n\n\ndef test_add_three_args():\n"
        "    # Requirement came ONLY from the prior session:\n"
        "    # 'make add() accept a third clamp argument, default 10'.\n"
        "    assert add(1, 2) == 3\n"
        "    assert add(1, 9, 5) == 5\n"
        "    assert add(1, 2) == 3\n",
        encoding="utf-8")

    # Synthetic prior-agent sessions. The requirement and a contradiction live
    # here; a second WorkThread is a decoy. source_missing exercises retention.
    db = workdir / "index.db"
    store = Store(db)
    store.con.commit()
    sid = "codex:calc-task"
    src = workdir / "calc.jsonl"
    src.write_text("{}\n", encoding="utf-8")
    events = [
        new_event(sid=sid, ts=1_700_000_000, seq=0, kind="user",
                  content="GOAL: extend add() with an optional third argument clamp, default 10"),
        new_event(sid=sid, ts=1_700_000_010, seq=1, kind="assistant",
                  content="NEXT: add a third positional `clamp=10`; cap the sum at clamp"),
        new_event(sid=sid, ts=1_700_000_020, seq=2, kind="tool_call",
                  content="", command="pytest -q", exit_code=0),
    ]
    row = new_session(
        id=sid, provider="codex", native_session_id="calc-task",
        title="calc clamp work", started_at=1_700_000_000,
        updated_at=1_700_000_030, cwd=str(repo), repo_root=str(repo),
        message_count=3, tool_count=1, can_resume=False, metadata={}, raw_metadata={})
    store.replace_session(row, events, "codex", src)

    # A contradicting earlier session (kept; never silently deleted).
    sid_old = "claude:calc-old"
    src_old = workdir / "calc-old.jsonl"
    src_old.write_text("{}\n", encoding="utf-8")
    ev_old = [new_event(sid=sid_old, ts=1_699_000_000, seq=0, kind="assistant",
                        content="OLD: maybe clamp default should be 100")]
    row_old = new_session(
        id=sid_old, provider="claude", native_session_id="calc-old",
        title="old clamp idea", started_at=1_699_000_000,
        updated_at=1_699_000_010, cwd=str(repo), repo_root=str(repo),
        message_count=1, can_resume=False, metadata={}, raw_metadata={})
    store.replace_session(row_old, ev_old, "claude", src_old)
    store.q("UPDATE sessions SET source_state='SOURCE_MISSING' WHERE id=?", (sid_old,))
    store.con.commit()

    correct_tid = store.thread_create(repo_root=str(repo), title="calc clamp",
                                      goal="extend add() with a clamp argument")
    store.thread_attach(correct_tid, sid)
    store.thread_attach(correct_tid, sid_old)
    # A decoy WorkThread whose title suggests a different goal.
    decoy_tid = store.thread_create(repo_root=str(repo), title="unrelated docs cleanup",
                                    goal="rewrite README")
    store.con.commit()
    store.close()

    manifest = {
        "task_id": task_id,
        "repo": str(repo),
        "hidden_test": str(hidden_test),
        "index_db": str(db),
        "goal": "extend add() with an optional third argument clamp, default 10",
        "correct_thread_id": correct_tid,
        "decoy_thread_id": decoy_tid,
        "session_ids": [sid, sid_old],
        "required_fact": "clamp, default 10",
    }
    (workdir / "task_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def _build_contexts(workdir: Path, manifest: Dict[str, Any]) -> Dict[str, str]:
    """Prepare the four context variants from the synthetic index."""
    db = Path(manifest["index_db"])
    store = Store(db)
    sids = manifest["session_ids"]
    rows = [store.session(s)[0] for s in sids]

    raw = "\n".join(
        str(e["content"] or "") + " " + str(e["command"] or "")
        for r in rows for e in store.events(r["id"]))
    latest = rows[0]
    latest_text = "\n".join(
        str(e["content"] or "") + " " + str(e["command"] or "")
        for e in store.events(latest["id"]))
    naive = " ".join("%s:%s" % (r["provider"], r["title"]) for r in rows)

    from voyager.store import Store as _S  # noqa: F401  (explicit for clarity)
    thread = store.thread_get(manifest["correct_thread_id"])
    bundle = build_continuation_bundle(
        store, rows, goal=manifest["goal"], thread=thread)
    packed, _ = apply_budget(bundle, parse_budget(str(DEFAULT_BUDGET)))
    store.close()

    return {
        "no_history": "",
        "raw_history": raw,
        "latest_summary": latest_text + "\n" + naive,
        "sessionflow_bundle": packed,
    }


def _write_prompts(workdir: Path, manifest: Dict[str, Any],
                   contexts: Dict[str, str]) -> Dict[str, str]:
    """Write one prompt file per baseline; the hidden answer is never included."""
    prompts: Dict[str, str] = {}
    for name, ctx in contexts.items():
        p = workdir / ("prompt_%s.txt" % name)
        text = (
            "You are continuing work in the repo at %s.\n"
            "Continue the existing task. Do not invent a new one.\n\n" % manifest["repo"]
        )
        if ctx:
            text += "<prior-context>\n%s\n</prior-context>\n\n" % ctx
        else:
            text += "(no prior context provided)\n\n"
        text += "The current test suite is green. Make the needed change.\n"
        p.write_text(text, encoding="utf-8")
        prompts[name] = str(p)
    return prompts


# ---------------------------------------------------------------------------
# Executor. Default is DRY-RUN: build + prepare + evaluate, but no agent is
# launched. A real model needs an explicit, authorised command template and is
# recorded as LIVE MODEL with its own result — never mixed with dry-run data.
# ---------------------------------------------------------------------------
def evaluate_repo(repo: Path, hidden_test: Path, python: str) -> Dict[str, Any]:
    """Run the visible + hidden tests against whatever code is in `repo`.

    This is the independent verdict. It uses the hidden test the incoming agent
    never saw, plus the visible tests, and returns a boolean pass plus counts.
    """
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "VOYAGER_NO_SYNC": "1"}
    # Copy the hidden test into the repo just for the run, then leave it: the
    # repo is synthetic and disposable under `workdir`.
    target = repo / "tests" / "test_hidden.py"
    target.write_text(hidden_test.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", "-q", "tests"],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=120)
        out = proc.stdout + "\n" + proc.stderr
        return {
            "independent_tests_pass": proc.returncode == 0,
            "returncode": proc.returncode,
            "output_tail": out[-800:],
        }
    except Exception as e:  # noqa: BLE001 - a broken runner is a result, not a crash
        return {
            "independent_tests_pass": False,
            "returncode": None,
            "output_tail": "evaluation error: %s" % e,
        }


def _not_verified_row() -> Dict[str, Any]:
    row = {m: "LIVE_MODEL_NOT_VERIFIED" for m in OUTCOME_METRICS}
    return row


def run_level2(workdir: Path, runner: Optional[str] = None,
               python: Optional[str] = None) -> Dict[str, Any]:
    """Build one task, prepare four contexts, optionally run an authorised agent.

    With no `runner`, everything up to evaluation is real and reproducible, and
    all agent-outcome metrics are LIVE_MODEL_NOT_VERIFIED. With a `runner`, the
    command template is expanded with {prompt}, {repo} and executed once per
    baseline; only then are outcome metrics filled from the real run.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    py = python or sys.executable
    manifest = build_task(workdir)
    contexts = _build_contexts(workdir, manifest)
    prompts = _write_prompts(workdir, manifest, contexts)

    # Sanity: Level 2 must be a real task — hidden test FAILS on the green
    # starting repo (the requirement is not inferable from visible code).
    # The sanity check runs in a COPY so the agent repos stay pristine.
    import shutil
    sanity_repo = workdir / "sanity_repo"
    if sanity_repo.exists():
        shutil.rmtree(sanity_repo)
    shutil.copytree(manifest["repo"], sanity_repo)
    sanity = evaluate_repo(sanity_repo, Path(manifest["hidden_test"]), py)

    results: Dict[str, Any] = {}
    mode = "LIVE_MODEL" if runner else "DRY_RUN"
    for name in BASELINES:
        row: Dict[str, Any] = {
            "context_tokens": estimate_tokens(contexts[name]),
            "prompt_file": prompts[name],
            "evaluation": _not_verified_row(),
        }
        if runner:
            repo = workdir / ("repo_%s" % name)
            if repo.exists():
                shutil.rmtree(repo)
            shutil.copytree(manifest["repo"], repo)
            cmd = runner.format(prompt=prompts[name], repo=str(repo))
            import time
            t0 = time.time()
            try:
                proc = subprocess.run(
                    shlex.split(cmd), capture_output=True, text=True, timeout=900)
                verdict = evaluate_repo(repo, Path(manifest["hidden_test"]), py)
                row["evaluation"] = {
                    "used_sessionflow": None,  # filled by a run-specific probe
                    "found_correct_workthread": None,
                    "restored_correct_goal": None,
                    "fabricated_task": None,
                    "used_historical_evidence": None,
                    "final_code_meets_goal": verdict["independent_tests_pass"],
                    "independent_tests_pass": verdict["independent_tests_pass"],
                    "tool_call_count": None,
                    "token_spend": None,
                    "elapsed_seconds": round(time.time() - t0, 2),
                    "failure_type": None if verdict["independent_tests_pass"]
                                    else "tests_failed",
                    "agent_stdout_tail": proc.stdout[-800:],
                    "agent_stderr_tail": proc.stderr[-800:],
                }
            except Exception as e:  # noqa: BLE001
                row["evaluation"] = {
                    **_not_verified_row(),
                    "failure_type": "runner_error",
                    "error": str(e),
                }
        results[name] = row

    return {
        "schema": "agent-continuity-benchmark-v1",
        "mode": mode,
        "live_model_authorised": bool(runner),
        "note": ("DRY-RUN: task + contexts + independent evaluator are real; "
                 "no agent was launched, every outcome metric is "
                 "LIVE_MODEL_NOT_VERIFIED."
                 if not runner else
                 "LIVE MODEL: an authorised runner was executed per baseline."),
        "task": manifest,
        "sanity_hidden_test_fails_on_start": (sanity["independent_tests_pass"] is False),
        "sanity_detail": sanity,
        "baselines": results,
        "no_llm_in_harness": True,
        "no_real_user_data": True,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", required=True,
                    help="isolated scratch dir for the synthetic task")
    ap.add_argument("--out", default=None, help="write structured JSON here")
    ap.add_argument("--runner", default=None,
                    help="authorised agent command template with {prompt} and "
                         "{repo}; omit for DRY-RUN (no live model)")
    ap.add_argument("--python", default=None,
                    help="python with pytest for the independent evaluator")
    args = ap.parse_args(argv)

    result = run_level2(Path(args.workdir), args.runner, args.python)
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
