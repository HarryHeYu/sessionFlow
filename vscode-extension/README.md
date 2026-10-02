# Voyager VS Code extension — SCAFFOLD

This directory is the **Phase 7 client** for the voyager local API
(`voyager api serve`, stdio JSON-lines). It is a thin client: all logic
(indexing, ranking, budget packing, lease handling) lives in voyager core.

## What works today

- stdio JSON-lines bridge to `python -m voyager.api serve`
  (overview / thread_detail / sessions / bundle_preview ops)
- `Voyager: Show Overview` — index stats, active WorkThreads and recent
  sessions in an output channel
- WorkThreads tree view (activity bar → Voyager)
- `Voyager: Preview Continuation Bundle` — renders a bundle in a Markdown
  tab with the token estimate
- **`Voyager: Open Context Composer`** (also the `＋` on the WorkThreads view
  title) — the Phase 7 composer:
  - checkbox session picker, scoped to the configured repo, newest first
  - goal input + budget selector (`compact` / `balanced` / `full` / `auto`)
  - live Continuation Bundle preview with the token estimate, and the
    dropped / trimmed section names when a budget bites
  - `Copy CLI command` — yields the equivalent
    `voyager handoff <id> --goal "…" --budget … --to <agent>` or
    `voyager merge <ids> …`, ready to paste
  - `Copy bundle` — the rendered Markdown

## What is scaffold / pending

- One-click Switch: the Composer deliberately hands you a command instead of
  launching. `bundle_preview` writes nothing, and the lease flow (D13) is what
  decides who may write a WorkThread — starting an agent from a webview would
  route around the one safety property the core actually guarantees.
- timeline view mixing agent events with git history
- packaging: marketplace listing, published VSIX

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
