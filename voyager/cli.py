"""Voyager CLI — unified local session manager for AI coding agents."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# Import provider config for integrate command output
from .skill import PROVIDER_CONFIG, uninstall_integration, check_integration_status
from .db_health import (cmd_db_backup, cmd_db_check, cmd_db_compact,
                        cmd_db_repair)
from .adapters import load_all
from .adapters.base import enabled_adapters, git_info
from .store import Store, default_db_path, lease_state
from .integrations.hook import cmd_hook_startup
from .util import fmt_ts, same_repo_loose, parse_when as _parse_when


# Argument names that carry a filesystem path.  They are expanded once, at the
# argv boundary, instead of at each call site.
_PATH_ARGS = ("db", "home", "output", "cwd")


def _expand_path_args(args) -> None:
    """Expand `~` in every path-ish argument, in place.

    These flags receive what a shell would normally have expanded already, and
    people do write `--home ~`.  Without `expanduser()` the `~` stays literal,
    `Path("~")` is *relative*, and the value silently resolves against the
    current working directory instead of the home directory -- so
    `voyager integrate install grok --home ~` wrote its launcher to
    `./~/.voyager/bin/grok` rather than `$HOME/.voyager/bin`.  A stray `~/`
    directory in a repo root is what that looks like.

    Done centrally because these values are consumed in three different
    modules (`cli`, `integrations.hook`, `launcher`), and "remember to convert
    it at every call site" is precisely the convention that was missed.
    """
    for name in _PATH_ARGS:
        value = getattr(args, name, None)
        if isinstance(value, str) and value:
            setattr(args, name, os.path.expanduser(value))


def _spawn_argv(argv):
    """Return `argv` with `argv[0]` replaced by the path it resolves to.

    Every `resume_cmd` is built as a friendly string with a *bare* provider
    name (`claude --resume <id>`, `codex resume <id>`, ...), and that string is
    also what gets printed and exported, so it has to stay readable.  But a
    bare name is not always launchable: on Windows these providers are usually
    installed by npm as `claude.CMD` shims, and `subprocess` does not consult
    `PATHEXT` the way a shell does, so spawning the bare name raises
    `FileNotFoundError` / WinError 2 even though the CLI is plainly on PATH.
    Detection already used `shutil.which()`; the launch has to agree with it.

    A name that cannot be resolved is passed through unchanged, so the
    existing `OSError` handling still reports it rather than hiding it.
    """
    if not argv:
        return argv
    resolved = shutil.which(argv[0])
    if resolved and resolved != argv[0]:
        return [resolved, *argv[1:]]
    return list(argv)


def _launch(argv):
    """Run a provider CLI, resolving `argv[0]` first (see `_spawn_argv`).

    Centralised so the resolution cannot be forgotten at one of the launch
    sites -- "remember to convert it at every call site" is exactly the
    convention that was missed for path expansion.
    """
    return subprocess.call(_spawn_argv(argv))


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def run_scan(store: Store, providers: Optional[List[str]] = None,
             force: bool = False, quiet: bool = False) -> dict:
    """Incremental scan core (Phase 1b). Returns stats; respects mtime+size,
    never rewrites provider files, prunes vanished sources."""
    import time as _time
    t0 = _time.time()
    load_all()
    adapters = enabled_adapters(providers)

    grand_new = grand_skip = grand_evt = grand_changed = 0
    for ad in adapters:
        new = skip = evt = 0
        changed = 0
        multi = hasattr(ad, "scan") and callable(getattr(ad, "scan"))
        rescan = force

        sources = ad.discover()
        if not sources:
            # nothing on disk anymore: the provider's history is RETAINED, not
            # dropped (O2 — a vanished source is not a request to delete)
            gone = store.prune_missing_sessions(ad.provider, set())
            if gone and not quiet:
                print(f"  {ad.provider}: retained {gone} session(s) whose "
                      f"sources vanished")
            elif not quiet:
                print(f"  {ad.provider}: no sources found")
            continue

        for src in sources:
            try:
                if store.source_changed(ad.provider, src):
                    changed += 1
                    rescan = True
            except OSError:
                continue

        # Sessions whose sources are still on disk survive pruning even when
        # they were skipped (unchanged) or failed to parse this round; only
        # sources that vanished from disk release their sessions.
        disk_paths = {str(p) for p in sources}

        if not rescan:
            skip = len(sources)
        elif multi:
            # one artifact (SQLite DB) -> many sessions; scan() returns ALL
            # bundles exactly once — never call it inside a per-source loop.
            # If the artifact is temporarily unreadable (locked DB) treat the
            # round as skipped instead of pruning everything it owns.
            try:
                # `--force` means "re-read everything", and an adapter's own
                # per-file fingerprint check is exactly what force is meant to
                # bypass.  Without this, force only made the pipeline *call*
                # scan(); the adapter still skipped every unchanged file, so a
                # session whose source was recorded but never indexed could never
                # come back.
                bundles = ad.scan((lambda p, f: True) if force
                                  else store.source_changed)
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
                    new += 1
                    evt += len(bundle["events"])
        else:
            for src in sources:
                try:
                    if not force and not store.source_changed(ad.provider, src):
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
                new += 1
                evt += len(result["events"])

        gone = store.prune_missing_sessions(ad.provider, disk_paths)
        if gone and not quiet:
            print(f"  {ad.provider}: retained {gone} session(s) whose "
                  f"sources vanished")

        stats = store.stats()
        if not quiet:
            print(f"  {ad.provider}: {stats['by_provider'].get(ad.provider, 0)} sessions indexed "
                  f"({new} new/refreshed, {skip} unchanged)", flush=True)
        grand_new += new; grand_skip += skip; grand_evt += evt
        grand_changed += changed

    # Automatic Continuity: resolve pending attaches against the freshly
    # indexed sessions (Phase A). Quiet scans still resolve — only the
    # output is suppressed.
    from .auto import resolve_pending_attaches, continuity_cycle
    pend = resolve_pending_attaches(store, quiet=quiet)
    renewed = store.thread_lease_renew_alive()
    if not quiet:
        for a in pend["attached"]:
            print("  ↳ auto-attached {0} → {1}".format(a["session"], a["thread"]))
        for a in pend["ambiguous"]:
            print("  ↳ pending {0}: AMBIGUOUS ({1} candidates) — not attached".format(
                a["thread"], len(a["candidates"])))
        for st in pend["stale"]:
            print("  ↳ pending stale: {0}".format(st["thread"]))
        if renewed:
            print("  ↳ lease heartbeat renewed ({0})".format(renewed))

    stats = store.stats()
    res = {"new": grand_new, "skip": grand_skip, "events_added": grand_evt,
           "changed_sources": grand_changed, "elapsed": _time.time() - t0,
           "sessions": stats["sessions"], "events": stats["events"],
           "auto_attached": pend["attached"], "pending_stale": pend["stale"]}
    if not quiet:
        print(f"\nscan complete: {stats['sessions']} sessions, {stats['events']} events "
              f"(new/refreshed: {grand_new}, unchanged: {grand_skip})")
        print(f"index: {store.db_path}")
    return res


def _refresh_grok_rules(store: Store) -> None:
    """Refresh Grok's continuation rule for the repo this sync ran in.

    Grok's SessionStart hook cannot inject anything (the event is passive and
    its stdout is ignored), so the only channel into an interactive session is
    the rules file Grok loads before the first turn. The launcher writes it, but
    the launcher only runs when ``~/.voyager/bin`` precedes the real binary on
    ``PATH`` -- which is not the default -- so the sync also refreshes it. A scan
    is the last thing to run before a handoff in the documented flow.

    This belongs to the *command*, not to ``run_scan()``. ``run_scan()`` is a
    core primitive that context compilation calls back into
    (``auto.get_continuation_context(sync=True)``), and this write compiles
    context itself, so putting it in ``run_scan()`` made the two re-enter each
    other::

        run_scan -> write_context_rules -> startup_continuity -> compile
                 -> get_continuation_context -> run_scan -> ...

    With a stale cache that recursed until the stack ran out, which hung
    ``voyager scan`` and every Claude SessionStart that had to compile.

    ``clear=False`` because a sync is not a launch: ``voyager watch`` is started
    from the Startup folder, so its cwd is not the repo the user is in, and
    clearing on "no thread for *my* cwd" would delete the rule the handoff
    depends on once per interval. Only a caller that knows where the next
    session starts (the launcher) may remove the file.
    """
    try:
        from .integrations.grok_native import write_context_rules
        grok_ctx = write_context_rules(cwd=os.getcwd(), store=store,
                                       clear=False)
    except Exception:
        return
    if grok_ctx.get("status") == "written":
        print("  ↳ grok continuation rule refreshed ({0})".format(
            grok_ctx["path"]))


def cmd_scan(args) -> int:
    store = Store(args.db)
    run_scan(store,
             providers=args.platform.split(",") if args.platform else None,
             force=args.force, quiet=False)
    _refresh_grok_rules(store)
    return 0


def cmd_provenance(args) -> int:
    """Inspect provenance coverage, or deterministically fill NULL origins."""
    import json as _json

    from .provenance import coverage, enrich

    store = Store(args.db)
    providers = args.provider.split(",") if args.provider else None

    if args.action == "coverage":
        rows = coverage(store.con)
        if args.json:
            print(_json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        print("%-8s %10s %10s %10s %10s %9s"
              % ("provider", "user_evts", "enriched", "human", "unknown", "coverage"))
        for r in rows:
            print("%-8s %10d %10d %10d %10d %8.1f%%"
                  % (r["provider"], r["user_events"], r["enriched"], r["human"],
                     r["unknown"], r["coverage_pct"]))
        return 0

    report = enrich(store.con, dry_run=not args.apply, providers=providers)
    if args.json:
        print(_json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    mode = "APPLY" if args.apply else "DRY-RUN"
    print("provenance enrich [%s]" % mode)
    print("  candidates  : %d" % report["candidates"])
    print("  classified  : %d" % report["classified"])
    print("  left unknown: %d" % report["left_unknown"])
    print("  applied     : %d" % report["applied"])
    print("  %-8s %-11s %-11s %-10s" % ("provider", "user_evts", "classified", "coverage"))
    for prov, stat in sorted(report["per_provider"].items()):
        uc = stat.get("user_candidates", 0)
        uk = stat.get("user_classified", 0)
        print("    %-8s %-11d %-11d %s"
              % (prov, uc, uk, ("%.1f%%" % (100.0 * uk / uc)) if uc else "—"))
    if not args.apply and report["classified"]:
        print("  (dry run: nothing written; re-run with --apply)")
    return 0


def _ensure_fresh(args, store: Store, providers: Optional[List[str]] = None) -> dict:
    """Phase 1b: incremental scan before compiling/reading sessions.
    Never --force; prints one freshness line so stale bundles are explicable.
    Set VOYAGER_NO_SYNC=1 to skip (tests, offline inspection).

    The line is suppressed under ``--json``: a machine-readable stream must not
    be polluted by a human diagnostic, or the caller cannot parse it."""
    quiet = bool(getattr(args, "json", False))
    if os.environ.get("VOYAGER_NO_SYNC"):
        if not quiet:
            print("freshness: skipped (VOYAGER_NO_SYNC)")
        return {"changed_sources": 0, "elapsed": 0.0}
    res = run_scan(store, providers=providers, force=False, quiet=True)
    if not quiet:
        print(f"freshness: scanned in {res['elapsed']:.1f}s, "
              f"{res['changed_sources']} source(s) changed")
    return res


def _scan_scope_for_sessions(store: Store, refs) -> Optional[List[str]]:
    """Provider scope for the pre-compile scan, or ``None`` for "scan all".

    D12 requires a command to refresh the index before compiling from it, but
    it does not require refreshing *every* provider: when every session the
    command names is already indexed, the only providers whose files that
    command can read are those sessions' providers -- plus, for a session that
    belongs to a WorkThread, every member provider, because the engine
    compiles the whole thread.  Scoping there skips the filesystem walk and
    the ``git`` spawns of unrelated providers (the cost O1 measured).

    Scoping is an **optimisation, never a correctness change**: if any named
    ref is not resolvable -- a brand-new session that has not been scanned yet,
    which is exactly the case the pre-compile scan exists for -- the scope is
    unknown, so this returns ``None`` and the caller scans everything.  A scan
    can therefore never miss a source it would otherwise have refreshed.
    """
    if not refs:
        return None
    provs: set = set()
    for ref in refs:
        try:
            row, _ambiguous = store.session(ref)
        except Exception:
            return None
        if row is None:
            return None
        if row["provider"]:
            provs.add(row["provider"])
        try:
            tid = store.thread_find_containing({row["id"]})
        except Exception:
            return None
        if tid:
            try:
                for member in store.thread_members(tid):
                    if member["provider"]:
                        provs.add(member["provider"])
            except Exception:
                return None
    return sorted(provs) or None


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
    retained = 0
    for r in rows:
        native = r["native_id"] or ""
        nid = native if len(native) <= 36 else native[:33] + "..."
        # O2: mark, do not hide.  A retained session is real history, but it is
        # not resumable and not part of live continuity.
        mark = ""
        if r["source_state"] == "SOURCE_MISSING":
            mark = "[source missing] "
            retained += 1
        print(f"{nid:<44} {r['provider']:<6} {_short_ts(r['updated_at']):<17} "
              f"{r['message_count']:>4} {r['tool_count']:>4}  {mark}{(r['title'] or '')[:60]}")
    print(f"\n{len(rows)} session(s). Use `voyager show <native-id|prefix>` for details.")
    if retained:
        print(f"{retained} of them are retained history: every source file is gone, "
              f"so they stay searchable but are excluded from continuity "
              f"(`voyager doctor` reports the totals).")
    return 0


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
    if row["source_state"] == "SOURCE_MISSING":
        # O2: say it here too — otherwise this looks like a resumable session.
        print(f"  source  : MISSING since {_short_ts(row['source_missing_since'])}"
              f"  — retained history: searchable, not resumable")
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


def _search_filters(args) -> dict:
    return {
        "providers": [p.strip() for p in (getattr(args, "provider", None) or "").split(",") if p.strip()],
        "repo": getattr(args, "repo", None),
        "since": _parse_when(getattr(args, "since", None)),
        "until": _parse_when(getattr(args, "until", None)),
        "kinds": [k.strip() for k in (getattr(args, "kind", None) or "").split(",") if k.strip()],
        "tool": getattr(args, "tool", None),
        "file": getattr(args, "file", None),
        "origins": [o.strip() for o in (getattr(args, "origin", None) or "").split(",") if o.strip()],
        "human_only": bool(getattr(args, "human_only", False)),
    }


def cmd_search(args) -> int:
    store = Store(args.db)
    try:
        rows = store.search(args.query, limit=args.limit,
                            filters=_search_filters(args))
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
    applied = {k: v for k, v in _search_filters(args).items() if v}
    if applied:
        print("  filters: %s" % ", ".join("%s=%s" % (k, v) for k, v in applied.items()))
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
        return _launch(parts)
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


def cmd_api(args) -> int:
    """Phase 7: local stdio JSON-lines API (the VS Code sidebar's client)."""
    from .api import serve
    serve(Path(args.db) if getattr(args, "db", None) else None)
    return 0


def cmd_integration_info(args) -> int:
    """Machine-readable version/capability probe for client integrations.

    A thin client (the DSH plugin, the VS Code extension) must fail clearly
    when the core is older than the interface it drives, so this reports the
    bridge ``schema_version`` alongside the package version.  Always JSON: the
    caller is a program, and a human can pipe it to ``jq``.
    """
    from .api import integration_info
    print(json.dumps(integration_info(), ensure_ascii=False, indent=2))
    return 0


def cmd_skill(args) -> int:
    """Legacy wrapper for backward compatibility."""
    from .skill import install_skills, skill_source

    results = install_skills(agent=args.agent, force=args.force,
                             home=Path(args.home) if args.home else None)
    print(f"skill source: {skill_source()}")
    for r in results:
        # Legacy install_skills uses 'agent' key; new integrate uses 'provider'
        agent_key = r.get('agent') or r.get('provider', 'unknown')
        line = f"  {agent_key:<8} {r['status']}"
        if r.get("path"):
            line += f"  ({r['path']})"
        if r.get("backup"):
            line += f"  backup={r['backup']}"
        print(line)
    return 0


def cmd_integrate(args) -> int:
    """Install full integration for a provider."""
    from .skill import install_integration

    result = install_integration(
        provider=args.provider,
        force=args.force,
        home=Path(args.home) if args.home else None,
    )

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print(f"integrate: {PROVIDER_CONFIG.get(args.provider, {}).get('name', args.provider)}")
    print(f"  status: {result['status']}")

    skill = result.get("skill", {})
    print(f"  skill: {skill.get('status')}")
    if skill.get("path"):
        print(f"           {skill['path']}")

    mcp = result.get("mcp", {})
    print(f"  mcp: {mcp.get('status', 'unknown')}")
    if mcp.get("message"):
        print(f"       {mcp['message']}")

    bootstrap = result.get("bootstrap", {})
    if bootstrap.get("status") == "generated":
        print(f"  bootstrap: generated")
        print(f"             {bootstrap.get('path')}")
    elif bootstrap.get("status") == "error":
        print(f"  bootstrap: error - {bootstrap.get('error')}")

    hook = result.get("hook") or {}
    if hook.get("status") == "installed":
        print(f"  hook: installed (matcher={hook.get('matcher')})")
        print(f"        {hook.get('command')}")
    elif hook:
        print(f"  hook: {hook.get('status')} - {hook.get('message', '')}")

    if result.get("warnings"):
        print("  warnings:")
        for w in result["warnings"]:
            print(f"          {w}")

    print(f"\nverification: {result.get('verification', 'none')}")
    return 0 if result["status"] not in ("error",) else 1


def cmd_integrate_status(args) -> int:
    """Check integration status for providers."""
    from .skill import check_integration_status

    VALID_PROVIDERS = ["codex", "claude", "grok", "zcode", "cursor",
                       "kiro", "antigravity", "dsh"]
    
    # Validate provider arguments if any were provided
    if args.providers:
        for p in args.providers:
            if p not in VALID_PROVIDERS:
                print(f"voyager integrate status: error: argument providers: "
                      f"invalid choice: {p!r} (choose from {', '.join(repr(x) for x in VALID_PROVIDERS)})")
                return 2
    
    providers = args.providers if args.providers else None
    results = check_integration_status(providers=providers,
                                        home=Path(args.home) if args.home else None)

    if getattr(args, "deep", False):
        from .capability_matrix import DIMENSIONS, collect_evidence, resolve_cell
        deep = {}
        for p in (providers or ["codex", "claude", "grok", "zcode", "cursor",
                                "kiro", "antigravity", "dsh"]):
            ev = collect_evidence(p)
            deep[p] = {d: resolve_cell(p, d, ev) for d in DIMENSIONS}
        if args.json:
            print(json.dumps({"integration": results, "capabilities": deep},
                             indent=2, ensure_ascii=False, default=str))
            return 0
        print("Capabilities (declared ceiling capped by what this machine observed)")
        for p, dims in deep.items():
            print("")
            print("  %s" % p)
            for d, (state, note) in dims.items():
                print("    %-30s %-26s %s" % (d, state, note[:54]))
        return 0

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0

    # Print table header (ASCII compatible)
    print(f"{'Provider':<15} {'Skill':<8} {'MCP':<10} {'Startup':<10} {'Auto':<6}")
    print("-" * 70)

    for r in results:
        skill_y = "Y" if r.get("skill", {}).get("installed") else "N"
        mcp_reg = "R" if r.get("mcp", {}).get("registered") else ("A" if r.get("mcp", {}).get("available") else "N")
        start_stat = r.get("startup_status", "N")  # Y=verified live, H=hook registered (unverified), A=assisted, N=none
        auto = "Y" if r.get("auto_attach") else "N"

        print(f"{r['installed']:<15} {skill_y:<8} {mcp_reg:<10} {start_stat:<10} {auto:<6}")

    # Add notes section
    print("\nLegend:")
    print("  Skill: Y=installed, N=not found")
    print("  MCP:   R=registered, A=available(unsupported), N=no support")
    print("  Start: Y=zero-touch verified live, H=native hook registered (trigger unverified),")
    print("         A=startup-assisted, N=no hook")
    print("  Auto:  Y=core supports auto-attach")
    print("\nNote: 'H' means the hook is registered in the provider's config and the handler is")
    print("      verified, but nothing has yet observed the provider firing it. Only 'Y' claims that.")
    return 0


def cmd_dashboard(args) -> int:
    """Render the local dashboard: one self-contained HTML file."""
    from .dashboard import build, render_html, write

    store = Store(args.db)
    try:
        if getattr(args, "json", False):
            print(json.dumps(build(store, repo=getattr(args, "repo", None)),
                             indent=2, ensure_ascii=False, default=str))
            return 0
        out = getattr(args, "out", None)
        if out:
            path = write(store, Path(out), repo=getattr(args, "repo", None))
        else:
            from .store import default_db_path
            path = write(store, Path(default_db_path()).parent / "dashboard.html",
                         repo=getattr(args, "repo", None))
        print(str(path))
        return 0
    finally:
        store.close()


def _db_path_arg(args):
    """argparse hands us a string; the store-facing functions want a Path."""
    from pathlib import Path as _Path
    value = getattr(args, "db", None)
    return _Path(value) if value else None


def _cmd_verify(args) -> int:
    """`voyager verify` -- strictly read-only evidence view.

    The wrapper exists so the harness is imported on demand and so the command
    has exactly one entry point; the harness itself exposes no way to write from
    a read path.
    """
    from .verification_harness import cmd_verify

    provider = args.provider
    # --all or no provider means all providers
    if getattr(args, "all", False) or provider is None:
        provider = None

    return cmd_verify(provider=provider, verbose=args.verbose,
                      json_output=args.json, db_path=_db_path_arg(args),
                      matrix=getattr(args, "matrix", False))


def cmd_doctor(args) -> int:
    """Is this installation healthy, and what is still open?

    Reads the same canonical capability matrix the README does, so the two cannot
    disagree, and classifies everything it finds as blocking / external /
    non-blocking debt.

    O4: ``--fix`` runs only SAFE_DERIVED_REPAIR (currently: clearing stale
    cache entries).  ``--dry-run`` shows what ``--fix`` would do without doing
    it.  Plain ``doctor`` is always read-only.
    """
    from .doctor import render, run, apply_fix

    db_path = _db_path_arg(args)

    # O4.9: --fix runs only SAFE_DERIVED_REPAIR.  --dry-run is a plan only.
    if getattr(args, "fix", False):
        result = apply_fix(db_path=db_path,
                           dry_run=getattr(args, "dry_run", False))
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        else:
            if result.get("dry_run"):
                print("doctor --fix (dry run)")
            else:
                print("doctor --fix")
            print("=" * 62)
            print("safe repairs: %d fixable" % result.get("fixable", 0))
            for step in result.get("executed", []):
                mark = "WOULD" if result.get("dry_run") else (
                    "OK" if step.get("ok", True) else "FAIL")
                print("  [%s] %s: %s" % (mark, step.get("code", "?"),
                                         step.get("action", step.get("error", ""))))
            if not result.get("executed"):
                print("  nothing to fix")
        # O4: the exit code reflects the state AFTER fixing -- a failed
        # repair or a remaining blocking issue still means 1.
        report = run(repo=getattr(args, "repo", None), db_path=db_path)
        return 1 if report.get("blocking") else 0

    report = run(repo=getattr(args, "repo", None), db_path=db_path)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(render(report))
    # 0 = healthy, 1 = blocking issues found (useful for scripts, not an error)
    return 1 if report.get("blocking") else 0


def cmd_integrate_remove(args) -> int:
    """Remove integration for a provider."""
    from .skill import uninstall_integration

    result = uninstall_integration(
        provider=args.provider,
        home=Path(args.home) if args.home else None,
    )

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print(f"remove: {args.provider}")
    print(f"  status: {result['status']}")

    skill = result.get("skill", {})
    print(f"  skill: {skill.get('status')}")
    if skill.get("path"):
        print(f"         {skill['path']}")

    if "mcp" in result:
        mcp = result["mcp"]
        print(f"  mcp: {mcp.get('status')}")
        if mcp.get("path"):
            print(f"       {mcp['path']}")

    if "bootstrap" in result:
        boot = result["bootstrap"]
        print(f"  bootstrap: {boot.get('status')}")
        if boot.get("paths"):
            for p in boot["paths"]:
                print(f"             {p}")

    hook = result.get("hook") or {}
    if hook:
        removed = hook.get("removed", 0)
        print(f"  hook: {hook.get('status')} ({removed} entr{'y' if removed == 1 else 'ies'} removed)")

    return 0 if result["status"] != "error" else 1


def _continuity_status_enum(disc: dict) -> str:
    """READY | PENDING_ATTACH | AMBIGUOUS | STALE | NO_THREAD."""
    if not disc.get("continuity_available"):
        return "NO_THREAD"
    pend = disc.get("pending_attach") or []
    if any(p.get("status") == "ambiguous" for p in pend):
        return "AMBIGUOUS"
    if pend:
        return "PENDING_ATTACH"
    lease = disc.get("lease_state") or {}
    if lease.get("held") and lease.get("expired"):
        return "STALE"
    return "READY"


def cmd_status(args) -> int:
    """Phase I: show the automatic-continuity state for the current repo."""
    import json as _json
    store = Store(args.db)
    from .auto import discover_continuity
    disc = discover_continuity(store, cwd=os.getcwd(),
                               repo=getattr(args, "repo", None))
    enum = _continuity_status_enum(disc)
    if args.json:
        print(_json.dumps({**disc, "status": enum}, ensure_ascii=False,
                          indent=2, default=str))
        return 0
    t = disc.get("active_thread")
    if not t:
        print("continuity: NO_THREAD — no active WorkThread for this repo")
        print("  create one: voyager thread create --repo <path>")
        return 0
    print("continuity: {0}".format(enum))
    print("  thread : {0}  {1}".format(t["id"], (t["title"] or "")[:70]))
    print("  goal   : {0}".format(t.get("goal") or t.get("title") or "?"))
    print("  repo   : {0}".format(disc["repo_root"]))
    ls = disc.get("lease_state") or {}
    print("  lease  : {0}{1}".format(
        ls.get("holder") or "free",
        " (EXPIRED: " + ls.get("why", "") + ")" if ls.get("expired") else ""))
    for p in disc.get("pending_attach") or []:
        print("  pending: {0} continuation launched, awaiting session".format(
            p.get("provider")))
    print("  action : {0}".format(disc.get("recommended_action")))
    return 0


def cmd_stats(args) -> int:
    store = Store(args.db)
    stats = store.stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def _run_launcher_prelaunch(args) -> int:
    """Wrapper for launcher prelaunch command."""
    from .launcher import main as launcher_main
    sys.exit(launcher_main(["prelaunch", f"--provider={args.provider}",
                           f"--cwd={args.cwd}"] + (["--json"] if args.json else [])))


def cmd_hook_grok_context(args) -> int:
    """Write Grok's continuation rule file before the launcher execs the CLI.

    Grok loads every ``*.md`` in ``$GROK_HOME/rules/`` into the system prompt at
    session start, before the first turn, which is the only verified way to get
    continuation context into an *interactive* Grok session. The launcher runs
    this first so the rule is on disk by the time Grok reads its rules.
    """
    from .integrations.grok_native import write_context_rules

    home = Path(args.home) if getattr(args, "home", None) else None
    store = Store(args.db) if getattr(args, "db", None) else None
    try:
        result = write_context_rules(cwd=args.cwd, home=home, store=store)
    finally:
        if store is not None:
            store.close()

    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not getattr(args, "quiet", False):
        status = result.get("status")
        if status == "written":
            print("Voyager: continuation context -> {0} ({1} chars, thread {2})"
                  .format(result["path"], result["chars"], result["thread"]))
        elif status == "no_thread":
            print("Voyager: no active WorkThread for this repo; rules cleared")
        else:
            print("Voyager: could not write Grok rules: {0}"
                  .format(result.get("message", status)))
    return 0


def cmd_hook_grok_session_start(args) -> int:
    """Grok ``SessionStart`` hook body: record the pending attach.

    Grok passes ``GROK_SESSION_ID``/``GROK_WORKSPACE_ROOT`` in the environment,
    so the native id is known at startup and the later resolution is an identity
    match. Always exits 0: a hook must fail open.
    """
    from .integrations.grok_native import session_start

    home = Path(args.home) if getattr(args, "home", None) else None
    store = Store(args.db) if getattr(args, "db", None) else None
    try:
        result = session_start(
            session_id=getattr(args, "session_id", None),
            cwd=getattr(args, "cwd", None),
            home=home,
            store=store,
        )
    finally:
        if store is not None:
            store.close()
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _watch_sleep(args, store) -> float:
    """Interval for the watch loop. Active thread leases pull it well under
    the D13 heartbeat expiry (120s) so holder leases never go stale between
    cycles, even with a slow scan in front of them."""
    interval = max(10, args.interval)
    if store.thread_lease_active_count() > 0:
        return min(interval, 30.0)
    return interval


def cmd_watch(args) -> int:
    """Keep the index in sync automatically: scan on a fixed interval."""
    import time as _time
    store = Store(args.db)
    print(f"watching for agent session changes every {max(10, args.interval)}s "
          f"(Ctrl+C to stop)", flush=True)
    while True:
        try:
            args.force = False
            before = store.stats()
            cmd_scan(args)
            after = store.stats()
            if after["sessions"] != before["sessions"]:
                print(f"  ↳ {after['sessions'] - before['sessions']:+d} sessions")
            from .auto import continuity_cycle
            cyc = continuity_cycle(store, quiet=False)
            _time.sleep(_watch_sleep(args, store))
        except KeyboardInterrupt:
            print("\nwatch stopped")
            return 0
        except Exception as e:
            print(f"  ! scan error: {e}; retrying in {interval}s", file=sys.stderr)
            _time.sleep(max(10, args.interval))



def _continue_thread(store: Store, t, args) -> int:
    """Continue inside a WorkThread.

    The engine resumes the newest natively-resumable member when no `--to` is
    given (D7) and otherwise compiles the continuation bundle from the
    thread's members.  The banner is printed here because the engine is mute.
    """
    all_members = store.thread_members(t["id"])
    # O2: retained members (every source vanished) stay in the thread for
    # display and search, but continuity runs on the live ones only.
    members = store.live_thread_members(t["id"])
    retained = len(all_members) - len(members)
    if not members:
        print("thread " + t["id"] + " has no live member sessions"
              + (" ({0} retained: every source vanished)".format(retained)
                 if retained else ""), file=sys.stderr)
        store.close()
        return 1
    print("thread {0}  [{1}]  {2} member(s){3}".format(
        t["id"], t["status"], len(members),
        ", {0} retained".format(retained) if retained else ""))
    target = getattr(args, "to", None)
    if not target:
        resumable = [m for m in members if m["can_resume"] and m["resume_cmd"]]
        if resumable:
            target = max(resumable,
                         key=lambda x: x["updated_at"] or 0)["provider"]
        else:
            target = "claude"
            print("no natively-resumable member — compiling continuation "
                  "bundle for claude (override with --to)")
    return _handoff_via_engine(store, args, "continue", thread=t, target=target)


def cmd_continue(args) -> int:
    """One command to pick work back up: native resume when possible,
    automatic cross-agent handoff otherwise.

    A thin adapter over continuity.handoff_thread(): WorkThread scope first
    (explicit --thread, else the newest active thread for this repo), then
    session scope (--from / one session / the newest session).
    """
    store = Store(args.db)
    # Phase 1b: the index is only as fresh as the last scan
    _ensure_fresh(args, store,
                  providers=[args.platform] if getattr(args, "platform", None) else None)

    # -- WorkThread scope --------------------------------------------------
    # Deterministic only: an explicit thread wins, otherwise the most
    # recently updated active thread for this repo is picked LOUDLY.
    # Multi-signal auto-clustering is deliberately NOT implemented
    # (roadmap Deferred) — sessions are never silently swallowed.
    t = None
    if getattr(args, "thread", None):
        t = store.thread_get(args.thread)
        if not t:
            print("thread not found: " + args.thread, file=sys.stderr)
            store.close()
            return 1
    elif not args.session and not getattr(args, "from_sessions", None):
        repo_ref = getattr(args, "repo", None)
        if repo_ref:
            repo = repo_ref
        else:
            repo = (git_info(os.getcwd()).get("repo_root")
                    or os.getcwd().replace("\\", "/"))
        cands = [x for x in store.thread_list("active")
                 if x["repo_root"] and same_repo_loose(x["repo_root"], repo)]
        if cands:
            t = max(cands, key=lambda x: x["updated_at"] or 0)
            print("active thread: {0}  ({1})".format(
                t["id"], (t["title"] or "")[:70]))
    if t is not None:
        return _continue_thread(store, t, args)

    # -- session scope -----------------------------------------------------
    from_sessions = getattr(args, "from_sessions", None)
    if from_sessions:
        refs = [s.strip() for s in from_sessions.split(",") if s.strip()]
        if not refs:
            print("error: --from requires at least one session id", file=sys.stderr)
            store.close()
            return 2
        rows = [_resolve(store, ref) for ref in refs]
        target = getattr(args, "to", None)
        if not target:
            target = "claude"
            print("defaulting continuation target to 'claude' (override with --to)")
        return _handoff_via_engine(store, args, "continue",
                                   sessions=rows, target=target)

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
            store.close()
            return 1
        row = rows[0]
        print(f"latest session: [{row['provider']}] {(row['title'] or '')[:70]} "
              f"({_short_ts(row['updated_at'])})")

    target = getattr(args, "to", None)
    if not target:
        if row["can_resume"] and row["resume_cmd"]:
            target = row["provider"]        # native resume in place (D7)
        else:
            # native resume unsupported (e.g. ZCode): hand off instead
            target = "claude"
            print(f"native resume unsupported for '{row['provider']}' — "
                  f"falling back to cross-agent handoff")
    return _handoff_via_engine(store, args, "continue", source=row, target=target)


# ---------------------------------------------------------------------------
# Handoff rendering (P9)
#
# continuity.handoff_thread() owns every decision — source resolution, lease,
# native resume, the canonical context, the pending attach.  These helpers only
# choose words and exit codes, so `switch` / `continue` / `handoff` / `merge`
# cannot drift apart again: they are dialects of one engine, not four
# implementations.  `voyager_context` / `voyager_continue` (MCP) are the
# read-only *context* facet of the same evidence and deliberately do not come
# through here — they compile and return text without leasing or launching.
# ---------------------------------------------------------------------------

_DEFAULT_READY = "add --launch to start it now"

_HANDOFF_DIALECTS = {
    # `switch` launches by default, so its "not launching" lines explain what
    # to do next instead of repeating the generic hint.
    "switch": {
        "ready_resume": "native resume ready",
        "ready_transplant": "transplant ready",
        "ready_bundle": "switch ready (--no-launch); the target agent should read "
                        "the bundle, then `voyager thread attach {tid} <new-id>` "
                        "resolves the pending attach",
        "launched": "switch complete: after the target agent starts, run "
                    "`voyager thread attach {tid} <new-session-id>` to link the "
                    "continuation",
    },
    "continue": {"launched": ""},
    "merge": {"launched": ""},
    "handoff": {"launched": ""},
}


def _handoff_nouns(style: str) -> tuple:
    """(artifact, prompt, launch) nouns for the context shape actually used.

    Derived from the engine's *effective* style rather than from which command
    was typed: `continue` asks for a package but a multi-member source gets a
    Continuation Bundle, and the words have to follow the file, not the flag.
    """
    if style == "package":
        return "context package", "handoff prompt", "handoff"
    return "continuation bundle", "continuation prompt", "continuation"


def _wants_launch(kind: str, args) -> bool:
    """`switch` launches by default (`--no-launch` suppresses it); every other
    entry point is opt-in (`--launch`).  That asymmetry is deliberate and
    predates the convergence — it is kept, not silently normalised."""
    if kind == "switch":
        return not getattr(args, "no_launch", False)
    return bool(getattr(args, "launch", False))


def _handoff_launch(store: Store, res, lines: list) -> int:
    """Launch a handoff result's argv, releasing the WorkThread lease if the
    launch fails.

    A dangling live lease is worse than the failed launch itself: it blocks
    every later handoff of that thread until someone runs `thread unlock
    --steal`.  Centralised here so all four entry points share one policy
    (this block used to be copy-pasted three times inside `cmd_switch`).
    """
    tid, token = res.get("thread_id"), res.get("lease_token")

    def _release(reason: str) -> None:
        if tid and token:
            from .store import Store as _Store
            s = _Store(store.db_path)
            s.thread_lease_release(tid, token, reason=reason)
            s.close()

    try:
        rc = _launch(res["argv"])
        if rc != 0:
            _release("launch-failed")
            lines.append("launch exited with code {0}; lease released".format(rc))
        return rc
    except KeyboardInterrupt:
        _release("launch-interrupted")
        lines.append("launch interrupted; lease released")
        return 130
    except OSError as e:
        _release("launch-failed")
        lines.append("launch failed ({0}); lease released".format(e))
        return 1


def _render_handoff(store: Store, res, args, kind: str) -> int:
    """Render a continuity.handoff_thread() result for a CLI entry point."""
    d = _HANDOFF_DIALECTS.get(kind, _HANDOFF_DIALECTS["continue"])
    lines: list = ["warning: {0}".format(w) for w in res.get("warnings", [])]
    action = res["action"]

    if action in ("error", "invalid"):
        if lines:
            print("\n".join(lines))
        print("error: {0}".format(res.get("error")), file=sys.stderr)
        raise SystemExit(res.get("exit_code") or 2)
    if action == "refused":
        lines.append("refused: {0}".format(res.get("error")))
        print("\n".join(lines))
        return 1

    if res.get("budget_info"):
        info = res["budget_info"]
        lines.append("estimated tokens: ~{0}".format(info["estimated_tokens"])
                     + ("  (budget {0})".format(info["budget"])
                        if info["budget"] else ""))

    from .continuity import PROMPT_TARGETS
    launchable = ", ".join(sorted(PROMPT_TARGETS))
    launch_now = _wants_launch(kind, args)
    tid = res.get("thread_id")
    noun, prompt, launch_noun = _handoff_nouns(res.get("style") or "")

    def _ready(key: str) -> str:
        return d.get(key) or _DEFAULT_READY

    if action == "native-resume":
        lines.append("$ {0}".format(" ".join(res["argv"])))
        if not launch_now:
            lines.append(_ready("ready_resume"))
            print("\n".join(lines))
            return 0
        rc = _handoff_launch(store, res, lines)
        print("\n".join(lines))
        return rc

    if action == "transcript":
        lines.append("transcript written: session id {0}".format(
            res.get("native_session_id", "unknown")))
        lines.append("$ {0}".format(" ".join(res["argv"])))
        if not launch_now:
            lines.append(_ready("ready_transplant"))
            print("\n".join(lines))
            return 0
        rc = _handoff_launch(store, res, lines)
        print("\n".join(lines))
        return rc

    # -- bundle (Continuation Bundle or handoff package) -------------------
    lines.append("{0}: {1} ({2} chars)".format(
        noun, res["context_path"], res["context_chars"]))

    if not res.get("target"):
        ref = (res.get("source_session") or "<session>").split(":")[-1][:16]
        lines.append("next: pick a target agent, e.g. `voyager {0} {1} --to "
                     "claude` (targets with direct launch: {2})".format(
                         kind, ref, launchable))
        print("\n".join(lines))
        return 0

    if not res.get("argv"):
        lines.append("Direct {0} launch is not supported for '{1}'. "
                     "Launchable targets: {2}. You can still paste {3} into "
                     "that agent manually.".format(
                         launch_noun, res["target"], launchable,
                         res["context_path"]))
        print("\n".join(lines))
        return 1

    lines.append("$ {0} \"<{1}>\"".format(res["argv"][0], prompt))
    if not launch_now:
        lines.append(_ready("ready_bundle").format(tid=tid))
        print("\n".join(lines))
        return 0
    rc = _handoff_launch(store, res, lines)
    if rc == 0 and d.get("launched"):
        lines.append(d["launched"].format(tid=tid))
    print("\n".join(lines))
    return rc


def _handoff_via_engine(store: Store, args, kind: str,
                        style: Optional[str] = None, **source) -> int:
    """Run the one engine and render it in this command's dialect.

    `source` carries the already-resolved entry point (`thread=`, `sessions=`,
    `source=`) plus `target=`; everything else is read off `args` so the four
    CLI commands cannot diverge in what they pass.

    `voyager handoff` is an *export*: it always compiles a package, because
    `--to` names the agent that will READ the package, not a session to
    resume.  `voyager resume` / `voyager continue` own native resume.  So
    `handoff` forces the bundle path even when the source provider happens to
    equal the target (which would otherwise hit the D7 shortcut).

    Style follows the *shape of the work*, which is also how the commands
    behaved before the convergence: a single session gets a Context Package
    (`handoff`, and `continue <session>`), while a WorkThread or a multi-session
    merge gets a Continuation Bundle (`switch`, `merge`).  Asking for
    `"package"` and handing the engine several members therefore degrades to
    the bundle, and the engine reports which one it actually used.
    """
    from .continuity import handoff_thread
    if style is None:
        style = "continuation" if kind in ("switch", "merge") else "package"
    res = handoff_thread(
        store=store,
        target=source.pop("target", None),
        goal=getattr(args, "goal", None),
        budget=getattr(args, "budget", None),
        mode=getattr(args, "mode", "bundle"),
        steal=getattr(args, "steal", False),
        output=getattr(args, "output", None) or None,
        style=style,
        force_bundle=(kind == "handoff"
                      or getattr(args, "bundle", False)),
        **source)
    if getattr(args, "json", False):
        # Machine-readable handoff: the engine already returns a dict, and the
        # compiled context is the whole point of the call, so include its text
        # rather than making a client re-open the file it just wrote.
        payload = dict(res)
        ctx_path = res.get("context_path") or res.get("bundle_path")
        if ctx_path:
            try:
                payload["context"] = Path(ctx_path).read_text(encoding="utf-8")
            except Exception:
                pass
        store.close()
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        code = res.get("exit_code")
        return int(code) if code is not None else 0
    store.close()
    return _render_handoff(store, res, args, kind)


def cmd_switch(args) -> int:
    """Phase 6 / #7: one command to switch agent inside the active WorkThread.

    A thin adapter over continuity.handoff_thread(): scan → resolve the
    WorkThread → the unified engine → render. Returns 0 on success, non-zero
    on error/abort.
    """
    store = Store(args.db)
    _ensure_fresh(args, store)

    target = args.agent
    from .continuity import PROMPT_TARGETS
    if target not in PROMPT_TARGETS:
        print("error: switch target must be one of {0} (got '{1}')".format(
            ", ".join(sorted(PROMPT_TARGETS)), target), file=sys.stderr)
        store.close()
        return 2

    # -- resolve the WorkThread -------------------------------------------
    if getattr(args, "thread", None):
        t = store.thread_get(args.thread)
        if not t:
            print("thread not found: " + args.thread, file=sys.stderr)
            store.close()
            return 1
    else:
        repo_ref = getattr(args, "repo", None)
        from .adapters.base import git_info
        repo = repo_ref or (git_info(os.getcwd()).get("repo_root")
                            or os.getcwd().replace("\\", "/"))
        cands = [x for x in store.thread_list("active")
                 if x["repo_root"] and same_repo_loose(x["repo_root"], repo)]
        if not cands:
            print("error: no active WorkThread for this repo ({0}).".format(repo))
            print("Create one: voyager thread create --repo {0} --attach <ids>".format(repo))
            print("Or continue without switching: voyager continue")
            store.close()
            return 1
        # P9: this branch used to fall through to `thread=t` with `t` unbound
        # (the comprehension's `t` never escapes in py3), so `voyager switch
        # <agent>` without --thread raised UnboundLocalError.  Pick the newest
        # active thread for the repo, loudly, exactly like `continue` does.
        t = max(cands, key=lambda x: x["updated_at"] or 0)
        print("active thread: {0}  ({1})".format(t["id"], (t["title"] or "")[:70]))

    return _handoff_via_engine(store, args, "switch", thread=t, target=target)



def _lease_age(lease) -> float:
    import time as _time
    return _time.time() - (lease["heartbeat_at"] or 0)


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
        print(f"[{fmt_ts(r['updated_at'])}] {r['provider']:<7} {r['native_id'][:16]}")
        print(f"    {line}")
        print(f"    repo: {r['repo_root'] or r['cwd'] or '?'}"
              + (f"  branch:{r['git_branch']}" if r["git_branch"] else "")
              + f"  ({r['message_count']} msgs / {r['tool_count']} tools)\n")
    return 0


def cmd_handoff(args) -> int:
    """Export one session as a context package for another agent.

    A thin adapter over continuity.handoff_thread() with `style="package"`.
    When the session already belongs to a WorkThread the handoff now takes the
    single-writer lease and records the pending attach, so the target agent's
    new session is adopted by the next scan.  A WorkThread is never created
    implicitly — `voyager merge` is how you get one.
    """
    store = Store(args.db)
    _ensure_fresh(args, store,
                  providers=_scan_scope_for_sessions(store, [args.session]))
    row = _resolve(store, args.session)
    return _handoff_via_engine(store, args, "handoff",
                               source=row, target=getattr(args, "to", None))


def cmd_merge(args) -> int:
    """Synthesize multiple sessions into one Continuation Bundle + WorkThread."""
    store = Store(args.db)
    _ensure_fresh(args, store,
                  providers=_scan_scope_for_sessions(store, args.sessions))
    session_refs = args.sessions
    if not session_refs:
        print("error: at least one session id required", file=sys.stderr)
        store.close()
        return 2
    rows = [_resolve(store, ref) for ref in session_refs]

    # The WorkThread comes first, so the engine can lease it and record the
    # pending attach: a merged handoff is then exactly as safe as a `switch`.
    tid = _thread_from_merge(store, rows, args)
    return _handoff_via_engine(store, args, "merge", thread=tid, sessions=rows,
                               target=getattr(args, "to", None))


def _thread_from_merge(store: Store, rows: list, args) -> str:
    """Phase 2: a merge over N sessions yields (or updates) a WorkThread
    whose member set is exactly those N sessions.  Returns the thread id."""
    sids = {r["id"] for r in rows}
    tid = store.thread_find_by_members(sids)
    repo_root = next((r["repo_root"] or r["cwd"] for r in rows
                      if r["repo_root"] or r["cwd"]), None)
    label = " + ".join("{0}:{1}".format(r["provider"], (r["native_id"] or "")[:10])
                       for r in rows)
    title = getattr(args, "thread_title", None) or getattr(args, "goal", None)         or "merge: " + label
    if tid:
        store.thread_touch(tid)
        action = "updated"
    else:
        tid = store.thread_create(repo_root=repo_root, title=title,
                                  goal=getattr(args, "goal", None))
        action = "created"
    for sid in sids:
        store.thread_attach(tid, sid)
    if not getattr(args, "json", False):
        print("Thread {0} {1}".format(tid, action))
        print("repo: " + (repo_root or "?"))
        print("members:")
        for r in rows:
            print("  {0} {1}".format(r["provider"], r["native_id"]))
    return tid


def _print_lease(store: Store, tid: str) -> None:
    """One human-readable line about a thread's writer lease (D13)."""
    import time as _time
    lease = store.thread_lease_get(tid)
    st = lease_state(lease)
    if not st["held"]:
        print("lease: free")
        return
    holder = "{0} pid={1}".format(lease["holder"], lease["pid"])
    if st["expired"]:
        print("lease: EXPIRED — {0} ({1})".format(holder, st["why"]))
        print("       clear it with: voyager thread unlock " + tid)
    else:
        print("lease: held by {0}, heartbeat {1:.0f}s ago".format(
            holder, _time.time() - (lease["heartbeat_at"] or 0)))


def cmd_thread(args) -> int:
    store = Store(args.db)
    action = args.thread_cmd
    if action == "list":
        status = getattr(args, "status", "active")
        rows = store.thread_list(status)
        if not rows:
            print("no {0} threads".format(status))
            return 0
        for t in rows:
            print("{0}  [{1}]  members:{2}  repo: {3}".format(
                t["id"], t["status"], t["members"], t["repo_root"] or "?"))
            print("    " + (t["title"] or "")[:100])
        return 0
    if action == "timeline":
        # O3: one canonical timeline (voyager/timeline.py).  This command is a
        # renderer, not a second aggregation.
        from .timeline import build_thread_timeline, render_text

        kinds = [k.strip() for k in (getattr(args, "kind", None) or "").split(",")
                 if k.strip()]
        if getattr(args, "retained_only", False):
            state = "retained"
        elif getattr(args, "live_only", False):
            state = "live"
        else:
            state = None
        tl = build_thread_timeline(
            store, args.thread, limit=getattr(args, "limit", None),
            kinds=kinds or None, provider=getattr(args, "provider", None),
            state=state)
        if getattr(args, "json", False):
            # the stable contract the UIs consume; they never parse the prose
            print(json.dumps(tl, indent=2, ensure_ascii=False, default=str))
            return 0 if "error" not in tl else 1
        if "error" in tl:
            print(tl["error"], file=sys.stderr)
            return 1
        print(render_text(tl, show_ids=True))
        return 0
    if action in ("activity", "summarize"):
        # One WorkThread, every agent that touched it.  A brief is a derivation
        # over the canonical thread -- never a concatenation of transcripts.
        from .thread_brief import activity, render, summarize

        t = store.thread_get(args.thread)
        if not t:
            print("thread not found: " + args.thread, file=sys.stderr)
            return 1
        tid = t["id"]
        if action == "activity":
            data = activity(store, tid, limit=getattr(args, "limit", 20))
            if getattr(args, "json", False):
                print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
                return 0 if "error" not in data else 1
            print("WorkThread %s  %s" % (tid, data.get("title") or ""))
            print("providers: %s   members: %s"
                  % (", ".join(data.get("providers") or []) or "-",
                     data.get("member_count")))
            for c in data["contributions"]:
                print("  %-9s %-8s events=%-6d human=%-4d last=%s"
                      % (c["provider"], c["band"], c["events"], c["human_turns"],
                         int(c["last_ts"]) if c["last_ts"] else "-"))
                if c.get("last_line"):
                    print("      " + c["last_line"])
            return 0

        brief = summarize(store, tid, turns=getattr(args, "turns", 3))
        if getattr(args, "json", False):
            print(json.dumps(brief.to_dict(), indent=2, ensure_ascii=False,
                             default=str))
            return 0
        print(render(brief))
        return 0
    if action == "show":
        t = store.thread_get(args.thread)
        if not t:
            print("thread not found: " + args.thread, file=sys.stderr)
            return 1
        args.thread = t["id"]          # resolve prefix to the real id
        members = store.thread_members(args.thread)
        print("Thread {0}  [{1}]".format(t["id"], t["status"]))
        print("repo: " + (t["repo_root"] or "?"))
        print("title: " + (t["title"] or "?"))
        if t["goal"]:
            print("goal: " + t["goal"])
        _print_lease(store, t["id"])
        pending = store.thread_pending_list(t["id"])
        for pend in pending:
            print("pending: {0} continuation launched, new session id unknown"
                  " ({1})".format(pend["provider"], pend["note"] or ""))
        print("members:")
        for m in members:
            print("  {0:<8} {1}  ({2} msgs)  {3}".format(
                m["provider"], m["native_id"], m["message_count"],
                (m["title"] or "")[:60]))
        return 0
    if action == "create":
        tid = store.thread_create(repo_root=getattr(args, "repo", None),
                                  title=getattr(args, "title", None),
                                  goal=getattr(args, "goal", None))
        for ref in (getattr(args, "attach", None) or []):
            for part in ref.split(","):
                part = part.strip()
                if not part:
                    continue
                row, amb = store.session(part)
                if row is None:
                    print("  ! cannot attach '{0}': not found".format(part),
                          file=sys.stderr)
                    continue
                store.thread_attach(tid, row["id"])
        print("Thread {0} created".format(tid))
        return 0
    if action == "attach":
        t = store.thread_get(args.thread)
        if not t:
            print("thread not found: " + args.thread, file=sys.stderr)
            return 1
        args.thread = t["id"]          # resolve prefix to the real id
        attached = skipped = 0
        for ref in (args.sessions or []):
            for part in ref.split(","):
                part = part.strip()
                if not part:
                    continue
                row, amb = store.session(part)
                if row is None:
                    print("  ! skip '{0}': not found".format(part), file=sys.stderr)
                    skipped += 1
                    continue
                if store.thread_attach(args.thread, row["id"]):
                    attached += 1
                    # a newly attached session resolves a pending continuation
                    store.thread_pending_clear(args.thread, row["provider"])
                else:
                    skipped += 1
        print("attached {0} session(s)".format(attached)
              + (", {0} skipped/duplicate".format(skipped) if skipped else ""))
        return 0
    if action in ("reopen", "archive"):
        t = store.thread_get(args.thread)
        if not t:
            print("thread not found: " + args.thread, file=sys.stderr)
            return 1
        tid = t["id"]
        if action == "archive":
            store.thread_set_status(tid, "archived")
            print("thread {0} archived (sessions untouched)".format(tid))
            return 0
        # Reopening is an explicit act, and it can create the ambiguity the core
        # refuses to resolve on its own: two active WorkThreads for one repo.
        # Timestamps must never settle that, so say it out loud instead.
        repo = t["repo_root"]
        peers = []
        if repo:
            peers = [r for r in store.thread_list("active")
                     if r["id"] != tid and r["repo_root"] == repo]
        store.thread_set_status(tid, "active")
        print("thread {0} reopened".format(tid))
        if peers:
            print("WARNING: {0} other active WorkThread(s) for {1}: {2}".format(
                len(peers), repo, ", ".join(r["id"] for r in peers)))
            print("         automatic continuation will now report AMBIGUOUS and")
            print("         refuse to choose; close one, or pick explicitly with")
            print("         `voyager continue --thread <id>`.")
        return 0
    if action == "stale":
        import time as _time
        days = float(getattr(args, "days", 14) or 14)
        cutoff = _time.time() - days * 86400
        rows = []
        for r in store.thread_list("active"):
            last = r["updated_at"] or r["created_at"] or 0
            if last < cutoff:
                rows.append({"id": r["id"], "title": r["title"],
                             "repo_root": r["repo_root"],
                             "last_activity": last,
                             "age_days": round((_time.time() - last) / 86400, 1),
                             "members": r["members"]})
        if getattr(args, "json", False):
            print(json.dumps({"days": days, "stale": rows}, indent=2,
                             ensure_ascii=False, default=str))
            return 0
        if not rows:
            print("no active thread is stale (> {0:g} days idle)".format(days))
            return 0
        for r in rows:
            print("{0}  idle {1:g}d  members:{2}  {3}".format(
                r["id"], r["age_days"], r["members"], r["title"] or ""))
            print("    repo: {0}".format(r["repo_root"] or "?"))
        print("")
        print("A stale thread is still active: close or archive it explicitly.")
        return 0
    if action == "close":
        t = store.thread_get(args.thread)
        if not t:
            print("thread not found: " + args.thread, file=sys.stderr)
            return 1
        store.thread_set_status(t["id"], "closed")
        print("thread {0} closed (sessions untouched)".format(args.thread))
        return 0
    if action == "unlock":
        import time as _time
        t = store.thread_get(args.thread)
        if not t:
            print("thread not found: " + args.thread, file=sys.stderr)
            return 1
        lease = store.thread_lease_get(t["id"])
        if lease is None:
            print("thread {0} holds no lease".format(t["id"]))
            return 0
        st = lease_state(lease)
        holder = "{0} pid={1}".format(lease["holder"], lease["pid"])
        if st["expired"]:
            store.thread_lease_release(t["id"], lease["lease_token"],
                                       reason="expired")
            print("cleared EXPIRED lease on {0} (was {1}, {2})".format(
                t["id"], holder, st["why"]))
            return 0
        if not getattr(args, "steal", False):
            print("thread {0} is leased to {1}, heartbeat {2:.0f}s ago".format(
                t["id"], holder, _time.time() - (lease["heartbeat_at"] or 0)))
            print("refusing to unlock a live lease (D13); "
                  "pass --steal to take it (logged)")
            return 1
        if store.thread_lease_release(t["id"], lease["lease_token"],
                                      reason="steal"):
            print("stole lease on {0} from {1} (logged to {2})".format(
                t["id"], holder, store.db_path.parent / "leases.log"))
        else:
            print("lease vanished while unlocking", file=sys.stderr)
        return 0
    print("unknown thread action: " + action, file=sys.stderr)
    return 1


def cmd_checkpoint(args) -> int:
    """WorkThread checkpoint management commands."""
    from .checkpoint import (
        init_checkpoint_schema, checkpoint_create, checkpoint_list,
        checkpoint_get, checkpoint_update, checkpoint_export, checkpoint_import,
        get_latest_checkpoint, checkpoint_summary,
    )

    store = Store(args.db)
    init_checkpoint_schema(store)

    action = args.checkpoint_cmd

    # Auto-detect thread from cwd if not specified
    thread_id = None
    if hasattr(args, 'thread') and args.thread:
        thread_id = args.thread

    # If no thread specified, try to detect from current continuity state
    if not thread_id:
        from .auto import discover_continuity
        disc = discover_continuity(store, cwd=os.getcwd())
        thread = disc.get("active_thread")
        if thread:
            thread_id = thread["id"]
            print(f"auto-detected thread: {thread_id}")

    if action == "create":
        if not thread_id:
            print("error: need WORKING DIRECTORY or --thread flag", file=sys.stderr)
            return 1
        cid = checkpoint_create(
            store,
            thread_id=thread_id,
            goal=args.goal,
            phase=getattr(args, "phase", "analysis"),
        )
        print("checkpoint created:", cid)
        return 0

    if action == "list":
        checkpoints = checkpoint_list(store, thread_id=thread_id,
                                     phase=getattr(args, "phase", None),
                                     limit=getattr(args, "limit", 10))
        if not checkpoints:
            print("no checkpoints found")
            return 0
        for cid, ckpt in checkpoints[:getattr(args, "limit", 10)]:
            lines = checkpoint_summary(ckpt).split("\n")
            print(f"\n[{cid}]")
            for line in lines[:3]:  # First few lines only
                print(f"  {line}")
        return 0

    if action == "show":
        checkpoint = checkpoint_get(store, args.checkpoint)
        if not checkpoint:
            print("checkpoint not found:", args.checkpoint, file=sys.stderr)
            return 1
        print(checkpoint_summary(checkpoint))
        return 0

    if action == "update":
        if not thread_id:
            print("error: need WORKING DIRECTORY or --checkpoint ID", file=sys.stderr)
            return 1

        # Get latest checkpoint if not specified
        checkpoint_id = getattr(args, "checkpoint", None)
        if not checkpoint_id:
            ckpts = checkpoint_list(store, thread_id=thread_id, limit=1)
            if not ckpts:
                print("no checkpoints to update", file=sys.stderr)
                return 1
            checkpoint_id = ckpts[0][0]

        cid = checkpoint_update(
            store,
            checkpoint_id,
            add_blocker=getattr(args, "add_blocker", None),
            severity=getattr(args, "severity", "medium"),
            add_decision={
                "topic": getattr(args, "add_decision_topic", ""),
                "rationale": getattr(args, "decision_rationale", ""),
            } if getattr(args, "add_decision_topic", None) else None,
            add_next_action=getattr(args, "add_next_action", None),
            record_git_state=getattr(args, "record_git", False),
        )
        print("checkpoint updated:", cid)
        return 0

    if action == "export":
        success = checkpoint_export(store, args.checkpoint, Path(args.output))
        if not success:
            print("failed to export checkpoint:", args.checkpoint, file=sys.stderr)
            return 1
        print("exported:", args.output)
        return 0

    if action == "restore":
        # For now, just show what would be restored
        checkpoint = checkpoint_get(store, args.checkpoint)
        if not checkpoint:
            print("checkpoint not found:", args.checkpoint, file=sys.stderr)
            return 1
        print(f"would restore from checkpoint: {args.checkpoint}")
        print(checkpoint_summary(checkpoint))
        if not getattr(args, "merge", False):
            print("\nnote: restore replaces current WorkThread goal/phase with checkpoint state")
        else:
            print("\nnote: restore merges checkpoint data with current state")
        return 0

    print("unknown checkpoint action:", action, file=sys.stderr)
    return 1


# NOTE (P9): `_merge_and_handoff` and `_handoff_from_row` used to live here.
# Both were a second, weaker implementation of the handoff pipeline — no
# lease, no pending attach, no D7 native-resume priority, and their own
# budget/launch/error handling — so `continue --thread T --to X` behaved
# differently from `switch T --to X` on the same thread.  They are gone:
# continuity.handoff_thread() is now the single engine, and `_render_handoff`
# is the only place that turns its result into words.



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

    # provenance: structural origin of each event (who really produced it).
    # Read-only by default; `enrich` only ever fills NULLs and only on --apply.
    sp = sub.add_parser("provenance",
                        help="inspect or fill per-event provenance (origin)")
    sp.add_argument("action", nargs="?", default="coverage",
                    choices=["coverage", "enrich"])
    sp.add_argument("--apply", action="store_true",
                    help="write the enrichment (default is a dry run)")
    sp.add_argument("--provider", default=None,
                    help="comma-separated provider filter")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_provenance)

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
    sp.add_argument("--provider", help="comma list: codex,claude,grok,...")
    sp.add_argument("--repo", help="substring of the session's repo/cwd")
    sp.add_argument("--since", help="2026-09-30, 2026-09-30T12:00, or 7d")
    sp.add_argument("--until", help="same forms as --since")
    sp.add_argument("--kind", help="comma list: user,assistant,tool_call,...")
    sp.add_argument("--tool", help="substring of the tool name")
    sp.add_argument("--file", help="substring of a touched file path")
    sp.add_argument("--origin", help="comma list: human,provider_bootstrap,...")
    sp.add_argument("--human-only", dest="human_only", action="store_true",
                    help="only turns a human actually wrote")
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
    sp.add_argument("--goal", help="rank the evidence against this goal")
    sp.add_argument("--output", "-o", help="package file path (default ~/.voyager/bundles/...)")
    sp.add_argument("--budget", help="context budget: compact|balanced|full|auto|Nk|<int>")
    sp.add_argument("--launch", action="store_true", help="launch the target agent with the package")
    sp.set_defaults(func=cmd_handoff)

    sp = sub.add_parser("merge", parents=[common],
                        help="synthesize multiple sessions into a continuation bundle")
    sp.add_argument("sessions", nargs="+", help="session ids/prefixes to merge")
    sp.add_argument("--thread-title")
    sp.add_argument("--goal", help="explicit primary goal for the next agent")
    sp.add_argument("--to", help="target agent (claude, codex, grok)")
    sp.add_argument("--output", "-o", help="bundle file path (default ~/.voyager/bundles/...)")
    sp.add_argument("--launch", action="store_true", help="launch the target agent with the bundle")
    sp.add_argument("--budget", help="context budget: compact|balanced|full|auto|Nk|<int>")
    sp.add_argument("--json", action="store_true",
                    help="machine-readable result, including the compiled context")
    sp.set_defaults(func=cmd_merge)

    sp = sub.add_parser("watch", help="keep the index in sync automatically",
                        parents=[common])
    sp.add_argument("--interval", type=int, default=300, help="seconds between scans (default 300)")
    sp.add_argument("--platform", help="limit to these providers (comma list)")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("switch", parents=[common],
                        help="switch the active WorkThread to another agent (Phase 6)")
    sp.add_argument("agent", help="target agent (claude|codex|grok)")
    sp.add_argument("--thread", help="explicit WorkThread id/prefix")
    sp.add_argument("--repo", help="resolve the thread by repo")
    sp.add_argument("--goal", help="primary goal for the continuation bundle")
    sp.add_argument("--budget", help="context budget: compact|balanced|full|auto|Nk|<int>")
    sp.add_argument("--bundle", action="store_true",
                    help="force a continuation bundle even for same-provider members")
    sp.add_argument("--steal", action="store_true",
                    help="take over a LIVE lease held by another provider (logged)")
    sp.add_argument("--mode", choices=["bundle", "transcript"], default="bundle",
                    help="continuation mode: bundle (default) or opt-in "
                         "transcript transplant (codex/grok only, writes a NEW "
                         "native session id under the lease)")
    sp.add_argument("--no-launch", action="store_true",
                    help="print what would be launched instead of launching")
    sp.add_argument("--output", "-o",
                    help="bundle file path (when compiling a continuation)")
    sp.set_defaults(func=cmd_switch)

    sp = sub.add_parser("thread", help="WorkThread: list/show/create/attach/close/unlock")
    tsub = sp.add_subparsers(dest="thread_cmd", required=True)
    tsp = tsub.add_parser("list", parents=[common], help="list active threads")
    tsp.add_argument("--status", default="active")
    tsp.set_defaults(func=cmd_thread)
    tsp = tsub.add_parser("activity", parents=[common],
                          help="per-agent contribution to a WorkThread")
    tsp.add_argument("thread")
    tsp.add_argument("--limit", type=int, default=20)
    tsp.add_argument("--json", action="store_true")
    tsp.set_defaults(func=cmd_thread)
    tsp = tsub.add_parser("summarize", parents=[common],
                          help="one continuous-task brief across every agent")
    tsp.add_argument("thread")
    tsp.add_argument("--turns", type=int, default=3,
                     help="recent turns to quote per agent")
    tsp.add_argument("--json", action="store_true")
    tsp.set_defaults(func=cmd_thread)
    tsp = tsub.add_parser("timeline", parents=[common],
                          help="lifecycle/milestone timeline for a WorkThread")
    tsp.add_argument("thread")
    tsp.add_argument("--limit", type=int, default=None,
                     help="keep the newest N events (first screen, not a dump)")
    tsp.add_argument("--kind", help="comma list: HANDOFF,CHECKPOINT_CREATED,...")
    tsp.add_argument("--provider", help="only events for this provider")
    g = tsp.add_mutually_exclusive_group()
    g.add_argument("--live-only", dest="live_only", action="store_true",
                   help="only events from sessions that are still live")
    g.add_argument("--retained-only", dest="retained_only", action="store_true",
                   help="only events from retained (source-missing) history")
    tsp.add_argument("--json", action="store_true",
                     help="the stable contract the UIs consume")
    tsp.set_defaults(func=cmd_thread)
    tsp = tsub.add_parser("show", parents=[common], help="show one thread and its members")
    tsp.add_argument("thread")
    tsp.set_defaults(func=cmd_thread)
    tsp = tsub.add_parser("create", parents=[common], help="create an empty thread")
    tsp.add_argument("--repo")
    tsp.add_argument("--title")
    tsp.add_argument("--goal")
    tsp.add_argument("--attach", action="append",
                     help="session id/prefix to attach (repeatable, comma-ok)")
    tsp.set_defaults(func=cmd_thread)
    tsp = tsub.add_parser("attach", parents=[common], help="attach sessions to a thread")
    tsp.add_argument("thread")
    tsp.add_argument("sessions", nargs="+",
                     help="session id/prefix (repeatable, comma-ok)")
    tsp.set_defaults(func=cmd_thread)

    # voyager thread checkpoint <subcommand>
    tch = tsub.add_parser("checkpoint", help="WorkThread checkpoint management")
    tcsub = tch.add_subparsers(dest="checkpoint_cmd", required=True)

    tsp = tcsub.add_parser("create", parents=[common],
                           help="create a new checkpoint for this WorkThread")
    tsp.add_argument("--goal", required=True, help="primary objective")
    tsp.add_argument("--phase", choices=["analysis", "design", "implementation",
                                        "testing", "review", "complete"],
                     default="analysis", help="development phase")
    tsp.add_argument("--from-session", dest="from_session",
                     help="copy milestones/blockers from this session ID prefix")
    tsp.set_defaults(func=cmd_checkpoint)

    tsp = tcsub.add_parser("list", parents=[common],
                           help="list checkpoints for a WorkThread")
    tsp.add_argument("thread", nargs="?", help="thread ID (default: auto-detect)")
    tsp.add_argument("--status", dest="phase",
                     choices=["analysis", "design", "implementation",
                             "testing", "review", "complete"],
                     help="filter by phase")
    tsp.add_argument("--limit", type=int, default=10)
    tsp.set_defaults(func=cmd_checkpoint)

    tsp = tcsub.add_parser("show", parents=[common],
                           help="show detailed checkpoint summary")
    tsp.add_argument("checkpoint", help="checkpoint ID or prefix")
    tsp.set_defaults(func=cmd_checkpoint)

    tsp = tcsub.add_parser("update", parents=[common],
                           help="update last checkpoint with new information")
    tsp.add_argument("--checkpoint", help="checkpoint ID (default: latest)")
    tsp.add_argument("--add-blocker", dest="add_blocker",
                     help="add a blocker description")
    tsp.add_argument("--severity", choices=["high", "medium", "low"],
                     default="medium", help="blocker severity")
    tsp.add_argument("--decision", dest="add_decision_topic",
                     help="record a decision topic")
    tsp.add_argument("--decision-rationale", dest="decision_rationale",
                     help="rationale for the decision")
    tsp.add_argument("--next-action", dest="add_next_action",
                     help="add immediate next action")
    tsp.add_argument("--record-git", action="store_true",
                     help="record current git state (branch/HEAD)")
    tsp.set_defaults(func=cmd_checkpoint)

    tsp = tcsub.add_parser("export", parents=[common],
                           help="export checkpoint to JSON file")
    tsp.add_argument("checkpoint", help="checkpoint ID")
    tsp.add_argument("--output", "-o", required=True, help="output file path")
    tsp.set_defaults(func=cmd_checkpoint)

    tsp = tcsub.add_parser("restore", parents=[common],
                           help="restore checkpoint to current WorkThread state")
    tsp.add_argument("checkpoint", help="checkpoint ID")
    tsp.add_argument("--merge", action="store_true",
                     help="merge with current state rather than replace")
    tsp.set_defaults(func=cmd_checkpoint)

    tsp = tsub.add_parser("reopen", parents=[common],
                          help="reopen a closed thread (warns if it creates ambiguity)")
    tsp.add_argument("thread")
    tsp.set_defaults(func=cmd_thread)
    tsp = tsub.add_parser("archive", parents=[common],
                          help="archive a thread (terminal, sessions untouched)")
    tsp.add_argument("thread")
    tsp.set_defaults(func=cmd_thread)
    tsp = tsub.add_parser("stale", parents=[common],
                          help="active threads with no recent activity")
    tsp.add_argument("--days", type=float, default=14)
    tsp.add_argument("--json", action="store_true")
    tsp.set_defaults(func=cmd_thread)
    tsp = tsub.add_parser("close", parents=[common], help="mark a thread closed (sessions untouched)")
    tsp.add_argument("thread")
    tsp.set_defaults(func=cmd_thread)
    tsp = tsub.add_parser("unlock", parents=[common],
                          help="release a thread's writer lease (--steal for a live one)")
    tsp.add_argument("thread")
    tsp.add_argument("--steal", action="store_true",
                     help="force-release a LIVE lease (expired ones clear freely; "
                          "steals are logged)")
    tsp.set_defaults(func=cmd_thread)

    sp = sub.add_parser("continue", parents=[common],
                        help="pick work back up in one command (native resume, or auto-handoff)")
    sp.add_argument("session", nargs="?", help="session id/prefix (default: newest session)")
    sp.add_argument("--from", dest="from_sessions",
                    help="comma-separated session ids to synthesize and continue from")
    sp.add_argument("--goal", help="explicit primary goal for the continuation bundle")
    sp.add_argument("--repo", help="pick the newest session of this repo")
    sp.add_argument("--platform", help="pick the newest session of this provider")
    sp.add_argument("--thread", help="continue from a WorkThread's members")
    sp.add_argument("--to", help="force cross-agent handoff to this target")
    sp.add_argument("--bundle", action="store_true",
                    help="force a continuation bundle even for same-provider members")
    sp.add_argument("--output", "-o", help="bundle file path (when continuing via handoff/merge)")
    sp.add_argument("--budget", help="context budget: compact|balanced|full|auto|Nk|<int>")
    sp.add_argument("--launch", action="store_true", help="launch immediately (default: print)")
    sp.add_argument("--no-launch", action="store_true",
                    help="print what would be launched instead of launching")
    sp.add_argument("--json", action="store_true",
                    help="machine-readable result, including the compiled context")
    sp.set_defaults(func=cmd_continue)

    sp = sub.add_parser("brief", parents=[common],
                        help="compact digest of recent agent activity (agent-friendly)")
    sp.add_argument("--hours", type=float, default=48, help="look-back window (default 48h)")
    sp.add_argument("--repo", help="filter by repo/cwd substring")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(func=cmd_brief)

    # New integrate command group replacing basic skill install
    sp = sub.add_parser("integrate", help="install Voyager integration for a provider",
                        parents=[common])
    isp = sp.add_subparsers(dest="int_cmd", required=True)

    # voyager integrate install <provider>
    iisp = isp.add_parser("install", help="install full integration for provider")
    iisp.add_argument("provider", choices=["codex", "claude", "grok", "dsh"],
                      help="target provider to integrate")
    iisp.add_argument("--force", action="store_true",
                      help="overwrite modified files and re-generate bootstraps")
    iisp.add_argument("--home", help="override HOME for paths (testing)")
    iisp.add_argument("--json", action="store_true",
                      help="output results as JSON")
    iisp.set_defaults(func=cmd_integrate)

    # voyager integrate status
    istp = isp.add_parser("status", help="check integration status for providers")
    istp.add_argument("providers", nargs="*", default=None,
                      help="providers to check; omit for all")
    istp.add_argument("--home", help="override HOME for paths (testing)")
    istp.add_argument("--json", action="store_true")
    istp.add_argument("--deep", action="store_true",
                      help="also report each capability dimension with its evidence")
    istp.set_defaults(func=cmd_integrate_status)

    # voyager dashboard [--out PATH] [--json]
    sp = sub.add_parser("dashboard",
                        help="render a local dashboard (one self-contained HTML file)")
    sp.add_argument("--out", help="output path (default: ~/.voyager/dashboard.html)")
    sp.add_argument("--repo", help="focus this repository")
    sp.add_argument("--json", action="store_true", help="emit the data, not the page")
    sp.set_defaults(func=cmd_dashboard)

    # voyager doctor [--json] [--fix] [--dry-run]
    sp = sub.add_parser("doctor", help="is this installation healthy?")
    sp.add_argument("--json", action="store_true", help="machine-readable report")
    sp.add_argument("--fix", action="store_true",
                    help="run only SAFE_DERIVED_REPAIR (stale cache, FTS); "
                         "never touches leases, ambiguity, or retained history")
    sp.add_argument("--dry-run", action="store_true",
                    help="with --fix: show what would be done, do nothing")
    sp.add_argument("--repo", help="limit continuity checks to this repository")
    sp.set_defaults(func=cmd_doctor)

    # voyager demo — synthetic five-minute first run (no real agents needed)
    sp = sub.add_parser(
        "demo", help="seed a synthetic demo index and show what to try next")
    def _cmd_demo(args) -> int:
        from .demo import main as demo_main
        return demo_main()
    sp.set_defaults(func=_cmd_demo)

    # voyager verify [provider] [--all] [--verbose] [--json] [--matrix]
    sp = sub.add_parser("verify", help="view automatic verification status for providers")
    sp.add_argument("provider", nargs="?", default=None,
                    choices=["codex", "claude", "grok", "zcode", "cursor",
                            "kiro", "antigravity", "dsh"],
                    help="provider to check; omit or use --all for all providers")
    sp.add_argument("--all", action="store_true",
                    help="show all providers (default when no provider given)")
    sp.add_argument("--verbose", "-v", action="store_true",
                    help="show evidence details (chains, events, last_live_event)")
    sp.add_argument("--json", action="store_true",
                    help="machine-readable JSON output")
    sp.add_argument("--matrix", action="store_true",
                    help="show full provider-by-dimension matrix")
    sp.set_defaults(func=lambda args: _cmd_verify(args))

    # voyager integration-info  (machine-readable version/capability probe)
    sp = sub.add_parser("integration-info",
                        help="machine-readable version/capability probe for integrations")
    sp.add_argument("--json", action="store_true",
                    help="accepted for symmetry; the output is always JSON")
    sp.set_defaults(func=cmd_integration_info)

    # voyager integrate remove <provider>
    irmp = isp.add_parser("remove", help="remove integration for provider")
    irmp.add_argument("provider", choices=["codex", "claude", "grok", "dsh"],
                      help="provider to remove integration for")
    irmp.add_argument("--home", help="override HOME for paths (testing)")
    irmp.add_argument("--json", action="store_true",
                      help="output results as JSON")
    irmp.set_defaults(func=cmd_integrate_remove)

    # voyager hook startup - provider lifecycle hook handler
    sp = sub.add_parser("hook", help="Voyager lifecycle hooks for native provider integration")
    hksub = sp.add_subparsers(dest="hook_cmd", required=True)

    hks = hksub.add_parser("startup", help="handle startup continuity for a provider session")
    hks.add_argument("--provider", required=True,
                     help="target provider (claude|codex|grok|...)")
    hks.add_argument("--cwd", required=True, help="current working directory")
    hks.add_argument("--session-id", help="native session ID (if available at startup)")
    hks.add_argument("--goal", help="primary goal for context ranking")
    hks.add_argument("--compact", action="store_true", help="use compact budget")
    hks.set_defaults(func=cmd_hook_startup)

    # Grok native surfaces (verified against Grok CLI 1.0.41). Kept as separate
    # subcommands rather than folded into `hook startup`, whose status/context
    # contract other callers depend on.
    hkgc = hksub.add_parser(
        "grok-context",
        help="write Grok's continuation rule file (run by the launcher)")
    hkgc.add_argument("--cwd", required=True, help="current working directory")
    hkgc.add_argument("--home", help="override HOME (testing)")
    hkgc.add_argument("--db", help="index db path")
    hkgc.add_argument("--json", action="store_true")
    hkgc.add_argument("--quiet", action="store_true")
    hkgc.set_defaults(func=cmd_hook_grok_context)

    hkgs = hksub.add_parser(
        "grok-session-start",
        help="Grok SessionStart hook body (records the pending attach)")
    hkgs.add_argument("--session-id",
                      help="native session id (defaults to $GROK_SESSION_ID)")
    hkgs.add_argument("--cwd",
                      help="working dir (defaults to $GROK_WORKSPACE_ROOT)")
    hkgs.add_argument("--home", help="override HOME (testing)")
    hkgs.add_argument("--db", help="index db path")
    hkgs.add_argument("--json", action="store_true")
    hkgs.set_defaults(func=cmd_hook_grok_session_start)

    # Legacy skill install still supported
    sp = sub.add_parser("skill", help="(legacy) install the voyager skill into known agents",
                        parents=[common], aliases=["skills"])
    lsp = sp.add_subparsers(dest="skill_cmd", required=True)
    lisp = lsp.add_parser("install", help="copy SKILL.md into agent skill dirs")
    lisp.add_argument("--agent", help="single agent (codex|claude|grok); "
                                      "unknown agents get a manual path")
    lisp.add_argument("--force", action="store_true",
                     help="overwrite a user-modified SKILL.md (backs it up first)")
    lisp.add_argument("--home", help="override HOME for skill roots (testing)")
    lisp.set_defaults(func=cmd_skill)

    sp = sub.add_parser("api", help="local stdio JSON-lines API (VS Code client)",
                        parents=[common])
    sp.set_defaults(func=cmd_api)

    sp = sub.add_parser("status", parents=[common],
                        help="automatic-continuity state for the current repo")
    sp.add_argument("--repo", help="resolve by repo instead of cwd")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    # voyager launcher prelaunch
    sp = sub.add_parser("launcher", help="Voyager launcher hook utilities")
    lsub = sp.add_subparsers(dest="launcher_cmd", required=True)

    lsp = lsub.add_parser("prelaunch", help="run prelaunch hook for wrapper scripts")
    lsp.add_argument("--provider", required=True, help="target provider")
    lsp.add_argument("--cwd", required=True, help="current working directory")
    lsp.add_argument("--db", help="index db path")
    lsp.add_argument("--json", action="store_true")
    lsp.set_defaults(func=lambda args: _run_launcher_prelaunch(args))

    sp = sub.add_parser("stats", help="index statistics", parents=[common])
    sp.set_defaults(func=cmd_stats)

    # voyager db <subcommand>
    dbsub = sub.add_parser("db", help="database maintenance: check/backup/repair")
    dbsub = dbsub.add_subparsers(dest="db_cmd", required=True)

    # voyager db check
    dbcheck = dbsub.add_parser("check", help="run integrity checks (read-only)")
    dbcheck.add_argument("--verbose", "-v", action="store_true")
    dbcheck.add_argument("--json", action="store_true")
    dbcheck.set_defaults(func=lambda a: cmd_db_check(db_path=_db_path_arg(a), verbose=a.verbose,
                                                      json_output=a.json))

    # voyager db backup
    dbbackup = dbsub.add_parser("backup", help="consistent snapshot via SQLite's backup API")
    dbbackup.add_argument("--output-dir", help="backup destination directory")
    dbbackup.add_argument("--json", action="store_true")
    dbbackup.set_defaults(func=lambda a: cmd_db_backup(db_path=_db_path_arg(a), output_dir=a.output_dir,
                                                        json_output=a.json))

    # voyager db repair -- plan by default; --apply authorises the safe steps
    dbrepair = dbsub.add_parser("repair", help="plan, and optionally apply, safe repairs")
    dbrepair.add_argument("--apply", action="store_true",
                          help="execute the SAFE_DERIVED_REPAIR steps (no confirmation bypass)")
    dbrepair.add_argument("--json", action="store_true")
    dbrepair.set_defaults(func=lambda a: cmd_db_repair(db_path=_db_path_arg(a), apply=a.apply, json_output=a.json))

    # voyager db compact -- VACUUM, separate on purpose
    dbcompact = dbsub.add_parser("compact", help="VACUUM the database (maintenance, not repair)")
    dbcompact.add_argument("--json", action="store_true")
    dbcompact.set_defaults(func=lambda a: cmd_db_compact(db_path=_db_path_arg(a), json_output=a.json))

    args = p.parse_args(argv)
    _expand_path_args(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
