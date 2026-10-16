"""The bundle write path: a reader must never see a half-written bundle.

A bundle is read back immediately after it is written (`--json` returns the
compiled context) and can be read by another process entirely — a background
`voyager watch`, an editor, a test suite.  The default bundle name is the member
list plus `int(time.time())`, so two merges of the same sessions in the same
second target the *same* file.  Ten parallel plugin test processes doing exactly
that produced an empty read once in eighty runs, which is what these tests pin
down.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from voyager import continuity as cont


def test_the_destination_is_replaced_never_truncated(tmp_path, monkeypatch):
    """At the moment of the swap the old file must still be complete.

    This is the property that makes a concurrent read safe: if the destination
    were opened for writing, a reader arriving mid-write would get a short file.
    """
    target = tmp_path / "bundle.md"
    target.write_text("old bundle", encoding="utf-8")

    seen = {}
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        seen["destination"] = Path(dst).read_text(encoding="utf-8")
        seen["staged"] = Path(src).read_text(encoding="utf-8")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    cont._write_atomic(target, "new bundle")

    assert seen["destination"] == "old bundle", (
        "the destination was truncated before the swap; a reader could have "
        "seen a partial bundle")
    assert seen["staged"] == "new bundle"
    assert target.read_text(encoding="utf-8") == "new bundle"


def test_a_failed_write_leaves_the_previous_bundle_intact(tmp_path, monkeypatch):
    """A disk-full part-way through must not destroy the bundle that was there."""
    target = tmp_path / "bundle.md"
    target.write_text("old bundle", encoding="utf-8")

    real_write_text = Path.write_text

    def failing(self, data, *args, **kwargs):
        if self.name.startswith("bundle.md.tmp-"):
            real_write_text(self, str(data)[:4], *args, **kwargs)  # partial
            raise OSError("no space left on device")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing)
    with pytest.raises(OSError):
        cont._write_atomic(target, "new bundle")

    assert target.read_text(encoding="utf-8") == "old bundle"
    assert list(tmp_path.glob("*.tmp-*")) == [], "a temp file was left behind"


def test_no_temp_file_survives_a_successful_write(tmp_path):
    target = tmp_path / "bundle.md"
    cont._write_atomic(target, "a bundle")
    assert target.read_text(encoding="utf-8") == "a bundle"
    assert list(tmp_path.glob("*.tmp-*")) == []


def test_a_second_write_replaces_the_first(tmp_path):
    """Two merges of the same members in the same second share a path."""
    target = tmp_path / "bundle.md"
    cont._write_atomic(target, "first")
    cont._write_atomic(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
    assert list(tmp_path.glob("*.tmp-*")) == []


# --- the default name is unique, so no invocation overwrites another ---------
#
# Atomic replacement makes a torn read impossible; it does nothing about two
# invocations choosing the same path.  These two properties are independent and
# both are needed, so both are pinned separately.

_ROWS = [{"provider": "claude", "native_id": "m2"},
         {"provider": "codex", "native_id": "m1"}]


def test_two_merges_in_the_same_second_get_different_names():
    """The old name was members + `int(time.time())`, so these collided."""
    first = cont.default_bundle_name(_ROWS)
    second = cont.default_bundle_name(_ROWS)
    assert first != second, (
        "two merges produced the same path; the second would silently "
        "overwrite the first")
    assert first.endswith(".md") and second.endswith(".md")


def test_the_default_name_keeps_its_documented_prefix():
    """`bundle-merged-*.md` is what users glob for; it must keep working."""
    name = cont.default_bundle_name(_ROWS)
    assert name.startswith("bundle-merged-2sessions-claude-codex-")


def test_a_single_session_name_stays_deterministic():
    """One session re-exported is meant to land on the same file."""
    rows = [{"provider": "codex", "native_id": "1111-2222-3333"}]
    assert (cont.default_bundle_name(rows)
            == cont.default_bundle_name(rows)
            == "bundle-codex-1111-2222-3333.md")


def test_the_name_is_filesystem_safe():
    rows = [{"provider": "codex", "native_id": 'a/b\\c:d*e?f"g<h>i|j'},
            {"provider": "claude", "native_id": "m2"}]
    name = cont.default_bundle_name(rows)
    assert not (set(name) & set('/\\:*?"<>|')), name


def test_concurrent_writes_of_one_target_do_not_share_a_staging_file(tmp_path):
    """Two threads of one process must not fight over the temp file."""
    import threading

    target = tmp_path / "bundle.md"
    payloads = ["A" * 20000, "B" * 20000]
    errors = []

    def writer(text):
        try:
            for _ in range(20):
                cont._write_atomic(target, text)
        except Exception as exc:          # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    # Whatever won, the file is one complete payload - never a mix, never empty.
    assert target.read_text(encoding="utf-8") in payloads
    assert list(tmp_path.glob("*.tmp-*")) == []


def test_the_engine_actually_writes_through_the_atomic_path(
        synthetic_trio, tmp_path, monkeypatch):
    """The helper being correct is not enough — the engine has to use it.

    Without this, reverting the call site to ``out.write_text`` leaves every
    test above passing while the race comes straight back.
    """
    store, rows = synthetic_trio
    out = tmp_path / "bundle.md"
    out.write_text("previous bundle", encoding="utf-8")

    seen = {}
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        seen["destination"] = Path(dst).read_text(encoding="utf-8")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    res = cont.handoff_thread(store, sessions=rows, goal="g",
                              style="continuation", output=out)

    assert res["context_path"] == str(out.resolve())
    assert seen.get("destination") == "previous bundle", (
        "handoff_thread wrote the bundle in place: a concurrent read could "
        "have seen a partial file")
    assert out.read_text(encoding="utf-8").startswith("# Continuation Bundle")
    assert list(tmp_path.glob("*.tmp-*")) == []


# --------------------------------------------------------------------------
# Output isolation: the bundle directory must be redirectable, and the default
# must not change.  `--db` scopes the index; it has never scoped the output, and
# a run pointed entirely at a scratch index still wrote into the real
# ~/.voyager/bundles/ because get_bundles_dir() is anchored to HOME.  These
# tests assert on the *real filesystem*, not on the returned path string: a
# function that reports the right path while writing somewhere else is exactly
# the failure being guarded against.
# --------------------------------------------------------------------------

#: The developer's actual home, captured at import time — during collection,
#: before conftest's autouse fixture redirects HOME.  Same trick as
#: tests/test_isolation.py, and for the same reason: after the fixture runs,
#: "the real home" is no longer discoverable from the environment.
REAL_HOME = Path(os.path.expanduser("~"))


def _files_under(root: Path) -> set:
    if not root.is_dir():
        return set()
    return {p for p in root.rglob("*") if p.is_file()}


def test_an_explicit_bundles_dir_receives_the_bundle(synthetic_trio, tmp_path):
    """The flag has to actually move the file, not just the reported path."""
    store, rows = synthetic_trio
    target_dir = tmp_path / "isolated-bundles"

    res = cont.handoff_thread(store, sessions=rows, goal="g",
                              style="continuation",
                              bundles_dir=str(target_dir))

    assert res["action"] == "bundle", res.get("error")
    written = Path(res["context_path"])
    assert written.parent == target_dir.resolve(), (
        "the bundle did not land in the requested directory")
    assert written.is_file()
    assert written.read_text(encoding="utf-8").startswith("# Continuation Bundle")
    # and nowhere else: the directory holds exactly the one new file
    assert _files_under(target_dir) == {written}


def test_an_explicit_output_still_beats_bundles_dir(synthetic_trio, tmp_path):
    """Naming the file is more specific than naming the directory."""
    store, rows = synthetic_trio
    bundles_dir = tmp_path / "dir-should-lose"
    exact = tmp_path / "exact-wins.md"

    res = cont.handoff_thread(store, sessions=rows, goal="g",
                              style="continuation",
                              output=exact, bundles_dir=str(bundles_dir))

    assert res["context_path"] == str(exact.resolve())
    assert exact.is_file()
    assert not bundles_dir.exists(), (
        "bundles_dir was created even though --output named the file")


def test_bundles_dir_applies_to_packages_too(synthetic_trio, tmp_path):
    """One flag has to isolate a whole run, not only its multi-session half.

    A package's documented default is the *current directory*; without this,
    `--bundles-dir` would redirect bundles and silently leave packages dropping
    into whatever cwd the process had.
    """
    store, rows = synthetic_trio
    target_dir = tmp_path / "packages"

    res = cont.handoff_thread(store, sessions=rows[:1], goal="g",
                              style="package", target="claude",
                              bundles_dir=str(target_dir))

    assert res["style"] == "package", res.get("error")
    written = Path(res["context_path"])
    assert written.parent == target_dir.resolve()
    assert written.is_file()


def test_the_default_location_is_unchanged_when_nothing_is_passed(
        synthetic_trio, monkeypatch, tmp_path):
    """Backward compatibility: no flag, no env var, same place as before."""
    store, rows = synthetic_trio
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("VOYAGER_BUNDLES_DIR", raising=False)
    # get_bundles_dir() reads HOME at call time, so it follows the patch
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    res = cont.handoff_thread(store, sessions=rows, goal="g",
                              style="continuation")

    written = Path(res["context_path"])
    assert written.parent == home / ".voyager" / "bundles", (
        "the default bundle directory moved")
    assert written.is_file()


def test_voyager_bundles_dir_env_redirects_without_any_flag(
        synthetic_trio, monkeypatch, tmp_path):
    """The escape hatch for callers that have no CLI flags — MCP, hooks, CI."""
    store, rows = synthetic_trio
    env_dir = tmp_path / "from-env"
    monkeypatch.setenv("VOYAGER_BUNDLES_DIR", str(env_dir))

    res = cont.handoff_thread(store, sessions=rows, goal="g",
                              style="continuation")

    written = Path(res["context_path"])
    assert written.parent == env_dir.resolve()
    assert written.is_file()


def test_an_uncreatable_bundles_dir_is_an_error_not_a_fallback(
        synthetic_trio, tmp_path):
    """Never silently write somewhere else when the caller's path fails.

    The failure is a *file* where a directory was asked for, which is the
    realistic version of this: a typo, or a stale path from another tool.
    """
    store, rows = synthetic_trio
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("I am a file", encoding="utf-8")

    res = cont.handoff_thread(store, sessions=rows, goal="g",
                              style="continuation",
                              bundles_dir=str(blocker / "bundles"))

    assert res["action"] == "error", res
    assert res["exit_code"] == 2
    assert "cannot create bundle directory" in (res["error"] or "")
    assert res["context_path"] is None, (
        "a failed write still reported a path — it may have written elsewhere")


def test_the_real_home_gains_nothing_from_an_isolated_run(
        synthetic_trio, tmp_path):
    """The whole point, asserted against the real filesystem.

    This is the regression for the actual incident: a run pointed at a scratch
    index wrote four bundles into the real ~/.voyager/bundles/ because nothing
    tied the output directory to the run.
    """
    store, rows = synthetic_trio
    real_bundles = REAL_HOME / ".voyager" / "bundles"
    before = _files_under(real_bundles)

    res = cont.handoff_thread(store, sessions=rows, goal="g",
                              style="continuation",
                              bundles_dir=str(tmp_path / "out"))
    assert res["action"] == "bundle", res.get("error")

    after = _files_under(real_bundles)
    assert after == before, (
        "an isolated run added files to the real bundles directory: "
        f"{sorted(str(p) for p in after - before)}")


def test_two_isolated_runs_in_the_same_second_stay_unique(synthetic_trio, tmp_path):
    """Same-second uniqueness must survive the redirect.

    The default name carries a per-invocation suffix precisely so two merges in
    one second do not collide; the redirect must not defeat that by, say,
    reusing one fixed name.
    """
    store, rows = synthetic_trio
    target_dir = tmp_path / "same-second"

    paths = {cont.handoff_thread(store, sessions=rows, goal="g",
                                 style="continuation",
                                 bundles_dir=str(target_dir))["context_path"]
             for _ in range(5)}

    assert len(paths) == 5, f"same-second runs collided: {paths}"
    assert len(_files_under(target_dir)) == 5


def test_isolated_writes_stay_atomic_and_leave_no_staging_files(
        synthetic_trio, tmp_path):
    """The redirect must not bypass _write_atomic."""
    store, rows = synthetic_trio
    target_dir = tmp_path / "atomic"
    target_dir.mkdir()
    existing = target_dir / cont.default_bundle_name(rows)
    existing.write_text("previous bundle", encoding="utf-8")

    seen = {}
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        seen["destination"] = Path(dst).read_text(encoding="utf-8")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(os, "replace", spy)
        res = cont.handoff_thread(store, sessions=rows, goal="g",
                                  style="continuation",
                                  output=existing)
    finally:
        monkeypatch.undo()

    assert res["action"] == "bundle", res.get("error")
    assert seen.get("destination") == "previous bundle", (
        "the isolated path wrote in place instead of staging a temp file")
    assert list(target_dir.glob("*.tmp-*")) == []
    assert existing.read_text(encoding="utf-8").startswith("# Continuation Bundle")


def test_the_cli_forwards_bundles_dir_to_the_engine(synthetic_trio, tmp_path,
                                                    capsys):
    """The engine parameter is useless if no entry point passes it.

    Every engine test above calls `handoff_thread` directly, so all of them stay
    green if `--bundles-dir` is parsed and then dropped on the floor. This is the
    test that fails in that case, and it asserts on the filesystem rather than on
    the printed path.
    """
    from voyager.cli import main

    db = tmp_path / "continuity_test.db"
    assert db.is_file(), "synthetic_trio's index moved; fix this path"
    target_dir = tmp_path / "cli-bundles"
    real_bundles = REAL_HOME / ".voyager" / "bundles"
    before = _files_under(real_bundles)

    rc = main(["--db", str(db), "merge", "sess-1", "sess-2",
               "--bundles-dir", str(target_dir), "--json"])
    capsys.readouterr()

    assert rc == 0
    written = _files_under(target_dir)
    assert len(written) == 1, f"expected exactly one bundle in the target dir: {written}"
    assert next(iter(written)).read_text(encoding="utf-8").startswith(
        "# Continuation Bundle")
    assert _files_under(real_bundles) == before, (
        "the CLI parsed --bundles-dir but wrote into the real bundles directory")


def test_the_cli_output_flag_still_beats_bundles_dir(synthetic_trio, tmp_path,
                                                     capsys):
    """Precedence, through the real entry point rather than the engine."""
    from voyager.cli import main

    db = tmp_path / "continuity_test.db"
    exact = tmp_path / "exact-from-cli.md"
    never = tmp_path / "dir-should-lose"

    rc = main(["--db", str(db), "merge", "sess-1", "sess-2",
               "--output", str(exact), "--bundles-dir", str(never), "--json"])
    capsys.readouterr()

    assert rc == 0
    assert exact.is_file(), "--output did not win over --bundles-dir"
    assert not never.exists(), "the losing --bundles-dir was created anyway"


