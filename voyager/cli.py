"""Voyager CLI — unified local session manager for AI coding agents."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .adapters import load_all
from .adapters.base import enabled_adapters
from .store import Store, default_db_path


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def cmd_scan(args) -> int:
    load_all()
    store = Store(args.db)
    adapters = enabled_adapters(args.platform.split(",") if args.platform else None)

    grand_new = grand_skip = grand_evt = 0
    for ad in adapters:
        new = skip = evt = 0
        multi = hasattr(ad, "scan") and callable(getattr(ad, "scan"))
        live_ids: set = set()
        rescan = args.force

        sources = ad.discover()
        if not sources:
            # nothing on disk anymore: drop everything this provider had
            gone = store.prune_missing_sessions(ad.provider, set())
            if gone:
                print(f"  {ad.provider}: pruned {gone} vanished session(s)")
            else:
                print(f"  {ad.provider}: no sources found")
            continue

        if multi:
            for src in sources:
                try:
                    if store.source_changed(ad.provider, src):
                        rescan = True
                        break
                except OSError:
                    continue
        else:
            for src in sources:
                try:
                    if store.source_changed(ad.provider, src):
                        rescan = True
                        break
                except OSError:
                    continue

        # Sessions whose sources are still on disk survive pruning even when
        # they were skipped (unchanged) or failed to parse this round; only
        # sources that vanished from disk release their sessions.
        disk_paths = {str(p) for p in sources}
        for r in store.q("SELECT path, sid FROM sources WHERE provider=?", (ad.provider,)):
            if r["sid"] and r["path"] in disk_paths:
                live_ids.add(r["sid"])

        if not rescan:
            skip = len(sources)
        elif multi:
            # one artifact (SQLite DB) -> many sessions; scan() returns ALL
            # bundles exactly once — never call it inside a per-source loop.
            # If the artifact is temporarily unreadable (locked DB) treat the
            # round as skipped instead of pruning everything it owns.
            try:
                bundles = ad.scan(store.source_changed)
            except Exception as e:
                print(f"  ! {ad.provider}: scan failed ({e}); keeping existing index",
                      file=sys.stderr)
                bundles = None
                skip = len(sources)
            if bundles is not None:
                anchor = sources[0]
                for bundle in bundles:
                    store.replace_session(
                        bundle["session"], bundle["events"], ad.provider,
                        Path(bundle.get("source_path") or anchor),
                        bundle.get("extra_sources"),
                    )
                    live_ids.add(bundle["session"]["id"])
                    new += 1
                    evt += len(bundle["events"])
        else:
            for src in sources:
                try:
                    if not args.force and not store.source_changed(ad.provider, src):
                        skip += 1
                        continue
                except OSError:
                    continue
                try:
                    result = ad.parse(src)
                except Exception as e:
                    print(f"  ! {src.name}: {e}", file=sys.stderr)
                    continue
                if not result:
                    continue
                if "__error__" in result:
                    print(f"  ! {src.name}: {result['__error__']}", file=sys.stderr)
                    continue
                store.replace_session(
                    result["session"], result["events"], ad.provider,
                    src, result.get("extra_sources"),
                )
                live_ids.add(result["session"]["id"])
                new += 1
                evt += len(result["events"])

        gone = store.prune_missing_sessions(ad.provider, live_ids)
        if gone:
            print(f"  {ad.provider}: pruned {gone} vanished session(s)")

        stats = store.stats()
        print(f"  {ad.provider}: {stats['by_provider'].get(ad.provider, 0)} sessions indexed "
              f"({new} new/refreshed, {skip} unchanged)", flush=True)
        grand_new += new; grand_skip += skip; grand_evt += evt

    stats = store.stats()
    print(f"\nscan complete: {stats['sessions']} sessions, {stats['events']} events "
          f"(new/refreshed: {grand_new}, unchanged: {grand_skip})")
    print(f"index: {store.db_path}")
    return 0


def _sid_of_source(store: Store, provider: str, src: Path):
    row = store.con.execute(
        "SELECT sid FROM sources WHERE provider=? AND path=?",
        (provider, str(src)),
    ).fetchone()
    return row["sid"] if row else f"{provider}:{src.stem}"


# ---------------------------------------------------------------------------
# list / show / search / repo
# ---------------------------------------------------------------------------

def _short_ts(ts):
    from datetime import datetime
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def cmd_list(args) -> int:
    store = Store(args.db)
    rows = store.sessions(args.platform)
    if args.repo:
        rows = [r for r in rows if _repo_match(r, args.repo)]
    if args.since:
        import time
        cutoff = time.time() - args.since * 86400
        rows = [r for r in rows if (r["updated_at"] or 0) >= cutoff]
    if not rows:
        print("no sessions (run `voyager scan` first?)")
        return 0
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"{'ID':<44} {'PROV':<6} {'UPDATED':<17} {'MSG':>4} {'TOOL':>4}  TITLE")
    for r in rows:
        native = r["native_id"] or ""
        nid = native if len(native) <= 36 else native[:33] + "..."
        print(f"{nid:<44} {r['provider']:<6} {_short_ts(r['updated_at']):<17} "
              f"{r['message_count']:>4} {r['tool_count']:>4}  {(r['title'] or '')[:60]}")
    print(f"\n{len(rows)} session(s). Use `voyager show <native-id|prefix>` for details.")
    return 0


def _fmt_ts(ts):
    from datetime import datetime
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _repo_match(row, pattern: str) -> bool:
    pat = pattern.lower().replace("\\", "/").rstrip("/")
    for field in ("repo_root", "cwd", "git_remote"):
        v = row[field]
        if v and pat in v.lower().replace("\\", "/"):
            return True
    return False


def _resolve(store: Store, ref: str):
    row, ambiguous = store.session(ref)
    if row is None and ambiguous:
        print(f"error: '{ref}' matches {len(ambiguous)} sessions:", file=sys.stderr)
        for r in ambiguous[:10]:
            print(f"  [{r['provider']}] {r['native_id']}  {(r['title'] or '')[:60]}",
                  file=sys.stderr)
        print("use a longer prefix", file=sys.stderr)
        sys.exit(2)
    if row is None:
        print(f"error: session not found: {ref}", file=sys.stderr)
        sys.exit(2)
    return row


def cmd_show(args) -> int:
    store = Store(args.db)
    row = _resolve(store, args.session)
    events = store.events(row["id"])
    if args.json:
        import json as j
        print(j.dumps({"session": dict(row), "events": [dict(e) for e in events]},
                      ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"{row['provider']}:{row['native_id']}")
    print(f"  title   : {row['title']}")
    print(f"  time    : {_short_ts(row['started_at'])} -> {_short_ts(row['updated_at'])}")
    print(f"  cwd     : {row['cwd']}")
    if row["repo_root"]:
        print(f"  repo    : {row['repo_root']}  branch={row['git_branch']} "
              f"commit={(row['git_commit'] or '')[:12]}")
    print(f"  model   : {row['model']}")
    print(f"  msgs    : {row['message_count']}   tools: {row['tool_count']}")
    if row["resume_cmd"]:
        print(f"  resume  : {row['resume_cmd']}")
    print()
    for ev in events:
        t = _short_ts(ev["ts"])
        kind = ev["kind"]
        label = {"user": "USER", "assistant": "ASST", "reasoning": "think",
                 "tool_call": "TOOL>", "tool_result": "  out", "error": "ERR!",
                 "usage": "usage", "snapshot": "files", "file": "file",
                 "meta": "meta"}.get(kind, kind)
        body = ""
        if kind in ("user", "assistant", "reasoning", "error"):
            body = (ev["content"] or "").strip().replace("\n", " ")[:110]
        elif kind == "tool_call":
            body = f"{ev['tool_name']} " + ((ev["command"] or ev["tool_input"] or "")
                                            .strip().replace("\n", " ")[:90])
            if ev["file_path"]:
                body += f" [{ev['file_path']}]"
        elif kind == "tool_result":
            body = (ev["tool_output"] or ev["stdout"] or "").strip().replace("\n", " ")[:100]
            rc = f" exit={ev['exit_code']}" if ev["exit_code"] is not None else ""
            body = f"{rc} {body}"
        elif kind == "snapshot":
            try:
                snap_files = json.loads(ev["files_json"] or "[]")
            except json.JSONDecodeError:
                snap_files = []
            body = ", ".join(snap_files[:3])
        if kind in ("user", "assistant", "tool_call"):
            print(f"[{t}] {label:<5} {body}")
        else:
            print(f"[{t}] {label:<5} {body}" + ("" if kind == "reasoning" else ""))
    return 0


def cmd_search(args) -> int:
    store = Store(args.db)
    try:
        rows = store.search(args.query, limit=args.limit)
    except Exception as e:
        print(f"search error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2, default=str))
        return 0
    if not rows:
        print("no matches")
        return 0
    seen = {}
    for r in rows:
        key = r["id"]
        if key not in seen:
            seen[key] = r
        print(f"[{r['provider']}] {r['native_id'][:36]}  {_short_ts(r['updated_at'])}")
        sn = (r["snippet"] or "").replace("\n", " ")[:150]
        print(f"    {sn}")
    print(f"\n{len(rows)} hit(s) across {len(seen)} session(s). `voyager show <id>` for detail.")
    return 0


def cmd_repo(args) -> int:
    store = Store(args.db)
    rows = [r for r in store.sessions() if _repo_match(r, args.repo)]
    if not rows:
        print(f"no sessions matched repo pattern: {args.repo}")
        return 0
    # group by resolved repo identity
    groups: dict = {}
    for r in rows:
        key = r["repo_root"] or r["cwd"] or r["git_remote"] or "?"
        groups.setdefault(key, []).append(r)
    for repo, sess in sorted(groups.items()):
        print(f"\n{repo}  ({len(sess)} sessions)")
        for r in sorted(sess, key=lambda x: x["updated_at"] or 0, reverse=True):
            print(f"  {_short_ts(r['updated_at'])}  {r['provider']:<7} "
                  f"\"{(r['title'] or '')[:60]}\"  "
                  f"{r['message_count']} msgs / {r['tool_count']} tools"
                  + (f"  branch:{r['git_branch']}" if r["git_branch"] else "")
                  + (f"  commit:{(r['git_commit'] or '')[:10]}" if r["git_commit"] else ""))
    return 0


# ---------------------------------------------------------------------------
# export / resume / diff / files
# ---------------------------------------------------------------------------

def cmd_export(args) -> int:
    from .export import write_export
    store = Store(args.db)
    row = _resolve(store, args.session)
    fmt = args.format
    out = args.output or f"{row['provider']}-{row['native_id'][:20]}.{fmt}"
    path = write_export(store, row, out, fmt)
    print(f"exported: {path}")
    return 0


def cmd_resume(args) -> int:
    store = Store(args.db)
    row = _resolve(store, args.session)
    if not row["can_resume"] or not row["resume_cmd"]:
        print(f"Resume unsupported for provider '{row['provider']}'. "
              f"(native session: {row['native_id']})")
        return 1
    cmd = row["resume_cmd"]
    if args.print:
        print(cmd)
        return 0
    # resume_cmd is built by our own adapters from the provider id, but the
    # id itself came from provider data files — never trust it with a shell.
    parts = cmd.split()
    print(f"$ {cmd}")
    try:
        return subprocess.call(parts)
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        print(f"failed to launch: {e}", file=sys.stderr)
        return 1


def cmd_files(args) -> int:
    store = Store(args.db)
    row = _resolve(store, args.session)
    rows = store.q("SELECT * FROM files WHERE sid=?", (row["id"],))
    snap_paths = []
    for ev in store.events(row["id"]):
        if ev["kind"] == "snapshot" and ev["files_json"]:
            snap_paths.extend(json.loads(ev["files_json"]))
        if ev["kind"] in ("tool_call", "file") and ev["file_path"]:
            snap_paths.append(ev["file_path"])
    for p in dict.fromkeys(snap_paths):
        print(p)
    for r in rows:
        if r["path"]:
            print(f"{r['path']}  (backup: {r['backup']})")
    if not rows and not snap_paths:
        print(f"no file history recorded for this session "
              f"(provider: {row['provider']})")
    return 0


def cmd_diff(args) -> int:
    """Best-effort: rebuild file diffs from Claude's file-history version chain."""
    store = Store(args.db)
    row = _resolve(store, args.session)
    import difflib
    fh_dir = Path.home() / ".claude" / "file-history" / (row["native_id"] or "")
    if not fh_dir.is_dir():
        print("no file-history for this session (only Claude Code sessions carry it)")
        return 1
    versions: dict = {}
    for f in sorted(fh_dir.iterdir()):
        if "@" not in f.name:
            continue
        base, ver = f.name.rsplit("@", 1)
        try:
            vnum = int(ver.lstrip("v"))
        except ValueError:
            continue
        versions.setdefault(base, []).append((vnum, f))
    if not versions:
        print("no versioned backups found")
        return 1
    shown = 0
    for base, vers in versions.items():
        if len(vers) < 2:
            continue
        vers.sort()
        a = vers[0][1].read_text(encoding="utf-8", errors="replace").splitlines()
        b = vers[-1][1].read_text(encoding="utf-8", errors="replace").splitlines()
        if a == b:
            continue
        if args.file and args.file not in base:
            continue
        diff = list(difflib.unified_diff(a, b, fromfile=f"{base}@v{vers[0][0]}",
                                         tofile=f"{base}@v{vers[-1][0]}", lineterm=""))[:400]
        print("\n".join(diff))
        shown += 1
    if not shown:
        print("no multi-version files to diff")
    return 0


