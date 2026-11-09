"""DSH adapter regression test.

Fixture: tests/fixtures/dsh/sessions/.../session.jsonl, zstd-compressed at
test time (the adapter reads the compressed artifact DSH writes). Also covers
the corrupt-archive path: a broken .zstd must surface as an error result, not
as a crash that aborts the scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def test_dsh_parse(adapter_of, patch_paths, dsh_fixture):
    ad = adapter_of("dsh")
    patch_paths(ad, SESSIONS_DIR=dsh_fixture)
    files = ad.discover()
    assert len(files) == 1
    r = ad.parse(files[0])
    s, evs = r["session"], r["events"]
    assert s["native_session_id"] == "session-dsh-1"
    assert s["title"] == "你好，跑个测试"
    assert s["model"] == "deepseek-v4-pro"
    # `dsh --resume <session>` does not run: dsh requires `--profile <name>`
    # ("error: --profile <name> is required", dsh 0.1.5-rc.3), and a DSH session
    # records no profile — the path is ~/.dsh/sessions/<munged-cwd>/session-<id>/
    # and the session record carries cwd and agentPreset, not a profile name.
    # So no resume command can be built that would actually work.
    assert s["can_resume"] is False
    assert s["resume_cmd"] is None
    kinds = [e["kind"] for e in evs]
    assert kinds.count("user") == 1 and kinds.count("assistant") == 1
    assert kinds.count("tool_call") == 1 and kinds.count("tool_result") == 1
    assert kinds.count("reasoning") == 1


def test_dsh_does_not_claim_a_resume_it_cannot_build(adapter_of):
    """The adapter is the authority the capability matrix reads.

    DSH is resumable in principle — `dsh --profile <p> --resume <session>` — but
    the profile is not recoverable from a session, so Voyager cannot construct a
    command that runs.  The adapter must therefore say so, or `verify --matrix`
    advertises SUPPORTED while `voyager resume` refuses the same session.
    """
    ad = adapter_of("dsh")
    assert ad.can_resume is False


def test_dsh_corrupt_zstd_is_reported(adapter_of, patch_paths, dsh_fixture):
    ad = adapter_of("dsh")
    patch_paths(ad, SESSIONS_DIR=dsh_fixture)
    f = list(ad.discover())[0]
    f.write_bytes(b"NOT-ZSTD")
    r = ad.parse(f)
    assert r and "__error__" in r     # surfaced, not a crash


def test_dsh_discovers_both_artifact_names(adapter_of, patch_paths, dsh_fixture,
                                           tmp_path):
    """DSH 0.2.1-rc.3 renamed session.jsonl.zstd -> session.v4.jsonl.zstd.

    Both shapes can be present on one machine.  A glob that only names the old
    one silently drops every newer session while `scan` still reports a clean
    total, which is worse than an error.
    """
    zstd = pytest.importorskip("zstandard", reason="dsh extra not installed")
    src = (FIXTURES / "dsh" / "sessions" / "--E--proj--demo--" /
           "session-dsh-1" / "session.jsonl")
    blob = zstd.ZstdCompressor().compress(src.read_bytes())

    new_dir = dsh_fixture / "--E--proj--demo--" / "session-dsh-v4"
    new_dir.mkdir(parents=True)
    (new_dir / "session.v4.jsonl.zstd").write_bytes(blob)

    # a future rename must not be parsed as a session, but must be reported
    future = dsh_fixture / "--E--proj--demo--" / "session-dsh-v9"
    future.mkdir(parents=True)
    (future / "rollout.jsonl.zstd").write_bytes(blob)

    ad = adapter_of("dsh")
    patch_paths(ad, SESSIONS_DIR=dsh_fixture)
    files = ad.discover()
    names = [p.name for p in files]
    assert "session.jsonl.zstd" in names
    assert "session.v4.jsonl.zstd" in names
    assert len(files) == 2

    # both parse: the parser is format-agnostic, only discovery was wrong
    for f in files:
        r = ad.parse(f)
        assert r and "session" in r and "events" in r

    unknown = ad.unrecognised_sources()
    assert [p.name for p in unknown] == ["rollout.jsonl.zstd"]


def test_dsh_unrecognised_sources_is_empty_for_known_names(
        adapter_of, patch_paths, dsh_fixture):
    ad = adapter_of("dsh")
    patch_paths(ad, SESSIONS_DIR=dsh_fixture)
    assert ad.unrecognised_sources() == []
