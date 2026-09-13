"""Turn a pytest log into GitHub annotations.

Job logs need an authenticated token; check-run annotations do not, so CI runs
pytest with `--tb=short -rf`, keeps the output in ``pytest.log`` and calls this
script when the run fails. The failing tests (and, if pytest died before
printing a summary, the tail of the log) then show up in the Actions UI *and*
in the checks API.

Usage: python scripts/ci_report_failures.py [pytest.log]
"""

from __future__ import annotations

import pathlib
import sys

MAX_ANNOTATIONS = 10
MAX_LEN = 900      # GitHub truncates annotation messages well below this


def main(argv: list[str]) -> int:
    log = pathlib.Path(argv[1] if len(argv) > 1 else "pytest.log")
    if not log.is_file():
        print(f"::error title=pytest::no log at {log}")
        return 0
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    failures = [ln.strip() for ln in lines
                if ln.startswith(("FAILED", "ERROR"))]
    if not failures:
        tail = " / ".join(ln.strip() for ln in lines[-6:] if ln.strip())
        print(f"::error title=pytest (no summary line)::{tail[:MAX_LEN]}")
        return 0
    if len(failures) > MAX_ANNOTATIONS:
        print(f"::warning title=pytest::{len(failures)} failures, "
              f"showing the first {MAX_ANNOTATIONS}")
    for ln in failures[:MAX_ANNOTATIONS]:
        print(f"::error title=pytest::{ln[:MAX_LEN]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