def cmd_stats(args) -> int:
    store = Store(args.db)
    stats = store.stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_watch(args) -> int:
    """Keep the index in sync automatically: scan on a fixed interval."""
    import time as _time
    store = Store(args.db)
    interval = max(10, args.interval)
    print(f"watching for agent session changes every {interval}s "
          f"(Ctrl+C to stop)", flush=True)
    while True:
        try:
            args.force = False
            before = store.stats()
            cmd_scan(args)
            after = store.stats()
            if after["sessions"] != before["sessions"]:
                print(f"  ↳ {after['sessions'] - before['sessions']:+d} sessions")
            _time.sleep(interval)
        except KeyboardInterrupt:
            print("\nwatch stopped")
            return 0
        except Exception as e:
            print(f"  ! scan error: {e}; retrying in {interval}s", file=sys.stderr)
            _time.sleep(interval)


def cmd_continue(args) -> int:
    """One command to pick work back up: native resume when possible,
    automatic cross-agent handoff otherwise."""
    store = Store(args.db)
    if args.session:
        row = _resolve(store, args.session)
    else:
        rows = store.sessions()
        if args.repo:
            rows = [r for r in rows if _repo_match(r, args.repo)]
        if args.platform:
            rows = [r for r in rows if r["provider"] == args.platform]
        rows = [r for r in rows if r["updated_at"]]
        if not rows:
            print("no sessions to continue (run `voyager scan` first)")
            return 1
        row = rows[0]
        print(f"latest session: [{row['provider']}] {(row['title'] or '')[:70]} "
              f"({_short_ts(row['updated_at'])})")

    if getattr(args, "to", None):
        # explicit cross-agent handoff wins
        return _handoff_from_row(store, row, args)
    if row["can_resume"] and row["resume_cmd"]:
        argv = row["resume_cmd"].split()
        print(f"$ {' '.join(argv)}")
        if not args.launch:
            print("add --launch to start it now")
            return 0
        try:
            return subprocess.call(argv)
        except KeyboardInterrupt:
            return 130
    # native resume unsupported (e.g. ZCode): fall back to a handoff package
    print(f"native resume unsupported for '{row['provider']}' — "
          f"falling back to cross-agent handoff")
    args.to = args.to or "claude"
    return _handoff_from_row(store, row, args)


