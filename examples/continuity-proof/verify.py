#!/usr/bin/env python3
"""Deterministically verify one public continuity-proof run."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run_tests(python: str, app: Path, test_dir: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(app),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.run(
        [python, "-m", "unittest", "discover", "-s", str(test_dir), "-q"],
        cwd=str(app), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="continuity-proof-run")
    parser.add_argument("--condition", choices=("control", "sessionflow"), required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    out = Path(args.out).resolve()
    app = out / "app"
    hidden = out / "evaluator" / "hidden_tests"
    setup_file = out / "evaluator" / "setup-evidence.json"
    if not app.is_dir() or not hidden.is_dir() or not setup_file.is_file():
        raise SystemExit(f"incomplete fixture in {out}; run setup.py first")

    setup = json.loads(setup_file.read_text(encoding="utf-8"))
    visible_rc, visible_out = run_tests(args.python, app, app / "tests")
    hidden_rc, hidden_out = run_tests(args.python, app, hidden)

    continuation = out / "continuation.md"
    continuation_ok = continuation.is_file()
    if args.condition == "control":
        ok = (setup["visible_rc"] == 0 and setup["hidden_rc"] != 0
              and not setup["hidden_fact_in_visible_repo"]
              and visible_rc == 0 and hidden_rc != 0)
    else:
        evidence_file = out / "evaluator" / "continuation-evidence.json"
        ok = (visible_rc == 0 and hidden_rc == 0 and continuation_ok
              and evidence_file.is_file()
              and json.loads(evidence_file.read_text(encoding="utf-8"))["continuation_exists"])

    result = {
        "schema": "public-continuity-proof-verification-v1",
        "condition": args.condition,
        "setup_visible_rc": setup["visible_rc"],
        "setup_hidden_rc": setup["hidden_rc"],
        "visible_rc": visible_rc,
        "hidden_rc": hidden_rc,
        "continuation_exists": continuation_ok,
        "expected_outcome": "hidden test fails" if args.condition == "control" else "hidden test passes",
        "verifier_pass": ok,
    }
    (out / "evaluator" / f"verify-{args.condition}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )

    print(f"condition={args.condition} visible_rc={visible_rc} hidden_rc={hidden_rc}")
    print(f"expected: {result['expected_outcome']}")
    if visible_out:
        print("visible tests:\n" + visible_out[-1200:])
    if hidden_out:
        print("hidden tests:\n" + hidden_out[-1200:])
    print(f"VERIFIER_{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
