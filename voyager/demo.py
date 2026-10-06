"""`voyager demo` — a synthetic, five-minute first-run experience.

Builds a small, entirely fictional index at ``~/.voyager/demo.db`` so a
newcomer can search, read a timeline and preview a continuation without
having any real agent installed.  The story is the "authentication flow"
project: Codex implements it, Claude Code reviews, ZCode debugs, DSH takes
the edge case — four agents, one WorkThread, zero real user content.

The demo index is deliberately SEPARATE from the real index
(``~/.voyager/index.db``): synthetic sessions must never surface in a
real scan's results.  Delete ``demo.db`` whenever you like.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from .model import new_event, new_session
from .store import Store

DEMO_DB_NAME = "demo.db"

REPO_ROOT = "E:/demo-project"


def _ts(hour: int, minute: int = 0) -> float:
    """A believable 'yesterday working day' timestamp."""
    day = time.localtime()
    return time.mktime((day.tm_year, day.tm_mon, day.tm_mday - 1,
                        hour, minute, 0, 0, 0, -1))


def _story() -> List[Dict[str, Any]]:
    """Four synthetic sessions: implement -> review -> debug -> edge case."""
    evs: List[Dict[str, Any]] = []

    def add(sid: str, ts: float, seq: int, kind: str, role: str,
            content: str = "", **kw: Any) -> None:
        evs.append(new_event(sid=sid, ts=ts, seq=seq, kind=kind, role=role,
                             content=content, **kw))

    # Morning — Codex implements the authentication flow
    sid = "codex:demo-auth-01"
    add(sid, _ts(9), 1, "user", "user",
        "Implement authentication flow for the demo-project API")
    add(sid, _ts(9, 10), 2, "reasoning", "assistant",
        "Stateless tokens fit the deployment: issue a JWT access token "
        "(15 min) plus a rotating refresh token stored server-side")
    add(sid, _ts(9, 20), 3, "tool_call", "assistant",
        tool_name="shell_command", tool_call_id="c1",
        command="python -m pytest tests/test_auth.py",
        tool_input='{"command":"python -m pytest tests/test_auth.py"}',
        file_path="E:/demo-project/auth.py")
    add(sid, _ts(9, 25), 4, "tool_result", "tool", tool_call_id="c1",
        tool_output="7 passed in 1.2s", exit_code=0)
    add(sid, _ts(9, 30), 5, "assistant", "assistant",
        "Decision: use JWT refresh tokens. auth.py issues access tokens; "
        "token.py owns rotation and revocation.", 
        file_path="E:/demo-project/token.py")

    # Afternoon — Claude Code reviews it
    sid = "claude:demo-auth-02"
    add(sid, _ts(14), 1, "user", "user",
        "Review the authentication flow: token lifetime and revocation")
    add(sid, _ts(14, 15), 2, "assistant", "assistant",
        "The 15-minute access window is right; token.py's refresh rotation "
        "should reject reuse of a rotated token, not just expire it")

    # Late afternoon — ZCode debugs an edge case
    sid = "zcode:demo-auth-03"
    add(sid, _ts(17), 1, "user", "user",
        "auth fails after clock skew: access token rejected a minute early")
    add(sid, _ts(17, 20), 2, "reasoning", "assistant",
        "auth.py validates exp with no leeway; add a 30-second clock-skew "
        "margin in token.py instead of the validator")
    add(sid, _ts(17, 30), 3, "assistant", "assistant",
        "Fixed: token.py now allows 30s leeway; auth.py untouched")

    # Evening — DSH picks up the hardening
    sid = "dsh:demo-auth-04"
    add(sid, _ts(20), 1, "user", "user",
        "harden the refresh rotation: what happens on token theft?")
    add(sid, _ts(20, 10), 2, "assistant", "assistant",
        "Reuse of a rotated refresh token now revokes the whole family in "
        "token.py; next step is a rate limit on the refresh endpoint")

    sessions = [
        ("codex:demo-auth-01", "codex", "demo-auth-01",
         "Implement authentication flow", _ts(9)),
        ("claude:demo-auth-02", "claude", "demo-auth-02",
         "Review the authentication flow", _ts(14)),
        ("zcode:demo-auth-03", "zcode", "demo-auth-03",
         "Debug clock-skew auth failure", _ts(17)),
        ("dsh:demo-auth-04", "dsh", "demo-auth-04",
         "Harden refresh-token rotation", _ts(20)),
    ]
    out = []
    for (sid, provider, native, title, started) in sessions:
        src = Path(REPO_ROOT) / f"{native}.jsonl"
        out.append((new_session(
            id=sid, provider=provider, native_session_id=native,
            title=title, started_at=started, updated_at=started + 1800,
            cwd=REPO_ROOT, repo_root=REPO_ROOT,
            message_count=sum(1 for e in evs if e["sid"] == sid),
            tool_count=sum(1 for e in evs
                           if e["sid"] == sid and e["kind"] == "tool_call"),
            can_resume=True, resume_cmd=f"{provider} resume {native}",
        ), [e for e in evs if e["sid"] == sid], provider, src))
    return out


def build(index: Path) -> str:
    """Seed the synthetic demo index; return the demo WorkThread id."""
    if index.parent.exists():
        index.unlink(missing_ok=True)
    s = Store(index)
    try:
        for (session, events, provider, src) in _story():
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("{}\n", encoding="utf-8")
            s.replace_session(session, events, provider, src)
        tid = s.thread_create(repo_root=REPO_ROOT,
                              title="authentication flow")
        for (session, _e, _p, _s) in _story():
            s.thread_attach(tid, session["id"])
        return tid
    finally:
        s.close()


def default_index() -> Path:
    from .store import default_db_path
    return default_db_path().parent / DEMO_DB_NAME


def run() -> int:
    """The `voyager demo` verb: seed, then tell the user what to try."""
    index = default_index()
    tid = build(index)
    rel = index
    try:
        rel = index.relative_to(Path.home())
        rel = Path("~") / rel
    except ValueError:
        pass
    print("Seeded a synthetic demo index (4 agents, 1 WorkThread, "
          "no real data):")
    print(f"  {index}")
    print()
    print("Try it now:")
    print(f'  voyager search --db "{index}" "authentication"')
    print(f'  voyager search --db "{index}" "JWT refresh token"')
    print(f"  voyager thread timeline --db \"{index}\" {tid}")
    print(f'  voyager show --db "{index}" codex:demo-auth-01')
    print()
    print(f"The demo index is separate from your real one ({rel});")
    print("delete it whenever you like.")
    print()
    print("`voyager continue` needs REAL agent sessions: its freshness check")
    print("would flag synthetic files as rotated sources. After your first")
    print("real `voyager scan`, continue works across your own agents:")
    print('  voyager scan && voyager search "your topic here"')
    return 0


def main() -> int:
    try:
        return run()
    except Exception as e:                       # a demo must never explode
        print(f"demo failed: {e}", file=sys.stderr)
        return 1
