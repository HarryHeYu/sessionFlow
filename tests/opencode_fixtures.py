"""Synthetic OpenCode databases for the adapter tests.

The DDL here mirrors the *upstream* schema read on 2026-11-10 from
anomalyco/opencode v2.2.6 (``packages/core/src/session/sql.ts``), and the
pre-v2 shape from ``packages/core/test/v1-migration.test.ts``. It is kept as
code rather than a committed .db so a reviewer can diff the schema.

These are SYNTHETIC. No real OpenCode install was read; tests that use them
are UNIT_VERIFIED, never LIVE_VERIFIED.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# --- current schema (v2) ---------------------------------------------------

V2_DDL = """
CREATE TABLE project (
  id text PRIMARY KEY, worktree text NOT NULL, vcs text, name text,
  icon_url text, icon_url_override text, icon_color text,
  time_created integer NOT NULL, time_updated integer NOT NULL,
  time_initialized integer, time_active integer NOT NULL DEFAULT 0,
  time_archived integer, sandboxes text NOT NULL, commands text
);
CREATE TABLE session_v2 (
  id text PRIMARY KEY, project_id text NOT NULL, workspace_id text,
  parent_id text, fork_session_id text, fork_boundary text,
  slug text NOT NULL, directory text NOT NULL, path text, title text,
  version text NOT NULL, share_url text,
  summary_additions integer, summary_deletions integer, summary_files integer,
  summary_diffs text, metadata text, cost real NOT NULL DEFAULT 0,
  tokens_input integer NOT NULL DEFAULT 0,
  tokens_output integer NOT NULL DEFAULT 0,
  tokens_reasoning integer NOT NULL DEFAULT 0,
  tokens_cache_read integer NOT NULL DEFAULT 0,
  tokens_cache_write integer NOT NULL DEFAULT 0,
  revert text, permission text, agent text, model text,
  time_created integer NOT NULL, time_updated integer NOT NULL,
  time_busy integer, time_idle integer, time_viewed integer,
  idle_outcome text, time_compacting integer, time_archived integer,
  time_suspended integer, resume_attempts integer NOT NULL DEFAULT 0
);
CREATE TABLE session_message (
  id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL,
  seq integer NOT NULL, time_created integer NOT NULL,
  time_updated integer NOT NULL, data text NOT NULL
);
CREATE TABLE session_part (
  message_id text NOT NULL, session_id text NOT NULL,
  ordinal integer NOT NULL, type text NOT NULL, call_id text,
  time_created integer NOT NULL, time_updated integer NOT NULL,
  data text NOT NULL,
  PRIMARY KEY (message_id, ordinal)
);
"""

# --- pre-v2 schema ---------------------------------------------------------

LEGACY_DDL = """
CREATE TABLE project (
  id text PRIMARY KEY, worktree text NOT NULL, vcs text, name text,
  time_created integer NOT NULL, time_updated integer NOT NULL
);
CREATE TABLE session (
  id text PRIMARY KEY, project_id text NOT NULL, workspace_id text,
  parent_id text, slug text NOT NULL, directory text NOT NULL, path text,
  title text NOT NULL, version text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL
);
CREATE TABLE message (
  id text PRIMARY KEY, session_id text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL,
  data text NOT NULL
);
CREATE TABLE part (
  id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL,
  data text NOT NULL
);
"""

T0 = 1_790_000_000_000        # a fixed millisecond epoch, 2026-ish


def _tool_part(name="bash", call_id="call_1", status="completed",
               inp=None, output="done", error=None):
    """One tool part: the CALL and its RESULT live in the same `state`."""
    state = {"status": status,
             "input": inp if inp is not None else {"command": "pytest -q"}}
    if status == "completed":
        state["content"] = [{"type": "text", "text": output}]
    if status == "error":
        state["error"] = {"type": "tool.execution", "message": error or "boom"}
    return {
        "type": "tool", "id": call_id, "name": name, "state": state,
        "time": {"created": T0 + 30, "completed": T0 + 40},
    }


def make_v2(path: Path, *, title="修复补丁", n_sessions=1,
            text="补丁已完成", tool=True, unicode=True,
            n_messages=0, wal=False) -> Path:
    """Build a v2 database. ``n_messages`` adds filler turns for a large fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        # WAL is what OpenCode's live database uses, and it is also the mode
        # whose side files we index. Setting it before any DDL avoids a
        # rollback journal (which some sandboxes refuse to create/remove).
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(V2_DDL)
        con.execute(
            "INSERT INTO project (id, worktree, vcs, name, time_created, "
            "time_updated, sandboxes) VALUES (?,?,?,?,?,?,'[]')",
            ("prj_1", "/repo/demo", "git", "demo", T0, T0))
        for s in range(n_sessions):
            sid = "ses_%03d" % s
            con.execute(
                "INSERT INTO session_v2 (id, project_id, slug, directory, "
                "title, version, time_created, time_updated, model, "
                "tokens_input, tokens_output) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "prj_1", "slug", "/repo/demo", title, "2",
                 T0 + s * 1000, T0 + s * 1000 + 900,
                 json.dumps({"id": "claude-sonnet-4", "providerID": "anthropic"}),
                 120, 40))
            seq = 0
            # user
            con.execute(
                "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                ("msg_%s_u0" % s, sid, "user", seq, T0, T0,
                 json.dumps({"text": text, "time": {"created": T0}},
                            ensure_ascii=False)))
            seq += 1
            # assistant with text / reasoning / tool parts
            parts = [{"type": "reasoning", "text": "先看一下测试"},
                     {"type": "text", "text": "正在处理"}]
            if tool:
                parts.append(_tool_part())
            if unicode:
                parts.append({"type": "text", "text": "日本語 한국어 🚀"})
            mid = "msg_%s_a0" % s
            con.execute(
                "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                (mid, sid, "assistant", seq, T0 + 10, T0 + 10,
                 json.dumps({"agent": "build", "time": {"created": T0 + 10}},
                            ensure_ascii=False)))
            for ordinal, p in enumerate(parts):
                call_id = p.get("id") if p.get("type") == "tool" else None
                con.execute(
                    "INSERT INTO session_part VALUES (?,?,?,?,?,?,?,?)",
                    (mid, sid, ordinal, p["type"], call_id, T0 + 20,
                     T0 + 20, json.dumps(p, ensure_ascii=False)))
            seq += 1
            for i in range(n_messages):
                con.execute(
                    "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                    ("msg_%s_f%d" % (s, i), sid, "user", seq,
                     T0 + 100 + i, T0 + 100 + i,
                     json.dumps({"text": "filler %d" % i,
                                 "time": {"created": T0 + 100 + i}})))
                seq += 1
        con.commit()
    finally:
        con.close()
    return path


