"""Export tests (Markdown / JSON) against a populated index."""

from __future__ import annotations

import json

from voyager.export import export_json, export_markdown, write_export
from voyager.store import Store


def test_export_markdown_contains_session_and_conversation(indexed_store, codex_row):
    row = codex_row
    events = indexed_store.events(row["id"])
    md = export_markdown(indexed_store, row, events)

    assert md.startswith("# fix the parser")
    assert "- **Provider**: codex" in md
    assert "- **Session ID**: `11111111-2222-3333-4444-555555555555`" in md
    assert "- **Resume**: `codex resume 11111111-2222-3333-4444-555555555555`" in md
    assert "**Usage**" in md and '"input": 100' in md
    # conversation bodies
    assert "### 🧑 User" in md and "fix the parser" in md
    assert "### 🤖 Assistant" in md and "parser fixed" in md
    assert "<summary>💭 Reasoning</summary>" in md
    assert "python -m pytest -q" in md
    assert "all good" in md
    assert "### ❌ Error" in md
    # codex carries no file/snapshot events: no Files Touched block
    assert "## Files Touched" not in md


def test_export_json_is_lossless_and_parsable(indexed_store, codex_row):
    row = codex_row
    events = indexed_store.events(row["id"])
    data = json.loads(export_json(indexed_store, row, events))

    assert data["session"]["id"] == row["id"]
    assert data["session"]["metadata"]["usage_totals"] == {"input": 100, "output": 20}
    assert data["session"]["raw_metadata"]["originator"] == "codex_vscode"
    assert len(data["events"]) == len(events) == 6
    kinds = [e["kind"] for e in data["events"]]
    assert kinds == ["user", "reasoning", "tool_call", "tool_result", "error",
                     "assistant"]
    user = data["events"][0]
    assert user["content"] == "fix the parser"
    assert user["files"] == [] and user["usage"] is None   # json columns decoded
    assert "files_json" not in user and "raw_json" not in user
    assert data["events"][2]["command"] == "python -m pytest -q"
    assert data["events"][3]["exit_code"] == 0


def test_write_export_writes_both_formats(indexed_store, codex_row, tmp_path):
    row = codex_row
    md_path = write_export(indexed_store, row, str(tmp_path / "s.md"), "md")
    json_path = write_export(indexed_store, row, str(tmp_path / "s.json"), "json")
    assert open(md_path, encoding="utf-8").read().startswith("# fix the parser")
    assert json.load(open(json_path, encoding="utf-8"))["session"]["provider"] == "codex"


def test_export_of_empty_store_row_database_roundtrip(tmp_path):
    """A brand-new store still opens and answers stats (no crash, no schema gap)."""
    store = Store(tmp_path / "fresh.db")
    assert store.stats() == {"sessions": 0, "events": 0, "by_provider": {}}
    store.close()


def test_export_markdown_files_touched_from_provider_events(
        adapter_of, patch_paths, claude_fixture, tmp_path):
    """Claude sessions carry file-history snapshots -> Files Touched section."""
    ad = adapter_of("claude")
    patch_paths(ad, PROJECTS_DIR=claude_fixture / "projects")
    source = ad.discover()[0]
    parsed = ad.parse(source)

    store = Store(tmp_path / "index.db")
    store.replace_session(parsed["session"], parsed["events"], "claude", source)
    row, _ = store.session("claude:cla-1111")
    md = export_markdown(store, row, store.events(row["id"]))
    assert "## Files Touched" in md and "`E:/proj/demo/a.py`" in md
    # the file-history backup recorded by the adapter lands in the files table
    assert store.q("SELECT path FROM files WHERE sid=?", (row["id"],))[0]["path"] \
        == "E:/proj/demo/a.py"
    store.close()
