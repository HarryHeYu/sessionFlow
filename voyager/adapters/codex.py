"""OpenAI Codex adapter (CLI / VS Code extension / Desktop share one store).

Source: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>_<window_id>.jsonl
Each line: {"timestamp": iso, "ordinal": int, "type": ..., "payload": {...}}
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Adapter, finish_session, git_info, register
from .. import provenance
from ..model import new_event, new_session, text_of

HOME = Path.home()
SESSIONS_DIR = HOME / ".codex" / "sessions"

#: The Codex app / VS Code extension keeps its own conversation store, and it does
#: NOT write rollouts: on this machine the newest rollout is from 2026-09-28 while
#: the app has been used every day since.  Two files matter:
#:
#:   thread_history_1.sqlite  thread_items / thread_turns -- the actual turns
#:   sqlite/codex-dev.db      local_thread_catalog    -- titles, cwd, source kind
#:
#: `local_thread_catalog` also lists cloud ("chatgpt") conversations, which have
#: no local content at all; those are recorded as metadata and never invented into
#: turns.  See `_scan_thread_history`.
#: The store is *versioned*, and it moves.  `thread_history_1.sqlite` existed for
#: months and then the Codex-managed source disappeared from `~/.codex` entirely
#: (leaving a 0-byte `-wal`); `state_5.sqlite::rollout_migration_state` records a
#: `legacy_to_paginated_v1` migration around that time, so the app-side story is a
#: migration/rotation, not a deletion by anything here.  Voyager never writes to
#: `~/.codex` at all -- every open in this module is `mode=ro`.
#:
#: The name is therefore globbed.  **All** matches are read, not just the newest:
#: whether these files are generations (one supersedes the next) or shards (each
#: holds a disjoint slice) is not something we can prove from here, and reading
#: only the newest would silently lose history in the shard case.  Sessions are
#: deduplicated by id, and a rollout always outranks this store.
THREAD_HISTORY_GLOB = "thread_history_*.sqlite"


def _thread_history_dbs() -> List[Path]:
    """Every thread-history store present right now, newest first."""
    try:
        return sorted((HOME / ".codex").glob(THREAD_HISTORY_GLOB),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def _thread_history_db() -> Optional[Path]:
    """The newest thread-history store, if one exists (kept for callers/tests)."""
    found = _thread_history_dbs()
    return found[0] if found else None


#: Kept for tests and for the isolation guard in conftest, which patches module
#: globals by name; `_scan_thread_history` reads the resolved path each run.
THREAD_HISTORY_DB = HOME / ".codex" / "thread_history_1.sqlite"
CODEX_DEV_DB = HOME / ".codex" / "sqlite" / "codex-dev.db"
STATE_DB = HOME / ".codex" / "state_5.sqlite"


def _normalise_cwd(value):
    """`state_5` stores Windows paths with a long-path prefix."""
    if not value:
        return None
    v = str(value)
    if v.startswith("\\\\?\\"):
        v = v[4:]
    return v or None


def _ro_connect(path: Path):
    """Open read-only. Never creates the file, never writes to it."""
    import sqlite3
    if not path.is_file():
        return None
    try:
        uri = "file:%s?mode=ro" % str(path).replace(chr(92), "/").replace("?", "%3f")
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        return con
    except Exception:
        return None


def parse_ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_ENV_NOISE = re.compile(
    r"<(environment_context|user_instructions|ENVIRONMENT_CONTEXT|user_instructions)>"
    r".*?</\1>", re.S,
)


def _clean_title(text: str) -> str:
    """Strip codex's injected <environment_context>/<user_instructions> blocks.

    Titles only need the head of the message; cap the regex input to keep
    the .*? scan linear on huge messages.
    """
    head = (text or "")[:8000]
    cleaned = _ENV_NOISE.sub(" ", head)
    cleaned = " ".join(cleaned.split())
    return cleaned[:120]


def _extract_output(output: Any) -> tuple:
    """Codex tool outputs come in two shapes; normalize both."""
    if isinstance(output, list):
        # custom_tool_call_output: [{exit_code, wall_time_seconds, output}]
        rc = None
        texts = []
        for item in output:
            if isinstance(item, dict):
                if item.get("exit_code") is not None:
                    rc = item["exit_code"]
                texts.append(str(item.get("output") or ""))
        return rc, "\n".join(texts)
    if isinstance(output, str):
        # function_call_output: "Exit code: N\nWall time: ...\nOutput:\n..."
        rc = None
        body = output
        for line in output.splitlines()[:3]:
            if line.lower().startswith("exit code:"):
                try:
                    rc = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        marker = "Output:"
        idx = output.find(marker)
        if idx >= 0:
            body = output[idx + len(marker):].lstrip("\n")
        return rc, body
    return None, text_of(output)


def _command_from_args(name: str, arguments: Any) -> Optional[str]:
    if not isinstance(arguments, dict):
        return None
    for key in ("command", "cmd", "script", "cmdline"):
        v = arguments.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
    return None


class CodexAdapter(Adapter):
    provider = "codex"
    can_resume = True
    can_fork = True

    def discover(self) -> List[Path]:
        """Rollouts, plus the app store -- which is a multi-session artifact.

        The store is listed here on purpose: the scan pipeline decides whether to
        call `scan()` at all from the change state of what `discover()` returns,
        so a file it never sees is a file whose updates never trigger a rescan.
        `scan()` filters it out of the rollout grouping; `_scan_thread_history`
        is what actually reads it.
        """
        out = sorted(SESSIONS_DIR.rglob("rollout-*.jsonl")) if SESSIONS_DIR.is_dir() else []
        out.extend(self._store_paths())
        return out

    def _session_id_of(self, path: Path) -> str:
        # rollout-<ts>-<session_id>_<window_id>.jsonl
        stem = path.stem  # rollout-2026-09-12T14-54-06-<sid>_<wid>
        stem = stem[len("rollout-"):]
        if "_" in stem:
            sid = stem.rsplit("_", 1)[0]
        else:
            sid = stem
        # sid itself is <timestamp>-<uuid>; take the trailing uuid part
        tail = sid.split("-")
        if len(tail) >= 5:
            return "-".join(tail[-5:])
        return sid

    def scan(self, source_changed) -> List[dict]:
        """Group continuation rollouts by session id and merge each group.

        Groups whose files all match their registered fingerprints are
        skipped (their index rows are still alive, so prune keeps them).
        """
        groups: Dict[str, List[Path]] = {}
        store_paths = set(self._store_paths())
        for f in self.discover():
            if f in store_paths:
                continue          # a multi-session store, handled below
            groups.setdefault(self._session_id_of(f), []).append(f)
        out: List[dict] = []
        for native, files in sorted(groups.items()):
            files.sort(key=lambda p: p.name)  # rollouts are named by start time
            try:
                if all(not source_changed(self.provider, f) for f in files):
                    continue
            except OSError:
                pass
            sid = f"codex:{native}"
            merged: Optional[dict] = None
            for f in files:
                r = self.parse(f)
                if not r or r.get("__error__"):
                    continue
                if merged is None:
                    merged = r
                    merged["session"]["id"] = sid
                    merged["session"]["native_session_id"] = native
                else:
                    # continuation rollout: keep base meta, extend the timeline
                    prev = merged
                    offset = (prev["session"].get("updated_at") or 0)
                    for e in r["events"]:
                        e["sid"] = sid
                        if e.get("ts") and prev["session"].get("started_at") \
                                and e["ts"] < prev["session"]["started_at"]:
                            e["seq"] += 10_000_000  # keep ordering sane
                    prev["events"].extend(r["events"])
                    if (r["session"].get("updated_at") or 0) > (prev["session"].get("updated_at") or 0):
                        prev["session"]["updated_at"] = r["session"]["updated_at"]
                    prev["session"]["raw_metadata"].setdefault(
                        "continuations", []
                    ).append(f.name)
                    merged["extra_sources"].append(f)
            if merged:
                s = merged["session"]
                merged["source_path"] = files[0]   # this group's own anchor file
                for e in merged["events"]:
                    e["sid"] = sid
                s["message_count"] = sum(1 for e in merged["events"]
                                         if e["kind"] in ("user", "assistant"))
                s["tool_count"] = sum(1 for e in merged["events"]
                                      if e["kind"] == "tool_call")
                s["updated_at"] = max((e["ts"] for e in merged["events"] if e.get("ts")),
                                      default=s.get("started_at"))
                out.append(merged)
        out.extend(self._scan_thread_history(source_changed))
        return out

    # -- the app / VS Code extension store ---------------------------------

    def _catalog(self) -> Dict[str, Dict[str, Any]]:
        """Per-thread metadata: title, cwd, source and where its rollout lives.

        `state_5.sqlite::threads` is the authoritative list of threads this
        machine has -- 109 of them here, every one with a `rollout_path`.  The
        rollout scan already covers most of those, so this exists mainly to find
        the few whose rollout is *not* under `~/.codex/sessions` (the newest ones)
        and to give every thread a cwd.
        """
        out: Dict[str, Dict[str, Any]] = {}
        con = _ro_connect(STATE_DB)
        if con is not None:
            try:
                for r in con.execute(
                        "SELECT id, title, cwd, source, rollout_path, updated_at "
                        "FROM threads"):
                    out[r["id"]] = {
                        "title": r["title"], "cwd": _normalise_cwd(r["cwd"]),
                        "source_kind": r["source"], "rollout_path": r["rollout_path"],
                        "updated_at": r["updated_at"],
                    }
            except Exception:
                pass
            finally:
                con.close()
        # the app's catalog also lists cloud conversations; keep it as a fallback
        # for titles/cwd only -- never as a reason to invent turns
        con = _ro_connect(CODEX_DEV_DB)
        if con is not None:
            try:
                for r in con.execute(
                        "SELECT thread_id, display_title, cwd, source_kind "
                        "FROM local_thread_catalog"):
                    out.setdefault(r["thread_id"], {
                        "title": r["display_title"],
                        "cwd": _normalise_cwd(r["cwd"]),
                        "source_kind": r["source_kind"],
                        "rollout_path": None,
                    })
            except Exception:
                pass
            finally:
                con.close()
        return out

    def _covered_by_a_rollout(self, meta: Dict[str, Any]) -> bool:
        """Is this thread already indexed from its rollout file?

        Indexing it again from the app's store would not add a row -- the session
        id is the same -- but it would replace rollout-derived turns with
        app-derived ones, which is a silent change of provenance for no gain.
        """
        rp = (meta or {}).get("rollout_path")
        if not rp:
            return False
        try:
            p = Path(rp)
            return p.is_file() and SESSIONS_DIR in p.parents
        except Exception:
            return False

    def _thread_turns(self, con, thread_id: str, sid: str) -> List[dict]:
        """Build events for one app thread from its stored items.

        Returns the events, and records the working directory the thread's own
        commands ran in on `self._last_cwd` -- an observation from the stored
        items, not a guess, and the only source of a repo for a thread the app
        never registered anywhere else.
        """
        events: List[dict] = []
        seq = 0
        self._last_cwd = None
        self._last_title = None
        try:
            rows = con.execute(
                "SELECT item_json, created_at_ms FROM thread_items "
                "WHERE thread_id=? ORDER BY rollout_ordinal, created_at_ms",
                (thread_id,)).fetchall()
        except Exception:
            return []
        for row in rows:
            try:
                item = json.loads(row["item_json"])
            except Exception:
                continue
            typ = item.get("type")
            ts = (row["created_at_ms"] or 0) / 1000.0 or None
            seq += 1
            if typ == "userMessage":
                text = text_of(item.get("content"))
                if not text:
                    continue
                # the app injects environment/instruction blocks through the user
                # channel exactly like the CLI does; those are not human turns
                origin = ("provider_bootstrap"
                          if _ENV_NOISE.search(text) else "human")
                events.append(new_event(sid=sid, ts=ts, seq=seq, kind="user",
                                        content=text, origin=origin))
                if origin == "human" and not self._last_title:
                    self._last_title = " ".join(text.split())[:120]
            elif typ == "agentMessage":
                text = item.get("text")
                if text:
                    events.append(new_event(sid=sid, ts=ts, seq=seq,
                                            kind="assistant", content=text))
            elif typ == "reasoning":
                text = text_of(item.get("content")) or text_of(item.get("summary"))
                if text:
                    events.append(new_event(sid=sid, ts=ts, seq=seq,
                                            kind="reasoning", content=text))
            elif typ == "commandExecution":
                cmd = item.get("command")
                if not cmd:
                    continue
                if not self._last_cwd and item.get("cwd"):
                    self._last_cwd = _normalise_cwd(item.get("cwd"))
                events.append(new_event(
                    sid=sid, ts=ts, seq=seq, kind="tool_call", content=cmd,
                    tool_name="shell", command=cmd,
                    tool_output=item.get("aggregatedOutput"),
                    exit_code=item.get("exitCode")))
            elif typ == "fileChange":
                for ch in (item.get("changes") or []):
                    seq += 1
                    events.append(new_event(
                        sid=sid, ts=ts, seq=seq, kind="tool_call",
                        content=ch.get("path"),
                        tool_name="file_change", file_path=ch.get("path")))
            elif typ in ("webSearch", "mcpToolCall"):
                events.append(new_event(sid=sid, ts=ts, seq=seq, kind="tool_call",
                                        content=item.get("query") or item.get("name"),
                                        tool_name=typ))
        return events

    def _store_paths(self) -> List[Path]:
        """Every place the app's conversations live *now*.

        A test (or the isolation guard) that patches `THREAD_HISTORY_DB`
        explicitly wins, because that is how this module's storage is redirected
        everywhere else; otherwise every match of the glob is used.
        """
        patched = THREAD_HISTORY_DB
        if patched != HOME / ".codex" / "thread_history_1.sqlite":
            return [patched] if patched.is_file() else []
        return _thread_history_dbs()

    def _scan_thread_history(self, source_changed) -> List[dict]:
        """The app's own conversations, which never appear as rollouts."""
        stores = self._store_paths()
        if not stores:
            return []
        out: List[dict] = []
        seen: set = set()
        for store_path in stores:
            for bundle in self._scan_one_store(store_path, source_changed):
                sid = bundle["session"]["id"]
                if sid in seen:
                    continue      # the same session in two stores: first wins
                seen.add(sid)
                out.append(bundle)
        return out

    def _scan_one_store(self, store_path: Path, source_changed) -> List[dict]:
        """Read one thread-history store. Never writes to it."""
        if not store_path.is_file():
            return []
        try:
            if not source_changed(self.provider, store_path):
                return []
        except OSError:
            pass

        catalog = self._catalog()
        con = _ro_connect(store_path)
        if con is None:
            return []
        out: List[dict] = []
        try:
            try:
                threads = con.execute(
                    "SELECT thread_id, MIN(created_at_ms) AS first_ms, "
                    "MAX(created_at_ms) AS last_ms, COUNT(*) AS n "
                    "FROM thread_items GROUP BY thread_id").fetchall()
            except Exception:
                return []
            for t in threads:
                tid = t["thread_id"]
                meta = catalog.get(tid, {})
                # A cloud conversation has a catalog entry and no local items; it
                # is not indexed as a session, because there is nothing to index.
                if not t["n"]:
                    continue
                # Already covered by the rollout scan: leave it to that pass.
                if self._covered_by_a_rollout(meta):
                    continue
                native = tid
                sid = f"codex:{native}"
                started = (t["first_ms"] or 0) / 1000.0 or None
                updated = (t["last_ms"] or 0) / 1000.0 or started
                events = self._thread_turns(con, tid, sid)
                # Prefer the thread's registered cwd; fall back to the directory
                # its own commands ran in, which is an observation, not a guess.
                cwd = meta.get("cwd") or self._last_cwd or None
                session = new_session(
                    id=sid, provider=self.provider, native_session_id=native,
                    title=meta.get("title") or self._last_title or None,
                    started_at=started, updated_at=updated,
                    repo_root=cwd, cwd=cwd,
                    raw_metadata={"source": "codex_app",
                                  "source_kind": meta.get("source_kind"),
                                  "git_branch": meta.get("git_branch"),
                                  "model_provider": meta.get("model_provider"),
                                  "source_file": store_path.name})
                if not events:
                    continue
                git = git_info(cwd) if cwd else {}
                session["message_count"] = sum(
                    1 for e in events if e["kind"] in ("user", "assistant"))
                session["tool_count"] = sum(
                    1 for e in events if e["kind"] == "tool_call")
                session["updated_at"] = max(
                    (e["ts"] for e in events if e.get("ts")),
                    default=session.get("updated_at"))
                out.append({"session": finish_session(session, git),
                            "events": events,
                            "source_path": store_path,
                            "extra_sources": []})
        finally:
            con.close()
        return out

    def parse(self, source: Path) -> Optional[dict]:
        session: Dict[str, Any] = None
        events: List[dict] = []
        title: Optional[str] = None
        model: Optional[str] = None
        usage_total: Dict[str, int] = {}
        first_user: Optional[str] = None
        first_clean: Optional[str] = None

        try:
            fh = open(source, encoding="utf-8", errors="replace")
        except OSError:
            return None
        with fh:
            for seq, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = row.get("type")
                payload = row.get("payload") or {}
                ts = parse_ts(row.get("timestamp"))

                if typ == "session_meta":
                    p = payload
                    session = new_session(
                        provider=self.provider,
                        native_session_id=p.get("session_id") or p.get("id"),
                        started_at=ts,
                        updated_at=ts,
                        cwd=p.get("cwd"),
                        model=p.get("model_provider"),
                        raw_metadata={
                            k: p.get(k)
                            for k in ("originator", "source", "cli_version",
                                      "model_provider", "history_base",
                                      "git", "timestamp")
                            if p.get(k) is not None
                        },
                    )
                    g = p.get("git") or {}
                    session["git_commit"] = g.get("commit_hash")
                    session["git_branch"] = g.get("branch")
                    session["git_remote"] = g.get("repository_url")
                    continue

                if session is None:
                    # tolerate truncated files that lost their meta line
                    session = new_session(provider=self.provider,
                                          native_session_id=source.stem)

                def ev(**kw):
                    # provenance is structural: who produced this record, not what
                    # it says.  Session-level source matters because a subagent
                    # session submits injected history through the user channel.
                    kw.setdefault("origin", provenance.codex_origin(
                        row, session.get("raw_metadata")))
                    e = new_event(sid=session["id"], ts=ts, seq=seq,
                                  raw_event=row, **kw)
                    events.append(e)
                    return e

                ptyp = payload.get("type")
                if typ == "response_item" and ptyp == "message":
                    role = payload.get("role") or "assistant"
                    text = text_of(payload.get("content"))
                    if role == "user" and text:
                        if first_user is None:
                            first_user = text
                        if first_clean is None:
                            first_clean = _clean_title(text) or None
                    if role in ("user", "assistant"):
                        ev(kind=role, role=role, content=text)
                elif typ == "response_item" and ptyp == "reasoning":
                    summary = text_of(payload.get("summary"))
                    ev(kind="reasoning", role="assistant",
                       content=summary or None,
                       model=payload.get("model"))
                elif typ in ("response_item",) and ptyp in (
                    "function_call", "custom_tool_call",
                ):
                    name = payload.get("name") or "unknown"
                    args = payload.get("arguments") or payload.get("input")
                    parsed = None
                    if isinstance(args, str):
                        try:
                            parsed = json.loads(args)
                        except json.JSONDecodeError:
                            parsed = None
                    cmd = _command_from_args(name, parsed) if parsed else (
                        args if isinstance(args, str) and name == "exec" else None
                    )
                    ev(kind="tool_call", role="assistant", tool_name=name,
                       tool_call_id=payload.get("call_id"),
                       tool_input=json.dumps(parsed, ensure_ascii=False)
                       if parsed is not None else (args if isinstance(args, str) else None),
                       command=cmd)
                elif typ in ("response_item",) and ptyp in (
                    "function_call_output", "custom_tool_call_output",
                ):
                    rc, body = _extract_output(payload.get("output"))
                    ev(kind="tool_result", role="tool",
                       tool_call_id=payload.get("call_id"),
                       tool_output=body or None, exit_code=rc)
                elif typ == "response_item" and ptyp == "web_search_call":
                    action = payload.get("action") or {}
                    ev(kind="tool_call", role="assistant",
                       tool_name="web_search", tool_input=json.dumps(action, ensure_ascii=False))
                elif typ == "turn_context":
                    if payload.get("model"):
                        model = payload["model"]
                    if payload.get("cwd") and not session.get("cwd"):
                        session["cwd"] = payload["cwd"]
                elif typ == "token_usage_record":
                    u = (payload.get("usage") or {})
                    for k in ("input", "cached_input", "cache_write_input",
                              "output", "reasoning_output", "total"):
                        if u.get(k):
                            usage_total[k] = usage_total.get(k, 0) + u[k]
                elif typ == "compacted":
                    ev(kind="meta", role="system",
                       content=payload.get("message") or "conversation compacted")

        if session is None:
            return None

        native = session.get("native_session_id") or source.stem
        session["id"] = f"codex:{native}"
        for e in events:
            e["sid"] = session["id"]
        session["title"] = first_clean or native
        session["model"] = model or session.get("model")
        session["updated_at"] = max(
            (e["ts"] for e in events if e.get("ts")), default=session.get("started_at")
        )
        session["message_count"] = sum(
            1 for e in events if e["kind"] in ("user", "assistant")
        )
        session["tool_count"] = sum(
            1 for e in events if e["kind"] == "tool_call"
        )
        session["can_resume"] = True
        session["can_fork"] = True
        session["resume_cmd"] = f"codex resume {native}"
        session["metadata"] = {
            "usage_totals": usage_total or None,
            "originator": session["raw_metadata"].get("originator"),
        }
        git = git_info(session.get("cwd"))
        return {
            "session": finish_session(session, git),
            "events": events,
            "extra_sources": [],
        }


register(CodexAdapter())