# --- intermediate v2.0.x schema: parts INLINE in session_message.data ------

#: No ``session_part`` table. Message content lives in ``data``: a ``user`` row
#: has a ``text`` string, an ``assistant`` row has a ``content`` array of
#: text / reasoning / tool items. This mirrors a real install's structure; the
#: values below are synthetic and contain no user data.
V20_INLINE_DDL = """
CREATE TABLE project (
  id text PRIMARY KEY, worktree text NOT NULL, vcs text, name text,
  icon_url text, icon_url_override text, icon_color text,
  time_created integer NOT NULL, time_updated integer NOT NULL,
  time_initialized integer, time_active integer NOT NULL DEFAULT 0,
  time_archived integer, sandboxes text NOT NULL, commands text
);
CREATE TABLE session_v2 (
  id text PRIMARY KEY, project_id text NOT NULL, workspace_id text,
  parent_id text, fork_session_id text, fork_boundary text,
  slug text NOT NULL, directory text NOT NULL, path text, title text,
  version text NOT NULL, share_url text,
  summary_additions integer, summary_deletions integer, summary_files integer,
  summary_diffs text, metadata text, cost real NOT NULL DEFAULT 0,
  tokens_input integer NOT NULL DEFAULT 0,
  tokens_output integer NOT NULL DEFAULT 0,
  tokens_reasoning integer NOT NULL DEFAULT 0,
  tokens_cache_read integer NOT NULL DEFAULT 0,
  tokens_cache_write integer NOT NULL DEFAULT 0,
  revert text, permission text, agent text, model text,
  time_created integer NOT NULL, time_updated integer NOT NULL,
  time_busy integer, time_idle integer, time_viewed integer,
  idle_outcome text, time_compacting integer, time_archived integer,
  time_suspended integer, resume_attempts integer NOT NULL DEFAULT 0
);
CREATE TABLE session_message (
  id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL,
  seq integer NOT NULL, time_created integer NOT NULL,
  time_updated integer NOT NULL, data text NOT NULL
);
"""


def _inline_tool(name="bash", call_id="call_1", status="completed",
                 inp=None, output="done", exit_code=0, error=None):
    """A v2.0.x inline tool item: exit lives in ``state.metadata.exit``."""
    state = {"status": status,
             "input": inp if inp is not None else {"command": "pytest -q"},
             "metadata": {"status": status, "truncated": False,
                          "exit": exit_code}}
    if status == "completed":
        state["content"] = [{"type": "text", "text": output}]
    if status == "error":
        state.pop("metadata", None)
        state["error"] = {"type": "tool.execution", "message": error or "boom"}
    return {"type": "tool", "id": call_id, "name": name, "executed": True,
            "state": state, "time": {"created": T0 + 30, "completed": T0 + 40}}


