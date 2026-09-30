"""Database health checks for Voyager index.

Provides integrity validation, backup mechanism, and safe repair operations.
All repairs are conservative - preview only unless --apply is explicitly given.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Severity of a finding.  Defined before the result type that defaults to it:
# a class attribute evaluated at import time cannot reference a later constant.

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"


# --- Health Check Results --------------------------------------------------

@dataclass
class HealthCheckResult:
    passed: bool
    category: str
    message: str
    details: Dict[str, Any] = None
    severity: str = INFO
    suggested_repair: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "passed": self.passed,
            "severity": self.severity,
            "message": self.message,
            "details": self.details or {},
            "suggested_repair": self.suggested_repair,
        }


def run_integrity_check(db_path: Path) -> List[HealthCheckResult]:
    """Read-only diagnosis, with a severity and a suggested repair per finding.

    Every check here is one that can actually fail for a real reason.  A check
    that cannot distinguish a healthy database from a broken one is worse than no
    check: it either cries wolf or lulls.  (The previous version of this function
    ended with a query that referenced columns `pragma_table_info` does not have,
    so *every* run reported a failure -- and the query's result was never used.)
    """
    results: List[HealthCheckResult] = []

    def add(passed, category, message, details=None, severity=None,
            repair=None):
        results.append(HealthCheckResult(
            passed=passed, category=category, message=message,
            details=details or {},
            severity=severity or (INFO if passed else WARNING),
            suggested_repair=repair))

    if not db_path.exists():
        add(False, "database", "database not found: %s" % db_path,
            severity=CRITICAL, repair="run `voyager scan` to build an index")
        return results

    con = _readonly_connect(db_path)
    try:
        # --- structural integrity: never auto-repairable --------------------
        try:
            rows = list(con.execute("PRAGMA integrity_check"))
            verdict = rows[0][0] if rows else "no result"
        except Exception as e:
            verdict = "error: %s" % e
        if verdict == "ok":
            add(True, "integrity", "PRAGMA integrity_check: ok")
        else:
            add(False, "integrity", "PRAGMA integrity_check: %s" % verdict,
                severity=CRITICAL,
                repair="restore from a backup (`voyager db backup`); this is not "
                       "something to repair automatically")

        # --- foreign keys ---------------------------------------------------
        try:
            fk = [dict(r) for r in con.execute("PRAGMA foreign_key_check")]
            add(not fk, "foreign_keys",
                "no foreign key violations" if not fk
                else "%d foreign key violation(s)" % len(fk),
                details={"violations": fk[:20]},
                repair=None if not fk else "inspect the referencing rows by hand")
        except Exception as e:
            add(False, "foreign_keys", "check failed: %s" % e)

        # --- orphan events ---------------------------------------------------
        try:
            n = con.execute("SELECT COUNT(*) FROM events e "
                            "LEFT JOIN sessions s ON e.sid = s.id "
                            "WHERE s.id IS NULL").fetchone()[0]
            add(not n, "orphan_events",
                "no orphaned events" if not n
                else "%d event(s) reference a session that is not indexed" % n,
                details={"orphans": n},
                repair=None if not n else "re-scan the provider source "
                                          "(`voyager scan`); they are otherwise inert")
        except Exception as e:
            add(False, "orphan_events", "check failed: %s" % e)

        # --- orphan thread members -------------------------------------------
        try:
            n = con.execute("SELECT COUNT(*) FROM thread_sessions ts "
                            "LEFT JOIN threads t ON ts.thread_id = t.id "
                            "WHERE t.id IS NULL").fetchone()[0]
            add(not n, "orphan_thread_members",
                "no orphaned thread members" if not n
                else "%d thread membership row(s) point at a missing thread" % n,
                details={"orphans": n},
                repair=None if not n else "delete the dangling membership rows by hand")
        except Exception as e:
            add(True, "orphan_thread_members", "not applicable: %s" % e)

        # --- FTS consistency: derived data, rebuildable -----------------------
        try:
            fts = con.execute("SELECT COUNT(*) FROM event_fts").fetchone()[0]
            ev = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            bad = (fts == 0 and ev > 0) or abs(fts - ev) > max(10, ev // 100)
            add(not bad, "fts_index",
                ("FTS index has %d row(s) against %d event(s)" % (fts, ev))
                if bad else "FTS index consistent (%d rows)" % fts,
                details={"fts_rows": fts, "events": ev},
                repair="rebuild the FTS index from events "
                       "(`voyager db repair --apply`)" if bad else None)
        except Exception as e:
            add(False, "fts_index", "FTS index unreadable: %s" % e,
                repair="rebuild the FTS index from events "
                       "(`voyager db repair --apply`)")

        # --- stats: informational, cannot fail --------------------------------
        try:
            ev = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            se = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            th = con.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            add(True, "stats",
                "index contains %d events, %d sessions, %d threads" % (ev, se, th),
                details={"events": ev, "sessions": se, "threads": th})
        except Exception as e:
            add(False, "stats", "statistics unavailable: %s" % e)
    finally:
        con.close()

    return results


def cmd_db_check(db_path: Optional[Path] = None, verbose: bool = False,
                 json_output: bool = False) -> int:
    """CLI command: voyager db check [--verbose] [--json].

    Read-only.  Protocol output goes to stdout; anything diagnostic goes to
    stderr, so `--json` is safe to pipe.
    """
    from .store import default_db_path
    db = db_path or default_db_path()

    if not db.exists():
        if json_output:
            print(json.dumps({"ok": False, "error": "database not found",
                              "path": str(db)}, ensure_ascii=False))
        else:
            print("database not found: %s" % db, file=sys.stderr)
        return 1

    results = run_integrity_check(db)
    all_passed = all(r.passed for r in results)
    failed = [r for r in results if not r.passed]

    if json_output:
        print(json.dumps({
            "ok": all_passed,
            "path": str(db),
            "checks": [r.to_dict() for r in results],
            "failed": [r.category for r in failed],
        }, ensure_ascii=False, indent=2))
        return 0 if all_passed else 1

    print("Checking database: %s" % db.resolve())
    print("-" * 60)
    for result in results:
        status = "[OK]" if result.passed else "[FAIL]"
        print("%s %s: %s" % (status, result.category, result.message))
        if verbose and result.details:
            print(json.dumps(result.details, indent=2, ensure_ascii=False))
        if result.suggested_repair:
            print("     -> %s" % result.suggested_repair)
    print("-" * 60)
    if all_passed:
        print("Database health: OK")
        return 0
    print("Database health: %d issue(s) detected" % len(failed))
    for f in failed:
        print("  - %s: %s" % (f.category, f.message))
    return 1


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def create_backup(db_path: Path, output_dir: Optional[Path] = None) -> Path:
    """A transactionally consistent snapshot, via SQLite's online backup API.

    Copying `index.db` with shutil is **not** a backup.  The index runs in WAL
    mode, so committed state can still live in `index.db-wal`: a file copy can
    capture a torn or stale database, and it cannot see a writer that is
    mid-transaction.  `Connection.backup()` reads through SQLite itself, so the
    snapshot includes committed-but-un-checkpointed content and excludes anything
    uncommitted.

    The sequence is deliberate:

        snapshot to a temporary destination
        -> verify the snapshot (integrity_check + row counts)
        -> atomically rename into place
        -> write metadata describing the file that now exists

    A failure at any point removes the temporary file, so nothing is ever left
    pretending to be a good backup.
    """
    if not Path(db_path).exists():
        # sqlite3.connect() would happily create an empty database and then
        # "back it up" successfully -- a backup of nothing, named like a backup
        # of something.  Refuse instead.
        raise FileNotFoundError("no database to back up: %s" % db_path)

    now = time.strftime("%Y%m%d_%H%M%S")
    if output_dir is None:
        output_dir = db_path.parent / "backups"
    output_dir.mkdir(parents=True, exist_ok=True)

    # second resolution is not enough: two backups inside one second would share
    # a name, and the atomic rename would silently replace the earlier one.
    stamp = "%s_%06d" % (now, int((time.time() % 1) * 1_000_000))
    backup_path = output_dir / f"index_backup_{stamp}.db"
    # a name that no reader will mistake for a finished backup
    tmp_path = output_dir / f".index_backup_{stamp}.partial"
    meta_path = output_dir / f"index_backup_{stamp}.meta.json"

    src = dst = None
    try:
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(tmp_path))
        src.backup(dst)                      # consistent snapshot, WAL included
        dst.close()
        dst = None
        src.close()
        src = None

        # verify before publishing: a snapshot nobody checked is not a backup
        con = sqlite3.connect(str(tmp_path))
        try:
            verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
            if verdict != "ok":
                raise RuntimeError("snapshot failed integrity_check: %s" % verdict)
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            counts = {}
            for t in tables:
                try:
                    counts[t] = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                except Exception:
                    counts[t] = None
        finally:
            con.close()

        tmp_path.replace(backup_path)        # atomic: same directory, same fs
    except Exception:
        raise
    finally:
        for c in (src, dst):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        # Unconditional: a temporary file must never survive a failure, whichever
        # branch raised, or the next reader cannot tell it from a real backup.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass

    # metadata describes the file that actually exists, after the rename
    meta = {
        "created_at": time.time(),
        "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "original_db": str(db_path),
        "backup_file": backup_path.name,
        "size_bytes": backup_path.stat().st_size,
        "sha256": _sha256(backup_path),
        "integrity_check": "ok",
        "table_counts": counts,
        "method": "sqlite3.Connection.backup",
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return backup_path


def list_backups(output_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List available backups."""
    from .store import default_db_path
    db = default_db_path()

    if output_dir is None:
        output_dir = db.parent / "backups"

    if not output_dir.exists():
        return []

    backups = []
    for meta_file in output_dir.glob("*.meta.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            backups.append({
                "path": str(meta_file),
                **meta,
            })
        except:
            continue

    # Sort by created_at descending
    backups.sort(key=lambda x: x.get("created_at", 0), reverse=True)

    return backups


