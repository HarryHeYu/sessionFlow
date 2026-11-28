"""R20/M regression: doctor must report structural damage read-only instead
of silently migrating the index through Store(path).
"""
from __future__ import annotations

import sqlite3

from voyager import doctor


def test_damaged_schema_is_reported_without_writing(monkeypatch):
    uri = "file:r20_doctor_damaged?mode=memory&cache=shared"
    con = sqlite3.connect(uri, uri=True)
    con.execute("CREATE TABLE sessions(id TEXT)")
    con.execute("CREATE TABLE events(sid TEXT)")
    con.commit()

    class FakePath:
        def exists(self):
            return True

        def __str__(self):
            return "fake://damaged"

    monkeypatch.setattr(doctor, "_store_path", lambda db=None: FakePath())
    monkeypatch.setattr(
        doctor, "_open_ro_conn",
        lambda path: sqlite3.connect(uri, uri=True))

    schema = doctor.check_schema_integrity()
    assert schema["ok"] is False
    assert "sources" in schema["missing_tables"]
    assert "sessions.source_state" in schema["missing_columns"]

    report = doctor.run()
    codes = [i["code"] for i in report["issues"]]
    assert "SCHEMA_MISSING_TABLE" in codes
    assert "SCHEMA_MISSING_COLUMN" in codes
    assert report["store"]["ok"] is False
    con.close()


def test_orphan_events_are_detected(monkeypatch):
    uri = "file:r20_doctor_orphan?mode=memory&cache=shared"
    con = sqlite3.connect(uri, uri=True)
    for table in doctor._REQUIRED_TABLES:
        if table == "sessions":
            con.execute("CREATE TABLE sessions(id TEXT, source_state TEXT, "
                        "source_missing_since REAL)")
        elif table == "events":
            con.execute("CREATE TABLE events(sid TEXT, origin TEXT)")
        else:
            con.execute("CREATE TABLE {0}(id TEXT)".format(table))
    con.execute("INSERT INTO events(sid) VALUES('missing-session')")
    con.commit()

    class FakePath:
        def exists(self):
            return True

        def __str__(self):
            return "fake://orphan"

    monkeypatch.setattr(doctor, "_store_path", lambda db=None: FakePath())
    monkeypatch.setattr(
        doctor, "_open_ro_conn",
        lambda path: sqlite3.connect(uri, uri=True))
    schema = doctor.check_schema_integrity()
    assert schema["orphan_events"] == 1
    codes = [i.code for i in doctor._schema_issues(schema)]
    assert "ORPHAN_EVENTS" in codes
    con.close()


def test_missing_index_keeps_full_doctor_report(monkeypatch):
    """A missing DB is not structural damage; doctor must stay complete."""
    class MissingPath:
        def exists(self):
            return False

        def __str__(self):
            return "fake://missing"

    monkeypatch.setattr(doctor, "_store_path", lambda db=None: MissingPath())
    schema = doctor.check_schema_integrity()
    assert schema["ok"] is True
    assert schema["db_missing"] is True

    report = doctor.run()
    assert set(report["providers"]) == set(doctor.PROVIDERS)
    codes = {i["code"] for i in report["issues"]}
    assert "STORE_UNAVAILABLE" in codes
