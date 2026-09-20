"""Voyager CLI — unified local session manager for AI coding agents."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# Import provider config for integrate command output
from .skill import PROVIDER_CONFIG, uninstall_integration, check_integration_status
from .adapters import load_all
from .adapters.base import enabled_adapters, git_info
from .store import Store, default_db_path, lease_state
from .integrations.hook import cmd_hook_startup


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
            # nothing on disk anymore: drop everything this provider had
            gone = store.prune_missing_sessions(ad.provider, set())
            if gone and not quiet:
                print(f"  {ad.provider}: pruned {gone} vanished session(s)")
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
            print(f"  {ad.provider}: pruned {gone} vanished session(s)")

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


def cmd_scan(args) -> int:
    store = Store(args.db)
    run_scan(store,
             providers=args.platform.split(",") if args.platform else None,
             force=args.force, quiet=False)
    return 0


def _ensure_fresh(args, store: Store, providers: Optional[List[str]] = None) -> dict:
    """Phase 1b: incremental scan before compiling/reading sessions.
    Never --force; prints one freshness line so stale bundles are explicable.
    Set VOYAGER_NO_SYNC=1 to skip (tests, offline inspection)."""
    if os.environ.get("VOYAGER_NO_SYNC"):
        print("freshness: skipped (VOYAGER_NO_SYNC)")
        return {"changed_sources": 0, "elapsed": 0.0}
    res = run_scan(store, providers=providers, force=False, quiet=True)
    print(f"freshness: scanned in {res['elapsed']:.1f}s, "
          f"{res['changed_sources']} source(s) changed")
    return res


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


def _render_budgeted(text: str, args, target: Optional[str] = None) -> str:
    """Phase 4: apply --budget to a rendered bundle/package and print the
    token estimate. Shared by handoff / merge / continue pipelines."""
    from .budget import apply_budget, auto_budget, parse_budget
    spec = getattr(args, "budget", None)
    try:
        tokens = parse_budget(spec)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)
    if tokens is None and spec and spec.strip().lower() == "auto":
        tokens = auto_budget(target)
    packed, info = apply_budget(text, tokens, target=target)
    print("estimated tokens: ~{0}".format(info["estimated_tokens"])
          + ("  (budget {0})".format(info["budget"]) if info["budget"] else ""))
    return packed


def cmd_api(args) -> int:
    """Phase 7: local stdio JSON-lines API (the VS Code sidebar's client)."""
    from .api import serve
    serve(Path(args.db) if getattr(args, "db", None) else None)
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
    
    if result.get("warnings"):
        print("  warnings:")
        for w in result["warnings"]:
            print(f"          {w}")
    
    print(f"\nverification: {result.get('verification', 'none')}")
    return 0 if result["status"] not in ("error",) else 1


def cmd_integrate_status(args) -> int:
    """Check integration status for providers."""
    from .skill import check_integration_status
    
    providers = args.providers if args.providers else None
    results = check_integration_status(providers=providers,
                                        home=Path(args.home) if args.home else None)
    
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0
    
    # Print table header (ASCII compatible)
    print(f"{'Provider':<15} {'Skill':<8} {'MCP':<10} {'Startup':<10} {'Auto':<6}")
    print("-" * 70)
    
    for r in results:
        skill_y = "Y" if r.get("skill", {}).get("installed") else "N"
        mcp_reg = "R" if r.get("mcp", {}).get("registered") else ("A" if r.get("mcp", {}).get("available") else "N")
        start_stat = r.get("startup_status", "N")  # Y=AUTO, A=ASSISTED, N=NONE
        auto = "Y" if r.get("auto_attach") else "N"
        
        print(f"{r['installed']:<15} {skill_y:<8} {mcp_reg:<10} {start_stat:<10} {auto:<6}")
    
    # Add notes section
    print("\nLegend:")
    print("  Skill: Y=installed, N=not found")
    print("  MCP:   R=registered, A=available(unsupported), N=no support")
    print("  Start: Y=verified zero-touch, A=startup-assisted, N=no hook")
    print("  Auto:  Y=core supports auto-attach")
    print("\nNote: No provider yet has 'Y' for startup - all are STARTUP_ASSISTED or BEST_EFFORT")
    print("      Real provider dogfood tests pending.")
    return 0


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



def _same_repo(a: str, b: str) -> bool:
    """Repo identity match: exact, path-suffix or substring (mirrors
    _repo_match's user-facing semantics — "black_box" must match
    "E:/models/black_box")."""
    a = (a or "").replace("\\", "/").rstrip("/").lower()
    b = (b or "").replace("\\", "/").rstrip("/").lower()
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)         or a in b or b in a


