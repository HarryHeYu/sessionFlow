#!/usr/bin/env python3
"""Level 2 — Agent Continuation Effectiveness harness (synthetic, no LLM).

WHY THIS EXISTS
---------------
`scripts/continuity_benchmark.py` (Core PR #64) measures *context preparation*
deterministically. This module measures whether a next agent can finish a real
code task. It never rewrites the Level 1 scorer and never reuses its historical
results.

FAIRNESS CONTRACT (v2)
----------------------
Three evaluation modes are kept separate and must not be pooled:

* ``oracle_conditioned`` — every baseline receives the same external goal AND
  the same explicit WorkThread id. It measures context quality with full
  oracle information. It is NOT a work-discovery result.
* ``explicit_goal_continuation`` — every baseline receives the same external
  user goal, and no baseline receives a WorkThread id. It measures continuation
  under an explicit goal.
* ``realistic_work_discovery`` — no baseline receives an external goal or a
  WorkThread id. The agent must discover both from the repo and its own tools.

In every mode, all four baselines use an identical repo template, identical
external information and an independent workspace. Hidden tests, the manifest
and grader data live in a private tree that is never passed to a runner.

RUNNER POLICY
-------------
This harness does NOT start an agent. Without an OS-level sandbox that can
enforce per-baseline filesystem boundaries, a shell template cannot guarantee
that one agent cannot read another baseline's repo, a real user index, or
ambient provider credentials. ``--runner`` is therefore accepted only to
report a disabled, inspectable execution contract; it never executes a model.
LIVE MODEL outcomes stay LIVE_MODEL_NOT_VERIFIED.

Run:
    python scripts/agent_continuity_benchmark.py --workdir DIR
    python scripts/agent_continuity_benchmark.py --workdir DIR --out result.json
    python scripts/agent_continuity_benchmark.py --workdir DIR --task T2
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("VOYAGER_NO_SYNC", "1")

from voyager.budget import apply_budget, estimate_tokens, parse_budget  # noqa: E402
from voyager.continuity import build_continuation_bundle  # noqa: E402
from voyager.model import new_event, new_session  # noqa: E402
from voyager.store import Store  # noqa: E402

SCHEMA = "agent-continuity-benchmark-v2"
BASELINES = ("no_history", "raw_history", "latest_summary", "sessionflow_bundle")
MODES = ("oracle_conditioned", "explicit_goal_continuation", "realistic_work_discovery")
DEFAULT_BUDGET = 4000
EXTERNAL_GOAL_MARKER = "EXTERNAL GOAL:"
THREAD_ID_MARKER = "EXPLICIT THREAD ID:"

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

# Allow-list for a future, externally sandboxed runner. No provider config,
# token, SSH agent, or real HOME is inherited.
RUNNER_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "OS",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
)


class BenchmarkContractError(RuntimeError):
    """The benchmark layout or runner request violates its own contract."""


class LiveRunnerDisabled(BenchmarkContractError):
    """Live execution needs an OS sandbox this harness does not provide."""


def _mkdir_empty(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Synthetic task construction. Private grader data is NEVER under agent_root.
# T1 keeps the historical calc scenario's context/session text unchanged; its
# hidden assertions additionally pin the default clamp. T2/T3 are independent
# code tasks with different modules, requirements and hidden acceptance tests.
# ---------------------------------------------------------------------------
TASK_SPECS: Dict[str, Dict[str, Any]] = {
    "T1": {
        "files": {
            "calc.py": "def add(a, b):\n    return a + b\n",
            "tests/test_visible.py": (
                "from calc import add\n\n\ndef test_add():\n"
                "    assert add(1, 2) == 3\n"),
        },
        "hidden_test": (
            "from calc import add\n\n\ndef test_add_three_args():\n"
            "    assert add(1, 2) == 3\n"
            "    assert add(1, 9, 5) == 5\n"
            "    assert add(8, 9) == 10\n"),
        "hidden_answer_line": "    assert add(1, 9, 5) == 5",
        "goal": "extend add() with an optional third argument clamp, default 10",
        "required_fact": "clamp, default 10",
        "sessions": [
            {
                "sid": "codex:calc-task", "provider": "codex",
                "native_id": "calc-task", "source_file": "calc.jsonl",
                "title": "calc clamp work", "started": 1_700_000_000,
                "updated": 1_700_000_030, "message_count": 3, "tool_count": 1,
                "events": [
                    {"ts": 1_700_000_000, "kind": "user",
                     "content": "GOAL: extend add() with an optional third argument clamp, default 10"},
                    {"ts": 1_700_000_010, "kind": "assistant",
                     "content": "NEXT: add a third positional `clamp=10`; cap the sum at clamp"},
                    {"ts": 1_700_000_020, "kind": "tool_call", "content": "",
                     "command": "pytest -q", "exit_code": 0},
                ],
            },
            {
                "sid": "claude:calc-old", "provider": "claude",
                "native_id": "calc-old", "source_file": "calc-old.jsonl",
                "title": "old clamp idea", "started": 1_699_000_000,
                "updated": 1_699_000_010, "message_count": 1, "tool_count": 0,
                "source_missing": True,
                "events": [
                    {"ts": 1_699_000_000, "kind": "assistant",
                     "content": "OLD: maybe clamp default should be 100"},
                ],
            },
        ],
        "thread_title": "calc clamp",
        "thread_goal": "extend add() with a clamp argument",
        "decoy_title": "unrelated docs cleanup",
        "decoy_goal": "rewrite README",
    },
    "T2": {
        "files": {
            "textutil.py": (
                "def normalize(value):\n    return value.strip()\n"),
            "tests/test_visible.py": (
                "from textutil import normalize\n\n\ndef test_normalize():\n"
                "    assert normalize('  Hi ') == 'Hi'\n"),
        },
        "hidden_test": (
            "from textutil import normalize\n\n\ndef test_normalize_case_option():\n"
            "    assert normalize('  Hi ') == 'Hi'\n"
            "    assert normalize('  Hi ', keep_case=True) == 'Hi'\n"
            "    assert normalize('  Hi ', keep_case=False) == 'hi'\n"),
        "hidden_answer_line": "    assert normalize('  Hi ', keep_case=False) == 'hi'",
        "goal": ("extend normalize() with keep_case=True by default; "
                 "pass keep_case=False to lower-case the result"),
        "required_fact": "keep_case, default True, lower-case when False",
        "sessions": [
            {
                "sid": "codex:textutil-task", "provider": "codex",
                "native_id": "textutil-task", "source_file": "textutil.jsonl",
                "title": "textutil case work", "started": 1_700_100_000,
                "updated": 1_700_100_030, "message_count": 3, "tool_count": 1,
                "events": [
                    {"ts": 1_700_100_000, "kind": "user",
                     "content": "GOAL: extend normalize() with keep_case=True by default; pass keep_case=False to lower-case the result"},
                    {"ts": 1_700_100_010, "kind": "assistant",
                     "content": "NEXT: add keep_case=True; when false return value.strip().lower()"},
                    {"ts": 1_700_100_020, "kind": "tool_call", "content": "",
                     "command": "pytest -q", "exit_code": 0},
                ],
            },
            {
                "sid": "claude:textutil-old", "provider": "claude",
                "native_id": "textutil-old", "source_file": "textutil-old.jsonl",
                "title": "old case idea", "started": 1_699_100_000,
                "updated": 1_699_100_010, "message_count": 1, "tool_count": 0,
                "source_missing": True,
                "events": [
                    {"ts": 1_699_100_000, "kind": "assistant",
                     "content": "OLD: maybe always lower-case even when keep_case is true"},
                ],
            },
        ],
        "thread_title": "textutil case option",
        "thread_goal": "add keep_case to normalize()",
        "decoy_title": "unrelated docs cleanup",
        "decoy_goal": "rewrite README",
    },
    "T3": {
        "files": {
            "ledger.py": (
                "def total(entries):\n"
                "    return sum(entry['amount'] for entry in entries)\n"),
            "tests/test_visible.py": (
                "from ledger import total\n\n\ndef test_total():\n"
                "    assert total([{'amount': 2}, {'amount': 3}]) == 5\n"),
        },
        "hidden_test": (
            "from ledger import total\n\n\ndef test_total_ignores_bad_entries():\n"
            "    assert total([{'amount': 10}, {'amount': -3}, "
            "{'amount': 'x'}]) == 10\n"),
        "hidden_answer_line": "    assert total([{'amount': 10}, {'amount': -3}, {'amount': 'x'}]) == 10",
        "goal": "make total() ignore negative amounts and non-numeric values",
        "required_fact": "ignore negative amounts and non-numeric values",
        "sessions": [
            {
                "sid": "codex:ledger-task", "provider": "codex",
                "native_id": "ledger-task", "source_file": "ledger.jsonl",
                "title": "ledger validation work", "started": 1_700_200_000,
                "updated": 1_700_200_030, "message_count": 3, "tool_count": 1,
                "events": [
                    {"ts": 1_700_200_000, "kind": "user",
                     "content": "GOAL: make total() ignore negative amounts and non-numeric values"},
                    {"ts": 1_700_200_010, "kind": "assistant",
                     "content": "NEXT: skip entries whose amount is negative or not an int/float"},
                    {"ts": 1_700_200_020, "kind": "tool_call", "content": "",
                     "command": "pytest -q", "exit_code": 0},
                ],
            },
            {
                "sid": "claude:ledger-old", "provider": "claude",
                "native_id": "ledger-old", "source_file": "ledger-old.jsonl",
                "title": "old ledger idea", "started": 1_699_200_000,
                "updated": 1_699_200_010, "message_count": 1, "tool_count": 0,
                "source_missing": True,
                "events": [
                    {"ts": 1_699_200_000, "kind": "assistant",
                     "content": "OLD: maybe subtract negative amounts to correct the total"},
                ],
            },
        ],
        "thread_title": "ledger validation",
        "thread_goal": "skip invalid ledger amounts",
        "decoy_title": "unrelated docs cleanup",
        "decoy_goal": "rewrite README",
    },
}


def _write_repo_template(repo: Path, spec: Dict[str, Any]) -> None:
    for rel, content in spec["files"].items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _write_index(db: Path, repo: Path,
                 spec: Dict[str, Any]) -> Dict[str, str]:
    """Build the task's synthetic sessions/threads, scoped to `repo`."""
    db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db)
    store.con.commit()
    for session in spec["sessions"]:
        sid = session["sid"]
        src = db.parent / session["source_file"]
        src.write_text("{}\n", encoding="utf-8")
        events = []
        for seq, raw in enumerate(session["events"]):
            kwargs: Dict[str, Any] = {
                "sid": sid, "ts": raw["ts"], "seq": seq,
                "kind": raw["kind"], "content": raw.get("content", ""),
            }
            for optional in ("command", "exit_code"):
                if optional in raw:
                    kwargs[optional] = raw[optional]
            events.append(new_event(**kwargs))
        row = new_session(
            id=sid, provider=session["provider"],
            native_session_id=session["native_id"], title=session["title"],
            started_at=session["started"], updated_at=session["updated"],
            cwd=str(repo), repo_root=str(repo),
            message_count=session["message_count"],
            tool_count=session["tool_count"], can_resume=False,
            metadata={}, raw_metadata={})
        store.replace_session(row, events, session["provider"], src)
        if session.get("source_missing"):
            store.q(
                "UPDATE sessions SET source_state='SOURCE_MISSING' WHERE id=?",
                (sid,))
    store.con.commit()

    correct = store.thread_create(
        repo_root=str(repo), title=spec["thread_title"], goal=spec["thread_goal"])
    for session in spec["sessions"]:
        store.thread_attach(correct, session["sid"])
    decoy = store.thread_create(
        repo_root=str(repo), title=spec["decoy_title"], goal=spec["decoy_goal"])
    store.con.commit()
    store.close()
    return {"correct_thread_id": correct, "decoy_thread_id": decoy}


