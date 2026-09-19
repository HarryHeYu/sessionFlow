# Post-1.0 — Polish & Ecosystem Backlog

> Continuity Engine core (index → WorkThread → goal rank → budget →
> skill → lease → switch → API client) is **shipped** as of `311645e`.
> Everything in this file is explicitly **not started** — it is the
> next-phase backlog, recorded so nobody has to remember it.

## 1. VS Code Context Composer (issue #8, remainder)

The extension scaffold ships an overview channel, a WorkThreads tree and a
bundle-preview flow. Still pending:

- Context Composer webview: checkbox session picker → live bundle preview
  → token estimate → launch button
- one-click Switch (today launch stays in the CLI: `voyager switch
  <agent> --launch` — the lease flow makes unattended launch risky to UX)
- timeline view mixing agent events with git history
- packaging: icon, marketplace listing, published VSIX

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
- ZCode incremental watermark (per-session max sequence) instead of
  full-provider re-scan on mtime change
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
