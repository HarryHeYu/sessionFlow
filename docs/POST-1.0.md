# Post-1.0 — Polish & Ecosystem Backlog

> Continuity Engine core (index → WorkThread → goal rank → budget →
> skill → lease → switch → API client) is **shipped** as of `311645e`.
> Everything in this file is explicitly **not started** — it is the
> next-phase backlog, recorded so nobody has to remember it.

## 1. VS Code Context Composer (issue #8, remainder)

**Shipped 2026-10-02**: the Context Composer webview — checkbox session
picker scoped to the repo, goal + budget controls, live Continuation Bundle
preview with the token estimate, dropped/trimmed reporting, and a copyable
CLI command. The activity-bar icon `media/voyager.svg` also now exists;
`package.json` had pointed at it since the scaffold landed, but `media/` was
never created, so the icon had always been blank.

Still pending:

- one-click Switch. The Composer deliberately hands you the command rather
  than launching: `bundle_preview` writes nothing, and the lease flow (D13)
  is what decides who may write a WorkThread, so an unattended launch from a
  webview would route around the core's one real safety property.
- timeline view mixing agent events with git history
- packaging: marketplace listing, published VSIX

## 2. Auto-clustering research (re-scoped from issue #3)

`continue --repo` currently resolves the newest **active, persisted**
WorkThread deterministically — silent clustering was deliberately rejected.
Research backlog, only behind an explicit opt-in:

- multi-signal scoring (repo_root + 48h window + branch + file overlap +
  FTS title/goal overlap) with a confidence threshold and a loud
  "clustered these sessions" preview before anything persists
- false-positive regression corpus built from real multi-task repos

## 3. Transcript writer gates (issue #10, remainder)

Current support: **codex ✅ / grok ✅** (probe HIT, lease-gated, new id
only). Still unsupported, each needs its own repeatable gate:

- **Claude** — headless `claude -p --resume` of a synthetic JSONL timed
  out (60s) on 2026-09-16; re-probe periodically
- **DSH** — synthetic-session resume unverified; probe required
- after any HIT: writer + fixture round-trip tests, then flip the gate

## 4. Performance & robustness

- **scoped scan** — provider/repo-limited incremental scans (Phase 1b
  follow-up; today `continue/handoff/merge` scan all providers)
- **filesystem event watcher** — replace the 300s `voyager watch` poll
  with OS file events (correctness must not depend on it, D12)
- ~~ZCode incremental watermark (per-session max sequence) instead of
  full-provider re-scan on mtime change~~ — **measured 2026-10-02 and NOT
  justified.** The full re-scan's wall clock is ~100 % process-startup tax
  (31 `git` spawns × 1.34 s in this sandbox), not database work: the parse
  itself is sub-second, and `git_info`'s per-cwd cache already collapses 78
  sessions to 9 lookups, so there is no N+1 to remove. Building the watermark
  would optimise a cost that does not exist while adding a "missed session"
  risk. See DECISIONS.md D4. Re-open only behind a non-sandbox measurement.
- Cursor token counts (currently often 0) — dig into `agentKv` usage
  payloads

## 5. Packaging & release polish

- publish to PyPI under a final name, then `pipx install <name>` becomes
  the README quickstart (three lines: install → scan → search)
- signed release tags + GitHub Releases with wheels
- optional `[llm]` extra (opt-in narrative refinement of bundles; never
  in core, D10)
- `voyager upgrade` / version pinning notes

## 6. Ecosystem

- more adapters (OpenCode, Goose, Aider, Continue — survey in
  docs/RECON.md §0 lists them as not-installed/unverified)
- **MCP**: `voyager_continue` / `voyager_switch` / `voyager_context` / 
  `voyager_thread_list/show/attach/close` are all shipped and available
- real-world dogfood pass: run the full flow against multi-week, multi-agent
  history and file robustness issues as they surface
