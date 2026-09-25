"""CLI tests: scan (incl. idempotency + pruning), list/show/search/repo,
export, continue, brief and the guards around session resolution.

Everything runs through ``voyager.cli.main`` with an explicit ``--db`` in
tmp_path — the real index at ~/.voyager/index.db is never touched, and no
agent process is ever launched (the launch-resolution tests below run a fake
CLI of their own, never a real provider).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from voyager import cli as cli_mod
from voyager.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]

CODEX_NATIVE = "11111111-2222-3333-4444-555555555555"
ZCODE_NATIVE = "sess_z9"


# --- scan ------------------------------------------------------------------

def test_scan_indexes_codex_fixture(adapter_of, patch_paths, codex_fixture,
                                    tmp_path, capsys):
    patch_paths(adapter_of("codex"), SESSIONS_DIR=codex_fixture)
    db = tmp_path / "index.db"
    assert main(["--db", str(db), "scan", "--platform", "codex"]) == 0
    out = capsys.readouterr().out
    assert "1 sessions, 5 events" in out
    assert "codex: 1 sessions indexed" in out


def test_scan_is_idempotent(adapter_of, patch_paths, codex_fixture, tmp_path,
                            capsys):
    patch_paths(adapter_of("codex"), SESSIONS_DIR=codex_fixture)
    db = tmp_path / "index.db"
    main(["--db", str(db), "scan", "--platform", "codex"])
    capsys.readouterr()
    assert main(["--db", str(db), "scan", "--platform", "codex"]) == 0
    out = capsys.readouterr().out
    assert "1 sessions, 5 events" in out      # no duplication
    assert "unchanged: 1" in out              # source fingerprint honoured

    data = json.loads(_run(capsys, ["--db", str(db), "stats"]))
    assert data == {"sessions": 1, "events": 5, "by_provider": {"codex": 1}}


def test_scan_prunes_sessions_whose_source_vanished(adapter_of, patch_paths,
                                                    codex_fixture, tmp_path,
                                                    capsys):
    ad = adapter_of("codex")
    patch_paths(ad, SESSIONS_DIR=codex_fixture)
    db = tmp_path / "index.db"
    main(["--db", str(db), "scan", "--platform", "codex"])
    capsys.readouterr()

    for f in list(ad.discover()):
        f.unlink()          # the agent deleted its session file
    assert main(["--db", str(db), "scan", "--platform", "codex"]) == 0
    out = capsys.readouterr().out
    assert "pruned 1 vanished session(s)" in out
    assert json.loads(_run(capsys, ["--db", str(db), "stats"]))["sessions"] == 0


# --- read commands ---------------------------------------------------------

def _run(capsys, argv):
    """Run the CLI and return its stdout (asserting a clean exit)."""
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 0, f"{argv} exited {rc}"
    return out


def test_list_shows_both_sessions(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "list"])
    assert CODEX_NATIVE in out and ZCODE_NATIVE in out
    assert "fix the parser" in out and "2 session(s)" in out


def test_list_json_and_platform_filter(indexed_store, capsys):
    rows = json.loads(_run(capsys, ["--db", str(indexed_store.db_path),
                                    "list", "--json"]))
    assert {r["provider"] for r in rows} == {"codex", "zcode"}
    only = json.loads(_run(capsys, ["--db", str(indexed_store.db_path),
                                    "list", "--platform", "zcode", "--json"]))
    assert len(only) == 1 and only[0]["native_id"] == ZCODE_NATIVE
    none = _run(capsys, ["--db", str(indexed_store.db_path), "list",
                         "--repo", "no-such-repo"])
    assert "no sessions" in none


def test_show_renders_timeline(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "show",
                        CODEX_NATIVE[:12]])
    assert "codex:11111111-2222-3333-4444-555555555555" in out
    assert "USER" in out and "ASST" in out and "TOOL>" in out
    assert "python -m pytest -q" in out
    assert "exit=0" in out
    assert "resume  : codex resume" in out


def test_show_json(indexed_store, capsys):
    data = json.loads(_run(capsys, ["--db", str(indexed_store.db_path), "show",
                                    CODEX_NATIVE, "--json"]))
    assert data["session"]["native_id"] == CODEX_NATIVE
    assert len(data["events"]) == 6


def test_show_ambiguous_prefix_lists_candidates(indexed_store, capsys):
    with pytest.raises(SystemExit) as e:
        main(["--db", str(indexed_store.db_path), "show", ""])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "matches 2 sessions" in err and "use a longer prefix" in err


def test_show_missing_session_exits_2(indexed_store, capsys):
    with pytest.raises(SystemExit) as e:
        main(["--db", str(indexed_store.db_path), "show", "nope-nope"])
    assert e.value.code == 2
    assert "session not found" in capsys.readouterr().err


def test_search_finds_events_across_sessions(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "search", "pytest"])
    assert CODEX_NATIVE in out and "1 hit(s)" in out
    assert "no matches" in _run(capsys, ["--db", str(indexed_store.db_path),
                                         "search", "zzzznotfound"])


def test_search_json_and_cjk_substring(indexed_store, capsys):
    hits = json.loads(_run(capsys, ["--db", str(indexed_store.db_path),
                                    "search", "pytest", "--json"]))
    assert hits[0]["native_id"] == CODEX_NATIVE
    # trigram FTS must match CJK substrings, not just whole tokens
    cjk = json.loads(_run(capsys, ["--db", str(indexed_store.db_path),
                                   "search", "架构图", "--json"]))
    assert cjk and cjk[0]["provider"] == "zcode"


def test_search_survives_fts5_operator_characters(indexed_store, capsys):
    """'-', ':' and quotes are text to a user, syntax to FTS5 — never an error."""
    hits = json.loads(_run(capsys, ["--db", str(indexed_store.db_path),
                                    "search", "pytest -q", "--json"]))
    assert hits and hits[0]["native_id"] == CODEX_NATIVE
    assert "no matches" in _run(capsys, ["--db", str(indexed_store.db_path),
                                         "search", "zzz-no-such-text"])
    assert "no matches" in _run(capsys, ["--db", str(indexed_store.db_path),
                                         "search", '"unbalanced'])
    assert "search error" not in capsys.readouterr().err


def test_repo_timeline_groups_by_repo(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "repo", "demo"])
    assert "E:/proj/demo" in out and "fix the parser" in out
    assert "no sessions matched repo pattern" in _run(
        capsys, ["--db", str(indexed_store.db_path), "repo", "nope"])


def test_stats(indexed_store, capsys):
    data = json.loads(_run(capsys, ["--db", str(indexed_store.db_path), "stats"]))
    assert data["sessions"] == 2 and data["events"] == 7
    assert data["by_provider"] == {"codex": 1, "zcode": 1}


def test_db_flag_accepted_on_either_side(indexed_store, capsys):
    """`voyager --db X stats` and `voyager stats --db X` must both work."""
    before = json.loads(_run(capsys, ["--db", str(indexed_store.db_path), "stats"]))
    after = json.loads(_run(capsys, ["stats", "--db", str(indexed_store.db_path)]))
    assert before == after


def test_isolation_guard_redirects_real_paths(_isolated_from_the_real_machine,
                                              tmp_path):
    """Guard test for the autouse isolation fixture (see conftest).

    A pathless Store, the cwd and every adapter's storage root must all point
    into the temp tree, so no test can read or write real agent data.
    """
    import voyager.store as store_mod
    from voyager.adapters.base import all_adapters
    from voyager.adapters import load_all
    import importlib

    assert not _isolated_from_the_real_machine.exists()   # "no agent storage"
    default_db = store_mod.default_db_path()
    assert ".voyager" not in str(default_db)
    assert store_mod.Store().db_path == default_db
    assert Path.cwd().parent == tmp_path.parent           # cwd is a temp dir

    load_all()
    for ad in all_adapters():
        mod = importlib.import_module(type(ad).__module__)
        for name in ("SESSIONS_DIR", "PROJECTS_DIR", "DB_PATH", "VSCDB",
                     "CONV_DIR", "FILE_HISTORY_DIR"):
            value = getattr(mod, name, None)
            if value is not None:
                assert "no-agent-storage" in str(value), f"{ad.provider}.{name}"


def test_files_reports_absent_file_history(indexed_store, capsys):
    """Codex persists no file history: say so instead of printing nothing."""
    out = _run(capsys, ["--db", str(indexed_store.db_path), "files",
                        CODEX_NATIVE])
    assert "no file history recorded" in out


def test_scan_claude_then_files_lists_backups(adapter_of, patch_paths,
                                              claude_fixture, tmp_path, capsys):
    """End-to-end: Claude project JSONL -> index -> `voyager files`."""
    patch_paths(adapter_of("claude"), PROJECTS_DIR=claude_fixture / "projects")
    db = tmp_path / "index.db"
    assert main(["--db", str(db), "scan", "--platform", "claude"]) == 0
    assert "claude: 1 sessions indexed" in capsys.readouterr().out

    out = _run(capsys, ["--db", str(db), "files", "cla-1111"])
    assert "E:/proj/demo/a.py" in out
    assert "(backup: hash1@v1)" in out


# --- export / continue / brief --------------------------------------------

def test_export_command_writes_both_formats(indexed_store, tmp_path, capsys):
    md = tmp_path / "out.md"
    js = tmp_path / "out.json"
    _run(capsys, ["--db", str(indexed_store.db_path), "export", CODEX_NATIVE,
                  "--format", "md", "-o", str(md)])
    _run(capsys, ["--db", str(indexed_store.db_path), "export", CODEX_NATIVE,
                  "--format", "json", "-o", str(js)])
    assert md.read_text(encoding="utf-8").startswith("# fix the parser")
    assert json.loads(js.read_text(encoding="utf-8"))["session"]["provider"] == "codex"


def test_continue_prints_native_resume_for_newest(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "continue"])
    assert "latest session: [codex]" in out
    assert f"$ codex resume {CODEX_NATIVE}" in out
    assert "add --launch to start it now" in out


def test_continue_falls_back_to_handoff_without_native_resume(
        indexed_store, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    out = _run(capsys, ["--db", str(indexed_store.db_path), "continue",
                        ZCODE_NATIVE])
    assert "native resume unsupported for 'zcode'" in out
    pkg = tmp_path / "handoff-zcode-sess_z9.md"
    assert pkg.is_file(), "fallback must leave a context package behind"
    assert "给 README 加个架构图" in pkg.read_text(encoding="utf-8")
    assert "$ claude" in out          # default fallback target


def test_continue_repo_filter(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "continue",
                        "--repo", "voyager"])
    assert "latest session: [zcode]" in out
    assert "native resume unsupported" in out
    # the fallback package goes to the cwd (a tmp dir, see conftest), never
    # into the checkout
    pkg = Path("handoff-zcode-sess_z9.md")
    assert pkg.is_file() and "架构图" in pkg.read_text(encoding="utf-8")


def test_continue_on_empty_index_is_not_an_error(tmp_path, capsys):
    rc = main(["--db", str(tmp_path / "empty.db"), "continue"])
    assert rc == 1
    assert "no sessions to continue" in capsys.readouterr().out


def test_brief_digest(indexed_store, capsys):
    out = _run(capsys, ["--db", str(indexed_store.db_path), "brief",
                        "--hours", "1000000"])
    assert "agent activity, last 1000000.0h (2 sessions" in out
    assert "fix the parser" in out and "给 README 加个架构图" in out
    assert "(2 msgs / 1 tools)" in out
    empty = _run(capsys, ["--db", str(indexed_store.db_path), "brief",
                          "--hours", "0.000001"])
    assert "no sessions updated in the last" in empty


# --- integrate -------------------------------------------------------------

def _fake_home(tmp_path):
    """A throwaway HOME that already has `.claude`.

    `install_integration` bails out with "provider not installed" when the
    provider's config directory is absent, so it must exist for the hook path
    to be exercised.  Using `--home` also guarantees the real `~/.claude` is
    never touched by the suite.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    return home


