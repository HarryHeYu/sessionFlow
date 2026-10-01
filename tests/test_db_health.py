"""`voyager db` -- repair semantics, real detection, and a JSON contract.

Three things are pinned here, because each is a way a maintenance command can
look helpful and be harmful:

  **D** a repair run is a *plan* by default; `--apply` authorises exactly the
        derived-data steps; VACUUM is not one of them and lives in its own
        command; there is no confirmation bypass to get wrong in a script.

  **E** the checks detect things.  Each test builds a database with a real
        defect and asserts the check notices it, with a severity and a suggested
        repair -- and that a CRITICAL finding is never claimed to be fixable.

  **F** every command has `--json`, stdout carries only the protocol result, and
        diagnostics go to stderr.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from voyager import db_health as dh
from voyager.store import Store


def _digest(path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


@pytest.fixture
def healthy(tmp_path):
    """A real, populated index with the FTS table the checks expect."""
    path = tmp_path / "index.db"
    store = Store(path)
    tid = store.thread_create(repo_root="E:/repo", title="t", goal="g")
    from voyager.model import new_event, new_session

    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    sess = new_session(id="codex:s1", provider="codex", native_session_id="s1",
                       title="seeded", started_at=1.0, updated_at=2.0,
                       repo_root="E:/repo", cwd="E:/repo")
    store.replace_session(
        sess, [new_event(sid="codex:s1", seq=1, kind="user", ts=1.0, content="hi")],
        "codex", src)
    store.thread_attach(tid, "codex:s1")
    store.close()

    # the store already builds event_fts; make sure it is populated and in sync
    con = sqlite3.connect(str(path))
    try:
        con.execute("DELETE FROM event_fts")
    except sqlite3.OperationalError:
        con.executescript(
            "CREATE VIRTUAL TABLE event_fts USING fts5(body, file_path, command, "
            "sid UNINDEXED, tokenize='trigram');")
    con.execute("INSERT INTO event_fts(rowid, body, file_path, command, sid) "
                "SELECT id, COALESCE(content,''), COALESCE(file_path,''), "
                "COALESCE(command,''), COALESCE(sid,'') FROM events")
    con.commit()
    con.close()
    return path


# --- D: repair is a plan, and maintenance is not repair ---------------------

def test_the_plan_is_read_only(healthy, capsys):
    before = _digest(healthy)
    rc = dh.cmd_db_repair(db_path=healthy)
    capsys.readouterr()
    assert _digest(healthy) == before, "a plan must not touch the database"
    assert rc == 0


def test_the_plan_is_classified(healthy):
    plan = dh.repair_plan(healthy)
    kinds = {p["kind"] for p in plan}
    assert kinds <= {dh.CHECK, dh.SAFE_DERIVED_REPAIR, dh.MAINTENANCE}
    assert dh.CHECK in kinds
    for step in plan:
        assert step["severity"] in (dh.INFO, dh.WARNING, dh.CRITICAL)
        assert "category" in step and "detail" in step


def test_vacuum_is_maintenance_and_never_executed_by_repair(healthy, monkeypatch):
    """The failure this guards: a repair run that silently VACUUMs."""
    plan = dh.repair_plan(healthy)
    assert all(p["kind"] != dh.MAINTENANCE or not p["executable"] for p in plan)

    called = []
    monkeypatch.setattr(dh, "compact", lambda *a, **k: called.append("vacuum"))
    dh.apply_safe_repairs(healthy)
    dh.cmd_db_repair(db_path=healthy, apply=True)
    assert called == [], "repair must never reach VACUUM"


def test_compact_is_its_own_command(healthy, capsys):
    before = _digest(healthy)
    rc = dh.cmd_db_compact(db_path=healthy)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Compacted" in out
    assert _digest(healthy) != before, "compact is allowed to rewrite the file"


def test_apply_only_touches_derived_data(healthy):
    """FTS is derived from events: dropping it loses nothing recomputable."""
    con = sqlite3.connect(str(healthy))
    events_before = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    con.close()

    executed = dh.apply_safe_repairs(healthy)

    con = sqlite3.connect(str(healthy))
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
    con.close()
    for step in executed:
        assert step["kind"] == dh.SAFE_DERIVED_REPAIR


def test_there_is_no_confirmation_bypass(healthy):
    """`--apply` is the authorisation; nothing takes a `force`."""
    import inspect

    for fn in (dh.cmd_db_repair, dh.cmd_db_check, dh.cmd_db_backup, dh.cmd_db_compact):
        params = inspect.signature(fn).parameters
        assert "force" not in params, fn.__name__
    assert "apply" in inspect.signature(dh.cmd_db_repair).parameters


# --- E: the checks detect real defects --------------------------------------

def test_a_healthy_database_passes(healthy, capsys):
    rc = dh.cmd_db_check(db_path=healthy)
    capsys.readouterr()
    assert rc == 0


def test_an_orphan_event_is_detected(healthy):
    con = sqlite3.connect(str(healthy))
    con.execute("INSERT INTO events (sid, ts, seq, kind, content) "
                "VALUES ('ghost:1', 1.0, 1, 'user', 'orphan')")
    con.commit()
    con.close()

    results = {r.category: r for r in dh.run_integrity_check(healthy)}
    assert results["orphan_events"].passed is False
    assert results["orphan_events"].details["orphans"] == 1
    assert results["orphan_events"].suggested_repair, "must say what to do"


def test_an_orphan_thread_member_is_detected(healthy):
    con = sqlite3.connect(str(healthy))
    cols = [r[1] for r in con.execute("PRAGMA table_info(thread_sessions)")]
    session_col = "sid" if "sid" in cols else "session_id"
    con.execute("INSERT INTO thread_sessions (thread_id, %s) VALUES ('thr_ghost', 'codex:s1')"
                % session_col)
    con.commit()
    con.close()

    results = {r.category: r for r in dh.run_integrity_check(healthy)}
    assert results["orphan_thread_members"].passed is False
    assert results["orphan_thread_members"].details["orphans"] == 1


def test_a_broken_fts_index_is_detected_and_is_repairable(healthy):
    con = sqlite3.connect(str(healthy))
    con.execute("DELETE FROM event_fts")
    con.commit()
    con.close()

    results = {r.category: r for r in dh.run_integrity_check(healthy)}
    fts = results["fts_index"]
    assert fts.passed is False
    assert fts.severity == dh.WARNING
    assert fts.suggested_repair and "repair --apply" in fts.suggested_repair

    # and the offered repair actually works
    dh.apply_safe_repairs(healthy)
    again = {r.category: r for r in dh.run_integrity_check(healthy)}
    assert again["fts_index"].passed is True


def test_a_broken_fts_is_executable_but_integrity_damage_is_not(healthy):
    con = sqlite3.connect(str(healthy))
    con.execute("DELETE FROM event_fts")
    con.commit()
    con.close()
    plan = {p["category"]: p for p in dh.repair_plan(healthy)}
    assert plan["fts_index"]["executable"] is True
    assert plan["integrity"]["executable"] is False


def test_real_corruption_is_critical_and_not_claimed_fixable(healthy, capsys):
    """Corrupt a page, and check that the answer is honest: restore, don't fix."""
    with open(str(healthy), "r+b") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(size // 2)
        f.write(b"\x00" * 4096)

    con = sqlite3.connect(str(healthy))
    try:
        verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as e:
        verdict = "malformed: %s" % e          # still not "ok", which is the point
    finally:
        con.close()

    results = {r.category: r for r in dh.run_integrity_check(healthy)}
    integrity = results["integrity"]
    if verdict != "ok":
        assert integrity.passed is False
        assert integrity.severity == dh.CRITICAL
        assert "restore" in (integrity.suggested_repair or "").lower()
        plan = {p["category"]: p for p in dh.repair_plan(healthy)}
        assert plan["integrity"]["executable"] is False, \
            "structural damage must never be offered as an automatic repair"
        assert dh.cmd_db_repair(db_path=healthy) == 1
    else:
        # the platform kept the damage out of the pages SQLite reads; the
        # assertion above still holds for the healthy case
        assert integrity.passed is True


# --- F: the JSON contract ---------------------------------------------------

def test_every_command_has_json_on_stdout(healthy, capsys):
    for call, key in ((lambda: dh.cmd_db_check(db_path=healthy, json_output=True), "checks"),
                      (lambda: dh.cmd_db_repair(db_path=healthy, json_output=True), "plan"),
                      (lambda: dh.cmd_db_compact(db_path=healthy, json_output=True), "kind")):
        capsys.readouterr()
        rc = call()
        payload = json.loads(capsys.readouterr().out)
        assert key in payload, (key, payload)
        assert rc in (0, 1)


def test_backup_json_contract(healthy, tmp_path, capsys):
    rc = dh.cmd_db_backup(db_path=healthy, output_dir=str(tmp_path / "b"),
                          json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert set(payload) == {"ok", "backup", "metadata", "meta"}
    assert payload["meta"]["integrity_check"] == "ok"


def test_missing_database_reports_on_the_contract_not_a_crash(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    for fn in (lambda: dh.cmd_db_check(db_path=missing, json_output=True),
               lambda: dh.cmd_db_repair(db_path=missing, json_output=True),
               lambda: dh.cmd_db_compact(db_path=missing, json_output=True),
               lambda: dh.cmd_db_backup(db_path=missing, json_output=True)):
        capsys.readouterr()
        rc = fn()
        payload = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert payload["ok"] is False
        assert "error" in payload


def test_diagnostics_never_go_to_stdout(healthy, capsys):
    """`--json` must be safe to pipe: stdout is the payload and nothing else."""
    dh.cmd_db_check(db_path=healthy, json_output=True)
    out = capsys.readouterr().out
    json.loads(out)          # would raise if anything else were on stdout


def test_every_db_command_honours_an_explicit_index(tmp_path, capsys):
    """`voyager --db X db check` must diagnose X -- and must not crash doing it.

    argparse hands the command a *string*, while the store-facing functions take a
    Path; getting that boundary wrong turned `db check --db` into an
    `AttributeError`, which the smoke test caught.  The same class of bug had
    already bitten once, when `--db` was silently ignored and `db compact` would
    have vacuumed the default index instead.
    """
    from voyager.cli import main
    from voyager.store import Store

    path = tmp_path / "explicit.db"
    store = Store(path)
    try:
        store.thread_create(repo_root="E:/explicit", title="t", goal="g")
    finally:
        store.close()

    for argv in (["db", "check"], ["db", "repair"], ["db", "compact", "--json"],
                 ["verify"], ["doctor"]):
        capsys.readouterr()
        rc = main(["--db", str(path)] + argv)
        out = capsys.readouterr()
        assert rc in (0, 1), (argv, out.err)
        assert "Traceback" not in out.err, (argv, out.err)
        assert "AttributeError" not in out.err, (argv, out.err)


def test_doctor_reports_the_index_it_actually_read(tmp_path, capsys):
    from voyager.cli import main
    from voyager.store import Store

    path = tmp_path / "named.db"
    Store(path).close()
    capsys.readouterr()
    main(["--db", str(path), "doctor"])
    assert str(path) in capsys.readouterr().out
