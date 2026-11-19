# Core #59 — Real Extension Host E2E gate — 2026-11-20

> **Verdict: NOT VERIFIED (environment-blocked).** No GUI Extension Host run
> happened in the sandbox. This records the evidence for the blocker, the
> harness prepared for an authorised machine, and the exact reproduction
> steps. It does not promote any DOM-stub or unit result to a real-host PASS.

## 1. Environment probe (executed, read-only)

| Check | Result |
|---|---|
| `code --version` | VS Code **1.147.0**, commit `8a14f0cc…`, x64 |
| `node --version` / `npm --version` | **v22.22.2** / **10.9.7** |
| `vscode-extension/package.json` | scripts: only `package: vsce package`; **no** `devDependencies`, **no** `@vscode/test-electron`, **no** test script |
| Existing Extension Host harness | **none** (`test_vscode_extension.py` header: "There is no `code --extensionDevelopmentPath` in CI") |
| `.vscode-test` cache | none |
| `DISPLAY` / `WAYLAND_DISPLAY` | empty / empty |
| `Xvfb` / `xvfb-run` | not present (Windows) |
| `@vscode` in repo node_modules | absent |
| Sandbox process policy | program blacklist active (`reg.exe` blocked this very session); user red line: "do not start unauthorised GUI windows" |
| Core #59 Environment note (pre-existing) | "Launching a VS Code window is prohibited in the current sandbox, so this cannot be completed here." |

## 2. Why this is a hard blocker, not a choice

`@vscode/test-electron` (`runTests`) launches a real VS Code Desktop /
Extension Development Host and executes tests inside it. The official testing
doc describes the target as "a special instance of VS Code named the Extension
Development Host" and `runTests` as "downloading, unzipping, and launching VS
Code". There is **no documented supported headless mode**; on Windows there is
no X display to virtualise. Starting it therefore means opening a real GUI
window, which the sandbox and the user's red line forbid. This is an
environment fact, not a gap in the harness.

## 3. What was actually delivered this round

A ready-to-run, isolated harness under `vscode-extension/e2e/`:

- `seed_index.py` — builds a synthetic index (4 sessions, 2 active WorkThreads
  in one repo, a `SOURCE_MISSING` member, a second repo). **Executed here**:
  produced 4 sessions, `thread_ambiguous=true` with candidates
  `['Config migration', 'Auth flow']`. No real data touched.
- `run-test.mjs` — launcher with a throwaway `user-data-dir`, `extensions-dir`,
  synthetic `HOME`, isolated workspace, pinned `VSCODE_VERSION`, and
  `HOME`/`USERPROFILE` redirection for the spawned core child.
- `suite/index.js` — real-host test module: extension discoverable + activates,
  all contributed commands have handlers, a real `WebviewPanel` opens/closes.
- `README.md` — reproduction command + manual in-iframe acceptance checklist.

Both JS files pass `node --check`. Neither was executed inside a real host.

## 4. Verification-state ledger

| Item | State | Evidence |
|---|---|---|
| Synthetic index seed | UNIT / SYNTHETIC — PASS | seed script ran, 4 sessions + ambiguity asserted via `api.current_work` |
| Harness JS syntax | UNIT — PASS | `node --check` on both files |
| Real host activation + panel lifecycle | REAL HOST E2E — **NOT VERIFIED** | no GUI in sandbox |
| In-iframe race / error / empty / switch / bridge-kill | REAL HOST E2E — **NOT VERIFIED** | manual checklist only |

## 5. Reproduction command (authorised machine)

```bash
cd E:/code/voyager/vscode-extension/e2e
npm install
E2E_PYTHON=<python-with-voyager> E2E_TMPDIR=E:/some/scratch npm test
```
Then follow the six-step manual checklist in `e2e/README.md` and record the
transcript in Core #59. Only then may the issue's E2E items be marked VERIFIED.

## 6. Scope compliance

No Core API, Store schema, Bridge schema, or shipped behaviour changed. No
real user session, index, home, or VS Code profile was read or written. No GUI
window was started. No VSIX built, no Marketplace publish, no tag/release.