def test_integrate_install_registers_native_hook(tmp_path, capsys):
    home = _fake_home(tmp_path)
    assert main(["integrate", "install", "claude", "--home", str(home)]) == 0
    assert "hook: installed" in capsys.readouterr().out

    cfg = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
    entries = cfg["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["matcher"] == "startup"
    assert entries[0]["hooks"][0]["type"] == "command"


def test_integrate_remove_does_not_crash(tmp_path, capsys):
    """Regression: `integrate remove` read `args.json` while its subparser
    never defined `--json`, so *every* removal died with AttributeError before
    removing anything."""
    home = _fake_home(tmp_path)
    assert main(["integrate", "install", "claude", "--home", str(home)]) == 0
    capsys.readouterr()

    assert main(["integrate", "remove", "claude", "--home", str(home)]) == 0
    assert "hook: removed" in capsys.readouterr().out

    cfg = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
    assert "SessionStart" not in cfg.get("hooks", {})


def test_integrate_subcommands_accept_json(tmp_path, capsys):
    home = _fake_home(tmp_path)
    for argv in (
        ["integrate", "install", "claude", "--home", str(home), "--json"],
        ["integrate", "status", "claude", "--home", str(home), "--json"],
        ["integrate", "remove", "claude", "--home", str(home), "--json"],
    ):
        assert main(argv) == 0, argv
        assert json.loads(capsys.readouterr().out), argv


def test_install_and_status_agree_on_startup_status(tmp_path):
    """Regression: `install_integration` and `check_integration_status` computed
    `startup_status` from *different* fields -- install off `has_startup_hook`,
    status off `mcp_enabled`.  For Codex that meant `integrate install codex`
    printed `N` while `integrate status` printed `A` on the very same machine.
    The two paths must agree for every provider.
    """
    from voyager.skill import (
        PROVIDER_CONFIG,
        check_integration_status,
        install_integration,
    )

    home = tmp_path / "home"
    for provider in PROVIDER_CONFIG:
        (home / f".{provider}").mkdir(parents=True, exist_ok=True)

    for provider in PROVIDER_CONFIG:
        install_result = install_integration(provider, force=True, home=home)
        reported = {
            entry["provider"]: entry
            for entry in check_integration_status(providers=[provider], home=home)
        }[provider]
        assert install_result["startup_status"] == reported["startup_status"], (
            f"{provider}: install said {install_result['startup_status']!r} "
            f"but status said {reported['startup_status']!r}"
        )


def test_startup_status_letters_match_the_legend(tmp_path, monkeypatch):
    """Pin the legend so a refactor cannot silently reshuffle it.

    `H` is reserved for a registered native hook; a provider with no hook
    surface must never report `H`, and nothing reports `Y` because no provider
    has a persisted live-evidence record.  Claude Code and Grok CLI both
    register a native `SessionStart` hook today.
    """
    from voyager.skill import (
        PROVIDER_CONFIG,
        check_integration_status,
        install_integration,
    )

    home = tmp_path / "home"
    for provider in PROVIDER_CONFIG:
        (home / f".{provider}").mkdir(parents=True, exist_ok=True)

    # Grok's installer only writes its native hook once the real binary
    # resolves, so point `which` at a stand-in: the letter must not depend on
    # this machine happening to have Grok installed.
    real_which = shutil.which
    monkeypatch.setattr(
        "shutil.which",
        lambda name, *args, **kwargs: (
            "/opt/grok/bin/grok" if name == "grok"
            else real_which(name, *args, **kwargs)),
    )

    seen = {}
    for provider in PROVIDER_CONFIG:
        install_integration(provider, force=True, home=home)
        entry = {
            s["provider"]: s
            for s in check_integration_status(providers=[provider], home=home)
        }[provider]
        seen[provider] = entry["startup_status"]

    # Both register a real native hook, so both report `H` -- never `Y`, because
    # nothing has observed the provider firing it.
    for provider in ("claude", "grok"):
        assert seen[provider] == "H", (provider, seen[provider])
    # No hook surface => cannot be `H` or `Y`.
    assert seen["dsh"] == "N", seen["dsh"]
    assert seen["codex"] in {"A", "N"}, seen["codex"]
    # `Y` needs a persisted live-evidence record; nothing may report it yet.
    assert "Y" not in seen.values(), seen


def test_install_threads_home_into_mcp_detection(tmp_path, monkeypatch):
    """Regression: `install_integration` called `_check_mcp_support(provider)`
    without `home`, so MCP detection read the *real* user profile even when the
    caller asked for a different one.

    This test is deliberately a spy rather than an end-to-end assertion, because
    the end-to-end version passed for the wrong reason: with the real profile
    consulted, the "not yet registered" branch fired and registered the server,
    which looks like success while the already-registered branch was never
    exercised at all.
    """
    from voyager import skill

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)

    seen = []
    real = skill._check_mcp_support

    def spy(provider, home=None):
        seen.append(home)
        return real(provider, home)

    monkeypatch.setattr(skill, "_check_mcp_support", spy)
    skill.install_integration("codex", force=True, home=home)

    assert seen, "_check_mcp_support was never called"
    assert all(h == home for h in seen), (
        f"install consulted the wrong profile: {seen}"
    )


