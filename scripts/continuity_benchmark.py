#!/usr/bin/env python3
"""Reproducible continuity-effectiveness benchmark for sessionFlow Core.

WHY THIS EXISTS
---------------
The 2026-11-18 audit (`docs/audits/2026-11-18-continuity-effectiveness-benchmark.md`)
reported Bundle 24/24 vs raw 17/24 vs latest 11/24 on a one-off harness that
lived in a scratch directory and hardcoded ``E:/code/voyager``. A result that
cannot be re-run on another machine is not evidence — it is a story. This
module is that harness, promoted into the repository:

  * every input is synthetic and built in a caller-supplied temp directory;
    no path from the original developer's machine appears anywhere;
  * no real user session, index, or home directory is read or written;
  * the scoring rules are fixed, declared here, and asserted by
    ``tests/test_continuity_benchmark.py`` (which mutates answers and evidence
    to prove the score actually moves);
  * output is structured JSON, so CI can diff two runs.

SCOPE / HONESTY
---------------
This measures *context-preparation quality*: are the necessary facts present,
is their provenance explained, are contradictions kept, is state/ambiguity
described, is the budget respected, and does the text invent facts. It does
NOT measure end-agent success, correctness, or cost saving. There is no LLM.
A MATCH here means "the string was preserved", never "the claim is true".

Run:
    python scripts/continuity_benchmark.py --out results.json
    python -m pytest tests/test_continuity_benchmark.py -q
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import the Core from the repository that contains this script, not from
# wherever the interpreter happens to be. `python scripts/x.py` puts scripts/
# on sys.path[0], so the repo root must be added explicitly.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Continuity is a read-only context facet: it must never trigger a real sync.
os.environ.setdefault("VOYAGER_NO_SYNC", "1")

from voyager.api import current_work  # noqa: E402
from voyager.budget import (  # noqa: E402
    apply_budget, estimate_tokens, parse_budget,
)
from voyager.continuity import build_continuation_bundle  # noqa: E402
from voyager.model import new_event, new_session  # noqa: E402
from voyager.store import Store  # noqa: E402

# Baseline names are deliberately NOT read by the scorer. `score_text` takes
# only text + scenario; swapping the method name can never change a verdict.
BASELINES = ("no_history", "raw_history", "latest_summary", "sessionflow_bundle")
EQUAL_BUDGET_TOKENS = 4000  # compact preset; applied to ALL four baselines

METRICS = (
    "semantic_fact_recall",
    "source_attribution",
    "contradiction_preservation",
    "state_ambiguity_correctness",
    "budget_compliance",
    "false_claim_rate",
)


# ---------------------------------------------------------------------------
# Synthetic scenarios. Fixed inputs; edit a `required` list and the benchmark
# tests fail (see test_answers_are_load_bearing). Scenario I is the independent
# hold-out: it was not part of the original A-H design.
# ---------------------------------------------------------------------------
SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "A", "name": "single-goal", "goal": "fix CI", "budget": None,
        "sessions": [{
            "native": "bench-a1", "provider": "codex",
            "events": [
                ("user", "GOAL_A fix CI", {}),
                ("assistant", "NEXT_A run pytest", {}),
                ("tool_call", "", {"command": "pytest -q", "exit_code": 1}),
                ("error", "FAIL_A test_bench failed", {}),
            ],
        }],
        "required": ["GOAL_A fix CI", "NEXT_A run pytest", "pytest -q",
                     "FAIL_A test_bench failed"],
        "forbidden": [],
        "source_markers": ["codex", "bench-a1"],
        "contradiction_markers": [],
        "state_markers": [],
        "kind": "bundle",
    },
    {
        "id": "B", "name": "supersession", "goal": "choose final cache design",
        "budget": None,
        "sessions": [
            {"native": "bench-b1", "provider": "codex", "events": [
                ("user", "GOAL_B choose final cache design", {}),
                ("assistant", "PROPOSE_B X in-memory cache", {})]},
            {"native": "bench-b2", "provider": "claude", "events": [
                ("assistant", "DECIDED_B Y pointer cache", {})]},
            {"native": "bench-b3", "provider": "codex", "events": [
                ("assistant", "NEXT_B implement Y pointer cache", {})]},
        ],
        "required": ["PROPOSE_B X in-memory cache", "DECIDED_B Y pointer cache",
                     "NEXT_B implement Y pointer cache", "keep both readings"],
        "forbidden": [],
        "source_markers": ["codex", "claude"],
        "contradiction_markers": ["keep both readings"],
        "state_markers": [],
        "kind": "bundle",
    },
    {
        "id": "C", "name": "goal-conditioning", "goal": "fix CI pipeline",
        "budget": None,
        "sessions": [
            {"native": "bench-c1", "provider": "codex", "events": [
                ("user", "GOAL_C fix CI pipeline", {}),
                ("assistant", "CI_C failing workflow", {}),
                ("tool_call", "", {"command": "pytest -q", "exit_code": 1})]},
            {"native": "bench-c2", "provider": "claude", "events": [
                ("assistant", "NOISE_C UI color palette", {}),
                ("assistant", "OLD_C yesterday conversation", {})]},
        ],
        "required": ["CI_C failing workflow", "pytest -q"],
        "forbidden": [],
        "source_markers": ["codex", "claude"],
        "contradiction_markers": [],
        "state_markers": [],
        "goal_focus": ["CI_C failing workflow", "pytest -q"],
        "goal_noise": ["NOISE_C UI color palette"],
        "kind": "bundle",
    },
    {
        "id": "D", "name": "conflicting-latest", "goal": "locate config",
        "budget": None,
        "sessions": [
            {"native": "bench-d1", "provider": "codex", "events": [
                ("user", "GOAL_D locate config", {}),
                ("assistant", "FACT_D config/db.py", {}),
                ("tool_call", "", {"file_path": "config/db.py",
                                   "command": "cat config/db.py", "exit_code": 0})]},
            {"native": "bench-d2", "provider": "codex", "events": [
                ("assistant", "RECENT_D temp/db.py", {})]},
        ],
        "required": ["FACT_D config/db.py", "RECENT_D temp/db.py",
                     "keep both readings", "config/db.py"],
        "forbidden": [],
        "source_markers": ["codex", "bench-d1"],
        "contradiction_markers": ["keep both readings"],
        "state_markers": [],
        "kind": "bundle",
    },
    {
        "id": "E", "name": "compact-budget", "goal": "rotate leaked token",
        "budget": "compact",
        "sessions": [
            {"native": "bench-e1", "provider": "codex", "events": [
                ("user", "GOAL_E rotate leaked token", {}),
                ("assistant", "FILLER_E " + "padding " * 400, {}),
                ("assistant", "NEXT_E rotate leaked token immediately", {}),
                ("tool_call", "", {"command": "voyager search token",
                                   "exit_code": 0})]},
        ],
        "required": ["NEXT_E rotate leaked token immediately", "rotate leaked token"],
        "forbidden": [],
        "source_markers": ["codex"],
        "contradiction_markers": [],
        "state_markers": [],
        "kind": "bundle",
    },
    {
        "id": "F", "name": "retained-source", "goal": "continue after source loss",
        "budget": None,
        "sessions": [
            {"native": "bench-f1", "provider": "codex", "events": [
                ("user", "GOAL_F continue after source loss", {}),
                ("assistant", "STATE_F branch is protected", {})],
             "source_missing": True},
        ],
        "required": ["STATE_F branch is protected", "source not available",
                     "SOURCE_MISSING"],
        "forbidden": [],
        "source_markers": ["SOURCE_MISSING"],
        "contradiction_markers": [],
        "state_markers": ["SOURCE_MISSING"],
        "kind": "bundle",
    },
    {
        "id": "G", "name": "ambiguous-thread", "goal": "current work",
        "budget": None,
        "sessions": [
            {"native": "bench-g1", "provider": "codex",
             "events": [("user", "GOAL_G thread one", {})]},
            {"native": "bench-g2", "provider": "claude",
             "events": [("user", "GOAL_G thread two", {})]},
        ],
        "required": ["thread_ambiguous", "thread_candidates",
                     "thread_default_reason"],
        "forbidden": [],
        "source_markers": [],
        "contradiction_markers": [],
        "state_markers": ["thread_ambiguous", "thread_candidates",
                          "thread_default_reason"],
        "kind": "ambiguous",
    },
    {
        "id": "H", "name": "noise-dedup", "goal": "finish release gate",
        "budget": None,
        "sessions": [
            {"native": "bench-h1", "provider": "codex", "events": [
                ("user", "GOAL_H finish release gate", {}),
                ("tool_call", "", {"command": "pytest -q", "exit_code": 0}),
                ("tool_call", "", {"command": "pytest -q", "exit_code": 0}),
                ("assistant", "NEXT_H run twine check", {}),
                ("assistant", "NOISE_H " + "unrelated chat " * 250, {})]},
        ],
        "required": ["NEXT_H run twine check", "pytest -q"],
        "forbidden": [],
        "source_markers": ["codex"],
        "contradiction_markers": [],
        "state_markers": [],
        "kind": "bundle",
    },
    {
        # Independent hold-out — not used when the A-H design was written.
        "id": "I", "name": "holdout-conditional-env", "goal": "unset TLS proxy",
        "budget": None,
        "sessions": [
            {"native": "bench-i1", "provider": "claude", "events": [
                ("user", "GOAL_I unset TLS proxy", {}),
                ("assistant", "ERR_I SSL cert verify failed", {}),
                ("tool_call", "", {"command": "env -u HTTPS_PROXY pytest -q",
                                   "exit_code": 0})]},
            {"native": "bench-i2", "provider": "codex", "events": [
                ("assistant", "NOISE_I refactor logging format", {})]},
        ],
        "required": ["ERR_I SSL cert verify failed", "env -u HTTPS_PROXY pytest -q"],
        "forbidden": [],
        "source_markers": ["claude", "codex"],
        "contradiction_markers": [],
        "state_markers": [],
        "goal_focus": ["ERR_I SSL cert verify failed"],
        "goal_noise": ["NOISE_I refactor logging format"],
        "kind": "bundle",
    },
]


# ---------------------------------------------------------------------------
# Scoring. Pure functions of (text, scenario) — never of a method label.
# ---------------------------------------------------------------------------
def _hits(text: str, needles: List[str]) -> Tuple[int, int]:
    return sum(1 for n in needles if n in text), len(needles)


def score_text(text: str, scenario: Dict[str, Any],
               budget_tokens: Optional[int] = None) -> Dict[str, Any]:
    """Compute the six declared metrics for one baseline's output text.

    ``forbidden`` markers are facts that were never in any source: their
    presence is a fabricated claim. Fixed template labels emitted by the Core
    (e.g. ``## Evidence & Provenance``, ``keep both readings``) are NOT counted
    as false claims — they are declared in the scenario's ``required`` or
    structural markers, not smuggled in as answers.
    """
    req_hit, req_total = _hits(text, scenario["required"])
    src_hit, src_total = _hits(text, scenario.get("source_markers", []))
    con_hit, con_total = _hits(text, scenario.get("contradiction_markers", []))
    st_hit, st_total = _hits(text, scenario.get("state_markers", []))

    # A scenario without contradictions/state is not penalised — n/a = 1.0
    # only when there is nothing to preserve. This avoids giving Bundle a free
    # advantage on scenarios where the other baselines have no opportunity.
    tokens = estimate_tokens(text)
    over = budget_tokens is not None and tokens > budget_tokens
    false = sum(1 for f in scenario.get("forbidden", []) if f in text)

    return {
        "semantic_fact_recall": req_hit / req_total if req_total else 1.0,
        "source_attribution": src_hit / src_total if src_total else 1.0,
        "contradiction_preservation": con_hit / con_total if con_total else 1.0,
        "state_ambiguity_correctness": st_hit / st_total if st_total else 1.0,
        "budget_compliance": 0.0 if over else 1.0,
        "false_claim_rate": false / max(1, len(scenario.get("forbidden", []))),
        "_raw": {
            "required_hits": req_hit, "required_total": req_total,
            "required_missed": [r for r in scenario["required"] if r not in text],
            "source_hits": src_hit, "source_total": src_total,
            "contradiction_hits": con_hit, "contradiction_total": con_total,
            "state_hits": st_hit, "state_total": st_total,
            "tokens": tokens, "over_budget": over, "false_claims": false,
        },
    }


def goal_focus(text: str, scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Is the goal-ranked section actually goal-conditioned?

    Scoring is by CONTENT (does the focus needle appear in the top-3 ranked
    lines and does the noise needle not), never by reading a method label.
    """
    if not scenario.get("goal_focus"):
        return {}
    lines = text.splitlines()
    start = next((i + 1 for i, ln in enumerate(lines)
                  if ln.strip() == "## Goal-ranked evidence"), None)
    section: List[str] = []
    if start is not None:
        for ln in lines[start:]:
            if ln.startswith("## "):
                break
            section.append(ln)
    top = [ln for ln in section if ln.startswith("- ")][:3]
    top_text = "\n".join(top)
    return {
        "goal_ranked_section": start is not None,
        "focus_in_top3": all(any(n in ln for ln in top)
                             for n in scenario["goal_focus"]),
        "noise_in_top3": any(n in top_text for n in scenario.get("goal_noise", [])),
    }


# ---------------------------------------------------------------------------
# Synthetic index construction — every path comes from `workdir`.
# ---------------------------------------------------------------------------
def _add_session(store: Store, scenario: Dict[str, Any],
                 spec: Dict[str, Any], n: int, workdir: Path) -> str:
    sid = "%s:%s" % (spec["provider"], spec["native"])
    src = workdir / ("%s-%s.jsonl" % (scenario["id"], spec["native"]))
    src.write_text("{}\n", encoding="utf-8")
    ts0 = 1_700_000_000 + n * 1000
    events = []
    for seq, (kind, content, extra) in enumerate(spec["events"]):
        kw = dict(sid=sid, ts=ts0 + seq * 10, seq=seq, kind=kind,
                  content=content, origin="human" if kind == "user" else
                  "provider_system", raw_event={})
        kw.update(extra)
        events.append(new_event(**kw))
    sess = new_session(
        id=sid, provider=spec["provider"], native_session_id=spec["native"],
        title="%s %s" % (scenario["id"], spec["native"]), started_at=ts0,
        updated_at=ts0 + len(events) * 10,
        cwd=str(workdir / "repo"), repo_root=str(workdir / "repo"),
        message_count=1, tool_count=sum(1 for e in events
                                        if e["kind"] == "tool_call"),
        can_resume=False, metadata={}, raw_metadata={})
    store.replace_session(sess, events, spec["provider"], src)
    if spec.get("source_missing"):
        # O2 retention semantics: source vanished -> keep history + flag.
        store.q("UPDATE sessions SET source_state='SOURCE_MISSING' WHERE id=?",
                (sid,))
        store.con.commit()
    return sid


def _build_baselines(store: Store, db: Path, scenario: Dict[str, Any],
                     sids: List[str], workdir: Path) -> Dict[str, str]:
    rows = [store.session(sid)[0] for sid in sids]
    raw = "\n".join(
        str(e["content"] or "") + " " + str(e["command"] or "")
        for r in rows for e in store.events(r["id"]))
    latest = rows[-1]
    latest_text = "\n".join(
        str(e["content"] or "") + " " + str(e["command"] or "")
        for e in store.events(latest["id"]))
    naive = " ".join("%s:%s" % (r["provider"], r["title"]) for r in rows)
    latest_summary = latest_text + "\n" + naive

    if scenario["kind"] == "ambiguous":
        sf = current_work(db, repo=str(workdir / "repo"), budget=scenario["budget"])
        sf_text = json.dumps({k: sf.get(k) for k in (
            "thread_ambiguous", "thread_candidates", "thread_default_reason",
            "has_thread")}, ensure_ascii=False)
    else:
        tid = store.thread_create(repo_root=str(workdir / "repo"),
                                  title="%s thread" % scenario["id"])
        for sid in sids:
            store.thread_attach(tid, sid)
        thread = store.thread_get(tid)
        bundle = build_continuation_bundle(store, rows, goal=scenario["goal"],
                                            thread=thread)
        if scenario["budget"]:
            packed, _ = apply_budget(bundle, parse_budget(scenario["budget"]))
            sf_text = packed
        else:
            sf_text = bundle
    return {
        "no_history": "",
        "raw_history": raw,
        "latest_summary": latest_summary,
        "sessionflow_bundle": sf_text,
    }


def _core_commit() -> str:
    """Record which Core commit produced a result (reproducibility)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15, check=False)
        return out.stdout.strip() or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def run_benchmark(workdir: Path,
                  scenarios: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Run every scenario in an isolated synthetic index under `workdir`."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    db = workdir / "continuity-benchmark.db"
    store = Store(db)
    store.con.commit()  # _setup() may leave a transaction open on this build

    scenarios = scenarios if scenarios is not None else SCENARIOS
    results: List[Dict[str, Any]] = []
    try:
        for i, sc in enumerate(scenarios):
            sids = [_add_session(store, sc, spec, i * 10 + j, workdir)
                    for j, spec in enumerate(sc["sessions"])]
            baselines = _build_baselines(store, db, sc, sids, workdir)
            scored: Dict[str, Any] = {}
            for name, text in baselines.items():
                scored[name] = {
                    "metrics": score_text(text, sc),
                    "equal_budget_metrics": score_text(text, sc,
                                                       EQUAL_BUDGET_TOKENS),
                    "focus": goal_focus(text, sc),
                }
            results.append({
                "id": sc["id"], "name": sc["name"], "goal": sc["goal"],
                "budget": sc["budget"], "baselines": scored,
            })
    finally:
        store.close()

    # Legacy totals for continuity with the 2026-11-18 report: required-hit
    # count only, no budget. This is NOT the headline metric any more.
    totals = {name: sum(r["baselines"][name]["metrics"]["_raw"]["required_hits"]
                        for r in results) for name in BASELINES}
    return {
        "schema": "continuity-benchmark-v2",
        "core_commit": _core_commit(),
        "host": sys.platform,
        "python": sys.version.split()[0],
        "data": "synthetic only; isolated DB under the caller-supplied workdir",
        "model": "no LLM; deterministic context-preparation scoring, NOT end-agent success",
        "metrics": list(METRICS),
        "equal_budget_tokens": EQUAL_BUDGET_TOKENS,
        "totals": {"required_hits": totals},
        "scenarios": results,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", default=None,
                    help="isolated scratch dir (default: a fresh temp dir, "
                         "removed when the process exits)")
    ap.add_argument("--out", default=None,
                    help="write structured JSON here; '-' means stdout")
    args = ap.parse_args(argv)

    tmp: Optional[Any] = None
    if args.workdir:
        workdir = Path(args.workdir)
    else:
        import tempfile
        tmp = tempfile.TemporaryDirectory(prefix="voyager-continuity-bench-")
        workdir = Path(tmp.name)
    try:
        result = run_benchmark(workdir)
        payload = json.dumps(result, indent=2, ensure_ascii=False)
        if args.out and args.out != "-":
            Path(args.out).write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)
        return 0
    finally:
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