def build_task(workdir: Path, task_id: str = "T1") -> Dict[str, Any]:
    """Create one synthetic task with private grader data and a repo template."""
    if task_id not in TASK_SPECS:
        raise BenchmarkContractError("unknown task id: " + task_id)
    spec = TASK_SPECS[task_id]
    workdir = Path(workdir)
    private = workdir / "private"
    _mkdir_empty(private)
    repo_template = private / "repo_template"
    _write_repo_template(repo_template, spec)

    hidden_dir = private / "hidden_tests"
    hidden_dir.mkdir(parents=True, exist_ok=True)
    hidden_test = hidden_dir / "test_hidden.py"
    hidden_test.write_text(spec["hidden_test"], encoding="utf-8")

    index_db = private / "index.db"
    threads = _write_index(index_db, repo_template, spec)

    manifest = {
        "schema": SCHEMA,
        "task_id": task_id,
        "repo_template": str(repo_template),
        "hidden_test": str(hidden_test),
        "hidden_answer_line": spec["hidden_answer_line"],
        "index_db": str(index_db),
        "private_root": str(private),
        "goal": spec["goal"],
        "required_fact": spec["required_fact"],
        "session_ids": [s["sid"] for s in spec["sessions"]],
        **threads,
    }
    (private / "task_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# Context construction: oracle inputs are mode-dependent, never implicit.
# ---------------------------------------------------------------------------
def build_contexts(workdir: Path, manifest: Dict[str, Any],
                   mode: str) -> Dict[str, str]:
    if mode not in MODES:
        raise BenchmarkContractError("unknown evaluation mode: " + mode)
    store = Store(Path(manifest["index_db"]))
    rows = [store.session(s)[0] for s in manifest["session_ids"]]
    raw = "\n".join(
        str(e["content"] or "") + " " + str(e["command"] or "")
        for r in rows for e in store.events(r["id"]))
    latest = rows[0]
    latest_text = "\n".join(
        str(e["content"] or "") + " " + str(e["command"] or "")
        for e in store.events(latest["id"]))
    naive = " ".join("%s:%s" % (r["provider"], r["title"]) for r in rows)

    # Only oracle_conditioned gets the goal and the correct thread. Other modes
    # must discover or receive those facts from the same external prompt.
    thread = None
    goal = None
    if mode == "oracle_conditioned":
        goal = manifest["goal"]
        thread = store.thread_get(manifest["correct_thread_id"])
    bundle = build_continuation_bundle(store, rows, goal=goal, thread=thread)
    packed, _ = apply_budget(bundle, parse_budget(str(DEFAULT_BUDGET)))
    store.close()
    return {
        "no_history": "",
        "raw_history": raw,
        "latest_summary": latest_text + "\n" + naive,
        "sessionflow_bundle": packed,
    }


def write_prompt(agent_root: Path, manifest: Dict[str, Any], mode: str,
                 name: str, context: str, repo: Path,
                 oracle_thread_id: Optional[str] = None) -> Path:
    lines = ["You are continuing work in the repo at %s." % repo]
    if mode == "oracle_conditioned":
        if not oracle_thread_id:
            raise BenchmarkContractError(
                "oracle_conditioned needs the baseline-local WorkThread id")
        lines += [
            "EVALUATION MODE: ORACLE_CONDITIONED (context-quality only).",
            EXTERNAL_GOAL_MARKER + " " + manifest["goal"],
            THREAD_ID_MARKER + " " + oracle_thread_id,
            "This run must NOT be counted as autonomous work discovery.",
        ]
    elif mode == "explicit_goal_continuation":
        lines += [
            "EVALUATION MODE: EXPLICIT_GOAL_CONTINUATION.",
            EXTERNAL_GOAL_MARKER + " " + manifest["goal"],
            "No WorkThread id is supplied; discover it if your tools allow it.",
        ]
    else:
        lines += [
            "EVALUATION MODE: REALISTIC_WORK_DISCOVERY.",
            "No external goal or WorkThread id is supplied.",
            "Discover the unfinished task from the repository and available history.",
        ]
    lines.append("")
    if context:
        lines += ["<prior-context>", context, "</prior-context>"]
    else:
        lines.append("(no prior context provided)")
    lines += ["", "The current test suite is green. Make the needed change."]
    path = agent_root / ("prompt_%s.txt" % name)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def prepare_baseline(workdir: Path, manifest: Dict[str, Any], mode: str,
                     name: str, context: str) -> Dict[str, Any]:
    """Give one baseline an independent repo, prompt, home and index."""
    agent_root = workdir / "baselines" / mode / name / "agent"
    _mkdir_empty(agent_root)
    repo = agent_root / "repo"
    shutil.copytree(manifest["repo_template"], repo)
    home = agent_root / "home"
    db = home / ".voyager" / "index.db"
    threads = _write_index(db, repo, TASK_SPECS[manifest["task_id"]])
    local_id = threads["correct_thread_id"]
    store = Store(db)
    try:
        thread = store.thread_get(local_id)
        if thread is None or len(store.thread_members(local_id)) != 2:
            raise BenchmarkContractError(
                "baseline index has no usable local WorkThread: " + local_id)
    finally:
        store.close()
    prompt = write_prompt(
        agent_root, manifest, mode, name, context, repo,
        oracle_thread_id=local_id if mode == "oracle_conditioned" else None)
    tmp = agent_root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {
        "agent_root": str(agent_root),
        "repo": str(repo),
        "prompt": str(prompt),
        "home": str(home),
        "index_db": str(db),
        "correct_thread_id": threads["correct_thread_id"],
    }


def _runner_env(agent_root: Path) -> Dict[str, str]:
    env = {k: os.environ[k] for k in RUNNER_ENV_ALLOWLIST if k in os.environ}
    home = str(agent_root / "home")
    tmp = str(agent_root / "tmp")
    env.update({
        "HOME": home,
        "USERPROFILE": home,
        "APPDATA": str(agent_root / "home" / "AppData"),
        "LOCALAPPDATA": str(agent_root / "home" / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(agent_root / "home" / ".config"),
        "XDG_DATA_HOME": str(agent_root / "home" / ".local"),
        "XDG_CACHE_HOME": str(agent_root / "home" / ".cache"),
        "TMP": tmp,
        "TEMP": tmp,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "VOYAGER_NO_SYNC": "1",
    })
    return env


def runner_contract(template: str, layout: Dict[str, Any],
                    workdir: Path) -> Dict[str, Any]:
    """Describe, but never execute, a requested runner."""
    if "{prompt}" not in template or "{repo}" not in template:
        raise BenchmarkContractError(
            "runner template must contain both {prompt} and {repo}")
    private = Path(manifest_private_root(workdir))
    for needle in (str(private), "test_hidden.py", "task_manifest.json"):
        if needle in template:
            raise BenchmarkContractError(
                "runner template contains an obvious private grader reference; "
                "this lint check is not an isolation boundary")
    agent_root = Path(layout["agent_root"])
    argv = shlex.split(template.format(prompt=layout["prompt"], repo=layout["repo"]))
    return {
        "status": "DISABLED",
        "reason": "NO_OS_SANDBOX",
        "live_execution_supported": False,
        "private_path_check": "LINT_ONLY_NOT_SECURITY_BOUNDARY",
        "cwd": layout["repo"],
        "argv": argv,
        "env_keys": sorted(_runner_env(agent_root)),
        "forbidden_env_omitted": [
            "HOME(real)", "USERPROFILE(real)", "APPDATA(real)", "LOCALAPPDATA(real)",
            "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "CODEX_*", "CLAUDE_*", "DSH_*", "WORKBUDDY_*",
        ],
        "private_grader_root": str(private),
        "other_baseline_repos readable": "NOT PREVENTED without OS sandbox",
    }


def manifest_private_root(workdir: Path) -> str:
    return str(Path(workdir) / "private")


RUNNER_STATUSES = (
    "completed", "spawn_failed", "timeout", "evaluator_failed",
)


def classify_outcome(runner_returncode: Optional[int],
                     verdict: Dict[str, Any],
                     runner_status: str = "completed") -> Dict[str, Any]:
    """Classify runner, evaluator and hidden-test failures separately.

    ``runner_status`` is supplied by the future OS-sandboxed runner.  This
    dry-run harness never launches that runner, but the contract must exist
    before live execution is enabled so a crashed agent can never be scored as
    a successful continuation.
    """
    if runner_status not in RUNNER_STATUSES:
        raise BenchmarkContractError("unknown runner status: " + runner_status)
    base: Dict[str, Any] = {
        "runner_status": runner_status,
        "runner_returncode": runner_returncode,
        "evaluator_returncode": verdict.get("evaluator_returncode"),
    }

    def failed(failure_type: str) -> Dict[str, Any]:
        return {
            **base,
            "final_code_meets_goal": False,
            "independent_tests_pass": False,
            "failure_type": failure_type,
        }

    if runner_status == "spawn_failed":
        return failed("runner_spawn_failed")
    if runner_status == "timeout":
        return failed("runner_timeout")
    if runner_status == "evaluator_failed" or verdict.get("evaluator_failed") is True:
        return failed("evaluator_failed")
    if runner_returncode is None:
        return failed("runner_error")
    if runner_returncode != 0:
        return failed("runner_failed")

    passed = bool(verdict.get("independent_tests_pass"))
    return {
        **base,
        "final_code_meets_goal": passed,
        "independent_tests_pass": passed,
        "failure_type": "success" if passed else "hidden_tests_failed",
    }


def _safe_component(value: Any) -> str:
    text = str(value)
    return "".join(ch if ch.isalnum() or ch in "-_" else "_"
                   for ch in text)[:80]


def evaluate_repo(repo: Path, hidden_test: Path, python: str,
                  eval_root: Path, mode: str = "unspecified",
                  baseline: str = "unspecified",
                  run_id: str = "adhoc") -> Dict[str, Any]:
    """Evaluate a finished repo with hidden tests copied OUTSIDE agent_root.

    Each mode/baseline/run gets its own evaluation directory.  Reusing a
    directory would let a later run's copied repo overwrite evidence from an
    earlier run, which makes concurrent or repeated comparisons misleading.

    The pytest exit code is reported ONLY as ``evaluator_returncode``.  No
    Agent Runner has executed here, so ``runner_returncode`` stays None and
    ``runner_status`` stays NOT_EXECUTED; the two codes must never be conflated.
    """
    scope = "_".join(
        _safe_component(x) for x in (run_id, mode, baseline))
    eval_repo = Path(eval_root) / (scope + "__eval")
    _mkdir_empty(eval_repo)
    shutil.copytree(repo, eval_repo, dirs_exist_ok=True)
    target = eval_repo / "tests" / "test_hidden.py"
    target.write_text(Path(hidden_test).read_text(encoding="utf-8"), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "VOYAGER_NO_SYNC": "1",
    }
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", "-q", "tests"],
            cwd=str(eval_repo), env=env, capture_output=True,
            text=True, timeout=120)
        passed = proc.returncode == 0
        # The pytest exit code belongs to the evaluator, never to the Agent
        # Runner.  No runner has executed in this dry-run.
        return {
            "independent_tests_pass": passed,
            "verdict_status": "passed" if passed else "hidden_tests_failed",
            "failure_type": "success" if passed else "hidden_tests_failed",
            "evaluator_returncode": proc.returncode,
            "runner_returncode": None,
            "runner_status": "NOT_EXECUTED",
            "eval_repo": str(eval_repo),
            "output_tail": (proc.stdout + "\n" + proc.stderr)[-800:],
        }
    except Exception as e:  # evaluation failure is not a pass
        return {
            "independent_tests_pass": False,
            "verdict_status": "evaluator_failed",
            "failure_type": "evaluator_failed",
            "evaluator_failed": True,
            "evaluator_returncode": None,
            "runner_returncode": None,
            "runner_status": "NOT_EXECUTED",
            "eval_repo": str(eval_repo),
            "output_tail": "evaluation error: %s" % e,
        }


def _not_verified_row() -> Dict[str, Any]:
    return {m: "LIVE_MODEL_NOT_VERIFIED" for m in OUTCOME_METRICS}


def run_level2(workdir: Path, runner: Optional[str] = None,
               python: Optional[str] = None,
               task_id: str = "T1") -> Dict[str, Any]:
    """Build all modes/baselines for one task, but never launch an agent."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    py = python or sys.executable
    manifest = build_task(workdir, task_id=task_id)
    private = Path(manifest["private_root"])
    eval_root = private / "eval"
    eval_root.mkdir(parents=True, exist_ok=True)

    sanity_repo = private / "sanity_repo"
    if sanity_repo.exists():
        shutil.rmtree(sanity_repo)
    shutil.copytree(manifest["repo_template"], sanity_repo)
    sanity = evaluate_repo(
        sanity_repo, Path(manifest["hidden_test"]), py, eval_root,
        mode="sanity", baseline="hidden_test", run_id="dryrun")

    runner_plan: Optional[Dict[str, Any]] = None
    if runner:
        first = prepare_baseline(workdir, manifest, MODES[0], BASELINES[0], "")
        runner_plan = runner_contract(runner, first, workdir)

    modes: Dict[str, Any] = {}
    for mode in MODES:
        contexts = build_contexts(workdir, manifest, mode)
        rows: Dict[str, Any] = {}
        for name in BASELINES:
            layout = prepare_baseline(workdir, manifest, mode, name, contexts[name])
            prompt_text = Path(layout["prompt"]).read_text(encoding="utf-8")
            hidden_token = Path(manifest["hidden_test"]).read_text(encoding="utf-8")
            answer_line = [ln for ln in hidden_token.splitlines()
                           if ln == manifest["hidden_answer_line"]]
            if answer_line and answer_line[0] in prompt_text:
                raise BenchmarkContractError("hidden answer leaked into " + name)
            rows[name] = {
                "context_tokens": estimate_tokens(contexts[name]),
                "prompt_file": layout["prompt"],
                "layout": layout,
                "external_goal_supplied": EXTERNAL_GOAL_MARKER in prompt_text,
                "thread_id_supplied": THREAD_ID_MARKER in prompt_text,
                "evaluation": _not_verified_row(),
                "runner": {"status": "NOT_EXECUTED", "reason": "LIVE_RUNNER_DISABLED"},
            }
        modes[mode] = {
            "counted_for": {
                "oracle_conditioned": "context_quality_only",
                "explicit_goal_continuation": "explicit_goal_continuation",
                "realistic_work_discovery": "work_discovery",
            }[mode],
            "external_goal": mode in ("oracle_conditioned", "explicit_goal_continuation"),
            "explicit_thread": mode == "oracle_conditioned",
            "baselines": rows,
        }

    return {
        "schema": SCHEMA,
        "mode": "DRY_RUN",
        "live_model_authorised": False,
        "note": ("No agent was launched. Live execution is disabled because an "
                 "OS sandbox is required to prevent cross-baseline/real-index "
                 "access. All agent outcomes are LIVE_MODEL_NOT_VERIFIED."),
        "task": manifest,
        "sanity_hidden_test_fails_on_start": (sanity["independent_tests_pass"] is False),
        "sanity_detail": sanity,
        "modes": modes,
        "runner": runner_plan or {
            "status": "NO_RUNNER_REQUESTED",
            "live_execution_supported": False,
            "reason": "NO_OS_SANDBOX",
        },
        "isolation": {
            "private_grader_root": str(private),
            "hidden_test_under_agent_root": False,
            "manifest_under_agent_root": False,
            "per_baseline_repo": True,
            "per_baseline_home": True,
            "os_sandbox": False,
            "live_runner_disabled": True,
        },
        "no_llm_in_harness": True,
        "no_real_user_data": True,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", required=True,
                    help="isolated scratch dir for the synthetic task")
    ap.add_argument("--task", default="T1", choices=sorted(TASK_SPECS),
                    help="synthetic task id (T1 calc, T2 textutil, T3 ledger)")
    ap.add_argument("--out", default=None, help="write structured JSON here")
    ap.add_argument("--runner", default=None,
                    help="inspect a runner contract only; NO agent is launched")
    ap.add_argument("--python", default=None,
                    help="python with pytest for the independent evaluator")
    args = ap.parse_args(argv)
    result = run_level2(Path(args.workdir), args.runner, args.python, args.task)
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
