"""The canonical capability matrix and `voyager doctor`.

Two things are pinned here:

1. The matrix is a single source of truth: every provider has an entry for every
   dimension, no cell claims more than the declared ceiling, and the headline
   states are exactly the ones the README and `doctor` publish.  A claim of
   LIVE / ZERO_TOUCH must be backed by machine evidence, never by code alone.

2. `doctor` never raises, classifies issues into blocking / external /
   non-blocking, and its JSON view is complete and serialisable.
"""

from __future__ import annotations

import json

import pytest

from voyager import doctor
from voyager.capability_matrix import (
    CONFIGURED, DIMENSIONS, LIVE_VERIFIED, NOT_FOUND, PROVIDERS, STATE_ORDER,
    SUPPORTED, UNIT_VERIFIED, ZERO_TOUCH_LIVE_VERIFIED,
    Evidence, collect_evidence, evidence_ceiling, matrix, provider_state,
    resolve_cell, summary,
)


# --- the matrix is complete ------------------------------------------------

def test_every_provider_has_every_dimension():
    m = matrix()
    assert set(m) == set(PROVIDERS)
    for p, dims in m.items():
        assert set(dims) == set(DIMENSIONS), p
        for d, cell in dims.items():
            assert cell["state"] in STATE_ORDER, (p, d, cell["state"])
            assert cell["note"], (p, d)


def test_a_cell_never_exceeds_its_declared_ceiling():
    from voyager.capability_matrix import DECLARED
    for p in PROVIDERS:
        ev = collect_evidence(p)
        for d in DIMENSIONS:
            declared = DECLARED[p][d][0]
            got = resolve_cell(p, d, ev)[0]
            assert STATE_ORDER[got] <= STATE_ORDER[declared], (p, d, got, declared)


def test_headline_states_match_what_the_project_publishes():
    """The README, `doctor` and this table are the same claim."""
    assert provider_state("codex") == ZERO_TOUCH_LIVE_VERIFIED
    assert provider_state("claude") == ZERO_TOUCH_LIVE_VERIFIED
    assert provider_state("grok") == ZERO_TOUCH_LIVE_VERIFIED
    assert provider_state("zcode") == UNIT_VERIFIED
    assert provider_state("cursor") == UNIT_VERIFIED
    assert provider_state("kiro") == UNIT_VERIFIED
    assert provider_state("antigravity") == UNIT_VERIFIED
    assert provider_state("dsh") == NOT_FOUND


def test_a_provider_without_a_startup_surface_is_not_called_supported():
    """NOT_FOUND must survive; it is not folded into a softer state."""
    assert provider_state("dsh") == NOT_FOUND
    assert resolve_cell("dsh", "startup_hook")[0] == NOT_FOUND


def test_evidence_ceiling_floor_is_unit_verified():
    """A handler that exists and is tested is unit-verified whether or not the
    provider happens to be installed on this machine."""
    assert evidence_ceiling(Evidence()) == UNIT_VERIFIED
    assert evidence_ceiling(Evidence(installed=True)) == UNIT_VERIFIED
    assert evidence_ceiling(Evidence(installed=True, hook_registered=True)) == UNIT_VERIFIED
    assert evidence_ceiling(Evidence(hook_fired=True)) == LIVE_VERIFIED
    assert evidence_ceiling(Evidence(zero_touch_observed=True)) == ZERO_TOUCH_LIVE_VERIFIED


def test_live_claims_need_evidence():
    """The failure this whole module exists to prevent: a LIVE claim with none."""
    ev = Evidence(installed=True, hook_registered=True, hook_fired=False)
    assert evidence_ceiling(ev) == UNIT_VERIFIED
    assert STATE_ORDER[evidence_ceiling(ev)] < STATE_ORDER[LIVE_VERIFIED]


def test_states_are_ordered_weakest_to_strongest():
    assert (STATE_ORDER[NOT_FOUND] < STATE_ORDER[SUPPORTED]
            < STATE_ORDER[CONFIGURED] < STATE_ORDER[UNIT_VERIFIED]
            < STATE_ORDER[LIVE_VERIFIED] < STATE_ORDER[ZERO_TOUCH_LIVE_VERIFIED])


def test_summary_is_json_serialisable():
    json.dumps(summary(), ensure_ascii=False)


# --- doctor ----------------------------------------------------------------

