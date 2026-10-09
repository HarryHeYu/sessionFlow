"""Codex native hooks integration tests (G3-B).

Mirrors the Claude integration contract: additive merge, idempotent install,
backup before rewrite, remove-only-Voyager, and the AGENTS.md managed block
that retires the startup-assisted wording.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from voyager.integrations.codex import (
    MANAGED_BEGIN,
    MANAGED_END,
    CodexIntegration,
)


@pytest.fixture
def integ(tmp_path):
    return CodexIntegration(home=tmp_path)


def _hooks_file(tmp_path):
    return tmp_path / ".codex/hooks.json"


def _write_hooks(tmp_path, config):
    f = _hooks_file(tmp_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(config), encoding="utf-8")
    return f


def test_install_creates_hooks_config(integ, tmp_path):
    result = integ.install()

    assert result["status"] == "installed"
    assert result["strategy"] == "NATIVE_SESSIONSTART_HOOK"
    f = _hooks_file(tmp_path)
    assert f.is_file()
    cfg = json.loads(f.read_text(encoding="utf-8"))
    voyager = cfg["hooks"]["SessionStart"][0]["hooks"][0]
    assert voyager["type"] == "command"
    # the command is the hidden relay: PowerShell dispatch cannot collect
    # pythonw stdout, and a console python pops a visible window
    assert "codex_hidden_relay.ps1" in voyager["command"]
    assert "codex_session_start.py" not in voyager["command"]
    assert voyager["commandWindows"] == voyager["command"]
    assert voyager["timeout"] == 30
    assert voyager["statusMessage"] == "Voyager continuity"


def test_install_is_additive_and_preserves_user_hooks(integ, tmp_path):
    _write_hooks(tmp_path, {
        "hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command", "command": "C:/bin/user-tool.exe"}]}],
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "C:/bin/prompt-tool.exe"}]}],
        }
    })

    result = integ.install()

    assert result["status"] == "installed"
    cfg = json.loads(_hooks_file(tmp_path).read_text(encoding="utf-8"))
    ss = cfg["hooks"]["SessionStart"]
    assert len(ss) == 2
    flat = ss[0]["hooks"][0]["command"]
    assert flat == "C:/bin/user-tool.exe"        # user hook untouched
    assert "codex_hidden_relay.ps1" in ss[1]["hooks"][0]["command"]
    assert cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == \
        "C:/bin/prompt-tool.exe"                 # other events untouched


def test_install_is_idempotent_and_backs_up(integ, tmp_path):
    integ.install()
    first = json.loads(_hooks_file(tmp_path).read_text(encoding="utf-8"))

    result = integ.install()

    assert result["status"] == "installed"
    assert result["hooks_replaced"] == 1         # replaced own entry, not duplicated
    assert result["backup"] and Path(result["backup"]).is_file()
    second = json.loads(_hooks_file(tmp_path).read_text(encoding="utf-8"))
    assert second == first                       # byte-identical after re-install
    backups = list((_hooks_file(tmp_path)).parent.glob("hooks.json.bak-*"))
    assert len(backups) >= 1


def test_install_rejects_malformed_hooks_with_backup(integ, tmp_path):
    f = _hooks_file(tmp_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json", encoding="utf-8")

    result = integ.install()

    assert result["status"] == "error"
    assert result["strategy"] == "FAILED_MALFORMED_CONFIG"
    assert result["backup"] and Path(result["backup"]).is_file()
    assert f.read_text(encoding="utf-8") == "{not json"  # original untouched


def test_remove_only_voyager_entry(integ, tmp_path):
    _write_hooks(tmp_path, {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "C:/bin/user.exe"}]},
                {"hooks": [{"type": "command",
                            "command": "C:/py/codex_session_start.py"}]},
            ],
        }
    })

    result = integ.remove()

    assert result["removed"] == 1
    cfg = json.loads(_hooks_file(tmp_path).read_text(encoding="utf-8"))
    ss = cfg["hooks"]["SessionStart"]
    assert len(ss) == 1
    assert ss[0]["hooks"][0]["command"] == "C:/bin/user.exe"  # user hook survives


def test_remove_pops_empty_containers_but_keeps_file(integ, tmp_path):
    integ.install()
    result = integ.remove()

    assert result["removed"] == 1
    f = _hooks_file(tmp_path)
    assert f.is_file()                           # file kept even when empty
    cfg = json.loads(f.read_text(encoding="utf-8"))
    assert "hooks" not in cfg or "SessionStart" not in cfg.get("hooks", {})


def test_verify_reflects_install_state(integ, tmp_path):
    before = integ.verify()
    assert before["verified"] is False
    assert before["strategy"] == "STARTUP_ASSISTED"

    integ.install()
    after = integ.verify()
    assert after["verified"] is True
    assert after["strategy"] == "NATIVE_SESSIONSTART_HOOK"
    assert after["checks"]["hooks_json_has_voyager"] is True
    assert after["checks"]["agents_md_managed_block"] is True


def test_agents_md_legacy_block_is_retired_not_duplicated(integ, tmp_path):
    """The pre-G3-B AGENTS.md instructs first-turn voyager_startup calls; the
    install must replace that startup-assisted block with retrieval-only
    guidance and preserve unrelated content."""
    agents = tmp_path / ".codex/AGENTS.md"
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text(
        "# My global notes\n\n"
        "## 跨 Agent 会话记忆（voyager）\n\n"
        "当用户提到历史时，先用 voyager 查询再回答。\n"
        "常用命令：voyager_startup / voyager continue --launch。\n"
        "## Other section\n\n"
        "keep me\n",
        encoding="utf-8")

    result = integ.install()
    assert result["agents_md_updated"] is True
    text = agents.read_text(encoding="utf-8")

    assert MANAGED_BEGIN in text and MANAGED_END in text
    assert text.count(MANAGED_BEGIN) == 1
    assert "voyager_startup" not in text         # startup-assisted wording retired
    assert "先用 voyager 查询再回答" not in text
    assert "voyager search" in text              # retrieval guidance kept
    assert "## Other section" in text and "keep me" in text  # neighbours preserved

    # idempotent: a second install replaces the marked block in place
    integ.install()
    text2 = agents.read_text(encoding="utf-8")
    assert text2.count(MANAGED_BEGIN) == 1
    assert "## Other section" in text2

    # remove strips the managed block and the neighbours again survive
    integ.remove()
    text3 = agents.read_text(encoding="utf-8")
    assert MANAGED_BEGIN not in text3
    assert "## Other section" in text3 and "keep me" in text3


# --- G3-B skill/bootstrap cleanup + full integration install -----------------

def test_install_integration_codex_is_retrieval_only(tmp_path):
    """`voyager integrate install codex` now registers the native hook,
    installs a retrieval-only skill, and writes a bootstrap document with no
    first-turn startup instructions."""
    from voyager.skill import install_integration

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)

    result = install_integration("codex", home=home)

    assert result["startup_status"] == "H"          # native hook, not assisted
    assert result["hook"]["status"] == "installed"
    assert result["mcp"]["status"] in ("registered", "up-to-date")

    skill = (home / ".codex/skills/voyager/SKILL.md").read_text(encoding="utf-8")
    # first-turn startup instructions are gone ...
    assert "Call `voyager_startup`" not in skill
    assert "instead of guessing" not in skill
    assert "BEFORE asking the user" not in skill
    assert "## Startup protocol" not in skill
    # ... the prohibition is explicit, and retrieval routing remains
    assert "Do NOT call" in skill and "`voyager_startup`" in skill
    assert "voyager search" in skill
    assert "voyager show" in skill

    bootstrap = (home / ".codex/skills/voyager/voyager_codex_bootstrap.md").read_text(
        encoding="utf-8")
    assert "Status: H" in bootstrap
    assert "Call tool: voyager_startup" not in bootstrap
    assert "Do NOT call voyager_startup" in bootstrap

    # the hook + managed AGENTS.md block landed with the same call
    hooks = json.loads((home / ".codex/hooks.json").read_text(encoding="utf-8"))
    assert "codex_hidden_relay.ps1" in json.dumps(hooks)
    agents = (home / ".codex/AGENTS.md").read_text(encoding="utf-8")
    assert MANAGED_BEGIN in agents


def test_install_integration_codex_is_idempotent(tmp_path):
    from voyager.skill import install_integration

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)

    first = install_integration("codex", home=home)
    second = install_integration("codex", home=home)

    assert first["startup_status"] == second["startup_status"] == "H"
    assert second["hook"]["hooks_replaced"] == 1   # replaced own entry, once
    assert second["skill"]["status"] == "installed"
    agents = (home / ".codex/AGENTS.md").read_text(encoding="utf-8")
    assert agents.count(MANAGED_BEGIN) == 1


def test_canonical_skill_template_has_no_first_turn_startup_instructions():
    """The packaged SKILL.md is the single source of truth: it must never
    carry the retired FIRST_TURN_ZERO_TOUCH wording again."""
    from voyager.skill import skill_source

    text = skill_source().read_text(encoding="utf-8")
    for forbidden in ("Call `voyager_startup`", "BEFORE asking the user",
                      "instead of guessing", "## Startup protocol",
                      "Call tool: voyager_startup"):
        assert forbidden not in text, forbidden
    assert "Do NOT call" in text and "`voyager_startup`" in text
    assert "voyager search" in text


# --- hidden relay safety boundaries (G3-B popup fix) -------------------------

def _seed_relay_home(request, tmp_path, repo_name="relay-test-repo"):
    """Seed the SAME isolated index the relay's handler will resolve to.

    conftest's autouse isolation redirects ``Store()`` to
    ``<isolated-root>/index.db`` and HOME to the same root, so seeding there
    is what makes the relayed handler find the WorkThread.

    ``repo_name`` is a parameter so a test can seed a repo whose path is not
    pure ASCII: the relay has to carry that path through its own stdin/stdout
    without letting the console code page transcode it.
    """
    from voyager.model import new_event, new_session
    from voyager.store import Store

    missing = request.getfixturevalue("_isolated_from_the_real_machine")
    root = missing.parent
    # the relayed handler is a SEPARATE process: monkeypatched
    # store.default_db_path does not cross the process boundary, so seed the
    # path its own default_db_path() resolves to under the redirected HOME
    # (Path.home()/".voyager"/"index.db").
    from voyager.store import Store
    db = Path.home() / ".voyager" / "index.db"
    store = Store(db)
    # Cross-platform repo path using tmp_path instead of hardcoded E:\ drive
    repo = str(tmp_path / repo_name)
    tid = store.thread_create(repo_root=repo, title="relay thread",
                              goal="continue across agents")
    src = tmp_path / "relay_seed.jsonl"
    src.write_text("{}", encoding="utf-8")
    sess = new_session(id="grok:relaywork", provider="grok",
                       native_session_id="relaywork", title="prior work",
                       started_at=1.0, updated_at=2.0,
                       repo_root=repo, cwd=repo)
    store.replace_session(
        sess,
        [new_event(sid="grok:relaywork", seq=1, kind="user", ts=1.0,
                   content="implement the relay acceptance"),
         new_event(sid="grok:relaywork", seq=2, kind="assistant", ts=2.0,
                   content="relay acceptance work done")],
        "grok", src)
    store.thread_attach(tid, "grok:relaywork")
    return store, tid, repo


def _canonical_argv(home=None):
    """The exact argv the installer writes.

    The relay tests run this rather than a hand-written flag list, so a test
    cannot keep passing on a command the product no longer emits.
    """
    from voyager.integrations.codex import CodexIntegration

    return CodexIntegration(home=home).hook_command().split()


def _run_relay(payload: dict, tmp_out: Path, argv=None):
    import subprocess
    out_f = tmp_out / "relay_out.json"
    err_f = tmp_out / "relay_err.txt"
    env = dict(os.environ, VOYAGER_NO_SYNC="1")
    with open(out_f, "wb") as fo, open(err_f, "wb") as fe:
        subprocess.run(
            argv or _canonical_argv(),
            input=json.dumps(payload).encode("utf-8"),
            stdout=fo, stderr=fe, timeout=120, env=env)
    return out_f.read_text(encoding="utf-8"), err_f.read_text(encoding="utf-8")


def test_hook_command_shape_is_the_live_verified_one():
    """The canonical command carries no `-NonInteractive`.

    Measured directly on this machine, both flag sets deliver the payload and
    emit byte-identical protocol JSON, so the flag adds nothing; the canonical
    shape stays the one that was live-verified.
    """
    from voyager.integrations.codex import CodexIntegration

    cmd = CodexIntegration().hook_command()
    assert "-NonInteractive" not in cmd
    assert cmd.startswith(
        "powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File ")
    assert cmd.endswith(".ps1")
    # the relay path must be forward-slashed and unquoted (live-proven shape)
    assert "\\" not in cmd and '"' not in cmd


@pytest.mark.skipif(sys.platform != "win32",
                    reason="the relay drives Windows PowerShell; CI windows "
                           "legs run it, linux legs cannot")
def test_relay_delivers_protocol_json_and_cleans_temp(
        tmp_path, request, monkeypatch):
    """One relay run: protocol JSON out (seeded WorkThread visible through
    the isolated index), clean stderr, and the three per-run temp files
    (payload/stdout/stderr) are removed in the finally block."""
    import glob

    store, tid, repo = _seed_relay_home(request, tmp_path)
    payload = {"session_id": "relay-sess-1", "cwd": repo,
               "hook_event_name": "SessionStart",
               "model": "gpt-5.6-luna", "permission_mode": "default",
               "source": "startup"}
    out, err = _run_relay(payload, tmp_path)

    d = json.loads(out)                                # protocol-valid only
    assert d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = d["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("format: tiered-v1")
    assert "implement the relay acceptance" in ctx     # seeded member visible
    assert err.strip() == ""                           # stderr stays clean
    leftovers = [p for p in glob.glob(os.path.join(os.environ["TEMP"],
                "voy_[poe]*")) if Path(p).stat().st_mtime > time.time() - 120]
    assert leftovers == [], leftovers                  # finally-cleanup ran
    # identity: the hook's native session id went into the pending attach
    rows = store.q("SELECT provider, native_session_id, status FROM "
                   "thread_pending WHERE thread_id=?", (tid,))
    assert [(r["provider"], r["native_session_id"], r["status"])
            for r in rows] == [("codex", "relay-sess-1", "open")]


@pytest.mark.skipif(sys.platform != "win32",
                    reason="the relay drives Windows PowerShell; CI windows "
                           "legs run it, linux legs cannot")
def test_relay_concurrent_runs_do_not_collide(
        tmp_path, request, monkeypatch):
    """Two simultaneous SessionStarts get unique temp names (ms stamp + PID):
    both runs deliver valid protocol JSON independently."""
    import threading

    store, tid, repo = _seed_relay_home(request, tmp_path)
    env = dict(os.environ, VOYAGER_NO_SYNC="1")
    outs = {}
    lock = threading.Lock()

    def run(tag, sid):
        out_f = tmp_path / ("relay_" + tag + ".json")
        with open(out_f, "wb") as fo, open(tmp_path / ("relay_" + tag + ".err"),
                                           "wb") as fe:
            subprocess.run(
                _canonical_argv(),
                input=json.dumps({"session_id": sid, "cwd": repo,
                                  "hook_event_name": "SessionStart"}).encode(),
                stdout=fo, stderr=fe, timeout=120, env=env)
        with lock:
            outs[tag] = out_f.read_text(encoding="utf-8")

    threads = [threading.Thread(target=run, args=(t, s))
               for t, s in (("a", "relay-sess-a"), ("b", "relay-sess-b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    assert len(outs) == 2
    for tag, text in outs.items():
        d = json.loads(text)
        assert d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert d["hookSpecificOutput"]["additionalContext"].startswith(
            "format: tiered-v1")
    # pending rows are keyed (thread_id, provider): concurrent same-provider
    # starts collapse to one open row -- the transport, not the attach book-
    # keeping, is what this test pins.
    rows = store.pending_open(thread_id=tid)
    assert len(rows) <= 1
    assert all(r["provider"] == "codex" and r["native_session_id"]
               in ("relay-sess-a", "relay-sess-b") for r in rows)


# The character classes the relay has to carry untouched.  `…` is the one that
# started this: it is U+2026, and on a Windows console code page it is a single
# byte (0x85 in cp1252), so any path through [Console]::In/[Console]::Out
# corrupts it -- and it is common in real transcript text.
NON_ASCII_REPO_NAMES = [
    pytest.param("repo-ascii", id="ascii"),
    pytest.param("repo-\u5237\u65b0\u4ee4\u724c", id="chinese"),          # 刷新令牌
    pytest.param("repo-\U0001f680", id="emoji"),                          # 🚀
    pytest.param("repo-JWT\u5237\u65b0token", id="mixed"),                # JWT刷新token
    pytest.param("repo-ellipsis\u2026", id="cp1252-ellipsis"),            # …
]


@pytest.mark.skipif(sys.platform != "win32",
                    reason="the relay drives Windows PowerShell; CI windows "
                           "legs run it, linux legs cannot")
@pytest.mark.parametrize("repo_name", NON_ASCII_REPO_NAMES)
def test_relay_round_trips_non_ascii_repo_path(
        tmp_path, request, monkeypatch, repo_name):
    """A non-ASCII repo path survives the relay in both directions.

    The relay reads the payload on its own stdin and writes the handler's
    stdout back.  Both used the console code page, so a Chinese path (or an
    emoji, or U+2026) was transcoded on the way through: the handler then
    matched nothing, or Codex received mojibake.  Seeding the thread under a
    non-ASCII path makes that observable -- the path has to come back out of
    the protocol JSON intact for the seeded context to appear at all.
    """
    store, tid, repo = _seed_relay_home(request, tmp_path, repo_name=repo_name)
    assert repo_name in repo          # the fixture honoured the name

    payload = {"session_id": "relay-sess-utf8", "cwd": repo,
               "hook_event_name": "SessionStart",
               "model": "gpt-5.6-luna", "permission_mode": "default",
               "source": "startup"}
    out, err = _run_relay(payload, tmp_path)

    # 1. the raw bytes on the wire are valid UTF-8 and carry no BOM
    raw = (tmp_path / "relay_out.json").read_bytes()
    assert raw, "relay produced no output at all"
    assert not raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM leaked into the protocol"
    text = raw.decode("utf-8")            # raises if the bytes are not UTF-8

    # 2. it parses as protocol JSON
    d = json.loads(text)
    assert d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = d["hookSpecificOutput"]["additionalContext"]

    # 3. the non-ASCII path round-trips.  The handler resolves the WorkThread
    #    by the cwd it is given, so if the relay transcoded the payload the
    #    handler would look for a different path -- and the failure is silent:
    #    it still emits a context, just not this thread's.  Assert on the
    #    seeded member, and on the path itself appearing in the evidence.
    assert ctx.startswith("format: tiered-v1")
    assert "implement the relay acceptance" in ctx, (
        f"the handler did not resolve the seeded thread for repo {repo!r}")
    assert repo in ctx, (
        "the repo path did not survive the relay; the handler saw "
        f"{repo!r} encoded through the console code page instead")

    # 4. and nothing was replaced or lost
    assert "\ufffd" not in ctx, "a replacement character reached the protocol"
    assert err.strip() == ""


@pytest.mark.skipif(sys.platform != "win32",
                    reason="the relay drives Windows PowerShell; CI windows "
                           "legs run it, linux legs cannot")
@pytest.mark.parametrize("sample", [
    pytest.param('{"title": "plain ascii"}', id="ascii"),
    pytest.param('{"title": "\u5237\u65b0\u4ee4\u724c"}', id="chinese"),
    pytest.param('{"title": "\U0001f680"}', id="emoji"),
    pytest.param('{"title": "JWT\u5237\u65b0token"}', id="mixed"),
    pytest.param('{"title": "wait\u2026"}', id="cp1252-ellipsis"),
])
def test_relay_is_byte_transparent(tmp_path, sample):
    """The relay itself must not transcode anything.

    This is the test that actually protects the encoding fix.  Going through
    the real handler cannot: it writes its protocol JSON with
    ``ensure_ascii=True`` (so its stdout is pure ASCII) and it resolves the
    WorkThread without consulting the payload's path, so both the stdin and the
    stdout transcoding bugs are invisible through it -- verified by reverting
    each fix and watching the end-to-end test still pass.

    So test the relay directly, with an echo handler: whatever UTF-8 bytes go in
    on stdin have to come back out on stdout, unchanged.  The old relay failed
    this because [Console]::In and [Console]::Out both use the console code page
    (gb2312 here, cp1252 on a runner), which mangles anything non-ASCII.
    """
    from voyager.integrations.codex import CodexIntegration

    relay_src = CodexIntegration().relay
    shim = tmp_path / "relay"
    shim.mkdir()
    (shim / "codex_hidden_relay.ps1").write_text(
        relay_src.read_text(encoding="utf-8"), encoding="utf-8")
    # an echo handler next to the copied relay: the relay resolves its handler
    # through $PSScriptRoot, so this is the one it will run
    (shim / "codex_session_start.py").write_text(
        "import sys\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        "sys.stdout.buffer.flush()\n",
        encoding="utf-8")

    raw = sample.encode("utf-8")
    out_f = tmp_path / "echo_out.bin"
    err_f = tmp_path / "echo_err.txt"
    # PowerShell 5.1 builds its environment dictionary case-insensitively, so a
    # machine that exports both HTTPS_PROXY and https_proxy makes the relay
    # throw before it runs.  Give the child a clean environment.
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY"}}
    with open(out_f, "wb") as fo, open(err_f, "wb") as fe:
        subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(shim / "codex_hidden_relay.ps1")],
            input=raw, stdout=fo, stderr=fe, timeout=120, env=env)

    got = out_f.read_bytes()
    assert got == raw, (
        f"relay transcoded the payload: in={raw!r} out={got!r}")
    assert not got.startswith(b"\xef\xbb\xbf"), "a BOM was added"
    # and the bytes really are the sample, decoded as UTF-8
    assert json.loads(got.decode("utf-8"))["title"] == json.loads(sample)["title"]
