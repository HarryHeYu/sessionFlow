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

## 7. Post-merge executable-contract corrections

A follow-up static audit of the harness itself found five real defects that the
first report did not catch:

1. `vscode-extension/e2e/` had no `package.json`, although the README told
   operators to run `npm install && npm test` there. The dependency existed
   only in prose.
2. `@vscode/test-electron` was not declared. Its documented API is
   `runTests({ extensionDevelopmentPath, extensionTestsPath, launchArgs,
   extensionTestsEnv })`, and `extensionTestsPath` must name a runner module
   that exports `run()`. The launcher passed a directory instead of
   `suite/index.js`.
3. The launcher isolated only `HOME` and `USERPROFILE`. On Windows, Python also
   resolves user site from `APPDATA` / `LOCALAPPDATA`, and the child inherited
   ambient `TMP`/`TEMP` and XDG locations.
4. It set `VOYAGER_PYTHON`, but the shipped client reads the
   `voyager.pythonPath` setting. The suite never applied it, and never applied
   `voyager.repoPath`; `VOYAGER_E2E_INDEX` was likewise unconsumed.
5. The host-side suite opened one `WebviewPanel` and printed
   "REAL HOST E2E", which overstated a lifecycle smoke test as all three
   read-only workflows.

Corrections: added a private `package.json` + lockfile pinning
`@vscode/test-electron` 3.1.0 (Node >= 22), added `npm run check`; resolved
`E2E_PYTHON` once and applied it plus the synthetic workspace through
workspace configuration; isolated the full environment; pointed
`extensionTestsPath` at `suite/index.js`; added `E2E_VSCODE_EXECUTABLE` for a
local 1.147.0 build while keeping the pinned default `1.95.0` (both satisfy
the manifest's `^1.80.0` engine range); and relabelled the automated result as
"REAL HOST SMOKE (automated subset)". The three workflows remain a manual
checklist and **NOT VERIFIED**.

New gate: `tests/test_vscode_e2e_contract.py`. Red baseline before the fix was
5 failed / 1 passed; after the fix, 35 passed together with `test_docs.py`.
`npm run check` also caught a real block-comment syntax error in the rewritten
launcher. No GUI was started, so the real-host rows remain unchanged.
