"""Goal-conditioned candidate extraction & ranking (roadmap Phase 3 / #4).

Two halves, both deterministic and offline (D10):

1. **Extraction (Phase 3a).** Turn the indexed events of N sessions into
   atomic, provenance-bound :class:`CandidateFact` objects. One fact is one
   claim about the work: a user instruction, an assistant conclusion, a
   command run, a failure, a file touched.

2. **Ranking (Phase 3b).** :func:`rank_candidates` orders facts against an
   optional goal. Scoring is lexical overlap (goal tokens vs fact text,
   paths and commands) expanded through a small static concept lexicon
   (``GOAL_CONCEPTS``) — a *boost*, never a special case — plus recency and
   a fixed per-kind priority (failures and errors outrank chatter).

No goal (or a blank one) means **no ranking**: facts come back in their
input order with ``score=None`` and every consumer keeps the pre-Phase-3
behavior. Provenance is never dropped: every fact carries
``provider:native#seq`` and rendered evidence lines must include it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# CandidateFact (Phase 3a)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateFact:
    kind: str                      # user | assistant | tool_call | error | file
    text: str                      # main human-readable body of the fact
    source_session: str            # voyager session id ("codex:abc")
    provider: str
    native_session_id: str
    ts: Optional[float]
    seq: Optional[int]
    paths: Tuple[str, ...] = ()
    command: Optional[str] = None
    exit_code: Optional[int] = None
    provenance: str = ""           # "provider:native#seq"
    confidence: str = "extracted"  # Phase 3 emits only extracted facts

    def tokens_source(self) -> str:
        """Everything a goal could lexically match against."""
        parts = [self.text, self.command or "", " ".join(self.paths)]
        return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# tokenization + concept lexicon (Phase 3b helpers)
# ---------------------------------------------------------------------------

_LATIN = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> set:
    """Lowercase lexical tokens: latin words (>=2 chars) + CJK bigrams.

    CJK has no spaces, so overlapping character bigrams give deterministic
    substring recall without a segmenter (mirrors the FTS trigram choice).
    """
    t = (text or "").lower()
    toks: set = set()
    for w in _LATIN.findall(t):
        if len(w) >= 2:
            toks.add(w)
    for run in _CJK.findall(t):
        if len(run) == 1:
            toks.add(run)
        else:
            for i in range(len(run) - 1):
                toks.add(run[i:i + 2])
    return toks


# Static concept lexicon. Keys are goal tokens; values are related tokens the
# goal should also match. This is domain *knowledge*, applied as a uniform
# token expansion — not per-goal branching. Deterministic and data-like: add
# rows, never code paths.
GOAL_CONCEPTS: Dict[str, set] = {
    "ci": {"ci", "cd", "pytest", "test", "tests", "testing", "workflow",
           "workflows", "actions", "github", "runner", "pipeline", "build",
           "yml", "act"},
    "cd": {"cd", "pipeline", "deploy", "workflow", "actions"},
    "workflow": {"workflow", "workflows", "actions", "github", "yml", "ci"},
    "workflows": {"workflow", "workflows", "actions", "github", "yml", "ci"},
    "action": {"action", "actions", "github", "workflow", "workflows", "ci"},
    "actions": {"action", "actions", "github", "workflow", "workflows", "ci"},
    "test": {"test", "tests", "testing", "pytest", "assert", "coverage", "ci"},
    "tests": {"test", "tests", "testing", "pytest", "assert", "coverage", "ci"},
    "testing": {"test", "tests", "testing", "pytest", "assert", "coverage", "ci"},
    "pytest": {"pytest", "test", "tests", "testing", "ci"},
    "readme": {"readme", "readme_md", "docs", "documentation", "md", "guide"},
    "doc": {"doc", "docs", "documentation", "readme", "md", "guide"},
    "docs": {"doc", "docs", "documentation", "readme", "md", "guide"},
    "documentation": {"doc", "docs", "documentation", "readme", "md", "guide"},
    "ui": {"ui", "css", "html", "layout", "style", "sidebar", "view", "render"},
    "css": {"css", "style", "html", "layout"},
    "layout": {"layout", "ui", "css", "style"},
    "fix": {"fix", "fixed", "bug", "error", "exit", "fail", "failed",
            "failing", "failure", "traceback", "debug"},
    "fixing": {"fix", "fixed", "bug", "error", "fail", "failed", "debug"},
    "bug": {"bug", "error", "fix", "fail", "failed", "traceback"},
    "fail": {"fail", "failed", "failing", "failure", "exit", "error",
             "traceback"},
    "failed": {"fail", "failed", "failing", "failure", "exit", "error"},
    "failing": {"fail", "failed", "failing", "failure", "exit", "error"},
    "failure": {"fail", "failed", "failing", "failure", "exit", "error"},
    "error": {"error", "errors", "traceback", "exit", "fail", "failed"},
    "adapter": {"adapter", "adapters", "parse", "parser", "dsh", "grok",
                "codex", "claude", "cursor", "kiro", "antigravity"},
    "adapters": {"adapter", "adapters", "parse", "parser", "dsh", "grok",
                 "codex", "claude", "cursor", "kiro", "antigravity"},
    "parse": {"parse", "parser", "parsing", "jsonl", "adapter", "adapters"},
    "parser": {"parse", "parser", "parsing", "jsonl", "adapter", "adapters"},
    "refactor": {"refactor", "refactoring", "cleanup", "clean", "restructure"},
    "refactoring": {"refactor", "refactoring", "cleanup", "clean"},
    "cleanup": {"cleanup", "clean", "refactor"},
    "perf": {"perf", "performance", "optimize", "optimization", "slow",
             "fast", "cache"},
    "performance": {"perf", "performance", "optimize", "optimization",
                    "slow", "cache"},
    "optimize": {"optimize", "optimization", "perf", "performance", "cache"},
    "release": {"release", "publish", "ship", "tag", "version", "pypi"},
    "publish": {"publish", "release", "ship", "pypi", "tag"},
    "security": {"security", "secure", "vulnerability", "cve", "sanitize",
                 "escape"},
    "mcp": {"mcp", "tool", "tools", "server"},
    "thread": {"thread", "threads", "workthread", "lease"},
    "continue": {"continue", "continuation", "resume", "handoff"},
    "resume": {"resume", "continue", "continuation"},
}

# Fixed per-kind priority (a boost, applied identically to every fact).
FACT_PRIORITY = {
    "error": 1.4,
    "tool_result": 1.0,
    "tool_call": 0.8,
    "assistant": 0.6,
    "user": 0.5,
    "file": 0.4,
}


def expand_tokens(tokens: set) -> set:
    """Goal-token expansion through GOAL_CONCEPTS (boost only)."""
    out = set(tokens)
    for t in tokens:
        out |= GOAL_CONCEPTS.get(t, set())
    return out


# ---------------------------------------------------------------------------
# extraction (Phase 3a)
# ---------------------------------------------------------------------------

_TEXT_CAP = 600


def _mk_fact(row, ev) -> CandidateFact:
    prov = "{0}#{1}".format(row["id"], ev["seq"] if ev["seq"] is not None else "?")
    paths: List[str] = []
    if ev["file_path"]:
        paths.append(ev["file_path"])
    if ev["files_json"]:
        try:
            paths.extend(json.loads(ev["files_json"]))
        except json.JSONDecodeError:
            pass
    return CandidateFact(
        kind=ev["kind"],
        text=(ev["content"] or "").strip(),
        source_session=row["id"],
        provider=row["provider"],
        native_session_id=row["native_id"],
        ts=ev["ts"],
        seq=ev["seq"],
        paths=tuple(paths),
        command=ev["command"],
        exit_code=ev["exit_code"],
        provenance=prov,
    )


def extract_candidate_facts(store: Store, session_rows: List[Any]) -> List[CandidateFact]:
    """Compile the indexed events of the given sessions into CandidateFacts.

    Chronological, deduplicated (same kind + same normalized body keeps the
    earliest occurrence), and failure-biased: successful tool results are
    noise, failed ones are facts. Reasoning events are skipped — they are
    process, not claims (and often server-encrypted).
    """
    rows = sorted(session_rows,
                  key=lambda r: (r["updated_at"] or 0, r["started_at"] or 0))
    facts: List[CandidateFact] = []
    seen: Dict[Tuple[str, str], int] = {}
    for r in rows:
        for ev in store.events(r["id"]):
            kind = ev["kind"]
            if kind == "reasoning":
                continue
            if kind == "tool_call":
                body = ev["command"] or (ev["tool_input"] or "")[:300] \
                    or ev["content"] or ""
                fact = _mk_fact(r, ev)
                fact = CandidateFact(
                    kind="tool_call", text=body.strip(),
                    source_session=fact.source_session, provider=fact.provider,
                    native_session_id=fact.native_session_id, ts=fact.ts,
                    seq=fact.seq, paths=fact.paths, command=fact.command,
                    exit_code=fact.exit_code, provenance=fact.provenance,
                )
                if not fact.text:
                    continue
            elif kind == "tool_result":
                # only failures: a successful tool result is noise for ranking
                if (ev["exit_code"] or 0) == 0:
                    continue
                fact = _mk_fact(r, ev)
                fact = CandidateFact(
                    kind="tool_result", text=(fact.text or "")[:_TEXT_CAP],
                    source_session=fact.source_session, provider=fact.provider,
                    native_session_id=fact.native_session_id, ts=fact.ts,
                    seq=fact.seq, command=fact.command,
                    exit_code=ev["exit_code"], provenance=fact.provenance,
                )
                if not fact.text:
                    continue
            elif kind in ("user", "assistant", "error"):
                fact = _mk_fact(r, ev)
                if not fact.text:
                    continue
            else:
                continue  # meta/snapshot/usage/file: not rankable claims

            key = (fact.kind, " ".join(fact.text.split()).lower())
            if key in seen:
                # duplicate body: keep the earlier fact but fold the ts forward
                prev = facts[seen[key]]
                if fact.ts and (prev.ts or 0) < fact.ts:
                    facts[seen[key]] = prev.__class__(**{
                        **prev.__dict__, "ts": fact.ts})
                continue
            seen[key] = len(facts)
            facts.append(fact)
    return facts


# ---------------------------------------------------------------------------
# ranking (Phase 3b)
# ---------------------------------------------------------------------------

_RECENCY_WEEK = 7 * 86400.0


def score_fact(fact: CandidateFact, goal_raw: set, goal_exp: set,
               now: Optional[float] = None) -> float:
    """Deterministic goal score: coverage + path/command overlap + direct
    token hits + recency + fact-type priority. Pure function."""
    import time as _time
    now = _time.time() if now is None else now
    if not goal_exp:
        return 0.0

    source = fact.tokens_source()
    toks = tokenize(source)
    cover = len(goal_exp & toks) / max(1, len(goal_exp))

    path_cmd_toks = tokenize(" ".join(fact.paths)) | tokenize(fact.command or "")
    path_cmd = len(goal_exp & path_cmd_toks) / max(1, len(goal_exp))

    direct = len(goal_raw & toks)

    recency = 0.0
    if fact.ts:
        age = max(0.0, now - fact.ts)
        recency = 1.0 / (1.0 + age / _RECENCY_WEEK)

    prio = FACT_PRIORITY.get(fact.kind, 0.5)
    if fact.kind == "tool_call" and fact.exit_code not in (0, None):
        prio += 0.4                      # failed command = strong evidence

    return round(3.0 * cover + 1.0 * path_cmd + 0.75 * direct
                 + 0.8 * recency + prio, 4)


def rank_candidates(facts: List[CandidateFact], goal: Optional[str] = None,
                    now: Optional[float] = None
                    ) -> List[Tuple[CandidateFact, Optional[float]]]:
    """Order facts for a bundle.

    With a goal: descending deterministic score (ties broken by newer ts,
    then provenance, so ordering is stable). Without a goal: the input
    order comes back untouched with ``score=None`` — the pre-Phase-3
    semantics exactly.
    """
    if not goal or not goal.strip():
        return [(f, None) for f in facts]
    import time as _time
    now = _time.time() if now is None else now
    raw = tokenize(goal)
    scored = [(f, score_fact(f, raw, expand_tokens(raw), now=now))
              for f in facts]
    scored.sort(key=lambda p: (-p[1], -(p[0].ts or 0), p[0].provenance))
    return scored
