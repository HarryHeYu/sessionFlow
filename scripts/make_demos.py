#!/usr/bin/env python
"""Generate the RC1 demo transcripts (docs/demos/*.md) from a synthetic index.

Builds a small, entirely synthetic index — the "sqlite migration" story:
Codex implements the migration in the morning, Claude Code reviews it in the
afternoon, DSH keeps debugging the edge case in the evening — then runs the
real CLI against it and records what a user actually sees.

The demo index is built under VOYAGER_SCRATCH_ROOT when that is set (the
developer's scratch policy), else under the platform temp dir.  Nothing on
the real machine is touched: the seeded sessions are synthetic text.

Usage:  python scripts/make_demos.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from voyager.model import new_event, new_session          # noqa: E402
from voyager.store import Store                            # noqa: E402


def scratch_root() -> Path:
    override = os.environ.get("VOYAGER_SCRATCH_ROOT")
    if override:
        return Path(override)
    import tempfile
    return Path(tempfile.gettempdir())


def demo_dir() -> Path:
    d = scratch_root() / "rc1-demo"
    d.mkdir(parents=True, exist_ok=True)
    return d


MORNING = time.mktime((2026, 10, 5, 9, 0, 0, 0, 0, -1))
AFTERNOON = time.mktime((2026, 10, 5, 14, 0, 0, 0, 0, -1))
EVENING = time.mktime((2026, 10, 5, 20, 0, 0, 0, 0, -1))


def seed(index: Path) -> str:
    if index.exists():
        index.unlink()
    s = Store(index)
    root = "E:/proj/payments"

    def session(sid, provider, native, title, started, events):
        src = index.parent / f"{provider}-{native}.jsonl"
        src.write_text("{}\n", encoding="utf-8")
        s.replace_session(new_session(
            id=sid, provider=provider, native_session_id=native,
            title=title, started_at=started, updated_at=started + 600,
            cwd=root, repo_root=root, message_count=len(events),
            tool_count=sum(1 for e in events if e["kind"] == "tool_call"),
            can_resume=True, resume_cmd=f"{provider} resume {native}",
        ), events, provider, src)

    # Morning: Codex implements the migration
    session("codex:mig-01", "codex", "mig-01",
            "Add payments.idempotency_key column + index", MORNING, [
        new_event(sid="codex:mig-01", ts=MORNING, seq=1, kind="user",
                  role="user", content="We need an idempotency key on payments"),
        new_event(sid="codex:mig-01", ts=MORNING + 300, seq=2, kind="reasoning",
                  role="assistant",
                  content="SQLite migration: ALTER TABLE payments ADD COLUMN "
                          "idempotency_key TEXT, then a unique index"),
        new_event(sid="codex:mig-01", ts=MORNING + 600, seq=3, kind="tool_call",
                  role="assistant", tool_name="shell_command",
                  tool_call_id="c1", command="python scripts/migrate.py",
                  tool_input='{"command":"python scripts/migrate.py"}'),
        new_event(sid="codex:mig-01", ts=MORNING + 900, seq=4, kind="tool_result",
                  role="tool", tool_call_id="c1",
                  tool_output="migrated 2 rows", exit_code=0),
        new_event(sid="codex:mig-01", ts=MORNING + 1200, seq=5, kind="assistant",
                  role="assistant",
                  content="Migration committed; decided against NOT NULL "
                          "because backfill must run first",
                  file_path="E:/proj/payments/scripts/migrate.py"),
    ])

    # Afternoon: Claude reviews
    session("claude:rev-01", "claude", "rev-01",
            "Review the sqlite migration", AFTERNOON, [
        new_event(sid="claude:rev-01", ts=AFTERNOON, seq=1, kind="user",
                  role="user",
                  content="review the sqlite migration: the partial index "
                          "and the deferred backfill"),
        new_event(sid="claude:rev-01", ts=AFTERNOON + 600, seq=2, kind="assistant",
                  role="assistant",
                  content="Backfill plan: batched UPDATE with the request hash; "
                          "the partial index from the morning is correct",
                  file_path="E:/proj/payments/scripts/migrate.py"),
    ])

    # Evening: DSH debugs the edge case
    session("dsh:dbg-01", "dsh", "dbg-01",
            "Debug duplicate charge on retry", EVENING, [
        new_event(sid="dsh:dbg-01", ts=EVENING, seq=1, kind="user",
                  role="user", content="duplicate charge when the client retries"),
        new_event(sid="dsh:dbg-01", ts=EVENING + 300, seq=2, kind="reasoning",
                  role="assistant",
                  content="the retry bypasses the idempotency_key check added "
                          "by the sqlite migration"),
        new_event(sid="dsh:dbg-01", ts=EVENING + 600, seq=3, kind="assistant",
                  role="assistant",
                  content="fix: enforce the unique index lookup before insert"),
    ])

    tid = s.thread_create(repo_root=root, title="payments idempotency")
    for sid in ("codex:mig-01", "claude:rev-01", "dsh:dbg-01"):
        s.thread_attach(tid, sid)
    s.close()
    return tid


def run(args: list[str]) -> str:
    r = subprocess.run([sys.executable, "-m", "voyager.cli"] + args,
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=str(REPO), timeout=120)
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
    return out.strip()


def block(title: str, cmd: str, out: str) -> str:
    return f"### {title}\n\n```\n$ {cmd}\n{out}\n```\n"


def main() -> None:
    d = demo_dir()
    index = d / "index.db"
    tid = seed(index)
    db = ["--db", str(index)]

    # Demo 1 — search
    out1 = run(db + ["search", "sqlite migration"])
    out1b = run(db + ["show", "codex:mig-01"])
    Path("docs/demos/demo1-search.md").write_text(
        "# Demo 1 — cross-agent search\n\n"
        "One query, three agents: the decision, the command and the file\n"
        "come back from Codex, Claude Code and DSH sessions alike — and\n"
        "`voyager show` compiles one session into a continuation context.\n\n"
        + block('voyager search "sqlite migration"',
                'voyager search "sqlite migration"', out1)
        + block("voyager show codex:mig-01", "voyager show codex:mig-01",
                out1b),
        encoding="utf-8")

    # Demo 2 — cross-agent continuation
    out2 = run(db + ["thread", "timeline", tid])
    Path("docs/demos/demo2-continuation.md").write_text(
        "# Demo 2 — cross-agent continuation\n\n"
        "One WorkThread spans three agents. The timeline is the lifecycle:\n"
        "who attached, when, in what order — and `voyager continue` hands the\n"
        "whole story to the next agent.\n\n"
        + block("voyager thread timeline <thread>", "voyager thread timeline "
                + tid, out2),
        encoding="utf-8")

    # Demo 3 — doctor detects + safe repair
    import sqlite3
    con = sqlite3.connect(str(index))
    con.execute("DELETE FROM event_fts WHERE rowid > 0")   # break derived FTS
    con.commit()
    con.close()
    out3a = run(db + ["doctor"])
    out3b = run(db + ["doctor", "--fix", "--dry-run"])
    out3c = run(db + ["doctor", "--fix"])
    out3d = run(db + ["search", "idempotency", "--json"])
    Path("docs/demos/demo3-doctor.md").write_text(
        "# Demo 3 — doctor diagnostics and safe repair\n\n"
        "The derived search index is corrupted (every FTS row deleted; the\n"
        "canonical events are untouched). Doctor detects it, the dry run\n"
        "plans the repair, `--fix` rebuilds the index from canonical rows,\n"
        "and search works again. Retained/archived history is reported as\n"
        "informational — never as corruption.\n\n"
        + block("voyager doctor", "voyager doctor", out3a)
        + block("voyager doctor --fix --dry-run",
                "voyager doctor --fix --dry-run", out3b)
        + block("voyager doctor --fix", "voyager doctor --fix", out3c)
        + block("search works again", 'voyager search "idempotency" --json',
                out3d),
        encoding="utf-8")

    print("demo transcripts written to docs/demos/ (index at %s)" % index)


if __name__ == "__main__":
    main()
