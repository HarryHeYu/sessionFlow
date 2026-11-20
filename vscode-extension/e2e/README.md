# Real Extension Host smoke harness — Core #59

> **Status: NOT VERIFIED in the sandbox that authored this harness.** Launching
> a VS Code window is prohibited there (see Core #59 "Environment note"). This
> directory is a ready-to-run harness for an **authorised machine with a GUI**.
> The synthetic seed and static/startup contract tests run here; the GUI launch
> did not. Automated coverage is a **host-side smoke subset**, not a three-panel
> workflow E2E.

## What is actually covered

| Layer | Where | Status here |
|---|---|---|
| Synthetic index seed (4 sessions, 2 WorkThreads, `SOURCE_MISSING`) | `seed_index.py` | **executed** (synthetic DB, isolated path) |
| Static npm/start contract tests | `tests/test_vscode_e2e_contract.py` | **passed** (no GUI) |
| Extension activates, commands registered, one WebviewPanel opens/closes | `suite/index.js` in a real host | **NOT VERIFIED** (no GUI in sandbox) |
| Real iframe rendering, host→webview delivery, A→B→A race, error/empty states, switch command text, bridge-child cleanup | manual checklist below | **NOT VERIFIED** |

Do not promote any NOT VERIFIED row to PASS without a recorded transcript from
the authorised machine (Core #59 acceptance #1). Opening one `WebviewPanel`
proves lifecycle/activation only; it is a **smoke only** check.

## Install and run (authorised GUI machine only)

The harness has its own npm manifest and lockfile; `npm ci` installs the real
`@vscode/test-electron` dependency pinned at `3.1.0`. That package requires
Node >= 22 (the authoring machine has Node v22.22.2).

```bash
cd E:/code/voyager/vscode-extension/e2e
npm ci
npm run check
E2E_PYTHON=<absolute-python-with-voyager> E2E_TMPDIR=E:/some/scratch npm test
```

`npm test` runs `node ./run-test.mjs`, which **launches a real VS Code
Extension Development Host**. It is not headless and must not be run in CI or
an unattended sandbox. `@vscode/test-electron` uses `extensionTestsPath` to
load `suite/index.js`, whose exported `run()` executes inside that host.

Optional version selection:

```bash
VSCODE_VERSION=1.147.0 E2E_PYTHON=<python> npm test
E2E_VSCODE_EXECUTABLE="C:/path/to/Code.exe" E2E_PYTHON=<python> npm test
```

The default `VSCODE_VERSION` is `1.95.0`. The extension manifest declares
`"vscode": "^1.80.0"`, so both the pinned default and the locally observed
VS Code 1.147.0 satisfy the engine range. `E2E_VSCODE_EXECUTABLE` bypasses the
download and uses an explicitly named build; no GUI was started in the
authoring sandbox to compare rendering between versions.

## Isolation — never touches the developer's profile

`run-test.mjs` creates one throwaway temp root (override with `E2E_TMPDIR`)
containing:

- fresh `user-data-dir` and `extensions-dir`;
- isolated `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_CONFIG_HOME`,
  `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `TMP` and `TEMP`;
- synthetic `HOME/.voyager/index.db` seeded by `seed_index.py`;
- an empty synthetic workspace and no real session data.

It resolves `E2E_PYTHON` once, passes the resolved executable to the host, and
`suite/index.js` writes it to `voyager.pythonPath` plus the synthetic workspace
to `voyager.repoPath`. Setting `VOYAGER_PYTHON` alone does not configure the
client. `--disable-extensions` prevents loading installed extensions; the
extension under development remains active. Nothing scans a real provider.

## Manual acceptance checklist (authorised operator)

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

Record the transcript (commands + assertions + outcomes) in Core #59. Only a
recorded transcript can close the real-host items.

## Files

- `package.json` / `package-lock.json` — real npm contract for this harness.
- `seed_index.py` — synthetic index builder (executed here, isolated).
- `run-test.mjs` — isolated Extension Host launcher.
- `suite/index.js` — host-side CommonJS runner exporting `run()`.
- `README.md` — this file.

No VSIX is produced by this harness, nothing is published to Marketplace, and
no extension is installed into the developer's daily VS Code profile.
