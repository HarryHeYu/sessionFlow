/**
 * Launcher for the real VS Code Extension Host E2E harness.
 *
 * THIS SCRIPT IS NOT RUN IN THE SANDBOX that authored it: launching a VS Code
 * window is prohibited there (see Core #59 Environment note). It is written to
 * be executed on an AUTHORISED machine with a GUI and the network access needed
 * for @vscode/test-electron to fetch a pinned VS Code build.
 *
 * Isolation guarantees (never touches the developer's profile):
 *   - a throwaway --user-data-dir and --extensions-dir under a temp root;
 *   - a synthetic index seeded by seed_index.py under that temp root;
 *   - HOME/USERPROFILE redirected to the temp root for the spawned core;
 *   - a pinned VS Code version (VSCODE_VERSION) for reproducibility;
 *   - a dedicated synthetic workspace folder opened as the test workspace.
 *
 * Usage (on an authorised machine):
 *   cd vscode-extension/e2e
 *   npm install                 # installs @vscode/test-electron + @types/vscode
 *   npm test
 */
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');
const extDevPath = join(HERE, '..');           // vscode-extension/
const suitePath = join(HERE, 'suite');         // compiled/loaded by runTests

// All scratch under one temp root; removed on exit. Set E2E_TMPDIR to keep it
// off an unwanted drive. This never uses the real user profile.
const root = process.env.E2E_TMPDIR
  ? join(process.env.E2E_TMPDIR, 'sessionflow-e2e-' + Date.now())
  : join(tmpdir(), 'sessionflow-e2e-' + Date.now());
const userDataDir = join(root, 'user-data');
const extensionsDir = join(root, 'extensions');
const workspace = join(root, 'workspace');
const home = join(root, 'home');
const index = join(home, '.voyager', 'index.db');

for (const d of [userDataDir, extensionsDir, workspace, home, dirname(index)]) {
  mkdirSync(d, { recursive: true });
}
// A marker file so the webview's repoPath config points at a real, empty dir.
writeFileSync(join(workspace, 'README.md'),
  'Synthetic workspace for sessionFlow Extension Host E2E. No real project.\n');

// Seed the synthetic index with an ISOLATED home so nothing writes real data.
const py = process.env.E2E_PYTHON || 'python';
const seed = spawnSync(py, [join(HERE, 'seed_index.py'), index], {
  env: { ...process.env, HOME: home, USERPROFILE: home, VOYAGER_NO_SYNC: '1' },
  encoding: 'utf8',
});
if (seed.status !== 0) {
  console.error('seed_index.py failed:\n' + seed.stdout + '\n' + seed.stderr);
  process.exit(1);
}
console.log(seed.stdout.trim());

// The suite reads these from env; they configure the isolated core + workspace.
process.env.VOYAGER_E2E_INDEX = index;
process.env.VOYAGER_E2E_HOME = home;
process.env.VOYAGER_E2E_WORKSPACE = workspace;

const { runTests } = await import('@vscode/test-electron');
try {
  await runTests({
    version: process.env.VSCODE_VERSION || '1.95.0',
    extensionDevelopmentPath: extDevPath,
    extensionTestsPath: suitePath,
    launchArgs: [
      workspace,
      '--user-data-dir=' + userDataDir,
      '--extensions-dir=' + extensionsDir,
      '--disable-extensions',
      '--disable-gpu',
    ],
    // The extension spawns `python -m voyager.api serve`; give it the isolated
    // HOME and a python that has the core installed.
    extensionTestsEnv: {
      ...process.env,
      HOME: home,
      USERPROFILE: home,
      VOYAGER_NO_SYNC: '1',
      VOYAGER_PYTHON: py,
    },
  });
  console.log('Extension Host E2E: PASS');
  process.exitCode = 0;
} catch (err) {
  console.error('Extension Host E2E FAILED:', err);
  process.exitCode = 1;
} finally {
  // Best-effort cleanup of the isolated scratch. Never touches real profiles.
  try { rmSync(root, { recursive: true, force: true }); } catch { /* keep on failure */ }
}
