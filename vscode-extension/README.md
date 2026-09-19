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

## What is scaffold / pending

- Context Composer webview (checkbox session picker → live preview →
  launch) — the API op (`bundle_preview`) already exists
- One-click Switch (uses the same core as `voyager switch`; the lease flow
  makes unattended launch risky, so launch stays manual for now)
- Packaging/icon (media/voyager.svg is a placeholder)

## Try it

1. Install voyager: `pip install -e ".[all]"` (or point `voyager.pythonPath`
   at an interpreter that has it).
2. `voyager scan` once so the index exists.
3. Open this folder in VS Code and press F5 (Extension Development Host).
4. Run the commands from the Command Palette.

The extension never touches `~/.voyager/index.db` or provider session
files directly — every operation goes through the local API, which is the
same core the CLI uses.