def _continue_from_thread(store: Store, t, args) -> int:
    """Continue inside a WorkThread: newest natively-resumable member wins
    without --to; otherwise compile the continuation bundle from members."""
    member_rows = store.thread_members(t["id"])
    if not member_rows:
        print("thread " + t["id"] + " has no live member sessions",
              file=sys.stderr)
        return 1
    print("thread {0}  [{1}]  {2} member(s)".format(
        t["id"], t["status"], len(member_rows)))
    if not getattr(args, "to", None):
        for m in sorted(member_rows, key=lambda x: x["updated_at"] or 0,
                        reverse=True):
            if m["can_resume"] and m["resume_cmd"]:
                argv = m["resume_cmd"].split()
                print("$ " + " ".join(argv))
                if not args.launch:
                    print("add --launch to start it now")
                    return 0
                try:
                    return subprocess.call(argv)
                except KeyboardInterrupt:
                    return 130
        args.to = "claude"
        print("no natively-resumable member — compiling continuation bundle "
              "for claude (override with --to)")
    return _merge_and_handoff(store, member_rows, args)


def cmd_continue(args) -> int:
    """One command to pick work back up: native resume when possible,
    automatic cross-agent handoff otherwise."""
    store = Store(args.db)
    # Phase 1b: the index is only as fresh as the last scan
    _ensure_fresh(args, store,
                  providers=[args.platform] if getattr(args, "platform", None) else None)
    if getattr(args, "thread", None):
        t = store.thread_get(args.thread)
        if not t:
            print("thread not found: " + args.thread, file=sys.stderr)
            return 1
        return _continue_from_thread(store, t, args)
    # Phase 2 task-centric default: --repo / cwd -> active WorkThread.
    # Deterministic only: an explicit thread wins, otherwise the most
    # recently updated active thread for this repo is picked LOUDLY.
    # Multi-signal auto-clustering is deliberately NOT implemented
    # (roadmap Deferred) — sessions are never silently swallowed.
    if not args.session and not getattr(args, "from_sessions", None):
        repo_ref = getattr(args, "repo", None)
        if repo_ref:
            repo = repo_ref
        else:
            repo = (git_info(os.getcwd()).get("repo_root")
                    or os.getcwd().replace("\\", "/"))
        cands = [t for t in store.thread_list("active")
                 if t["repo_root"] and _same_repo(t["repo_root"], repo)]
        if cands:
            t = max(cands, key=lambda x: x["updated_at"] or 0)
            print("active thread: {0}  ({1})".format(
                t["id"], (t["title"] or "")[:70]))
            return _continue_from_thread(store, t, args)
    from_sessions = getattr(args, "from_sessions", None)
    if from_sessions:
        refs = [s.strip() for s in from_sessions.split(",") if s.strip()]
        if not refs:
            print("error: --from requires at least one session id", file=sys.stderr)
            return 2
        rows = [_resolve(store, ref) for ref in refs]
        return _merge_and_handoff(store, rows, args)

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
        if args.no_launch or not args.launch:
            print("add --launch to start it now" if not args.no_launch
                  else "(--no-launch: not launching)")
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


