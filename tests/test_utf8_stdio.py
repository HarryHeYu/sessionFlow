"""Regression: the CLI must speak UTF-8 whatever the Windows code page is.

The DSH bridge decodes our stdout with ``setEncoding('utf8')`` and writes our
stdin as UTF-8.  On a Chinese Windows install Python defaults *both* streams to
cp936 (GBK), so:

* stdout: every non-ASCII title arrived as U+FFFD while the index itself was
  perfectly fine -- the data was never wrong, only the transport.
* stdin: the ``api`` op's JSON-lines requests were silently decoded through
  GBK, so ``{"query": "会话记录"}`` became ``"浼氳瘽璁板綍"`` and the search
  answered ``count: 0``.  "No such session" and "your query was corrupted in
  transit" are the same answer to the caller, which is what made it survive.

Reported with byte-level evidence against voyager 1.3.0 (dsh-sessionflow#1).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from voyager.util import force_utf8_stdio

ROOT = Path(__file__).resolve().parents[1]


def _gbk_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")


def test_force_utf8_switches_a_gbk_stream(monkeypatch):
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", _gbk_stream())

    force_utf8_stdio()

    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
    sys.stdout.write("插件优化调整\n")
    sys.stdout.flush()
    # the bytes a UTF-8 reader (the bridge) would receive
    assert "插件优化调整".encode("utf-8") in buf.getvalue()


def test_force_utf8_tolerates_streams_without_reconfigure(monkeypatch):
    """A detached stream or a test double must not abort the command."""

    class _Bare:
        def write(self, s):          # pragma: no cover - never reached
            return len(s)

    monkeypatch.setattr(sys, "stdout", _Bare())
    monkeypatch.setattr(sys, "stderr", None)
    force_utf8_stdio()               # must not raise


def test_force_utf8_covers_stdin(monkeypatch):
    """The `api` op reads requests from stdin; it must not stay GBK.

    Regression for the encode/decode asymmetry: stdout was fixed at the process
    boundary but stdin was left at the ANSI code page, so a client writing UTF-8
    request lines had them silently decoded through GBK.
    """
    stdin = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", _gbk_stream())
    monkeypatch.setattr(sys, "stderr", _gbk_stream())

    force_utf8_stdio()

    assert sys.stdin.encoding.lower().replace("-", "") == "utf8", (
        "stdin stayed %r: an api client's UTF-8 request would be decoded as GBK"
        % (sys.stdin.encoding,))


def test_force_utf8_tolerates_none_stdin(monkeypatch):
    """fd 0 closed (sys.stdin is None) must not break every command."""
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", _gbk_stream())
    monkeypatch.setattr(sys, "stderr", _gbk_stream())
    force_utf8_stdio()               # must not raise


def test_cli_main_forces_utf8_before_parsing(monkeypatch):
    """The fix lives at the single entry point, so every subcommand inherits it."""
    called = []
    monkeypatch.setattr("voyager.cli.force_utf8_stdio",
                        lambda: called.append(True))
    from voyager import cli

    try:
        cli.main(["--version"])
    except SystemExit:
        pass                        # argparse's version action exits 0
    assert called == [True]


def test_cli_json_bytes_are_utf8_under_gbk(tmp_path):
    """End to end: Japanese, Korean, CJK and an emoji survive the pipe.

    The unit tests above prove the stream is reconfigured; this one proves the
    bytes a UTF-8 reader receives are correct, which is what the bridge sees.
    """
    from voyager.model import new_event, new_session
    from voyager.store import Store

    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store = Store(db)
    try:
        sess = new_session(id="codex:x", provider="codex", native_session_id="x",
                           title="日本語 한국어 補丁", started_at=1.0, updated_at=1.0,
                           repo_root="E:/p", cwd="E:/p")
        ev = new_event(sid="codex:x", seq=1, kind="user", ts=1.0,
                       content="テスト 한국어 補丁 🚀", origin="human")
        store.replace_session(sess, [ev], "codex", src)
    finally:
        store.close()

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData/Roaming"),
        "LOCALAPPDATA": str(home / "AppData/Local"),
        "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "gbk",     # the condition that used to corrupt it
    })
    proc = subprocess.run(
        [sys.executable, "-m", "voyager.cli", "--db", str(db),
         "search", "テスト", "--json"],
        capture_output=True, env=env, cwd=str(tmp_path), timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout.decode("utf-8")     # raises UnicodeDecodeError if broken
    assert "テスト" in text
    assert "🚀" in text


def test_api_op_reads_utf8_stdin_under_gbk(tmp_path):
    """End to end over the bridge's real path: a CJK query must still match.

    ``search`` is an FTS5 search over *event bodies*, so the query word goes in
    the event body; a title-only match never happens and would make this test
    pass for the wrong reason.

    ``count`` is the assertion, not the echoed ``query``: for this pair a GBK
    echo of the UTF-8 bytes renders as plausible CJK
    (``gbk("浼氳瘽") == utf8("会话") == e4bc9ae8af9d``), so an assertion on the
    echo can pass while the match is already corrupted.
    """
    _assert_cjk_query_matches([sys.executable, "-m", "voyager.cli", "api"],
                              tmp_path)


def test_documented_serve_verb_is_accepted(tmp_path):
    """`python -m voyager.api serve` is what the shipped client spawns.

    ``vscode-extension/extension.js`` builds exactly
    ``spawn(python, ["-m", "voyager.api", "serve"])`` and docs/API.md documents
    ``voyager api serve``, but neither parser ever accepted the verb: the child
    exited 2 with "unrecognized arguments: serve" before reading a request, so
    the sidebar could never have worked.  A documented invocation is a contract;
    this asserts it answers rather than exits.
    """
    _assert_cjk_query_matches([sys.executable, "-m", "voyager.api", "serve"],
                              tmp_path)


def test_bare_api_module_still_serves(tmp_path):
    """The un-verbed form must keep working -- it is the other documented path."""
    _assert_cjk_query_matches([sys.executable, "-m", "voyager.api"], tmp_path)


def test_unknown_api_verb_is_still_rejected():
    """Accepting `serve` must not turn the parser into "anything goes".

    A typo has to fail loudly; the fix is one accepted verb, not a catch-all.
    """
    proc = subprocess.run([sys.executable, "-m", "voyager.api", "bogus"],
                          capture_output=True, cwd=str(ROOT), timeout=180)
    assert proc.returncode == 2, (
        "a bogus verb exited %d, not argparse's usage error 2"
        % proc.returncode)


def _assert_cjk_query_matches(argv, tmp_path):
    """Seed one CJK session, then assert ``argv`` answers a CJK query.

    Shared by the invocation-form tests so they differ only in the command a
    client would type -- the assertion (``count``, never the echo) stays fixed.
    """
    from voyager.model import new_event, new_session
    from voyager.store import Store

    query = "会话记录"
    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}\n", encoding="utf-8")
    store = Store(db)
    try:
        sess = new_session(
            id="codex:zh", provider="codex", native_session_id="zh",
            title="会话 中文标题", started_at=1.0, updated_at=1.0,
            repo_root="D:/插件开发", cwd="D:/插件开发")
        store.replace_session(
            sess,
            [new_event(sid="codex:zh", seq=1, kind="user", ts=1.0,
                       content="这里是一段会话记录，用于测试中文检索。",
                       origin="human")],
            "codex", src)
    finally:
        store.close()

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData/Roaming"),
        "LOCALAPPDATA": str(home / "AppData/Local"),
        "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "gbk",     # the condition that used to corrupt it
    })
    env.pop("PYTHONUTF8", None)

    request = json.dumps({"id": 1, "op": "search",
                          "params": {"query": query, "db": str(db)}},
                         ensure_ascii=False) + "\n"
    proc = subprocess.run(
        argv,
        input=request.encode("utf-8"),      # exactly what the Node bridge writes
        capture_output=True, env=env, cwd=str(tmp_path), timeout=180,
    )
    assert proc.returncode == 0, (
        "%s exited %d: %s"
        % (" ".join(argv[1:]), proc.returncode,
           proc.stderr.decode("utf-8", "replace").strip()[:300]))
    line = proc.stdout.decode("utf-8").strip().splitlines()[-1]
    result = json.loads(line)["result"]

    assert result["count"] == 1, (
        "a UTF-8 query read through a GBK stdin found nothing; echoed as %r"
        % (result.get("query"),))
    assert result["results"][0]["id"] == "codex:zh"