def cmd_brief(args) -> int:
    """Compact digest of what every agent has been doing recently —
    designed to be read by an agent in one shot."""
    import time as _time
    store = Store(args.db)
    cutoff = _time.time() - args.hours * 3600
    rows = [r for r in store.sessions() if (r["updated_at"] or 0) >= cutoff]
    if args.repo:
        rows = [r for r in rows if _repo_match(r, args.repo)]
    if not rows:
        print(f"no sessions updated in the last {args.hours}h")
        return 0
    print(f"agent activity, last {args.hours}h ({len(rows)} sessions, "
          f"newest first):\n")
    for r in sorted(rows, key=lambda x: x["updated_at"] or 0, reverse=True)[:args.limit]:
        # one-line "what is this session doing": last user message beats title
        last_user = ""
        for e in reversed(store.events(r["id"])):
            if e["kind"] == "user" and e["content"]:
                last_user = " ".join(e["content"].split())[:140]
                break
        line = last_user or (r["title"] or "")[:140]
        print(f"[{_fmt_ts(r['updated_at'])}] {r['provider']:<7} {r['native_id'][:16]}")
        print(f"    {line}")
        print(f"    repo: {r['repo_root'] or r['cwd'] or '?'}"
              + (f"  branch:{r['git_branch']}" if r["git_branch"] else "")
              + f"  ({r['message_count']} msgs / {r['tool_count']} tools)\n")
    return 0


