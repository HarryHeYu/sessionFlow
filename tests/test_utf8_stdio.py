"""Regression: the CLI must speak UTF-8 whatever the Windows code page is.

The DSH bridge decodes our stdout with ``setEncoding('utf8')``.  On a Chinese
Windows install Python defaults stdout to cp936 (GBK), so every non-ASCII title
arrived as U+FFFD while the index itself was perfectly fine -- the data was
never wrong, only the transport.  Reported with byte-level evidence against
voyager 1.3.0 (dsh-sessionflow#1).
"""

from __future__ import annotations

import io
import sys

from voyager.util import force_utf8_stdio


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