def test_already_registered_mcp_is_reported_as_registered(tmp_path, monkeypatch):
    """Regression: an already-registered MCP server was reported as `available`
    instead of `registered`, which then downgraded `startup_status` to `N` on an
    idempotent re-run -- while `integrate status` still said `A`."""
    from voyager import skill

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)

    monkeypatch.setattr(
        skill, "_check_mcp_support",
        lambda provider, home=None: (True, "Voyager MCP already registered"),
    )
    result = skill.install_integration("codex", force=True, home=home)

    assert result["mcp"]["status"] == "registered"
    assert result["startup_status"] == "A"


def test_already_registered_mcp_reports_assisted_on_both_paths(tmp_path):
    """End-to-end form of the above, with real files instead of a stub: a
    scratch profile whose Codex config already names Voyager must give the same
    `startup_status` from `install` and from `status`."""
    from voyager.skill import check_integration_status, install_integration

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text(
        '[mcp_servers.voyager]\ncommand = "voyager"\n', encoding="utf-8"
    )

    install_result = install_integration("codex", force=True, home=home)
    status_entry = {
        s["provider"]: s
        for s in check_integration_status(providers=["codex"], home=home)
    }["codex"]

    # The message proves the already-registered branch was the one taken, which
    # is only possible if `home` reached `_check_mcp_support`.
    assert "already registered" in install_result["mcp"]["message"].lower()
    assert install_result["mcp"]["status"] == "registered"
    assert install_result["startup_status"] == "A"
    assert status_entry["startup_status"] == "A"