def make_v20_inline(path: Path, *, title="内联形状", n_sessions=1,
                    text="补丁已完成", tool=True, unicode=True,
                    n_messages=0, wal=False, empty_user=False,
                    running_tool=False, meta=True) -> Path:
    """Build a v2.0.x database: inline ``data.content``, no ``session_part``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(V20_INLINE_DDL)
        con.execute(
            "INSERT INTO project (id, worktree, vcs, name, time_created, "
            "time_updated, sandboxes) VALUES (?,?,?,?,?,?,'[]')",
            ("prj_1", "/repo/inline", "git", "inline", T0, T0))
        for s in range(n_sessions):
            sid = "ses_inline_%03d" % s
            con.execute(
                "INSERT INTO session_v2 (id, project_id, slug, directory, "
                "title, version, time_created, time_updated, model, "
                "tokens_input, tokens_output) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "prj_1", "slug", "/repo/inline", title, "2.0.29",
                 T0 + s * 1000, T0 + s * 1000 + 900,
                 json.dumps({"id": "claude-sonnet-4",
                             "providerID": "anthropic", "variant": "default"}),
                 120, 40))
            seq = 0
            # user: text is a plain string in this shape (files/agents arrays)
            con.execute(
                "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                ("msg_%s_u0" % s, sid, "user", seq, T0, T0,
                 json.dumps({"text": "" if empty_user else text,
                             "files": [], "agents": [],
                             "time": {"created": T0}}, ensure_ascii=False)))
            seq += 1
            # assistant: content array holds the parts inline
            content = [{"type": "reasoning", "text": "先看一下测试",
                        "time": {"created": T0 + 10}},
                       {"type": "text", "text": "正在处理"}]
            if tool:
                content.append(_inline_tool(
                    status="running" if running_tool else "completed"))
            if unicode:
                content.append({"type": "text", "text": "日本語 한국어 🚀"})
            con.execute(
                "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                ("msg_%s_a0" % s, sid, "assistant", seq, T0 + 10, T0 + 10,
                 json.dumps({"agent": "build",
                             "model": {"id": "claude-sonnet-4",
                                       "providerID": "anthropic"},
                             "time": {"created": T0 + 10,
                                      "completed": T0 + 20},
                             "content": content}, ensure_ascii=False)))
            seq += 1
            if meta:
                # provider-generated rows must stay meta, never a human turn
                con.execute(
                    "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                    ("msg_%s_sys" % s, sid, "system", seq, T0 + 11, T0 + 11,
                     json.dumps({"text": "system note", "time": {"created": T0 + 11}},
                                ensure_ascii=False)))
                seq += 1
                con.execute(
                    "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                    ("msg_%s_cmp" % s, sid, "compaction", seq, T0 + 12, T0 + 12,
                     json.dumps({"summary": "compacted summary", "reason": "auto",
                                 "status": "completed",
                                 "time": {"created": T0 + 12}},
                                ensure_ascii=False)))
                seq += 1
            for i in range(n_messages):
                con.execute(
                    "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                    ("msg_%s_f%d" % (s, i), sid, "user", seq,
                     T0 + 100 + i, T0 + 100 + i,
                     json.dumps({"text": "filler %d" % i, "files": [],
                                 "time": {"created": T0 + 100 + i}})))
                seq += 1
        con.commit()
    finally:
        con.close()
    return path


def make_legacy(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(LEGACY_DDL)
        con.execute("INSERT INTO project VALUES (?,?,?,?,?,?)",
                    ("prj_1", "/repo/legacy", "git", "legacy", T0, T0))
        con.execute(
            "INSERT INTO session (id, project_id, slug, directory, title, "
            "version, time_created, time_updated) VALUES (?,?,?,?,?,?,?,?)",
            ("ses_legacy", "prj_1", "slug", "/repo/legacy", "legacy session",
             "1", T0, T0 + 500))
        con.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                    ("msg_l_u", "ses_legacy", T0, T0,
                     json.dumps({"role": "user", "text": "legacy prompt"})))
        con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                    ("prt_l_1", "msg_l_u", "ses_legacy", T0, T0,
                     json.dumps({"type": "text", "text": "legacy prompt"})))
        con.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                    ("msg_l_a", "ses_legacy", T0 + 10, T0 + 10,
                     json.dumps({"role": "assistant"})))
        # legacy tool shape: tool/callID/state.output (not name/id/content)
        con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                    ("prt_l_2", "msg_l_a", "ses_legacy", T0 + 20, T0 + 20,
                     json.dumps({"type": "tool", "tool": "read",
                                 "callID": "legacy_call",
                                 "state": {"status": "completed",
                                           "input": {"path": "a.py"},
                                           "output": "legacy output"}})))
        con.commit()
    finally:
        con.close()
    return path


def make_unknown(path: Path) -> Path:
    """Readable SQLite, but not an OpenCode database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE something_else (id text)")
        con.commit()
    finally:
        con.close()
    return path


def make_corrupt(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    return path


def open_writer(db: Path):
    """A live writer's connection, kept open by the caller.

    SQLite checkpoints and removes ``-wal`` when the LAST connection closes, so
    a fixture that closes its connection cannot model a running agent. Hold
    this connection open for as long as the test needs a real WAL side file.
    """
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    return con


def append_message(writer, sid: str, text: str) -> None:
    """Add one turn through a live writer; the new pages land in ``-wal``."""
    seq = writer.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM session_message WHERE session_id=?",
        (sid,)).fetchone()[0]
    writer.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                   ("msg_new_%d" % seq, sid, "user", seq,
                    T0 + 999_000, T0 + 999_000,
                    json.dumps({"text": text,
                                "time": {"created": T0 + 999_000}},
                               ensure_ascii=False)))
    writer.execute("UPDATE session_v2 SET time_updated=? WHERE id=?",
                   (T0 + 999_000, sid))
    writer.commit()
