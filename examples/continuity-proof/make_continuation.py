#!/usr/bin/env python3
"""Compile a real sessionFlow continuation from the seeded prior session."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_CORE_ROOT = Path(__file__).resolve().parents[2]


def demo_env(out: Path, core_root: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    home = out / "home"
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "VOYAGER_NO_SYNC": "1",
    })
    if core_root is not None:
        env["PYTHONPATH"] = str(core_root)
    return env


def cli(python: str, out: Path, args: list[str], core_root: Path | None) -> dict[str, object]:
    db = out / "home" / ".voyager" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [python, "-m", "voyager.cli", "--db", str(db), *args],
        cwd=str(out / "app"), env=demo_env(out, core_root),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    return {
        "argv": args,
        "returncode": proc.returncode,
        "stdout": proc.stdout[:8000],
        "stderr": proc.stderr[:4000],
    }


def first_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        stripped = line.strip()
        if (not stripped or stripped.startswith("ID") or stripped.startswith("-")
                or stripped.startswith("no sessions") or "session(s)" in stripped):
            continue
        return stripped.split()[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="continuity-proof-run")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--core-root", default=None,
                        help="Core checkout root; auto-detected when running from this repository")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    if not (out / "app").is_dir():
        raise SystemExit(f"fixture not found in {out}; run setup.py first")

    core_root = Path(args.core_root).resolve() if args.core_root else None
    if core_root is None and (DEFAULT_CORE_ROOT / "voyager" / "cli.py").is_file():
        core_root = DEFAULT_CORE_ROOT

    app = str(out / "app").replace("\\", "/")
    continuation = out / "continuation.md"

    scan = cli(args.python, out, ["scan", "--platform", "codex", "--force"], core_root)
    listed = cli(args.python, out, ["list"], core_root)
    session_id = first_id(str(listed["stdout"]))
    if not session_id:
        raise SystemExit(f"no seeded session was indexed:\n{listed['stdout']}\n{listed['stderr']}")

    created = cli(args.python, out, [
        "thread", "create", "--repo", app,
        "--title", "Public continuity proof",
        "--goal", "Finish the pending textutil requirement from Agent A.",
        "--attach", session_id,
    ], core_root)
    thread_id = None
    for line in str(created["stdout"]).splitlines():
        if line.startswith("Thread ") and " created" in line:
            thread_id = line.split()[1]
            break
    if not thread_id:
        raise SystemExit(f"no thread id was returned:\n{created['stdout']}\n{created['stderr']}")

    handoff = cli(args.python, out, [
        "continue", "--repo", app, "--thread", thread_id, "--to", "codex",
        "--bundle", "--no-launch", "--output", str(continuation),
    ], core_root)

    evidence = {
        "schema": "public-continuity-proof-continuation-v1",
        "session_id": session_id,
        "thread_id": thread_id,
        "continuation": str(continuation),
        "continuation_exists": continuation.exists(),
        "steps": {"scan": scan, "list": listed, "thread": created, "continue": handoff},
    }
    (out / "evaluator").mkdir(parents=True, exist_ok=True)
    (out / "evaluator" / "continuation-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )

    print(f"session={session_id} thread={thread_id}")
    print("rc:", {k: v["returncode"] for k, v in evidence["steps"].items()})
    print(f"continuation_exists={continuation.exists()} path={continuation}")
    return 0 if all(v["returncode"] == 0 for v in evidence["steps"].values()) and continuation.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
