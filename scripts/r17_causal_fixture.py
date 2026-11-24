#!/usr/bin/env python3
"""Generate and verify the privacy-safe R17 causal continuity fixtures."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

TASKS = ("e1_textutil", "e2_amount", "e3_config")

FILES = {
    "e1_textutil": {
        "textutil.py": '''"""Small text helpers."""\n\n\ndef normalize(text: str) -> str:\n    """Return text with leading and trailing ASCII spaces removed."""\n    return text.strip(" ")\n''',
        "tests/test_textutil.py": '''import unittest\n\nfrom textutil import normalize\n\n\nclass TestNormalize(unittest.TestCase):\n    def test_leading_trailing_spaces(self):\n        self.assertEqual(normalize("  Hi  "), "Hi")\n\n    def test_empty(self):\n        self.assertEqual(normalize("   "), "")\n''',
    },
    "e2_amount": {
        "amount.py": '''"""Tiny amount formatter."""\n\n\ndef format_amount(value):\n    """Format a numeric amount with two decimals."""\n    return f"{value:.2f}"\n''',
        "tests/test_amount.py": '''import unittest\n\nfrom amount import format_amount\n\n\nclass TestFormatAmount(unittest.TestCase):\n    def test_float(self):\n        self.assertEqual(format_amount(1.5), "1.50")\n\n    def test_int(self):\n        self.assertEqual(format_amount(2), "2.00")\n''',
    },
    "e3_config": {
        "config.py": '''"""Tiny configuration parser."""\n\n\ndef parse(text):\n    """Parse simple key=value lines."""\n    result = {}\n    for line in text.splitlines():\n        line = line.strip()\n        if not line or line.startswith("#"):\n            continue\n        key, value = line.split("=", 1)\n        result[key.strip()] = value.strip()\n    return result\n''',
        "tests/test_config.py": '''import unittest\n\nfrom config import parse\n\n\nclass TestParse(unittest.TestCase):\n    def test_simple_pairs(self):\n        self.assertEqual(parse("a=1\\nb=2"), {"a": "1", "b": "2"})\n\n    def test_comment_line(self):\n        self.assertEqual(parse("# comment\\na=1"), {"a": "1"})\n''',
    },
}

HIDDEN = {
    "e1_textutil": '''import unittest\n\nfrom textutil import normalize\n\n\nclass TestHidden(unittest.TestCase):\n    def test_mixed_whitespace(self):\n        self.assertEqual(normalize("  Hello\\t World\\n"), "Hello World")\n\n    def test_nbsp(self):\n        self.assertEqual(normalize("a\\u00a0\\u00a0b"), "a b")\n\n    def test_existing(self):\n        self.assertEqual(normalize("  Hi  "), "Hi")\n''',
    "e2_amount": '''import inspect\nimport unittest\nfrom decimal import Decimal\n\nfrom amount import format_amount\n\n\nclass TestHidden(unittest.TestCase):\n    def test_string(self):\n        self.assertEqual(format_amount(" 3.5 "), "3.50")\n\n    def test_decimal(self):\n        self.assertEqual(format_amount(Decimal("2.5")), "2.50")\n\n    def test_signature(self):\n        self.assertEqual(str(inspect.signature(format_amount)), "(value)")\n\n    def test_existing(self):\n        self.assertEqual(format_amount(1.5), "1.50")\n''',
    "e3_config": '''import unittest\n\nfrom config import parse\n\n\nclass TestHidden(unittest.TestCase):\n    def test_hash_in_value(self):\n        self.assertEqual(parse("token=abc#def"), {"token": "abc#def"})\n\n    def test_quoted_values(self):\n        self.assertEqual(parse('name="a=b"\\nnote="x # y"'), {"name": "a=b", "note": "x # y"})\n\n    def test_trailing_comment(self):\n        self.assertEqual(parse("a=1 # trailing"), {"a": "1"})\n''',
}


def write_files(out: Path) -> None:
    for task in TASKS:
        root = out / "fixtures" / task
        (root / "tests").mkdir(parents=True, exist_ok=True)
        for rel, text in FILES[task].items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        hidden_dir = out / "hidden-tests" / task
        hidden_dir.mkdir(parents=True, exist_ok=True)
        (hidden_dir / "test_hidden.py").write_text(HIDDEN[task], encoding="utf-8", newline="\n")
    oracle = {
        "schema": "r17-causal-oracle-v1",
        "tasks": [
            {"id": "E1", "task": "e1_textutil", "critical_omission": "any required whitespace fact missing"},
            {"id": "E2", "task": "e2_amount", "critical_omission": "string/Decimal support, stdlib only or signature changed"},
            {"id": "E3", "task": "e3_config", "forbidden": "line.split('#', 1) style comment stripping"},
        ],
    }
    (out / "benchmark-oracles.json").write_text(
        json.dumps(oracle, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def env_for(repo: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(repo)
    return env


def preflight(out: Path, python: str) -> int:
    bad = 0
    for task in TASKS:
        repo = out / "fixtures" / task
        hidden = out / "hidden-tests" / task
        visible = subprocess.run(
            [python, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=str(repo), env=env_for(repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        hidden_proc = subprocess.run(
            [python, "-m", "unittest", "discover", "-s", str(hidden), "-v"],
            cwd=str(repo), env=env_for(repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        ok = visible.returncode == 0 and hidden_proc.returncode != 0
        print(f"{task}: visible_rc={visible.returncode} hidden_rc={hidden_proc.returncode} ok={ok}")
        bad += 0 if ok else 1
    return 1 if bad else 0


def verify(out: Path, python: str) -> int:
    bad = 0
    found = 0
    for task in TASKS:
        hidden = out / "hidden-tests" / task
        for condition in ("control", "sessionflow"):
            repo = out / "runs" / task / condition / "repo"
            if not repo.exists():
                continue
            found += 1
            proc = subprocess.run(
                [python, "-m", "unittest", "discover", "-s", str(hidden), "-v"],
                cwd=str(repo), env=env_for(repo), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            print(f"{task}/{condition}: hidden_rc={proc.returncode}")
            bad += 0 if proc.returncode == 0 else 1
    if not found:
        print("no Agent B run directories found; deterministic setup only")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="r17-causal-artifact")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--action", choices=("generate", "preflight", "verify", "all"), default="all")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if args.action in ("generate", "all"):
        write_files(out)
        print(f"generated fixtures at {out}")
    if args.action in ("preflight", "all"):
        rc = preflight(out, args.python)
        if rc:
            return rc
    if args.action in ("verify", "all"):
        return verify(out, args.python)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
