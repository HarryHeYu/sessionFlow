# Voyager VS Code extension

The **Phase 7 client** for the voyager local API (`voyager api serve`, stdio
JSON-lines). It is a thin client: all logic (indexing, ranking, budget packing,
lease handling) lives in voyager core.

Shipped: the WorkThreads tree view, Overview, bundle preview, the Context
Composer, the WorkThread timeline, and one-click switch. Not shipped:
marketplace listing and a published VSIX — see *What is still pending*.

## What works today

- stdio JSON-lines bridge to `python -m voyager.api serve`
  (overview / thread_detail / sessions / bundle_preview ops)
- `Voyager: Show Overview` — index stats, active WorkThreads and recent
  sessions in an output channel
- WorkThreads tree view (activity bar → Voyager)
- `Voyager: Preview Continuation Bundle` — renders a bundle in a Markdown
  tab with the token estimate
- **`Voyager: Open WorkThread Timeline`** (also the `＋` on the WorkThreads view
  title) — the O3 lifecycle view: what happened to one WorkThread, in order,
  across every agent that touched it. Filters for provider, event type and
  live/retained. It is a **consumer** of the `thread_timeline` API op, which is
  a pass-through to the canonical `voyager.timeline.build_thread_timeline` — the
  same function the CLI and the dashboard use. It aggregates nothing itself,
  because two timelines would drift and the drift would be invisible.
- **`Voyager: Open Context Composer`** (also on the WorkThreads view title) — the
  Phase 7 composer:
  - checkbox session picker, scoped to the configured repo, newest first
  - goal input + budget selector (`compact` / `balanced` / `full` / `auto`)
  - live Continuation Bundle preview with the token estimate, and the
    dropped / trimmed section names when a budget bites
  - `Copy CLI command` — yields the equivalent
    `voyager handoff <id> --goal "…" --budget … --to <agent>` or
    `voyager merge <ids> …`, ready to paste
  - `Copy bundle` — the rendered Markdown

## What is still pending

- packaging: marketplace listing, published VSIX
- timeline view mixing agent events with git history

## One-click Switch — shipped, and how it stays safe

`Voyager: Switch WorkThread to another agent` (the inline action on a thread in
the WorkThreads view) is shipped. It does **not** launch the agent from the
webview: it runs `voyager switch <agent>` in a terminal. The lease flow (D13)
is what decides who may write a WorkThread, and starting an agent directly from
a webview would route around the one safety property the core guarantees.

## Timeline: what it will and will not do

The timeline shows only what the canonical data proves — a column that already
carries a time, or the append-only `thread_events` log. It never scans assistant
prose: "this sentence looks like a milestone" is a guess, and a timeline that
guesses is worse than a short one (DECISIONS D16).

- **It will not switch anything.** Switching decides who may write a WorkThread,
  and that lives in the handoff engine (`continuity.handoff_thread`). The view
  offers *Copy switch command* instead — there is no second switch path.
- **A retained session is history, not a place to switch into.** Its provider
  source is gone, so the command offered is the thread-level one; view / search /
  summarise / inspect are all still available, native resume is not.
- Timestamps order the display and nothing else. They never settle a WorkThread
  ambiguity or establish authority.

## Composer safety notes

- The webview is one self-contained document with a nonce-based CSP: no CDN,
  no network, no external asset.
- Session titles and paths come from agent transcripts and are untrusted, so
  every field is written with `textContent` — never as markup.
- Nothing in the Composer writes: no file, no provider store, no index. The
  only side effect a click can have is putting text on your clipboard.
- `tests/test_vscode_extension.py` guards the parts CI cannot run: that
  `package.json` only points at files that exist, that every contributed
  command has a handler, that the document is self-contained, and that the
  embedded script still parses (the document is built from a JS template
  literal, so a stray backtick silently truncates it).

## Try it

1. Install voyager: `pip install -e ".[all]"` (or point `voyager.pythonPath`
   at an interpreter that has it).
2. `voyager scan` once so the index exists.
3. Open this folder in VS Code and press F5 (Extension Development Host).
4. Run the commands from the Command Palette.

The extension never touches `~/.voyager/index.db` or provider session
files directly — every operation goes through the local API, which is the
same core the CLI uses.
