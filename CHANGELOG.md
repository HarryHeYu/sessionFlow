# ChangeLog

All notable changes to Voyager are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are dated.

## [Unreleased]

### Added
- **Test suite + CI** — the project went from one test file to 69 tests:
  per-adapter regression tests for all 8 platforms against synthetic
  fixtures (`tests/fixtures/`, no real session data), plus store, export,
  handoff, CLI and MCP tests. `.github/workflows/test.yml` runs them on
  Python 3.10–3.13 (Linux) and 3.10/3.13 (Windows), and a core-only job
  proves the CLI works with no optional dependencies. README badges added.
- `scripts/run_tests_core_only.py` — runs the suite with `mcp` and
  `zstandard` blocked, i.e. exactly what `pip install voyager` gives you.
- CI failures are now self-describing: the test steps write `pytest.log` and,
  on failure, `scripts/ci_report_failures.py` emits the failing tests as GitHub
  **annotations** (`::error title=pytest::`) — readable from the checks API and
  the UI without a token, unlike job logs.
- `tests/test_workflows.py` — dependency-free guards for the CI plumbing
  itself: every `python - <<'PY'` heredoc must terminate at column 0 of the
  step script, the matrix must still cover Python 3.10–3.13 plus Windows, and
  the failure-annotation reporter must stay wired up. (The heredoc guard exists
  because an indented terminator made bash swallow a whole step: exit code 2,
  pytest never ran, nothing to see.)
- **Architecture diagram** (`docs/screenshots/architecture.png`, drawn by
  `scripts/make_diagram.py`): 8 agents / 8 formats → one index → the six
  ways to use it. Embedded at the top of both READMEs.

### Fixed
- `--db` was only accepted *before* the subcommand (`voyager --db X stats`);
  `voyager stats --db X` died with an argparse usage error. It is now
  accepted on either side.
- **Test-suite isolation** — the autouse fixture in `tests/conftest.py`
  redirects *everything* a test could reach by accident: a pathless `Store()`,
  the working directory, and **every adapter's storage globals plus the
  `HOME`/`USERPROFILE`/`APPDATA` fallbacks**, all pointed at a non-existent
  directory. A test that forgets `patch_paths` now discovers nothing and fails
  loudly instead of reading — or overwriting — real agent data
  (`tests/test_adapters.py` asserts that for all 8 adapters,
  `tests/test_cli.py` asserts the guard itself). Verified: a full run leaves
  `~/.voyager/index.db` and the agent session files byte-identical, and the
  suite passes with an empty `HOME`/`APPDATA` (the CI condition).
- **A test really did destroy real data, and only CI noticed**:
  `test_dsh_corrupt_zstd_is_reported` never redirected the DSH adapter, so on a
  machine with DSH sessions it took the first *real*
  `~/.dsh/sessions/*/session.jsonl.zstd` and wrote `b"NOT-ZSTD"` over it; on CI
  (no DSH data) it failed with `IndexError` instead — which is how it surfaced,
  as 6 red matrix jobs. The session (`dsh:session-a459ebf4…`, 111 events /
  12 messages / 56 tool calls) was rebuilt from the index — intact provider rows
  reused verbatim, the 32 oversized rows rebuilt from the normalized columns,
  plus synthetic `session` / `session/title` / `request/context` rows —
  validated by re-parsing (identical title/model/counts) and re-indexed cleanly.
  The test now redirects its adapter like every other adapter test.
- **Test-suite safety net** (earlier, same class): an argparse default in the
  `--db` plumbing could make a CLI test fall back to `~/.voyager/index.db` —
  the CLI then scanned the fixture paths and pruned the real index's sessions
  of that provider (it happened once, to this project's own index; the data was
  restored by re-running `voyager scan`, which is the point of a derived
  index).
- **Packaging**: the MCP dependency was undeclared, so a fresh
  `pip install -e .` + `python -m voyager.mcp_server` died with
  `ModuleNotFoundError: No module named 'mcp'`. Added `[mcp]`, `[all]` and
  `[dev]` extras, and the server now prints the exact install command
  instead of a bare traceback. The `voyager-mcp` console script mentioned
  in the docs now actually exists (`[project.scripts]`).
- `voyager search` treated `-`, `:` and quotes as FTS5 operators
  (`voyager search "pytest -q"` errored with `no such column: q`); queries
  are now passed as one quoted phrase, so any user text is a substring
  search.
- Antigravity adapter: tool calls were dropped because the printable-run
  scanner hard-coded a 16-char minimum while the tool step asked for
  4-char runs (`run_command`, `toolu_…` ids never matched). Short strings
  are now extracted as documented.
- Antigravity adapter: only `https://` remotes were detected; scp-style
  `git@host:org/repo.git` URLs in the init payload are now recognized too.