def cmd_handoff(args) -> int:
    store = Store(args.db)
    row = _resolve(store, args.session)
    return _handoff_from_row(store, row, args)


def _handoff_from_row(store: Store, row, args) -> int:
    from .handoff import PROMPT_TARGETS, build_context_package, default_package_name, handoff_command
    out = Path(args.output) if getattr(args, "output", None) else Path(default_package_name(row))
    package = build_context_package(store, row)
    out.write_text(package, encoding="utf-8")
    print(f"context package: {out.resolve()} ({len(package)} chars)")

    target = getattr(args, "to", None)
    if not target:
        print("next: pick a target agent, e.g. "
              f"`voyager handoff {row['native_id'][:16]} --to claude` "
              f"(targets with direct launch: {', '.join(sorted(PROMPT_TARGETS))})")
        return 0

    argv = handoff_command(target, out)
    if argv is None:
        print(f"Direct handoff launch is not supported for '{target}'. "
              f"Launchable targets: {', '.join(sorted(PROMPT_TARGETS))}. "
              f"You can still paste {out.resolve()} into that agent manually.")
        return 1
    print(f"$ {argv[0]} \"<handoff prompt>\"")
    if getattr(args, "launch", False):
        try:
            return subprocess.call(argv)
        except KeyboardInterrupt:
            return 130
        except OSError as e:
            print(f"failed to launch: {e}", file=sys.stderr)
            return 1
    print("add --launch to start it now")
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="voyager",
        description="Unified local session manager for AI coding agents",
    )
    p.add_argument("--db", help=f"index db path (default {default_db_path()})")
    # `--db` is accepted on either side of the subcommand: `voyager --db X stats`
    # and `voyager stats --db X` both work. The subparser copy needs
    # default=SUPPRESS, otherwise argparse's sub-namespace would overwrite a
    # value given before the subcommand with its own default (None) — which
    # silently redirects the whole command to ~/.voyager/index.db.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="discover and index agent sessions",
                        parents=[common])
    sp.add_argument("--platform", help="comma list: codex,claude,zcode,dsh")
    sp.add_argument("--force", action="store_true", help="re-parse even if unchanged")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("list", help="list sessions", parents=[common])
    sp.add_argument("--platform")
    sp.add_argument("--repo", help="filter by repo/cwd substring")
    sp.add_argument("--since", type=float, help="only sessions updated in last N days")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="show one session's timeline", parents=[common])
    sp.add_argument("session")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("search", help="full-text search across all sessions",
                        parents=[common])
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("repo", help="timeline of all agent sessions for a repo",
                        parents=[common])
    sp.add_argument("repo", help="repo root / remote / cwd substring")
    sp.set_defaults(func=cmd_repo)

    sp = sub.add_parser("export", help="export a session", parents=[common])
    sp.add_argument("session")
    sp.add_argument("--format", choices=["md", "json"], default="md")
    sp.add_argument("--output", "-o")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("resume", help="resume a session in its native agent",
                        parents=[common])
    sp.add_argument("session")
    sp.add_argument("--print", action="store_true", help="print command instead of running")
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("files", help="list files touched by a session",
                        parents=[common])
    sp.add_argument("session")
    sp.set_defaults(func=cmd_files)

    sp = sub.add_parser("diff", help="rebuild file diffs (Claude file-history)",
                        parents=[common])
    sp.add_argument("session")
    sp.add_argument("--file", help="filter by path substring")
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("handoff", parents=[common],
                        help="export a session as a context package for another agent")
    sp.add_argument("session")
    sp.add_argument("--to", help="target agent (claude, codex, grok)")
    sp.add_argument("--output", "-o", help="package file path (default handoff-<provider>-<id>.md)")
    sp.add_argument("--launch", action="store_true", help="launch the target agent with the package")
    sp.set_defaults(func=cmd_handoff)

    sp = sub.add_parser("watch", help="keep the index in sync automatically",
                        parents=[common])
    sp.add_argument("--interval", type=int, default=300, help="seconds between scans (default 300)")
    sp.add_argument("--platform", help="limit to these providers (comma list)")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("continue", parents=[common],
                        help="pick work back up in one command (native resume, or auto-handoff)")
    sp.add_argument("session", nargs="?", help="session id/prefix (default: newest session)")
    sp.add_argument("--repo", help="pick the newest session of this repo")
    sp.add_argument("--platform", help="pick the newest session of this provider")
    sp.add_argument("--to", help="force cross-agent handoff to this target")
    sp.add_argument("--launch", action="store_true", help="launch immediately (default: print)")
    sp.set_defaults(func=cmd_continue)

    sp = sub.add_parser("brief", parents=[common],
                        help="compact digest of recent agent activity (agent-friendly)")
    sp.add_argument("--hours", type=float, default=48, help="look-back window (default 48h)")
    sp.add_argument("--repo", help="filter by repo/cwd substring")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("stats", help="index statistics", parents=[common])
    sp.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