def test_check_mcp_support_honours_the_home_argument(tmp_path):
    """Regression: `_check_mcp_support` hardcoded `Path.home()`, so `--home`
    runs silently inspected the real user profile instead of the one given."""
    from voyager.skill import _check_mcp_support

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text(
        '[mcp_servers.voyager]\ncommand = "voyager"\n', encoding="utf-8"
    )

    supported, message = _check_mcp_support("codex", home)
    assert supported is True
    assert "already registered" in message.lower()


def test_install_does_not_spawn_a_provider_cli_for_a_scratch_home(tmp_path, monkeypatch):
    """`integrate install --home <scratch>` must not reach outside that home.

    The MCP step prefers the provider CLI (`claude mcp add`), which has no
    `--home` of its own -- it writes wherever the provider is configured to
    look, i.e. the real profile.  So with a scratch home the CLI must not be
    spawned at all and the file must be written directly instead.
    """
    from voyager import skill

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)

    spawned = []

    class _Completed:
        returncode = 1
        stdout = ""
        stderr = ""

    class _FakeSubprocess:
        TimeoutExpired = Exception

        @staticmethod
        def run(argv, *args, **kwargs):
            spawned.append(list(argv))
            return _Completed()

    # Force the registration branch.  Without this the test is environment
    # dependent: on a machine whose real profile *already* names Voyager, the
    # old code took its "already registered" branch and never reached
    # `_register_claude_mcp`, so the spawn would not happen even without a gate.
    monkeypatch.setattr(
        skill, "_check_mcp_support",
        lambda provider, home=None: (
            True, "Provider supports MCP; Voyager not yet registered"),
    )
    monkeypatch.setattr(skill, "subprocess", _FakeSubprocess)
    result = skill.install_integration("claude", force=True, home=home)

    assert spawned == [], f"a provider process was spawned: {spawned}"
    assert result["mcp"]["status"] == "registered"
    assert (home / ".claude/mcp.json").exists()


