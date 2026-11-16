"""WorkBuddy adapter — synthetic (UNIT_VERIFIED) contract tests.

The local WorkBuddy install was used only to identify directory names, database
tables and JSON keys. Every string, id and cwd below is generated in this file;
no real conversation is read. These tests prove the read-only mapping, not live
continuity. See docs/audits/2026-11-workbuddy-provider-recon.md.
"""

from __future__ import annotations

import pytest

from workbuddy_fixtures import T0, T2, make_corrupt, make_session, make_unknown

from voyager.adapters import workbuddy as wb
from voyager.capability_matrix import (DECLARED, DIMENSIONS, NOT_FOUND,
                                       PROVIDERS, UNIT_VERIFIED)
from voyager.scanner import run_scan
from voyager.store import Store


@pytest.fixture
def projects(monkeypatch, tmp_path):
    root = tmp_path / "workbuddy-projects"
    monkeypatch.setenv("VOYAGER_WORKBUDDY_PROJECTS", str(root))
    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    return root


def test_override_is_authoritative(projects, tmp_path):
    """A test that names a projects dir must never fall back to the real one."""
    expected = make_session(projects / "demo" / "wb-1.jsonl")
    # A plausible real-looking file that is NOT under the override.
    decoy = tmp_path / "home" / ".workbuddy-ai" / "projects" / "real" / "x.jsonl"
    make_session(decoy, sid="real-should-not-be-read")

    assert wb.candidate_files() == [expected]
    adapter = wb.WorkbuddyAdapter()
    assert adapter.discover() == [expected]


def test_probe_recognises_a_workbuddy_jsonl(projects):
    path = make_session(projects / "demo" / "wb-1.jsonl")
    verdict, sid = wb.probe(path)
    assert (verdict, sid) == (wb.PROBE_OK, "wb-1")


def test_parse_maps_the_synthetic_session(projects, tmp_path):
    cwd = tmp_path / "repo"
    path = make_session(projects / "demo" / "wb-1.jsonl", cwd=str(cwd))
    bundle = wb.WorkbuddyAdapter().parse(path)

    session = bundle["session"]
    assert session["id"] == "workbuddy:wb-1"
    assert session["provider"] == "workbuddy"
    assert session["native_session_id"] == "wb-1"
    assert session["title"] == "Synthetic WorkBuddy title"
    assert session["cwd"] == str(cwd)
    assert session["started_at"] == T0 / 1000.0
    assert session["updated_at"] == T2 / 1000.0
    assert session["model"] == "synthetic-model"
    assert session["message_count"] == 2
    assert session["tool_count"] == 1
    assert session["can_resume"] is False
    assert session["metadata"]["host_kind"] == "desktop-synthetic"

    events = bundle["events"]
    assert [e["kind"] for e in events] == [
        "user", "assistant", "reasoning", "tool_call", "tool_result", "meta"]
    assert [e["seq"] for e in events] == list(range(1, 7))
    assert all(e["sid"] == "workbuddy:wb-1" for e in events)

    user = events[0]
    assert user["role"] == "user"
    assert user["origin"] == "human"
    assert user["content"] == "synthetic user turn"

    injected = events[-1]
    assert injected["kind"] == "meta"
    assert injected["origin"] == "provider_system"
    assert injected["content"] == "provider bootstrap text"

    call = events[3]
    assert call["tool_call_id"] == "call-1"
    assert call["tool_name"] == "shell"
    assert call["command"] == "echo synthetic"
    assert call["file_path"] == "/synthetic.py"
    assert "echo synthetic" in call["tool_input"]

    result = events[4]
    assert result["tool_call_id"] == "call-1"
    assert result["tool_output"] == "synthetic output"
    assert result["origin"] == "provider_system"


def test_companion_files_are_freshness_sources(projects):
    path = make_session(projects / "demo" / "wb-1.jsonl")
    bundle = wb.WorkbuddyAdapter().parse(path)
    names = {p.name for p in bundle["extra_sources"]}
    assert names == {"wb-1.meta.json", "wb-1.file-rollback.ndjson"}


def test_malformed_tail_does_not_lose_the_session(projects):
    path = make_session(projects / "demo" / "wb-1.jsonl", malformed_tail=True)
    bundle = wb.WorkbuddyAdapter().parse(path)
    assert bundle is not None
    assert bundle["session"]["id"] == "workbuddy:wb-1"


@pytest.mark.parametrize("builder", [make_unknown, make_corrupt])
def test_bad_files_are_reported_not_retired(projects, builder):
    path = builder(projects / "demo" / "bad.jsonl")
    assert wb.probe(path)[0] == wb.PROBE_UNKNOWN_SCHEMA
    assert path in wb.WorkbuddyAdapter().unusable_sources()
    assert wb.WorkbuddyAdapter().parse(path) is None


def test_absent_project_directory_is_not_an_error(projects, monkeypatch):
    monkeypatch.setenv("VOYAGER_WORKBUDDY_PROJECTS", str(projects / "missing"))
    assert wb.candidate_files() == []
    assert wb.WorkbuddyAdapter().discover() == []
    assert wb.WorkbuddyAdapter().unusable_sources() == []


def test_capability_matrix_declares_read_only_only(adapter_of):
    adapter = adapter_of("workbuddy")
    assert adapter.can_resume is False
    assert adapter.can_fork is False
    assert "workbuddy" in PROVIDERS
    assert set(DECLARED["workbuddy"]) == set(DIMENSIONS)
    for dim in ("native_resume", "startup_hook", "dynamic_context_injection",
                "native_auto_attach", "live_zero_touch_continuity"):
        assert DECLARED["workbuddy"][dim][0] == NOT_FOUND
    for dim in ("session_discovery", "session_indexing", "transcript_import",
                "tool_event_support", "cwd_repo_discovery"):
        assert DECLARED["workbuddy"][dim][0] == UNIT_VERIFIED


def test_run_scan_indexes_workbuddy_from_the_override(projects, tmp_path):
    make_session(projects / "demo" / "wb-1.jsonl")
    db = tmp_path / "index.db"
    store = Store(str(db))
    try:
        result = run_scan(store, providers=["workbuddy"], quiet=True)
        assert result["new"] == 1
        rows = store.q("SELECT id, provider, title FROM sessions "
                       "WHERE provider='workbuddy'")
        assert [dict(r) for r in rows] == [{
            "id": "workbuddy:wb-1",
            "provider": "workbuddy",
            "title": "Synthetic WorkBuddy title",
        }]
    finally:
        store.close()


def test_isolation_guard_knows_about_workbuddy_home():
    import iso_guard

    assert any(p.name == ".workbuddy-ai" for p in iso_guard.real_data_roots())
    assert "VOYAGER_WORKBUDDY_PROJECTS" in iso_guard._DISCOVERY_ENV