def test_doctor_never_raises_and_is_complete():
    report = doctor.run()
    for key in ("store", "continuity", "cache", "providers", "issues",
                "blocking", "external", "non_blocking"):
        assert key in report
    assert set(report["providers"]) == set(PROVIDERS)


def test_doctor_json_is_serialisable():
    json.dumps(doctor.run(), ensure_ascii=False, default=str)


def test_doctor_render_is_ascii_safe_and_mentions_every_provider():
    text = doctor.render(doctor.run())
    text.encode("ascii")  # a GBK/ASCII console must not choke on it
    for p in PROVIDERS:
        assert p in text


def test_doctor_flags_literal_unknown_rows_as_blocking(monkeypatch):
    class FakeStore:
        def q(self, sql, args=()):
            return []

        def close(self):
            pass

    monkeypatch.setattr(doctor, "check_continuity", lambda repo=None, db_path=None: {
        "active_threads": 2, "pending": 0, "ambiguous": False,
        "coverage": {"events": 10, "unclassified": 5, "classified_pct": 50.0,
                     "literal_unknown": 3},
    })
    report = doctor.run()
    ids = [i["id"] for i in report["blocking"]]
    assert "PROVENANCE_LITERAL_UNKNOWN" in ids


def test_doctor_flags_ambiguity_as_blocking(monkeypatch):
    monkeypatch.setattr(doctor, "check_continuity", lambda repo=None, db_path=None: {
        "active_threads": 2, "pending": 0, "ambiguous": True,
        "ambiguous_repos": ["E:/repo"], "coverage": None,
    })
    report = doctor.run()
    assert any(i["id"] == "AMBIGUOUS_WORKTHREAD" for i in report["blocking"])


def test_ambuity_counts_only_active_threads(tmp_path, monkeypatch):
    """Two closed threads for one repo are history, not a choice to make."""
    import subprocess

    from voyager.store import Store

    repo = str(tmp_path / "repo")
    subprocess.run(["git", "init", "-q", repo], capture_output=True)
    store = Store(tmp_path / "d.db")
    try:
        store.thread_create(repo_root=repo, title="one", goal="g")
        store.thread_create(repo_root=repo, title="two", goal="g")
        monkeypatch.setattr(doctor, "_store_path",
                            lambda explicit=None: tmp_path / "d.db")
        # both start active -> genuinely ambiguous
        assert doctor.check_continuity()["ambiguous"] is True
        # close them -> no choice left for the user
        store.con.execute("UPDATE threads SET status='closed'")
        store.con.commit()
        assert doctor.check_continuity()["ambiguous"] is False
    finally:
        store.close()


def test_known_debts_are_classified():
    kinds = {d["kind"] for d in doctor.KNOWN_DEBTS}
    assert kinds == {"non-blocking", "external"}
    ids = {d["id"] for d in doctor.KNOWN_DEBTS}
    assert "LIVE_VERIFICATION_BLOCKED_BY_PROVIDER_UI" in ids
    assert "EXTERNAL_CODEX_APPSERVER_CONPTY_POPUP" in ids


def test_doctor_can_be_pointed_at_a_specific_index(tmp_path):
    """`voyager --db X doctor` must diagnose X.  The `db` subcommands had exactly
    this trap -- `--db` silently ignored, so `db compact` would have vacuumed the
    wrong database."""
    from voyager import doctor
    from voyager.store import Store

    path = tmp_path / "other.db"
    store = Store(path)
    try:
        store.thread_create(repo_root="E:/probe", title="probe", goal="g")
    finally:
        store.close()

    report = doctor.run(db_path=path)
    assert report["store"]["path"] == str(path)
    assert report["store"]["threads"] == 1
    assert report["continuity"]["threads_total"] == 1

    # and the default stays untouched by that call
    default_report = doctor.run()
    assert default_report["store"]["path"] != str(path)


def test_the_cli_forwards_db_to_doctor(tmp_path, capsys):
    from voyager.cli import main
    from voyager.store import Store

    path = tmp_path / "cli.db"
    store = Store(path)
    try:
        store.thread_create(repo_root="E:/cli", title="cli", goal="g")
    finally:
        store.close()

    capsys.readouterr()
    assert main(["--db", str(path), "doctor"]) in (0, 1)
    out = capsys.readouterr().out
    assert str(path) in out, "the report must name the index it actually read"
    assert "threads: 1" in out
