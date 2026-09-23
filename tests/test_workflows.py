"""Guards for the CI workflow files themselves.

Two mistakes are easy to make and expensive to notice:

* an **indented heredoc terminator** — bash never sees the `PY` line, swallows
  the rest of the step script and the job dies with exit code 2. That is
  literally how a green test suite turned into 8 red CI jobs: pytest never ran,
  so the annotations stayed empty and only "Process completed with exit code 2"
  was visible;
* a silently dropped Python version or OS from the test matrix.

Both are checked without any third-party dependency, so they also run in the
core-only CI job.
"""

from __future__ import annotations

from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _workflow_files():
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files, f"no workflow files under {WORKFLOWS}"
    return files


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _run_blocks(text: str):
    """Yield the lines of every `run: |` block, with the block's base indent.

    YAML strips the common indentation of a block scalar, so a line's
    *effective* column inside the resulting shell script is
    ``indent - base``. A heredoc terminator is only valid at effective column 0.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() in ("run: |", "run: |-", "run: >", "run: >-"):
            run_indent = _indent(line)
            block, j = [], i + 1
            while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > run_indent):
                block.append(lines[j])
                j += 1
            body = [ln for ln in block if ln.strip()]
            base = min((_indent(ln) for ln in body), default=run_indent + 2)
            yield i + 1, base, body
            i = j
        else:
            i += 1


def test_heredoc_terminators_land_at_column_zero():
    """An indented `PY` makes bash swallow the rest of the step script.

    That is exactly how a green test suite turned into 8 red CI jobs
    ("Process completed with exit code 2"): pytest never ran, so the failure
    annotations stayed empty and there was nothing else to look at.
    """
    checked = 0
    for wf in _workflow_files():
        text = wf.read_text(encoding="utf-8")
        for offset, base, body in _run_blocks(text):
            for n, line in enumerate(body):
                if "<<'PY'" not in line and '<<"PY"' not in line:
                    continue
                term = next((ln for ln in body[n + 1:] if ln.strip() == "PY"), None)
                assert term is not None, (
                    f"{wf.name}:{offset + n}: heredoc opened without a PY terminator"
                )
                assert _indent(term) == base, (
                    f"{wf.name}: a heredoc terminator must sit at the run block's "
                    f"base indentation ({base} spaces here, found {_indent(term)}), "
                    f"so that it starts at column 0 of the shell script; otherwise "
                    f"bash reads to EOF and the job dies with exit code 2"
                )
                checked += 1
    assert checked, "no heredoc steps found — did the workflow change?"


def test_test_workflow_covers_supported_pythons_and_platforms():
    text = (WORKFLOWS / "test.yml").read_text(encoding="utf-8")
    for version in ("3.10", "3.11", "3.12", "3.13"):
        assert f'"{version}"' in text, f"python {version} missing from the matrix"
    assert "ubuntu-latest" in text, "linux jobs missing"
    assert "windows-latest" in text, "windows jobs missing (adapters read %APPDATA%)"
    assert "core-only" in text, "the zero-optional-dependency job is gone"
    assert "pip install -e \".[all,dev]\"" in text, "extras install changed"


def test_failure_reporting_script_exists():
    """CI annotates failures through this script (job logs need a token)."""
    script = Path(__file__).resolve().parent.parent / "scripts" / "ci_report_failures.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "::error title=pytest::" in text
    for wf in _workflow_files():
        body = wf.read_text(encoding="utf-8")
        if "pytest" in body and "run test suite" in body.lower():
            assert "ci_report_failures.py" in body, (
                f"{wf.name} runs pytest but does not report failures as annotations"
            )


def test_sessionstart_verifier_can_be_pointed_at_a_scratch_home():
    """Guard: the verifier must not hardcode the real profile.

    `settings_path()` used to bake in `Path.home()`, so on any machine whose
    real profile has no Voyager hook -- that is, every fresh machine -- the
    script stopped at "settings.json does not exist" and proved nothing.
    `--home` is what makes the install -> settings.json -> shell -> hook-JSON
    chain reproducible, so losing it is a regression worth failing on.
    """
    repo_root = Path(__file__).resolve().parent.parent
    source = (repo_root / "scripts" / "verify_claude_sessionstart.py").read_text(
        encoding="utf-8"
    )
    assert '"--home"' in source, "verifier lost its --home flag"
    assert "def settings_path(home" in source, (
        "settings_path must accept a home argument rather than hardcoding "
        "Path.home()"
    )