def cmd_db_backup(db_path: Optional[Path] = None, output_dir: Optional[str] = None,
                  json_output: bool = False) -> int:
    """CLI command: voyager db backup [--output-dir PATH] [--json]."""
    from .store import default_db_path
    db = db_path or default_db_path()

    out_dir = Path(output_dir) if output_dir else None

    if not db.exists():
        if json_output:
            print(json.dumps({"ok": False, "error": "database not found",
                              "path": str(db)}, ensure_ascii=False))
        else:
            print("database not found: %s" % db, file=sys.stderr)
        return 1

    try:
        backup_path = create_backup(db, out_dir)
    except Exception as e:
        if json_output:
            print(json.dumps({"ok": False, "error": str(e), "path": str(db)},
                             ensure_ascii=False))
        else:
            print("backup failed: %s" % e, file=sys.stderr)
        return 1

    meta_path = backup_path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if json_output:
        print(json.dumps({"ok": True, "backup": str(backup_path),
                          "metadata": str(meta_path), "meta": meta},
                         ensure_ascii=False, indent=2))
        return 0

    print("Backing up: %s" % db.resolve())
    print("-" * 60)
    print("Backup created: %s" % backup_path)
    print("Metadata: %s" % meta_path)
    print("Size: %.1f KB" % (backup_path.stat().st_size / 1024))
    print("Integrity: %s   sha256: %s" % (meta.get("integrity_check"),
                                          (meta.get("sha256") or "")[:16]))
    return 0


