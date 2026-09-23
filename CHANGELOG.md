# ChangeLog

All notable changes to Voyager are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are dated.

## [Unreleased]

> Headline: the "Claude Code has no automatic session-start trigger" conclusion
> recorded under 0.3.0 was **wrong**, and the cause was in Voyager, not in the
> provider. Full analysis in `claude_continuity_verdict.md`.

### Fixed
- **Native `SessionStart` hook registration (RC6)** — `ClaudeIntegration.install()`
  emitted an invented *flat* schema (`hooks.SessionStart[] = {command, ...}`) that
  Claude Code does not read, and the CLI's `integrate install` never called it at
  all. `install()` now writes the real nested shape
  (`hooks.SessionStart[].hooks[]` with `{"type": "command", "command": …,
  "timeout": 120}` and a `matcher` selecting the source), upgrades any legacy flat
  entry in place, preserves unrelated settings, and backs the file up first.
  Added `hook_command()`, `remove()` and a `verify()` that rejects the legacy shape.
- **`voyager integrate` lifecycle (RC7)** — `integrate remove` raised
  `AttributeError: 'NoneType' object has no attribute 'json'` for *every* provider;
  `remove` now accepts `--json` like the other subcommands. `install` / `status` /
  `remove` now agree on the same hook state, and `status` prints the hook result.
- **`integrate install` and `integrate status` disagreed on `startup_status`** —
  the two code paths keyed off *different* fields (install off
  `has_startup_hook`, status off `mcp_enabled`), so a machine with Codex MCP
  registered got `N` from `integrate install codex` and `A` from
  `integrate status` for the very same state. Both now share one rule.
- **A re-run of `integrate install` forgot an already-registered MCP server** —
  the MCP status was only set to `registered` on the branch that had just
  performed the registration, so idempotent re-runs downgraded the reported
  startup status.
- **`_check_mcp_support()` ignored its `home` argument** and read `Path.home()`
  instead, so every `--home`-scoped run silently inspected — and reported on —
  the real user profile rather than the directory it was pointed at.
- **SessionStart handler protocol** — the handler now emits a protocol-valid
  `hookSpecificOutput.additionalContext` payload and exits `0`; the context cap is
  measured in **UTF-16 code units** (Claude Code is JavaScript, so `len()` in
  Python undercounts non-BMP characters), truncation walks character by character,
  and the reported size is measured *before* truncation.
- **Context bundle did not survive the hook boundary** — the compiled bundle was
  cached in process memory, which is useless when every hook invocation is a fresh
  process. It is now persisted in the SQLite `meta` table, keyed by
  `ctx_cache:<thread>:<provider>:<budget>`, with hardening against
  `NaN`/`Infinity`/`OverflowError` inputs that previously passed `float()` and
  silently froze the cache.
- **`GrokIntegration.verify()`** — unguarded `stat()` on a possibly-missing
  launcher, plus a POSIX execute-bit check that can never pass on Windows
  (`chmod(0o755)` leaves `st_mode == 0o100666` there). The execute-bit requirement
  is now applied only on POSIX.
- **Capability/method shadowing** — six integration classes set
  `self.capabilities = None` while also defining a `capabilities()` method, so the
  instance attribute silently shadowed the method. Renamed to `_capabilities`.
- **`has_session_start_hook` hardcoded `False` for Claude** in
  `voyager/integrations/capabilities.py` and left stale after the integration
  shipped; it now reflects real detection (`config_valid` / `hook_invoked` are
  tracked separately from the platform ceiling).
- **A Unicode assertion that never executed** — `"\u4e00" <= ord(c)` compared a
  string to an int and would have raised had it ever run; corrected to a range
  check. It had been silently masked.
- **`voyager integrate status` legend** did not mention the new `H` state.

### Added
- `H` startup status: **native hook registered, live trigger not yet verified**.
  Only `Y` claims that a provider fires the hook by itself, and nothing claims `Y`.
- `tests/test_claude_session_start_hook.py` (29 tests) — output protocol, payload
  cap, UTF-16 length accounting, spill file, logging, log rotation, isolation.
- `tests/test_context_cache.py` (39 tests) — cache helpers, hostile input,
  end-to-end persistence, static guards.
- `scripts/verify_claude_sessionstart.py` — end-to-end hook verifier that fails
  loudly instead of passing on skips.
- `tests/test_cli.py` — four regression tests pinning the lifecycle invariants
  above: install/status must agree per provider, the `Y`/`H`/`A`/`N` legend must
  hold, an already-registered MCP must still report assisted, and
  `_check_mcp_support` must honour its `home` argument. Each was confirmed to
  fail against the pre-fix code.
- `.gitignore` entry for `.workbuddy-ai/` (agent-local memory, not product code).