### Changed
- **Architecture diagram re-laid out** (`scripts/make_diagram.py`): the three
  columns are now a real grid — left/right card stacks have the same width
  (368px) and the same height (532px), both outer margins are 56px, the two
  column gaps are equal (88px), and the `Voyager Index` box (416px wide, its
  lines centred) sits exactly on the canvas centre line, vertically centred on
  the stacks. Title, subtitle and footer lines are centred instead of
  left-aligned, the column headings share one row, and the vertical bus lines
  are plain lines again (the old version drew arrowheads on them, which read as
  a stray downward arrow). Canvas is 1440×880 and every box is sized against a
  worst-case monospace advance (0.62em), so the picture holds with Consolas,
  DejaVu Sans Mono or Menlo — the old middle box overflowed its text area by
  32px. A vector version, `docs/screenshots/architecture.svg`, comes out of the
  same layout, and `tests/test_diagram.py` guards the invariants (`pillow` moved
  into the `dev` extra so CI checks it too).
- Install docs lead with an isolated CLI install (`pipx install
  "voyager[all] @ git+…"`) instead of the developer-only `pip install -e .`.
- `CONTRIBUTING.md` documents the fixture-based test workflow and the rule
  that a new adapter ships with its fixture + test.
- Packaging metadata for the eventual PyPI page: `readme`, `keywords`,
  `classifiers`, `[project.urls]`, and the version now matches the CHANGELOG
  (it still said 0.1.0 while the 0.2.0 section was already released).

### Planned
- **PyPI release** (`pipx install voyager` / `pip install voyager[all]`):
  the install docs already show the final three-line flow; publishing needs
  `python -m build` + `twine upload` under the maintainer's account (the
  wheel builds clean: `voyager-0.2.0-py3-none-any.whl`).
- **GitHub repository metadata**: topics are still empty — set them with
  `gh repo edit HarryHeYu/voyager --add-topic ai,coding-agent,ai-agent,codex,claude-code,cursor,developer-tools,cli,mcp,sqlite,local-first,python`
  (or via the web UI). Homepage is unset as well.

## [0.2.0] — 2026-09-13

### Added
- **Cross-agent handoff** — `voyager handoff <id> --to claude|codex|grok`
  distills a session into a self-contained Context Package (goal,
  instructions, files, commands, errors, where the work stopped) and seeds
  a new session in the target agent; `--launch` starts it immediately.
  Non-launchable targets still get the package file.
- **`voyager continue [id]`** — one command to pick work back up: native
  resume for codex/claude/dsh/grok, automatic handoff package for the rest.
  Select the newest session, or by `--repo` / `--platform` / id prefix.
- **`voyager brief`** — compact digest of the last N hours of activity
  across all providers, designed to be read by an agent in one shot
  (`--hours`, `--repo`, `--limit`).
- **`voyager watch`** — keep the index current automatically; re-scans on a
  fixed interval, tolerates locked databases.
- **Four new adapters** — Grok CLI, Cursor (`state.vscdb`), Kiro IDE,
  Antigravity (experimental heuristic protobuf decode). Voyager now covers
  8 platforms.
- Global-instruction snippet for Codex (`AGENTS.md`) and Claude Code
  (`CLAUDE.md`) so agents discover voyager by default.

- **Docs** — `docs/API.md` (CLI + Python reference), `docs/FAQ.md`,
  real-output screenshots (`docs/screenshots/`), and a screenshot
  generator (`scripts/make_screenshot.py`).

- **MCP server** (`voyager-mcp` / `python -m voyager.mcp_server`) — exposes
  `voyager_brief/search/list/show/handoff` as native MCP tools; tested with
  Codex (`config.toml`), Claude Code (`claude mcp add`) and Cursor
  (`mcp.json`), so agents can query other agents' sessions without shell.

### Fixed
- Prune could delete sessions of a multi-session artifact (one SQLite DB →
  N sessions) because `sources` was keyed per path; source bookkeeping is
  now `(provider, path, sid)`.
- `voyager show` crashed on sessions with file-history snapshots.
- Claude parallel tool calls: only the first `tool_result` block was kept;
  exit code 0 was swallowed by an `or`.
- Cursor/Antigravity: several type-coercion crashes on real data.

### Changed
- Documentation rewritten (EN + 简体中文), no roadmap jargon; platform
  capability table covers all 8 providers.
- Raw/content/FTS payloads are tiered (8KB / 600KB / 600B) — full data
  always remains at the provider source.

## [0.1.0] — 2026-09-13

### Added
- Initial release: adapters for Codex (rollout JSONL, continuation
  grouping), Claude Code (JSONL + file-history version chain), ZCode
  (SQLite), DSH (zstd JSONL).
- CLI: `scan / list / show / search / repo / export / resume / files /
  diff / stats`.
- SQLite + FTS5(trigram) index; idempotent incremental scans; raw and
  normalized events stored side by side.
- MIT license, English + Chinese README, platform reconnaissance doc.
