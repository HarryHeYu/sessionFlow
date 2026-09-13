# ChangeLog

All notable changes to Voyager are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are dated.

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
