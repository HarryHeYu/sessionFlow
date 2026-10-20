"""The isolation guard must actually fire.

`tests/iso_guard.py` refuses a test that is about to hand real agent data to a
scan, a reader or a writer.  A guard that never triggers is indistinguishable
from no guard at all, so every branch below proves the opposite: given a path
that *is* real data, the guard refuses; given a path that only looks dangerous
(system executables, pytest's own temp root, the bare home), it stays quiet.

Nothing here reads the developer's sessions.  The "protected" directories are
synthetic -- `real_data_roots()` is monkeypatched to point at a fabricated tree
inside ``tmp_path`` -- so the test asserts the predicate's behaviour, never its
targets.  Testing the guard by scanning the real thing would be the exact
accident this module exists to prevent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iso_guard  # noqa: E402  (path set up above for conftest-free importing)


@pytest.fixture
def fake_protected(tmp_path, monkeypatch):
    """A synthetic stand-in for the developer's agent data.

    ``iso_guard.real_data_roots()`` normally answers for the real profile,
    which is precisely what must not be read or written here.  Pointing it at a
    fabricated tree lets the predicate be driven through every branch without
    going near a real session.
    """
    profile = tmp_path / "profile"
    (profile / ".codex" / "sessions").mkdir(parents=True)
    (profile / ".claude" / "projects").mkdir(parents=True)
    appdata = tmp_path / "appdata"
    (appdata / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent").mkdir(
        parents=True)

    roots = (profile / ".codex", profile / ".claude",
             appdata / "Kiro")
    monkeypatch.setattr(iso_guard, "real_data_roots", lambda: roots)
    return profile, appdata


# ---------------------------------------------------------------------------
# refusal: paths the guard must reject
# ---------------------------------------------------------------------------

def test_a_real_session_directory_is_refused(fake_protected):
    profile, _ = fake_protected
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_not_real_data(profile / ".codex" / "sessions",
                                      "scan root")


def test_a_data_root_itself_is_refused(fake_protected):
    profile, _ = fake_protected
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_not_real_data(profile / ".codex")


def test_a_deeper_path_inside_a_root_is_refused(fake_protected):
    profile, _ = fake_protected
    target = profile / ".codex" / "sessions" / "rollout-2026-01-01.jsonl"
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_scan_roots(target)


def test_a_file_that_does_not_exist_yet_is_refused(fake_protected):
    """The path a scan would *create* is as real as the ones it would read.

    Guarding only existing paths would approve ``~/.codex/sessions/new.jsonl``,
    which is the whole point of the write side.
    """
    profile, _ = fake_protected
    missing = profile / ".codex" / "sessions" / "not-written-yet.jsonl"
    assert not missing.exists()
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_scan_roots(missing)


def test_a_deeply_missing_chain_inside_a_root_is_refused(fake_protected):
    """No ancestor of the leaf exists -- only the root does.

    ``_nearest_existing`` walks up from the target to the first real directory,
    so a chain four levels deep must still land on the root rather than being
    waved through as "nonexistent, therefore safe".
    """
    profile, _ = fake_protected
    deep = profile / ".codex" / "sessions" / "a" / "b" / "c" / "new.jsonl"
    assert not deep.exists()
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_scan_roots(deep)


def test_dot_dot_that_stays_inside_the_root_is_refused(fake_protected):
    profile, _ = fake_protected
    # built with the platform's own separator: a hardcoded "\\" is a separator
    # on Windows and an ordinary filename character on POSIX, so a test that
    # hardcodes one passes here and fails on the Linux runner for a reason
    # that has nothing to do with the guard.
    inside = os.path.join(str(profile / ".codex"), "sessions", "..", "x.jsonl")
    assert iso_guard.is_inside_real_data(inside), (
        "`sessions/../x.jsonl` normalizes back inside `.codex`")


def test_dot_dot_escaping_the_root_is_not_refused(fake_protected):
    """The negative half: a path that genuinely leaves the root is allowed.

    Without this, the normalization test could be satisfied by a guard that
    simply refuses anything containing ``..``.
    """
    profile, _ = fake_protected
    outside = os.path.join(str(profile / ".codex"), "sessions", "..", "..",
                           "elsewhere", "x.jsonl")
    assert not iso_guard.is_inside_real_data(outside), (
        "this path leaves every data root; refusing it would be a false "
        "positive, and a guard that cries wolf gets switched off")


def test_a_relative_path_is_resolved_before_judged(fake_protected, tmp_path,
                                                   monkeypatch):
    profile, _ = fake_protected
    monkeypatch.chdir(profile)
    # relative, escaping and re-entering -- must land inside the root
    rel = os.path.join(".codex", "sessions")
    assert iso_guard.is_inside_real_data(rel)


@pytest.mark.skipif(os.name != "nt",
                    reason="only true where the filesystem is "
                           "case-insensitive; the POSIX half is its own test")
def test_case_differences_do_not_smuggle_a_root_past_on_windows(
        fake_protected):
    """``C:\\\\USERS\\\\ME\\\\.CODEX`` is the same directory as its lowercase.

    Two halves, because the two platforms genuinely differ -- and a test that
    asserts the Windows rule everywhere fails on Linux for a reason that has
    nothing to do with the guard, which is exactly how this round's first CI
    failure happened (there it was a hardcoded backslash separator).
    """
    profile, _ = fake_protected
    shouted = str(profile / ".codex" / "sessions").upper()
    assert iso_guard.is_inside_real_data(shouted), (
        "a Windows path is case-insensitive; the guard must be too")


def test_on_posix_a_different_casing_is_a_different_directory(
        fake_protected):
    """The honest counterpart, and a false-positive guard.

    On a case-sensitive filesystem ``.CODEX`` is not ``.codex``.  A guard that
    "helpfully" case-folded on POSIX would refuse paths unrelated to the real
    profile -- and a guard that cries wolf gets switched off.
    """
    if os.name == "nt":
        pytest.skip("Windows is case-insensitive; covered by its own test")
    profile, _ = fake_protected
    shouted = str(profile / ".codex" / "sessions").upper()
    assert not iso_guard.is_inside_real_data(shouted), (
        "on a case-sensitive filesystem, casing creates a different directory")


@pytest.mark.skipif(not hasattr(os, "symlink"),
                    reason="symlinks unavailable on this platform")
def test_a_symlink_into_a_root_is_refused(fake_protected, tmp_path,
                                          monkeypatch):
    """A link planted in the temp tree is the obvious bypass.

    On hosts where the sandbox cannot create a real link, the *resolved*
    spelling branch is exercised directly below instead; a test that silently
    passes on a fake directory would prove nothing.
    """
    profile, _ = fake_protected
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    link = outside / "looks-innocent"
    try:
        link.symlink_to(profile / ".codex" / "sessions",
                        target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a symlink here")
    if not link.is_symlink():
        pytest.skip("symlink creation was not honoured on this host; the "
                    "resolved-spelling branch is covered by "
                    "test_the_resolved_spelling_of_a_path_is_checked")
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_scan_roots(link)


def test_the_resolved_spelling_of_a_path_is_checked(fake_protected,
                                                    monkeypatch):
    """A harmless literal path whose *resolution* is real data must be refused.

    This is the branch a symlink takes: ``resolve()`` follows it, so the guard's
    second spelling is the damning one.  Driving it by patching the resolve
    step keeps the test independent of whether the host can make links.
    """
    profile, _ = fake_protected
    innocent = Path("E:/sessionflow-scratch/p28/harmless-looking")
    monkeypatch.setattr(iso_guard, "_nearest_existing",
                        lambda p: profile / ".codex" / "sessions")
    assert iso_guard.is_inside_real_data(innocent), (
        "the literal spelling is outside every root; the resolved one is not")


# ---------------------------------------------------------------------------
# refusal on the environment side
# ---------------------------------------------------------------------------

def test_an_unredirected_home_fails_closed(fake_protected, monkeypatch):
    """HOME naming the real profile is what makes ``$HOME/.codex`` real.

    The variable's own spelling is harmless -- it is the directory the adapters
    build underneath it that the guard has to see.
    """
    profile, _ = fake_protected
    monkeypatch.setenv("HOME", str(profile))
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_env()


def test_an_unredirected_appdata_fails_closed(fake_protected, monkeypatch):
    """The Kiro incident: HOME and USERPROFILE redirected, APPDATA forgotten."""
    _, appdata = fake_protected
    monkeypatch.setenv("HOME", str(Path.home() / "temp-elsewhere"))
    monkeypatch.setenv("APPDATA", str(appdata))
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_env()


def test_localappdata_is_guarded_too(fake_protected, monkeypatch):
    profile, _ = fake_protected
    monkeypatch.setenv("HOME", "/somewhere/else")
    monkeypatch.setenv("LOCALAPPDATA", str(profile / ".codex"))
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_env()


def test_a_fully_redirected_environment_passes(fake_protected, monkeypatch):
    """The negative half: the harness's own redirection must not be refused."""
    profile, _ = fake_protected
    elsewhere = Path(profile).parent / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    for name in iso_guard._DISCOVERY_ENV:
        monkeypatch.setenv(name, str(elsewhere))
    iso_guard.guard_env()          # must not raise


# ---------------------------------------------------------------------------
# no false positives -- a guard that cries wolf gets disabled
# ---------------------------------------------------------------------------

def test_the_bare_home_is_not_a_data_root(fake_protected):
    profile, _ = fake_protected
    assert not iso_guard.is_inside_real_data(profile), (
        "the home holds temp dirs, interpreters and the checkout's siblings; "
        "rejecting it would fail CI for a reason unrelated to isolation")


def test_a_system_executable_is_not_a_data_root(fake_protected):
    """Tests legitimately use the real Python, Git, Node and browsers."""
    profile, _ = fake_protected
    interpreter = Path(sys.executable)
    assert not iso_guard.is_inside_real_data(interpreter)


def test_a_sibling_of_the_profile_is_not_a_data_root(fake_protected):
    profile, _ = fake_protected
    sibling = profile.parent / "unrelated-project"
    assert not iso_guard.is_inside_real_data(sibling)


def test_empty_and_none_are_not_data_roots(fake_protected):
    assert not iso_guard.is_inside_real_data(None)
    assert not iso_guard.is_inside_real_data("")
    assert iso_guard.guard_not_real_data(None, "db") is None
