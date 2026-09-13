"""Claude Code adapter regression test.

Fixture: tests/fixtures/claude/projects/E--proj--demo/cla-1111.jsonl —
file-history-snapshot, user message, assistant turn with thinking + tool_use,
tool_result row (with toolUseResult), assistant text. Chain/format drift in
Claude Code's project JSONL breaks this test.
"""

from __future__ import annotations

import json


def test_claude_parse(adapter_of, patch_paths, claude_fixture):
    ad = adapter_of("claude")
    patch_paths(ad, PROJECTS_DIR=claude_fixture / "projects")
    files = ad.discover()
    assert len(files) == 1
    r = ad.parse(files[0])
    s, evs = r["session"], r["events"]
    assert s["native_session_id"] == "cla-1111"
    assert s["git_branch"] == "main"
    assert s["resume_cmd"] == "claude --resume cla-1111"
    kinds = [e["kind"] for e in evs]
    assert kinds.count("user") == 1 and kinds.count("assistant") == 1
    assert kinds.count("tool_call") == 1 and kinds.count("tool_result") == 1
    assert kinds.count("reasoning") == 1 and kinds.count("snapshot") == 1
    tr = next(e for e in evs if e["kind"] == "tool_result")
    assert tr["exit_code"] == 0 and tr["stdout"] == "file body"
    # snapshot files survive into the files table payload
    assert r["session"].get("_files"), "snapshot files must be recorded"
    assert s["message_count"] == 2 and s["tool_count"] == 1
    assert s["metadata"]["usage_totals"] == {
        "input_tokens": 50, "output_tokens": 30, "cache_read_input_tokens": 5,
    }


def test_claude_multiple_tool_results_kept(adapter_of, patch_paths, claude_fixture):
    ad = adapter_of("claude")
    patch_paths(ad, PROJECTS_DIR=claude_fixture / "projects")
    f = list(ad.discover())[0]
    lines = f.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[3])
    blk = row["message"]["content"]
    blk.append({"type": "tool_result", "tool_use_id": "t2", "content": "second"})
    row["message"]["content"] = blk
    row["toolUseResult"] = None
    lines[3] = json.dumps(row, ensure_ascii=False)
    f.write_text("\n".join(lines), encoding="utf-8")
    r = ad.parse(f)
    trs = [e for e in r["events"] if e["kind"] == "tool_result"]
    assert len(trs) == 2, "parallel tool_results must not be dropped"
