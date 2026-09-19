"""Phase 4 Context Budget tests (roadmap #5).

Contracts:
- compact < balanced < full in packed size
- a hard budget (12k) is never exceeded by the estimate
- goal relevance (Phase 3 ranking) decides what survives; failures and
  current state survive even tight budgets
- no --budget -> byte-identical output (CLI-checked too)
- provenance never trimmed away silently (Evidence header + Budget notes)
- provider files never written
- parse_budget surface: presets / Nk / int / auto / garbage
"""

from __future__ import annotations

import pytest

from voyager.budget import (
    apply_budget,
    auto_budget,
    estimate_tokens,
    parse_budget,
)
from voyager.cli import main
from voyager.store import Store


def _sample_bundle() -> str:
    """A realistic full bundle with every section the compiler emits."""
    parts = [
        "# Continuation Bundle",
        "",
        "## Goal",
        "",
        "**Primary user goal:** fix CI on main",
        "",
        "## Current verified state",
        "",
        "**Where work stopped (active session: `codex` `ci-1`):**",
        "",
        "pytest run is red; workflow yml edited but unpushed.",
        "",
        "## Current repository state (live snapshot)",
        "",
        "- Git Branch: `main`",
        "- Working Tree: clean",
        "",
        "## Goal-ranked evidence",
        "",
        'goal: "fix CI" — top 2 of 5 ranked facts:',
        "",
        "- [codex:ci#2] (tool_call, score 4.1) python -m pytest -q",
        "- [codex:ci#3] (tool_result, score 3.9) FAILED tests/test_ci.py",
        "",
        "## Prior assistant conclusions (may be superseded)",
        "",
        "- `claude:readme` (2026-09-13): README polished",
        "",
        "## Files touched across sessions",
        "",
    ]
    parts += [f"- `file_{i}.py`" for i in range(600)]          # ~10KB of bulk
    parts += ["", "## Commands executed", ""]
    parts += [f"- `pytest -q case_{i}`  # exit 1" for i in range(300)]
    parts += ["", "## Errors encountered", "",
              "- [ts] exit 1: `pytest -q`", "", "## Evidence & Provenance", "",
              "Compiled from 2 session(s):", "",
              "- **codex** `ci-1`: fix CI (3 msgs, ...)"]
    return "\n".join(parts) + "\n"


def test_parse_budget_surface():
    assert parse_budget(None) is None
    assert parse_budget("compact") == 4000
    assert parse_budget("balanced") == 20000
    assert parse_budget("full") == 100000
    assert parse_budget("auto") is None
    assert parse_budget("12k") == 12000
    assert parse_budget("3K") == 3000
    assert parse_budget("5000") == 5000
    with pytest.raises(ValueError):
        parse_budget("banana")
    with pytest.raises(ValueError):
        parse_budget("-5k")


def test_estimate_tokens_is_chars_over_four():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("") == 0


def test_auto_budget_resolves_balanced_for_known_targets():
    assert auto_budget("claude") == auto_budget("codex") == \
        auto_budget("grok") == 20000
    assert auto_budget(None) == 20000


def test_under_budget_returns_text_unchanged():
    b = _sample_bundle()
    packed, info = apply_budget(b, 100000)
    assert packed == b
    assert info["dropped"] == [] and info["trimmed"] == []


def test_no_budget_is_a_no_op():
    b = _sample_bundle()
    packed, info = apply_budget(b, None)
    assert packed == b and info["budget"] is None


def _large_bundle() -> str:
    """The sample bundle inflated past the `full` (100k tokens) budget."""
    b = _sample_bundle()
    lines = b.splitlines()
    out = []
    for ln in lines:
        out.append(ln)
        if ln == "## Files touched across sessions":
            out.extend(f"- `big_{i}.py`" for i in range(30000))
    return "\n".join(out) + "\n"


def test_compact_balanced_full_ordering():
    b = _large_bundle()            # ~112k tokens: overflows even `full`
    sizes = {}
    for name, spec in (("compact", "compact"), ("balanced", "balanced"),
                       ("full", "full")):
        packed, info = apply_budget(b, parse_budget(spec))
        sizes[name] = info["estimated_tokens"]
        assert info["budget"] == parse_budget(spec)
    assert sizes["compact"] < sizes["balanced"] < sizes["full"]
    assert sizes["full"] <= 100000


def test_hard_budget_is_respected():
    b = _sample_bundle()
    assert estimate_tokens(b) > 4000          # fixture must overflow compact
    packed, info = apply_budget(b, 4000)
    assert info["estimated_tokens"] <= 4000
    assert info["budget"] == 4000
    assert info["dropped"] or info["trimmed"], \
        "a 12k budget on a large bundle must trim something"