def delete_backup(backup_path: Path, force: bool = False) -> bool:
    """Delete a backup and its metadata."""
    try:
        meta_path = backup_path.with_suffix(".meta.json")

        if meta_path.exists():
            meta_path.unlink()

        backup_path.unlink()

        if not force:
            print(f"Deleted: {backup_path}")

        return True

    except Exception as e:
        print(f"Failed to delete backup: {e}")
        return False


# --- Repair Operations -----------------------------------------------------

# --- Repair classification -------------------------------------------------
#
# Three different things get called "repair", and conflating them is how a
# maintenance command ends up mutating a database somebody only asked about:
#
#   CHECK               -- read-only diagnosis
#   SAFE_DERIVED_REPAIR -- rebuild data that is *derived* from other tables, so
#                          the operation is deterministic and its result is
#                          reproducible from the source of truth
#   MAINTENANCE         -- VACUUM: reclaims space, changes no logical content,
#                          and is never part of a repair run

CHECK = "CHECK"
SAFE_DERIVED_REPAIR = "SAFE_DERIVED_REPAIR"
MAINTENANCE = "MAINTENANCE"



def _readonly_connect(db_path: Path) -> sqlite3.Connection:
    """Open for reading only.

    A diagnosis must not be able to write even by accident, so this is enforced
    by SQLite rather than by careful coding.
    """
    uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/").replace("?", "%3f")
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def repair_plan(db_path: Path) -> List[Dict[str, Any]]:
    """What is wrong, what could be done, and how each step is classified.

    Read-only by construction: the connection is opened with `mode=ro`, so even a
    bug here cannot mutate the database.  Nothing in this function executes a
    repair -- it returns the plan and stops.
    """
    plan: List[Dict[str, Any]] = []

    def add(kind, severity, category, detail, action=None, executable=False):
        plan.append({"kind": kind, "severity": severity, "category": category,
                     "detail": detail, "action": action,
                     "executable": executable})

    con = _readonly_connect(db_path)
    try:
        # --- integrity: never auto-repaired --------------------------------
        try:
            verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
        except Exception as e:
            verdict = "error: %s" % e
        if verdict == "ok":
            add(CHECK, INFO, "integrity", "PRAGMA integrity_check: ok")
        else:
            add(CHECK, CRITICAL, "integrity",
                "PRAGMA integrity_check: %s" % verdict,
                action="restore from a backup (`voyager db backup` keeps them) or "
                       "repair manually; this is NOT something to fix automatically")

        # --- foreign keys ---------------------------------------------------
        try:
            fk = list(con.execute("PRAGMA foreign_key_check"))
            if fk:
                add(CHECK, WARNING, "foreign_keys",
                    "%d foreign key violation(s)" % len(fk),
                    action="inspect the referencing rows; not auto-repairable")
            else:
                add(CHECK, INFO, "foreign_keys", "no foreign key violations")
        except Exception as e:
            add(CHECK, WARNING, "foreign_keys", "check failed: %s" % e)

        # --- orphans: rows whose parent is gone -----------------------------
        try:
            orphans = con.execute(
                "SELECT COUNT(*) FROM events e LEFT JOIN sessions s ON e.sid = s.id "
                "WHERE s.id IS NULL").fetchone()[0]
        except Exception:
            orphans = None
        if orphans:
            add(CHECK, WARNING, "orphan_events",
                "%d event(s) reference a session that is not indexed" % orphans,
                action="re-scan the provider source, or leave them: they are inert")
        elif orphans == 0:
            add(CHECK, INFO, "orphan_events", "no orphaned events")

        # --- FTS: derived data, so it is safe to rebuild ---------------------
        try:
            fts_rows = con.execute("SELECT COUNT(*) FROM event_fts").fetchone()[0]
            ev_rows = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            if fts_rows == 0 and ev_rows > 0:
                add(SAFE_DERIVED_REPAIR, WARNING, "fts_index",
                    "FTS index is empty while events has %d row(s)" % ev_rows,
                    action="rebuild the FTS index from events",
                    executable=True)
            elif abs(fts_rows - ev_rows) > max(10, ev_rows // 100):
                add(SAFE_DERIVED_REPAIR, WARNING, "fts_index",
                    "FTS index has %d row(s) against %d event(s)" % (fts_rows, ev_rows),
                    action="rebuild the FTS index from events",
                    executable=True)
            else:
                add(CHECK, INFO, "fts_index",
                    "FTS index consistent (%d rows)" % fts_rows)
        except Exception as e:
            add(SAFE_DERIVED_REPAIR, WARNING, "fts_index",
                "FTS index unreadable (%s)" % e,
                action="rebuild the FTS index from events", executable=True)
    finally:
        con.close()

    # --- maintenance is listed, and never part of repair --------------------
    try:
        size = db_path.stat().st_size
        free = None
        con = _readonly_connect(db_path)
        try:
            row = con.execute("PRAGMA page_count").fetchone()[0]
            free = con.execute("PRAGMA freelist_count").fetchone()[0]
        finally:
            con.close()
        if free:
            add(MAINTENANCE, INFO, "space",
                "%d free page(s) of %d; VACUUM would reclaim them" % (free, row),
                action="run `voyager db compact` (separate on purpose: it is "
                       "maintenance, not repair)")
        else:
            add(MAINTENANCE, INFO, "space",
                "no free pages (%d bytes)" % size)
    except Exception:
        pass

    return plan


def apply_safe_repairs(db_path: Path) -> List[Dict[str, Any]]:
    """Execute only the SAFE_DERIVED_REPAIR steps from :func:`repair_plan`.

    The FTS table is *derived* from `events`, so rebuilding it is deterministic:
    dropping it loses nothing that cannot be recomputed, and the rebuild is a
    single transaction.  Nothing else is ever executed here -- in particular no
    VACUUM, and nothing touching a CRITICAL integrity finding.
    """
    executed: List[Dict[str, Any]] = []
    plan = repair_plan(db_path)
    todo = [p for p in plan if p["executable"] and p["kind"] == SAFE_DERIVED_REPAIR]
    if not todo:
        return executed

    con = sqlite3.connect(str(db_path))
    try:
        # NB: executescript() COMMITs any pending transaction before it runs, so
        # the DROP must not be left in a transaction that the script would end.
        # Using execute() keeps the whole rebuild inside one transaction.
        con.execute("DROP TABLE IF EXISTS event_fts")
        con.execute(
            "CREATE VIRTUAL TABLE event_fts USING fts5("
            "body, file_path, command, sid UNINDEXED, tokenize='trigram')")
        con.execute(
            "INSERT INTO event_fts(rowid, body, file_path, command, sid) "
            "SELECT id, "
            "COALESCE(content,'') || ' ' || COALESCE(tool_input,'') || ' ' || "
            "COALESCE(tool_output,'') || ' ' || COALESCE(command,'') || ' ' || "
            "COALESCE(stdout,''), "
            "COALESCE(file_path,''), COALESCE(command,''), COALESCE(sid,'') "
            "FROM events")
        con.commit()
        for step in todo:
            executed.append({"category": step["category"],
                             "kind": SAFE_DERIVED_REPAIR, "ok": True,
                             "action": step["action"]})
    except Exception as e:
        try:
            con.rollback()
        except Exception:
            pass
        executed.append({"category": "fts_index", "kind": SAFE_DERIVED_REPAIR,
                         "ok": False, "error": str(e)})
    finally:
        con.close()
    return executed


def compact(db_path: Path) -> Dict[str, Any]:
    """VACUUM -- maintenance, deliberately not reachable from `db repair`."""
    before = db_path.stat().st_size
    con = sqlite3.connect(str(db_path))
    try:
        # VACUUM needs roughly the size of the database free, and can hit a
        # concurrent writer.  It is atomic, so a failure leaves a usable
        # database -- but the caller still has to be told, on the contract.
        con.execute("VACUUM")
        con.commit()
    except Exception as e:
        return {"ok": False, "kind": MAINTENANCE, "error": str(e),
                "before_bytes": before, "after_bytes": before,
                "reclaimed_bytes": 0}
    finally:
        con.close()
    after = db_path.stat().st_size
    return {"ok": True, "kind": MAINTENANCE, "before_bytes": before,
            "after_bytes": after, "reclaimed_bytes": max(0, before - after)}


def _render_plan(plan) -> None:
    for step in plan:
        print("  [%-20s] %-8s %-14s %s"
              % (step["kind"], step["severity"], step["category"], step["detail"]))
        if step.get("action"):
            print("      -> %s" % step["action"])


def cmd_db_repair(db_path: Optional[Path] = None, apply: bool = False,
                  json_output: bool = False) -> int:
    """CLI command: voyager db repair [--apply] [--json].

    Default is a plan, not an action.  `--apply` authorises exactly the steps
    classified SAFE_DERIVED_REPAIR -- deterministic rebuilds of data that is
    derived from other tables.  There is no confirmation bypass: a dry run is the
    default, and `--apply` *is* the authorisation.
    """
    from .store import default_db_path
    db = db_path or default_db_path()

    if not db.exists():
        if json_output:
            print(json.dumps({"ok": False, "error": "database not found",
                              "path": str(db)}, ensure_ascii=False))
        else:
            print("database not found: %s" % db, file=sys.stderr)
        return 1

    plan = repair_plan(db)
    executable = [p for p in plan if p["executable"]]
    critical = [p for p in plan if p["severity"] == CRITICAL]

    executed = []
    if apply:
        executed = apply_safe_repairs(db)

    # A failed step must not be reported as success: automation reads this.
    failed_steps = [s for s in executed if not s.get("ok")]
    ok = (not critical) and not failed_steps

    if json_output:
        print(json.dumps({
            "ok": ok,
            "applied": bool(apply),
            "plan": plan,
            "executable": [p["category"] for p in executable],
            "executed": executed,
            "critical": [p["category"] for p in critical],
            "failed_steps": failed_steps,
            "note": ("no automatic repair is offered for CRITICAL findings; "
                     "restore from a backup or repair by hand"),
        }, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    print("Database: %s" % db.resolve())
    print("-" * 66)
    _render_plan(plan)
    print("-" * 66)

    if critical:
        print("CRITICAL findings: %s" % ", ".join(p["category"] for p in critical))
        print("These are not auto-repairable. Restore from a backup "
              "(`voyager db backup` keeps them) or repair by hand.")
    if not executable:
        print("Nothing safe to repair.")
        return 1 if critical else 0

    if apply:
        for step in executed:
            mark = "ok" if step.get("ok") else "FAILED"
            print("[%s] %s" % (mark, step.get("action") or step.get("error")))
        return 0 if ok else 1
    print("Plan only. Run `voyager db repair --apply` to execute the "
          "%d safe step(s) above." % len(executable))
    return 0


def cmd_db_compact(db_path: Optional[Path] = None, json_output: bool = False) -> int:
    """CLI command: voyager db compact -- VACUUM, on purpose and by itself."""
    from .store import default_db_path
    db = db_path or default_db_path()
    if not db.exists():
        if json_output:
            print(json.dumps({"ok": False, "error": "database not found",
                              "path": str(db)}, ensure_ascii=False))
        else:
            print("database not found: %s" % db, file=sys.stderr)
        return 1
    result = compact(db)
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    print("Compacted: %.1f MB -> %.1f MB (reclaimed %.1f MB)"
          % (result["before_bytes"] / 1048576, result["after_bytes"] / 1048576,
             result["reclaimed_bytes"] / 1048576))
    return 0


# --- Main Entry Point ------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point for standalone execution."""
    import argparse

    parser = argparse.ArgumentParser(description="Voyager database health checks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # db check
    check_parser = subparsers.add_parser("check", help="run integrity checks")
    check_parser.add_argument("--verbose", "-v", action="store_true")

    # db backup
    backup_parser = subparsers.add_parser("backup", help="create database backup")
    backup_parser.add_argument("--output-dir", help="backup destination directory")

    # db repair (plan by default; --apply authorises the safe steps)
    repair_parser = subparsers.add_parser("repair", help="plan, and optionally apply, safe repairs")
    repair_parser.add_argument("--apply", action="store_true")
    repair_parser.add_argument("--json", action="store_true")

    # db compact (VACUUM, separate on purpose)
    compact_parser = subparsers.add_parser("compact", help="VACUUM the database")
    compact_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "check":
        return cmd_db_check(verbose=args.verbose)
    elif args.command == "backup":
        return cmd_db_backup(output_dir=args.output_dir)
    elif args.command == "repair":
        return cmd_db_repair(apply=args.apply, json_output=args.json)
    elif args.command == "compact":
        return cmd_db_compact(json_output=args.json)

    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