# --- path arguments from argv ----------------------------------------------
#
# `--home`, `--db` and `--output` receive what a shell would normally expand
# first, and people write `--home ~`.  Without `expanduser()` the `~` stays
# literal, `Path("~")` is *relative*, and the override resolves against the
# current working directory instead of the home directory.  That is how a
# stray `~/` directory appeared in this repo's root, holding a provider
# launcher that should have gone to `$HOME/.voyager/bin`.

def test_expand_path_args_expands_tilde():
    from argparse import Namespace
    from voyager.cli import _expand_path_args

    args = Namespace(db="~/index.db", home="~", output=None, cwd="~/repo")
    _expand_path_args(args)

    # compare via Path(): expanduser() substitutes the home prefix but keeps
    # whatever separator the input used ("~/x" -> "C:\\...\\isolated0/x").
    assert Path(args.db) == Path.home() / "index.db"
    assert Path(args.home) == Path.home()
    assert Path(args.cwd) == Path.home() / "repo"
    assert args.output is None                 # absent values are left alone


def test_expand_path_args_leaves_ordinary_values_untouched():
    from argparse import Namespace
    from voyager.cli import _expand_path_args

    args = Namespace(db="rel/index.db", home="", output="C:/abs/out.md")
    _expand_path_args(args)

    assert args.db == "rel/index.db"
    assert args.home == ""                     # empty stays empty, not None
    assert args.output == "C:/abs/out.md"