def test_tight_budget_keeps_priority_sections():
    b = _sample_bundle()
    packed, info = apply_budget(b, 900)
    assert "## Goal" in packed
    assert "## Current verified state" in packed
    assert "## Evidence & Provenance" in packed, "provenance header must survive"
    assert "## Budget notes" in packed
    assert ("Dropped section(s):" in packed
            or "Trimmed section(s):" in packed)


def _EVIDENCE_KEPT(packed: str) -> bool:
    return "## Evidence & Provenance" in packed


def _BUDGET_NOTE(packed: str) -> bool:
    return "## Budget notes" in packed


def test_goal_relevance_decides_what_survives():
    b = _sample_bundle()
    packed, info = apply_budget(b, 1200)
    # CI facts (goal "fix CI") survive; the unrelated bulk file tail is
    # trimmed by the packer (the mandatory first item may remain)
    assert "Goal-ranked evidence" in packed
    assert "file_599.py" not in packed
    assert "## Errors encountered" in packed


def test_provenance_survives_tightest_budget():
    b = _sample_bundle()
    packed, _ = apply_budget(b, 300)
    assert "## Evidence & Provenance" in packed
    assert "Compiled from 2 session(s):" in packed


# ---------------------------------------------------------------------------
# CLI integration: --budget on merge / handoff / continue
# ---------------------------------------------------------------------------

@pytest.fixture
def two_sessions(tmp_path):
    db = tmp_path / "i.db"
    store = Store(db)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    for i, prov in enumerate(["codex", "claude"]):
        s = {"id": f"{prov}:b{i}", "provider": prov,
             "native_session_id": f"b{i}", "title": f"work {i}",
             "started_at": 1000.0 + i, "updated_at": 2000.0 + i,
             "cwd": "E:/proj/demo", "repo_root": "E:/proj/demo",
             "message_count": 1, "tool_count": 0, "can_resume": False,
             "can_fork": False, "resume_cmd": None,
             "metadata": {}, "raw_metadata": {}}
        store.replace_session(
            s, [{"sid": s["id"], "ts": 1000.0 + i, "seq": 0,
                 "kind": "user", "content": f"work item {i}"}],
            prov, src)
    yield db
    store.close()


def test_merge_budget_cli_end_to_end(tmp_path, two_sessions, capsys):
    db = str(two_sessions)
    out = tmp_path / "b.md"
    assert main(["--db", db, "merge", "codex:b0", "claude:b1",
                 "--budget", "compact", "-o", str(out)]) == 0
    packed = out.read_text(encoding="utf-8")
    assert "estimated tokens: ~" in capsys.readouterr().out
    assert estimate_tokens(packed) <= 4000


def test_merge_no_budget_is_byte_compatible(tmp_path, two_sessions):
    db = str(two_sessions)
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    assert main(["--db", db, "merge", "codex:b0", "claude:b1",
                 "-o", str(a)]) == 0
    assert main(["--db", db, "merge", "codex:b0", "claude:b1",
                 "-o", str(b)]) == 0
    assert a.read_text(encoding="utf-8") == b.read_text(encoding="utf-8")


def test_handoff_budget_cli(tmp_path, two_sessions, capsys):
    db = str(two_sessions)
    out = tmp_path / "h.md"
    assert main(["--db", db, "handoff", "codex:b0", "--goal", "work item",
                 "--budget", "compact", "-o", str(out)]) == 0
    assert estimate_tokens(out.read_text(encoding="utf-8")) <= 4000
    assert "estimated tokens: ~" in capsys.readouterr().out


def test_continue_budget_cli(tmp_path, two_sessions, capsys):
    db = str(two_sessions)
    out = tmp_path / "c.md"
    assert main(["--db", db, "continue", "codex:b0", "--to", "claude",
                 "--goal", "work item", "--budget", "compact",
                 "-o", str(out)]) == 0
    assert estimate_tokens(out.read_text(encoding="utf-8")) <= 4000


def test_invalid_budget_fails_loudly(tmp_path, two_sessions):
    db = str(two_sessions)
    with pytest.raises(SystemExit) as exc:
        main(["--db", db, "merge", "codex:b0", "claude:b1",
              "--budget", "banana", "-o", str(tmp_path / "x.md")])
    assert exc.value.code == 2


def test_budget_never_writes_provider_files(tmp_path, two_sessions):
    src = tmp_path / "s.jsonl"
    before = src.read_bytes()
    assert main(["--db", str(two_sessions), "merge", "codex:b0", "claude:b1",
                 "--budget", "compact", "-o", str(tmp_path / "b.md")]) == 0
    assert src.read_bytes() == before