def cmd_switch(args) -> int:
    """Phase 6 / #7: one command to switch agent inside the active WorkThread.

    Unified via handoff_thread(): scan → thread resolve → lease → bundle/
    native resume → launch. Returns 0 on success, non-zero on error/abort.
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
        cands = [t for t in store.thread_list("active")
                 if t["repo_root"] and _same_repo(t["repo_root"], repo)]
        if not cands:
            print("error: no active WorkThread for this repo ({0}).".format(repo))
            print("Create one: voyager thread create --repo {0} --attach <ids>".format(repo))
            print("Or continue without switching: voyager continue")
            store.close()
            return 1

    # -- unified orchestration --------------------------------------------
    from .continuity import handoff_thread as _hot
    res = _hot(
        store=store,
        thread=t,
        target=target,
        goal=getattr(args, "goal", None),
        budget=getattr(args, "budget", None),
        launch=False,  # We'll launch manually below so we can handle errors
        mode=getattr(args, "mode", "bundle"),
        steal=getattr(args, "steal", False),
        output=args.output if getattr(args, "output", None) else None,
        no_launch=True,  # Don't launch in handoff_thread
    )
    
    # Save lease info before closing store for manual launch handling
    thread_id = t["id"]
    lease_token = res.get("lease_token")
    store.close()

    # -- present result uniformly -----------------------------------------
    output_lines = []
    if res["action"] == "refused":
        output_lines.append("refused: {0}".format(res["error"]))
        for w in res.get("warnings", []):
            output_lines.append("warning: {0}".format(w))
    elif res.get("warnings"):
        output_lines.extend(["warning: {0}".format(w) for w in res["warnings"]])
    if res["action"] == "native-resume":
        output_lines.append("$ {0}".format(" ".join(res["argv"])))
        if getattr(args, "no_launch", False):
            output_lines.append("native resume ready")
        else:
            try:
                rc = subprocess.call(res["argv"])
                if rc != 0:
                    # Launch failed - release lease
                    if thread_id and lease_token:
                        # Need to re-open store for lease operations
                        from .store import Store as _Store
                        _store = _Store(store.db_path)
                        _store.thread_lease_release(thread_id, lease_token, reason="launch-failed")
                        _store.close()
                    output_lines.append("launch exited with code {0}; lease released".format(rc))
                    print("\n".join(output_lines))
                return rc
            except KeyboardInterrupt:
                if thread_id and lease_token:
                    from .store import Store as _Store
                    _store = _Store(store.db_path)
                    _store.thread_lease_release(thread_id, lease_token, reason="launch-interrupted")
                    _store.close()
                output_lines.append("launch interrupted; lease released")
                print("\n".join(output_lines))
                return 130
            except OSError as e:
                if thread_id and lease_token:
                    from .store import Store as _Store
                    _store = _Store(store.db_path)
                    _store.thread_lease_release(thread_id, lease_token, reason="launch-failed")
                    _store.close()
                output_lines.append("launch failed ({0}); lease released".format(e))
                print("\n".join(output_lines))
                return 1
    elif res["action"] == "transcript":
        output_lines.append("transcript written: session id {0}".format(
            res.get("native_session_id", "unknown")))
        output_lines.append("$ {0}".format(" ".join(res["argv"])))
        if getattr(args, "no_launch", False):
            output_lines.append("transplant ready")
        else:
            try:
                rc = subprocess.call(res["argv"])
                if rc != 0:
                    if thread_id and lease_token:
                        from .store import Store as _Store
                        _store = _Store(store.db_path)
                        _store.thread_lease_release(thread_id, lease_token, reason="launch-failed")
                        _store.close()
                    output_lines.append("launch exited with code {0}; lease released".format(rc))
                return rc
            except KeyboardInterrupt:
                if thread_id and lease_token:
                    from .store import Store as _Store
                    _store = _Store(store.db_path)
                    _store.thread_lease_release(thread_id, lease_token, reason="launch-interrupted")
                    _store.close()
                output_lines.append("launch interrupted; lease released")
                return 130
            except OSError as e:
                if thread_id and lease_token:
                    from .store import Store as _Store
                    _store = _Store(store.db_path)
                    _store.thread_lease_release(thread_id, lease_token, reason="launch-failed")
                    _store.close()
                output_lines.append("launch failed ({0}); lease released".format(e))
                return 1
    elif res["action"] == "bundle":
        output_lines.append("continuation bundle: {0}".format(res["bundle_path"]))
        output_lines.append("$ {0} \"<continuation prompt>\"".format(res["argv"][0]))
        if getattr(args, "no_launch", False):
            output_lines.append("switch ready (--no-launch); the target agent should "
                  "read the bundle, then `voyager thread attach {0} <new-id>` "
                  "resolves the pending attach".format(t["id"]))
        else:
            try:
                rc = subprocess.call(res["argv"])
                if rc == 0:
                    output_lines.append("switch complete: after the target agent starts, run "
                          "`voyager thread attach {0} <new-session-id>` to link the "
                          "continuation".format(t["id"]))
                return rc
            except KeyboardInterrupt:
                output_lines.append("launch interrupted")
                return 130
            except OSError as e:
                output_lines.append("launch failed ({0}); lease released".format(e))
                return 1
    else:
        output_lines.append("unexpected action: {0}".format(res["action"]))
    
    # Print all accumulated output BEFORE returning
    if output_lines:
        print("\n".join(output_lines))
    
    if res["action"] == "refused" or res["action"] not in ("native-resume", "transcript", "bundle"):
        return 1
    return 0


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
        print(f"[{_fmt_ts(r['updated_at'])}] {r['provider']:<7} {r['native_id'][:16]}")
        print(f"    {line}")
        print(f"    repo: {r['repo_root'] or r['cwd'] or '?'}"
              + (f"  branch:{r['git_branch']}" if r["git_branch"] else "")
              + f"  ({r['message_count']} msgs / {r['tool_count']} tools)\n")
    return 0


def cmd_handoff(args) -> int:
    store = Store(args.db)
    _ensure_fresh(args, store)
    row = _resolve(store, args.session)
    return _handoff_from_row(store, row, args)


def cmd_merge(args) -> int:
    """Synthesize multiple sessions into one Continuation Bundle + WorkThread."""
    store = Store(args.db)
    _ensure_fresh(args, store)
    session_refs = args.sessions
    if not session_refs:
        print("error: at least one session id required", file=sys.stderr)
        return 2
    rows = [_resolve(store, ref) for ref in session_refs]

    rc = _merge_and_handoff(store, rows, args)
    _thread_from_merge(store, rows, args)
    return rc


def _thread_from_merge(store: Store, rows: list, args) -> None:
    """Phase 2: a merge over N sessions yields (or updates) a WorkThread
    whose member set is exactly those N sessions."""
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
    print("Thread {0} {1}".format(tid, action))
    print("repo: " + (repo_root or "?"))
    print("members:")
    for r in rows:
        print("  {0} {1}".format(r["provider"], r["native_id"]))


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


def _merge_and_handoff(store: Store, rows: list, args) -> int:
    from .continuity import (
        PROMPT_TARGETS,
        build_continuation_bundle,
        bundle_command,
        default_bundle_name,
        get_bundles_dir,
    )
    out_dir = get_bundles_dir()
    out = Path(args.output) if getattr(args, "output", None) else (out_dir / default_bundle_name(rows))
    out.parent.mkdir(parents=True, exist_ok=True)
    bundle = build_continuation_bundle(store, rows, goal=getattr(args, "goal", None))

    target = getattr(args, "to", None)
    if not target and getattr(args, "cmd", "") == "continue":
        target = "claude"
        print("defaulting continuation target to 'claude' (override with --to)")

    # Phase 4: ranking happened in the compiler; budgeting runs after it
    bundle = _render_budgeted(bundle, args, target=target)
    out.write_text(bundle, encoding="utf-8")
    print(f"continuation bundle: {out.resolve()} ({len(bundle)} chars)")

    if not target:
        if getattr(args, "cmd", "") != "continue":
            print(f"next: pick a target agent, e.g. "
                  f"`voyager merge {' '.join(r['native_id'][:8] for r in rows)} --to claude` "
                  f"(targets with direct launch: {', '.join(sorted(PROMPT_TARGETS))})")
            return 0

    argv = bundle_command(target, out)
    if argv is None:
        print(f"Direct continuation launch is not supported for '{target}'. "
              f"Launchable targets: {', '.join(sorted(PROMPT_TARGETS))}. "
              f"You can still paste {out.resolve()} into that agent manually.")
        return 1
    print(f"$ {argv[0]} \"<continuation prompt>\"")
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


def _handoff_from_row(store: Store, row, args) -> int:
    from .handoff import PROMPT_TARGETS, build_context_package, default_package_name, handoff_command
    out = Path(args.output) if getattr(args, "output", None) else Path(default_package_name(row))
    package = build_context_package(store, row, goal=getattr(args, "goal", None))
    package = _render_budgeted(package, args, target=getattr(args, "to", None))
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
    sp.add_argument("--output", "-o", help="bundle file path (when continuing via handoff/merge)")
    sp.add_argument("--budget", help="context budget: compact|balanced|full|auto|Nk|<int>")
    sp.add_argument("--launch", action="store_true", help="launch immediately (default: print)")
    sp.add_argument("--no-launch", action="store_true",
                    help="print what would be launched instead of launching")
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
                      choices=["codex", "claude", "grok", "dsh"],
                      help="providers to check; omit for all")
    istp.add_argument("--home", help="override HOME for paths (testing)")
    istp.add_argument("--json", action="store_true")
    istp.set_defaults(func=cmd_integrate_status)
    
    # voyager integrate remove <provider>
    irmp = isp.add_parser("remove", help="remove integration for provider")
    irmp.add_argument("provider", choices=["codex", "claude", "grok", "dsh"],
                      help="provider to remove integration for")
    irmp.add_argument("--home", help="override HOME for paths (testing)")
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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