def test_expand_path_args_tolerates_missing_attributes():
    """`--db` may be absent entirely (the subparser copy uses SUPPRESS)."""
    from argparse import Namespace
    from voyager.cli import _expand_path_args

    args = Namespace(cmd="stats")
    _expand_path_args(args)                    # must not raise
    assert not hasattr(args, "db")


def test_home_flag_expands_tilde_for_status(monkeypatch, tmp_path):
    from voyager import skill

    seen = {}

    def spy(providers=None, home=None):
        seen["home"] = home
        return []

    monkeypatch.setattr(skill, "check_integration_status", spy)
    monkeypatch.chdir(tmp_path)

    assert main(["integrate", "status", "--home", "~"]) == 0
    assert seen["home"] == Path.home()


def test_home_flag_expands_tilde_for_install(monkeypatch, tmp_path):
    from voyager import skill

    seen = {}

    def spy(provider=None, force=False, home=None):
        seen["home"] = home
        return {"status": "ok"}

    monkeypatch.setattr(skill, "install_integration", spy)
    monkeypatch.chdir(tmp_path)

    assert main(["integrate", "install", "grok", "--home", "~"]) == 0
    assert seen["home"] == Path.home()


def test_db_flag_expands_tilde(monkeypatch):
    from voyager import api

    seen = {}

    def spy(db=None):
        seen["db"] = db

    monkeypatch.setattr(api, "serve", spy)

    assert main(["--db", "~/index.db", "api"]) == 0
    assert seen["db"] == Path.home() / "index.db"


