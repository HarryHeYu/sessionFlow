#!/usr/bin/env python3
"""Create the deterministic, privacy-safe continuity-proof fixture."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

APP_FILES = {
    "README.md": """# textutil fixture

A tiny text helper library. Work only in this directory.
""",
    "textutil.py": '''"""Small text helpers."""\n\n\ndef normalize(text: str) -> str:\n    """Return text with leading and trailing ASCII spaces removed."""\n    return text.strip(" ")\n''',
    "tests/test_textutil.py": '''import unittest\n\nfrom textutil import normalize\n\n\nclass TestNormalize(unittest.TestCase):\n    def test_leading_trailing_spaces(self):\n        self.assertEqual(normalize("  Hi  "), "Hi")\n\n    def test_empty(self):\n        self.assertEqual(normalize("   "), "")\n''',
}

AGENT_A_TEXT = """Goal for the textutil task:
The normalizer is not finished. `normalize()` must collapse every internal run of whitespace into one plain ASCII space, including tabs, newlines and non-breaking spaces (U+00A0). Existing leading/trailing trimming must still work.

Implementation note:
Use only the Python standard library. `re.sub(r"\\s+", " ", text).strip()` satisfies the contract for Unicode string input.

Remaining work for the next agent:
Change `textutil.py` accordingly and add a visible regression test. Do not change the public function signature.
"""

HIDDEN_TEST = '''import unittest\n\nfrom textutil import normalize\n\n\nclass TestHiddenContract(unittest.TestCase):\n    def test_mixed_whitespace(self):\n        self.assertEqual(normalize("  Hello\\t World\\n"), "Hello World")\n\n    def test_non_breaking_space(self):\n        self.assertEqual(normalize("a\\u00a0\\u00a0b"), "a b")\n\n    def test_existing_behaviour(self):\n        self.assertEqual(normalize("  Hi  "), "Hi")\n        self.assertEqual(normalize(""), "")\n'''

ORACLE = {
    "schema": "public-continuity-proof-oracle-v1",
    "task": "textutil hidden whitespace contract",
    "critical_omission": "internal whitespace must collapse to one ASCII space, including U+00A0",
    "visible_repo_contains_requirement": False,
    "expected_control": "hidden test fails before the agent reads a continuation",
    "expected_sessionflow": "hidden test passes after the agent reads the continuation",
    "note": "The oracle is evaluator-only. Agent B is instructed to work only in app/.",
}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def env_for(app: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(app),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="continuity-proof-run")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(
            f"refusing to reuse non-empty output directory: {out}\n"
            "Use a fresh directory for each condition; no files are deleted."
        )

    app = out / "app"
    for rel, content in APP_FILES.items():
        write(app / rel, content)

    session_id = "public-continuity-agent-a"
    timestamp = "2026-01-01T00:00:00Z"
    cwd = str(app).replace("\\", "/")
    rows = [
        {"timestamp": timestamp, "ordinal": 0, "type": "session_meta", "payload": {
            "id": session_id,
            "session_id": session_id,
            "cwd": cwd,
            "originator": "public-continuity-demo",
            "source": "synthetic-seeded-session",
            "cli_version": "demo-fixture",
            "model_provider": "synthetic",
        }},
        {"timestamp": timestamp, "ordinal": 1, "type": "turn_context", "payload": {
            "cwd": cwd,
            "model": "synthetic-agent-a",
        }},
        {"timestamp": timestamp, "ordinal": 2, "type": "response_item", "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": AGENT_A_TEXT}],
        }},
    ]
    session = (out / "home" / ".codex" / "sessions" / "2026" / "01" / "01"
               / f"rollout-2026-01-01T00-00-00-{session_id}_1.jsonl")
    write(session, "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
    (out / "home" / ".voyager").mkdir(parents=True, exist_ok=True)

    hidden_dir = out / "evaluator" / "hidden_tests"
    write(hidden_dir / "test_hidden.py", HIDDEN_TEST)
    write(out / "evaluator" / "oracle.json",
          json.dumps(ORACLE, ensure_ascii=False, indent=2) + "\n")

    visible = subprocess.run(
        [args.python, "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=str(app), env=env_for(app), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    hidden = subprocess.run(
        [args.python, "-m", "unittest", "discover", "-s", str(hidden_dir), "-q"],
        cwd=str(app), env=env_for(app), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    visible_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in app.rglob("*") if path.is_file()
    ).lower()
    leak = any(word in visible_text for word in ("collapse", "whitespace", "u+00a0", "nbsp"))

    evidence = {
        "schema": "public-continuity-proof-setup-v1",
        "visible_rc": visible.returncode,
        "hidden_rc": hidden.returncode,
        "hidden_fact_in_visible_repo": leak,
        "oracle_written_before_agent_b": True,
        "seeded_session": str(session),
        "agent_b_workdir": str(app),
    }
    write(out / "evaluator" / "setup-evidence.json",
          json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")

    print(f"fixture ready at {out}")
    print(f"visible_rc={visible.returncode} hidden_rc={hidden.returncode} leaked={leak}")
    print(f"seeded prior session: {session}")
    print("Agent B workdir:", app)
    return 0 if visible.returncode == 0 and hidden.returncode != 0 and not leak else 1


if __name__ == "__main__":
    raise SystemExit(main())
