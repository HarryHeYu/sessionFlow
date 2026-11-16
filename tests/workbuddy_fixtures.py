"""Synthetic WorkBuddy JSONL fixtures.

Nothing here is copied from a real conversation. The strings, ids and cwd are
invented so the adapter's mapping can be tested without touching a profile.
"""

from __future__ import annotations

import json
from pathlib import Path

T0 = 1_700_000_000_000          # epoch milliseconds
T1 = T0 + 1_000
T2 = T0 + 2_000


def _write(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def make_session(path: Path, *, sid: str = "wb-1",
                 title: str = "Synthetic WorkBuddy title",
                 cwd: str | None = None, companions: bool = True,
                 malformed_tail: bool = False) -> Path:
    cwd = cwd or str(path.parent.parent)
    records = [
        {"id": "e0", "sessionId": sid, "timestamp": T0,
         "type": "session-meta",
         "meta": {"codebuddy.ai/hostKind": "desktop-synthetic"}},
        {"id": "e1", "sessionId": sid, "timestamp": T0,
         "type": "ai-title", "aiTitle": title, "cwd": cwd},
        {"id": "e2", "sessionId": sid, "timestamp": T0,
         "type": "message", "role": "user", "cwd": cwd,
         "content": [{"type": "input_text", "text": "synthetic user turn"}],
         "providerData": {"agent": "synthetic", "startsNewUserRequest": True}},
        {"id": "e3", "sessionId": sid, "timestamp": T1,
         "type": "message", "role": "assistant", "cwd": cwd,
         "content": [{"type": "output_text", "text": "synthetic answer"}],
         "providerData": {"agent": "synthetic",
                          "requestModelName": "synthetic-model"}},
        {"id": "e4", "sessionId": sid, "timestamp": T1,
         "type": "reasoning", "cwd": cwd,
         "rawContent": [{"type": "text", "text": "synthetic reasoning"}],
         "content": [], "providerData": {"agent": "synthetic"}},
        {"id": "e5", "sessionId": sid, "timestamp": T2,
         "type": "function_call", "cwd": cwd, "name": "shell",
         "callId": "call-1",
         "arguments": '{"command":"echo synthetic","file_path":"/synthetic.py"}',
         "providerData": {"agent": "synthetic"}},
        {"id": "e6", "sessionId": sid, "timestamp": T2,
         "type": "function_call_result", "cwd": cwd, "name": "shell",
         "callId": "call-1", "status": "completed",
         "output": {"content": [{"type": "text", "text": "synthetic output"}]},
         "providerData": {"agent": "synthetic",
                          "toolResult": {"renderer": "terminal"}}},
        {"id": "e7", "sessionId": sid, "timestamp": T2,
         "type": "message", "role": "user", "cwd": cwd,
         "content": [{"type": "input_text", "text": "provider bootstrap text"}],
         "providerData": {"agent": "synthetic", "isMeta": True}},
        {"id": "e8", "sessionId": sid, "timestamp": T2,
         "type": "file-history-snapshot", "cwd": cwd,
         "isSnapshotUpdate": False,
         "snapshot": {"messageId": "e6", "trackedFileBackups": {}}},
    ]
    result = _write(path, records)
    if malformed_tail:
        with result.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write("{ this is not json\n")
    if companions:
        path.with_name(path.stem + ".meta.json").write_text(
            "{}", encoding="utf-8")
        path.with_name(path.stem + ".file-rollback.ndjson").write_text(
            "", encoding="utf-8")
    return result


def make_unknown(path: Path) -> Path:
    return _write(path, [{"hello": "not a WorkBuddy session"}])


def make_corrupt(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]\nnot-even-json\n", encoding="utf-8")
    return path