def test_cwd_flag_expands_tilde_for_hook_startup(monkeypatch):
    """`--cwd` is consumed in `integrations/hook`, a different module from the
    one that parses it -- so the expansion has to happen before dispatch."""
    from voyager.integrations import hook as hook_mod

    seen = {}

    def spy(provider=None, cwd=None, **kwargs):
        seen["cwd"] = cwd
        return {"context": "", "status": "success"}

    monkeypatch.setattr(hook_mod, "startup_handler", spy)

    assert main(["hook", "startup", "--provider", "grok", "--cwd", "~"]) == 0
    assert seen["cwd"] == str(Path.home())


# --- launching a provider CLI ----------------------------------------------
#
# `resume_cmd` is a friendly string with a bare provider name (`claude --resume
# <id>`), and that string is printed and exported, so it must stay readable.
# But a bare name is not always launchable: on Windows these CLIs are npm
# `.cmd` shims and `subprocess` does not consult PATHEXT the way a shell does,
# so spawning the bare name raised FileNotFoundError / WinError 2 even though
# `shutil.which()` found it. `_spawn_argv`/`_launch` resolve it first.

FAKE_CLI = "voyager_fakecli_xyz"


def _install_fake_cli(bin_dir: Path, name: str = FAKE_CLI) -> Path:
    """Put a runnable `name` on PATH and return the file it resolves to."""
    if os.name == "nt":
        path = bin_dir / f"{name}.cmd"
        path.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
    else:
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    return path


def _prepend_to_path(monkeypatch, bin_dir: Path) -> None:
    monkeypatch.setenv(
        "PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))


def test_spawn_argv_resolves_a_cli_on_path(tmp_path, monkeypatch):
    """The bug: the bare name is not launchable, the resolved path is."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real = _install_fake_cli(bin_dir)
    _prepend_to_path(monkeypatch, bin_dir)

    argv = cli_mod._spawn_argv([FAKE_CLI, "resume", "abc"])
    assert Path(argv[0]) == real
    assert argv[1:] == ["resume", "abc"]


def test_spawn_argv_leaves_an_unresolvable_name_untouched():
    """Unknown names pass through, so the existing OSError handling reports them."""
    argv = [FAKE_CLI, "resume", "abc"]
    assert cli_mod._spawn_argv(argv) == argv


def test_spawn_argv_tolerates_an_empty_argv():
    assert cli_mod._spawn_argv([]) == []


def test_spawn_argv_does_not_mangle_an_absolute_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real = _install_fake_cli(bin_dir)

    argv = cli_mod._spawn_argv([str(real), "resume"])
    assert Path(argv[0]) == real
    assert argv[1:] == ["resume"]


@pytest.mark.skipif(
    os.name != "nt", reason=".cmd shims are Windows-specific")
def test_launch_starts_a_cmd_shim(tmp_path, monkeypatch):
    """Regression guard for the Windows failure, premise included.

    The premise assertion pins *why* `_launch` exists: spawning the bare name
    raises WinError 2. If that ever stops being true, this test says so and
    the helper can be reconsidered.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_fake_cli(bin_dir)
    _prepend_to_path(monkeypatch, bin_dir)

    with pytest.raises(FileNotFoundError):
        subprocess.call([FAKE_CLI])

    assert cli_mod._launch([FAKE_CLI, "--version"]) == 0


def test_provider_launches_go_through_the_resolving_helper():
    """Guard: no launch site may call `subprocess` directly again.

    Same reasoning as `_expand_path_args` -- a transformation that has to be
    repeated at every call site is one that will eventually be missed.
    """
    source = (REPO_ROOT / "voyager" / "cli.py").read_text(encoding="utf-8")
    assert source.count("subprocess.call(") == 1, (
        "a provider launch site bypasses _launch()")
    assert "return subprocess.call(_spawn_argv(argv))" in source
