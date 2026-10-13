"""DSH adapter regression test.

Fixture: tests/fixtures/dsh/sessions/.../session.jsonl, zstd-compressed at
test time (the adapter reads the compressed artifact DSH writes). Also covers
the corrupt-archive path: a broken .zstd must surface as an error result, not
as a crash that aborts the scan.
"""

from __future__ import annotations


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
