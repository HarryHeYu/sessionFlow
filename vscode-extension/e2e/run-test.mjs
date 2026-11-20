/**
 * Launcher for the real VS Code Extension Host smoke harness.
 *
 * THIS SCRIPT IS NOT RUN IN THE SANDBOX that authored it: launching a VS Code
 * window is prohibited there (see Core #59 Environment note). It is written to
 * be executed on an AUTHORISED machine with a GUI and the network access needed
 * for @vscode/test-electron to fetch a pinned VS Code build.
 *
 * Isolation guarantees (never touches the developer's profile):
 *   - throwaway --user-data-dir and --extensions-dir under one temp root;
 *   - isolated HOME/USERPROFILE/APPDATA/LOCALAPPDATA/XDG and TMP for the launcher,
 *     the seed process, the Extension Host and the spawned core child;
 *   - a synthetic index seeded under that isolated HOME;
 *   - an explicit Python executable resolved once and passed to the host;
 *   - a pinned VS Code version (or an explicitly supplied executable).
 *
 * `npm test` launches a real GUI Extension Development Host. There is no
 * supported headless mode; do not run it in CI or an unattended sandbox.
 */
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');
const extDevPath = join(HERE, '..');           // vscode-extension/
const suitePath = join(HERE, 'suite', 'index.js');

// One scratch root. E2E_TMPDIR should be an explicit scratch directory on the
// same drive as the work; never point it at a real profile or project root.
const root = process.env.E2E_TMPDIR
  ? join(process.env.E2E_TMPDIR, 'sessionflow-e2e-' + Date.now())
  : join(tmpdir(), 'sessionflow-e2e-' + Date.now());
const userDataDir = join(root, 'user-data');
const extensionsDir = join(root, 'extensions');
const workspace = join(root, 'workspace');
const home = join(root, 'home');
const temp = join(root, 'tmp');
const appData = join(home, 'AppData');
const localAppData = join(appData, 'Local');
const configHome = join(home, '.config');
const dataHome = join(home, '.local');
const cacheHome = join(home, '.cache');
const index = join(home, '.voyager', 'index.db');

for (const d of [userDataDir, extensionsDir, workspace, home, temp,
                 appData, localAppData, configHome, dataHome, cacheHome,
                 dirname(index)]) {
  mkdirSync(d, { recursive: true });
}
writeFileSync(join(workspace, 'README.md'),
  'Synthetic workspace for sessionFlow Extension Host E2E. No real project.\n');

const isolatedEnv = {
  ...process.env,
  HOME: home,
  USERPROFILE: home,
  APPDATA: appData,
  LOCALAPPDATA: localAppData,
  XDG_CONFIG_HOME: configHome,
  XDG_DATA_HOME: dataHome,
  XDG_CACHE_HOME: cacheHome,
  TMP: temp,
  TEMP: temp,
  VOYAGER_NO_SYNC: '1',
};

// Resolve E2E_PYTHON once. The suite writes the resolved executable into
// voyager.pythonPath; setting VOYAGER_PYTHON alone would not affect the client.
const requestedPython = process.env.E2E_PYTHON || process.env.VOYAGER_PYTHON || 'python';
const resolved = spawnSync(
  requestedPython, ['-c', 'import sys; print(sys.executable)'],
  { env: isolatedEnv, encoding: 'utf8' });
if (resolved.status !== 0) {
  console.error('E2E_PYTHON cannot start:\n' + resolved.stdout + '\n' + resolved.stderr);
  process.exit(1);
}
const py = resolved.stdout.trim().split(/\r?\n/).pop();

const seed = spawnSync(py, [join(HERE, 'seed_index.py'), index], {
  env: isolatedEnv,
  encoding: 'utf8',
});
if (seed.status !== 0) {
  console.error('seed_index.py failed:\n' + seed.stdout + '\n' + seed.stderr);
  process.exit(1);
}
console.log(seed.stdout.trim());

// The host-side suite consumes these to configure the extension under test.
const hostEnv = {
  ...isolatedEnv,
  VOYAGER_E2E_PYTHON: py,
  VOYAGER_E2E_INDEX: index,
  VOYAGER_E2E_HOME: home,
  VOYAGER_E2E_WORKSPACE: workspace,
  VOYAGER_PYTHON: py, // legacy spelling; the suite uses VOYAGER_E2E_PYTHON
};

const { runTests } = await import('@vscode/test-electron');
const options = {
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
  extensionTestsEnv: hostEnv,
};
if (process.env.E2E_VSCODE_EXECUTABLE) {
  options.vscodeExecutablePath = process.env.E2E_VSCODE_EXECUTABLE;
  delete options.version;
}
console.log('Isolated E2E root: ' + root);
console.log('Synthetic index: ' + index);
console.log('Python: ' + py);

try {
  await runTests(options);
  console.log('Extension Host smoke: PASS');
  process.exitCode = 0;
} catch (err) {
  console.error('Extension Host smoke FAILED:', err);
  process.exitCode = 1;
} finally {
  // Best-effort cleanup. E2E_KEEP_TMP=1 preserves evidence for a failure.
  if (process.env.E2E_KEEP_TMP !== '1') {
    try { rmSync(root, { recursive: true, force: true }); } catch { /* keep on failure */ }
  }
}