### Removed
- Five repo-root scripts that shadowed or duplicated real tests:
  `test_claude_sessionstart.py`, `test_discovery_scan_chain.py`,
  `test_full_grok_continuity.py`, `test_grok_wrapper.py`,
  `test_grok_wrapper_windows.py`. Coverage was promoted into `tests/` or was
  already redundant; `claude_continuity_verdict.md` records each one. Note that
  `test_claude_sessionstart.py` *encoded the retracted conclusion* — it declared
  `SESSION_START_ZERO_TOUCH` if any settings key merely contained the substring
  `hook`.

### Notes
- Test suite: `321 collected → 303 passed, 18 skipped, 0 failed`.
- `SESSIONSTART_TRIGGER_LIVE_VERIFIED` remains **false**. Zero-Touch Final
  Acceptance remains **open**; it now depends on a single manual observation, not
  on further code.

## [0.3.0] — 2026-09-19

> Continuity Engine core milestone. All roadmap phases
> (1 → 1b → 2 → 3 → 4 → 5 → 6 → 7) landed; issue #8 is partially shipped
> (API/bridge/scaffold), issue #10 stays open (codex/grok writers shipped,
> claude/dsh gated). Post-1.0 backlog: docs/POST-1.0.md.

- **Local API + VS Code extension scaffold (roadmap Phase 7, issue #8)** —
  `voyager/api.py` is a thin JSON-able client surface over the same core
  (overview / thread_detail / sessions / bundle_preview — read-only, no
  direct SQLite in callers), served as stdio JSON-lines via
  `voyager api serve`. Contract tests include a real subprocess
  round-trip. `vscode-extension/` is a scaffold client: overview output
  channel, WorkThreads tree view, and a bundle-preview flow that renders
  the bundle with its token estimate; the Context Composer webview is
  pending. Launch stays in the CLI on purpose (lease-aware UX later).

- **`voyager switch <agent>` (roadmap Phase 6, issue #7)** — one command to
  continue the active WorkThread in another agent. Flow: incremental scan →
  thread resolution (--thread > --repo > cwd) → D13 lease (live foreign
  holders refuse and are named; stale take over; --steal is explicit and
  audited; same-provider transfers freely) → git dirty warning (never
  stash/reset) → same-provider native resume (D7 priority) XOR
  Continuation Bundle (goal/budget applied) → launch. Launch failure
  releases the lease (no dangling holder); the target agent's new session
  id is unknowable at launch, so a pending attach is recorded
  (`thread show` lists it; `thread attach <new-id>` resolves it — never
  fabricated). --no-launch prints the plan. Contract tests cover every
  error path (11 tests).

- **Phase 2 deferred items closed (issue #3)** — MCP
  `voyager_thread_list/show/attach/close` tools (thin wrappers over the
  Store API) and deterministic `continue --repo` resolution: the newest
  active WorkThread for that repo is picked loudly and only its members
  are compiled; no match falls back to the newest session. Multi-signal
  auto-clustering is re-scoped as a later enhancement under #3 — explicit
  threads are the product semantics. False-positive regression tests:
  same-repo sessions from another thread are never swallowed.

- **Voyager Skill (roadmap Phase 5, issue #6)** — `voyager skill install`
  copies a routing SKILL.md into the skill dirs of known agents (Codex
  `~/.codex/skills`, Claude Code `~/.claude/skills`, Grok `~/.grok/skills`).
  The skill is pure natural-language routing (when to brief/search/show/
  continue/handoff/merge, and explicit NEVERs: no full-export dumps, no
  provider session writes, no lease bypass). First install succeeds,
  repeats are idempotent, user-modified targets are refused without
  --force (backed up and overwritten with it), not-installed agents are
  skipped without fabricating directories, unknown agents get a manual
  path. Provider session dirs are never touched (test-asserted).

- **Context Budget (roadmap Phase 4, issue #5)** — `--budget
  compact|balanced|full|auto|Nk|<int>` on `handoff`, `merge` and
  `continue`. Ranking (Phase 3) runs first, then deterministic packing
  keeps sections by priority (goal > current state > ranked evidence >
  prior conclusions > failures > files > commands > conversation); the
  Evidence & Provenance header always survives and every drop/trim is
  named in a trailing "## Budget notes" section. Token estimate is
  chars/4 (no tokenizer dep); the CLI prints the estimate. No `--budget`
  = byte-identical output. Shared pipeline across single-session and
  multi-session paths.

- **Goal-conditioned extraction (roadmap Phase 3, issue #4 / D10)** —
  `--goal` now ranks evidence on `handoff`, `merge`, `continue --repo`
  and `continue --thread` through ONE shared deterministic pipeline
  (`voyager/ranker.py`): CandidateFact extraction (failure-biased,
  provenance-bound `session#seq`) + lexicon-boosted lexical ranking
  (goal coverage, path/command overlap, direct hits, recency,
  fact-type priority). With `--goal`, bundles gain a "Goal-ranked
  evidence" section and Files/Commands shrink to top-fact artifacts;
  without it, output is byte-compatible with pre-Phase-3. CI facts
  rank above README/UI for goal "fix CI" and reverse for "improve
  README" (contrast-tested). No LLM, no network, no new core deps.

- **Single-writer thread lease (roadmap Phase 2b, issue #11 / D13)** —
  `thread_leases` table with additive migration; atomic acquire via
  SQLite `BEGIN IMMEDIATE` (a live lease blocks the second writer and
  names its holder; an expired one never blocks — expiry = heartbeat
  >120s stale OR holder pid gone, probed cross-platform); token-bound
  renew/release; explicit `--steal`, with every grant/steal/release
  appended to an audit log (`~/.voyager/leases.log`); CLI: `voyager
  thread unlock [--steal]` and a lease line in `voyager thread show`;
  `voyager watch` doubles as the lease heartbeat (renews live pids,
  shortens its sleep to ≤30s while a lease is held). Consumers of the
  lock — `voyager switch` (#7) and transcript writers (#10) — stay
  gated behind it.
- **Continuity Engine Phase 1: `voyager merge` & multi-session context synthesis** (`voyager/continuity.py`):
  - Synthesize $N$ sessions across agents into a coherent Continuation Bundle (`voyager merge <s1> <s2> ...`).
  - Chronological overlay: newest session's "where work stopped" is active state; older proposals are cataloged under **Prior assistant conclusions (may be superseded)** with source IDs and timestamps.
  - Automatic deduplication of touched files, executed commands, and non-zero exit errors.
  - Live git snapshot at bundle time (branch, HEAD commit, dirty file status).
  - `voyager continue --from <s1,s2,...>` to pick up work across multiple sessions.
  - Global bundle storage in `~/.voyager/bundles/` by default, avoiding working tree leftovers.
  - Added `voyager_merge` tool to the MCP server (`voyager/mcp_server.py`).
  - Direct `pytest` runner support via `pythonpath = ["."]` in `pyproject.toml`.
- **Continuity Engine roadmap** (`docs/ROADMAP.md`, `docs/ROADMAP.zh-CN.md`):
  the next product jump is compiling many sessions across agents into the
  context the next agent needs — not a TUI-first viewer. Phases: multi-session
  merge → WorkThread → `--goal` → Context Budget → Skill → `switch` →
  VS Code sidebar last. Decision records D9 / D10 in `docs/DECISIONS.md`.
- **Test suite + CI** — the project went from one test file to a full regression matrix:
  per-adapter regression tests for all 8 platforms against synthetic
  fixtures (`tests/fixtures/`, no real session data), plus store, export,
  handoff, CLI and MCP tests. `.github/workflows/test.yml` runs them on
  Python 3.10–3.13 (Linux) and 3.10/3.13 (Windows), and a core-only job
  proves the CLI works with no optional dependencies. README badges added.
  **Real-provider dogfood tests**: Codex/Claude startup discovery tested
  via `voyager-zero-touch-test/` — confirmed neither invokes `voyager_startup`
  automatically at session start → classification: STARTUP_ASSISTED.
  ⚠️ **Retracted — see the Unreleased section.** The *observation* (nothing fired)
  was real; the *attribution* was not. The cause was Voyager's own installer
  writing an invented hook schema and never being called by `integrate install`,
  not a platform limitation. Do not cite this entry as evidence about provider
  capability.
- `scripts/run_tests_core_only.py` — runs the suite with `mcp` and
  `zstandard` blocked, i.e. exactly what `pip install voyager` gives you.
- **Test count**: Suite grew from 188 → 208 tests (added integration tests for
  MCP registration + idempotent removal). Core-only mode verified: all optional
  dependencies properly gated.
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

- **Index freshness (roadmap Phase 1b)** — `continue` / `handoff` / `merge`
  run an incremental scan before reading sessions and print a freshness
  line (`freshness: scanned in 0.4s, 2 source(s) changed`);
  `VOYAGER_NO_SYNC=1` skips it (tests, offline inspection).

- **WorkThreads (roadmap #3, Phase 2a)** — task-centric continuity: threads +
  thread_sessions tables (additive migration; existing index.db upgrades in
  place), `voyager thread list/show/create/attach/close`, `merge A B C`
  now also yields/updates a thread with exactly that member set, and
  `continue --thread <id>` recompiles from members. Bare `voyager
  continue` is task-centric: cwd -> repo -> active thread -> newest member
  (native resume, or compile + handoff). Membership is explicit-only —
  same-repo sessions are never auto-swallowed; closing a thread keeps its
  sessions.

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
- Prune is now source-aware: sessions seeded without a source row are
  never auto-pruned, and a session is dropped only when ALL of its source
  files vanish from disk (previously an unrelated scan could wipe
  manually-seeded sessions).
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
