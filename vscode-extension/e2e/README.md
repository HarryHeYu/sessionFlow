# Real Extension Host E2E harness — Core #59

> **Status: NOT VERIFIED in the sandbox that authored this harness.** Launching
> a VS Code window is prohibited there (see Core #59 "Environment note"). This
> directory is a ready-to-run harness for an **authorised machine with a GUI**.
> It does not claim the Extension Host run happened. The seed step was executed
> here (synthetic, isolated); the GUI launch was not.

## Why this exists

The three read-only surfaces (Selector / Timeline / Composer) and the stdio
bridge have unit + DOM-stub coverage, but a headless DOM-script run is not a
real Extension Host run. Core #59 tracks the residue: activation, WebviewPanel
lifecycle, command palette, host→webview message delivery, and child-process
cleanup. This harness executes those in a real, **isolated** Extension Host.

## What is verified vs not

| Layer | Where | Status here |
|---|---|---|
| Synthetic index seed (4 sessions, 2 WorkThreads, SOURCE_MISSING) | `seed_index.py` | **executed** (synthetic DB, isolated path) |
| Harness script syntax (`node --check`) | `run-test.mjs`, `suite/index.js` | **passed** |
| Extension activates + commands registered + WebviewPanel opens/closes | `suite/index.js` in a real host | **NOT VERIFIED** (no GUI in sandbox) |
| In-iframe DOM: A→B→A race, generation echo, error/empty states, one-click switch command text | manual checklist below | **NOT VERIFIED** |

Do not promote any "NOT VERIFIED" row to PASS without a recorded transcript
from the authorised machine (acceptance #1 of Core #59).

## Isolation — never touches the developer's profile

`run-test.mjs` creates one throwaway temp root (override with `E2E_TMPDIR`)
containing: a fresh `user-data-dir`, `extensions-dir`, a synthetic `HOME` with
`~/.voyager/index.db`, an empty synthetic workspace, and no real session data.
It redirects `HOME`/`USERPROFILE`/`VOYAGER_PYTHON` for both the launcher and the
spawned `python -m voyager.api serve`. `--disable-extensions` prevents loading
the developer's installed extensions. Nothing here scans a real provider.

## Run (on an authorised GUI machine)

Prerequisites: a Python with the Core installed (`pip install -e <repo>`),
Node 22+, and a network path for `@vscode/test-electron` to fetch a pinned
VS Code build (or set `VSCODE_VERSION` to an already-cached version).

```bash
cd E:/code/voyager/vscode-extension/e2e
npm install                     # @vscode/test-electron (+ types)
E2E_PYTHON=<python-with-voyager> npm test
# optional: E2E_TMPDIR=E:/some/scratch VSCODE_VERSION=1.95.0 ...
```

The launcher seeds the index, starts the isolated host, runs `suite/index.js`,
then removes the temp root. It prints `Extension Host E2E: PASS` only if every
host-side assertion passed.

## Manual acceptance checklist (in-iframe, operator)

Open the three panels from the command palette and confirm, against the
synthetic index:

1. **Selector**: two WorkThreads listed; picking each shows its detail; select
   A, then B, then A again with a delayed `thread_detail` for the first A — the
   stale response must not overwrite the newest selection (PR #63 generation).
   Refresh must not let an old response win.
2. **Empty / error states**: point `voyager.repoPath` at a repo with no
   WorkThread (empty), then at a nonexistent python (`voyager.pythonPath`) — the
   "index has no thread" and "core unavailable" messages must be distinct.
3. **Timeline**: events for a WorkThread render with provider/kind labels;
   `codex:e2e-old` shows the `SOURCE_MISSING` retention caveat; order is display
   only, not a claim of authority.
4. **Composer**: select multiple sessions, set a goal and budget, preview a
   bundle; provenance/source hints appear. Verify with a read-only DB snapshot
   that preview wrote nothing (no merge / handoff / lease).
5. **One-click switch**: switching Thread A vs Thread B issues a terminal
   command containing that thread's id (`--thread "..."`, PR #61).
6. **Bridge lifecycle**: kill the `python -m voyager.api serve` child mid-run —
   pending requests reject, a later request spawns a fresh child; closing the
   panel and deactivating the extension leave no orphan process or timer.

Record the transcript (commands + assertions + outcomes) in Core #59.

## Files

- `seed_index.py` — synthetic index builder (executed here, isolated).
- `run-test.mjs` — isolated Extension Host launcher.
- `suite/index.js` — host-side test module (CommonJS, real `vscode` API).
- `README.md` — this file.

No VSIX is produced by this harness, and nothing is published to Marketplace.
