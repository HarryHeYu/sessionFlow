"""The public continuity-proof demo must run from a clean isolated directory."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "continuity-proof"


class TestContinuityProofDemo(unittest.TestCase):
    def run_script(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "VOYAGER_NO_SYNC": "1",
        })
        return subprocess.run(
            [sys.executable, str(DEMO / name), *args], cwd=str(ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=240,
        )

    def test_control_and_sessionflow_conditions(self) -> None:
        base = Path(os.environ.get("SESSIONFLOW_DEMO_TEST_TMPDIR", tempfile.gettempdir()))
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sf-proof-", dir=str(base)) as tmp:
            out = Path(tmp) / "run"

            setup = self.run_script("setup.py", "--out", str(out))
            self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
            evidence = json.loads(
                (out / "evaluator" / "setup-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["visible_rc"], 0)
            self.assertEqual(evidence["hidden_rc"], 1)
            self.assertFalse(evidence["hidden_fact_in_visible_repo"])

            control = self.run_script("verify.py", "--out", str(out), "--condition", "control")
            self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
            control_result = json.loads(
                (out / "evaluator" / "verify-control.json").read_text(encoding="utf-8"))
            self.assertTrue(control_result["verifier_pass"])
            self.assertEqual(control_result["hidden_rc"], 1)

            continuation = self.run_script("make_continuation.py", "--out", str(out))
            self.assertEqual(continuation.returncode, 0, continuation.stdout + continuation.stderr)
            self.assertTrue((out / "continuation.md").is_file())
            self.assertIn("whitespace", (out / "continuation.md").read_text(encoding="utf-8").lower())

            # Deterministic stand-in for Agent B after reading the continuation.
            (out / "app" / "textutil.py").write_text(
                '"""Small text helpers."""\n\nimport re\n\n\n'
                'def normalize(text: str) -> str:\n'
                '    return re.sub(r"\\s+", " ", text).strip()\n',
                encoding="utf-8", newline="\n",
            )
            sessionflow = self.run_script(
                "verify.py", "--out", str(out), "--condition", "sessionflow")
            self.assertEqual(sessionflow.returncode, 0, sessionflow.stdout + sessionflow.stderr)
            result = json.loads(
                (out / "evaluator" / "verify-sessionflow.json").read_text(encoding="utf-8"))
            self.assertTrue(result["verifier_pass"])
            self.assertEqual(result["hidden_rc"], 0)


if __name__ == "__main__":
    unittest.main()
