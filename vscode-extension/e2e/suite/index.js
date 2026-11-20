/**
 * Tests that run INSIDE the real Extension Development Host (CommonJS).
 *
 * Entry point for @vscode/test-electron `extensionTestsPath`, loaded with
 * require() inside a live Extension Host. It does NOT run in the sandbox that
 * authored this file (launching a VS Code window is prohibited there — Core
 * #59 Environment note).
 *
 * Automated here (host-side smoke subset): the extension is discoverable and
 * activates, every contributed command has a handler, the launcher-provided
 * isolated Python/repo/index are actually applied, and one real WebviewPanel
 * opens and closes without throwing. NOT automated: the three read-only
 * workflows in a real iframe, host→webview message delivery, race handling,
 * one-click switch, and bridge-child cleanup. Those remain a manual operator
 * checklist in ../README.md and Core #59.
 */
const vscode = require('vscode');

const EXTENSION_ID = 'HarryHeYu.voyager-sidebar';
const results = [];

async function check(name, fn) {
  try {
    await fn();
    results.push({ name, ok: true });
    console.log('  ok   - ' + name);
  } catch (e) {
    results.push({ name, ok: false, error: String((e && e.stack) || e) });
    console.error('  FAIL - ' + name + ': ' + e);
  }
}

async function run() {
  const ext = vscode.extensions.getExtension(EXTENSION_ID);
  await check('extension is discoverable', () => {
    if (!ext) throw new Error('extension ' + EXTENSION_ID + ' not found');
  });
  await check('extension activates in a real host', async () => {
    if (!ext) throw new Error('extension unavailable');
    await ext.activate();
    if (!ext.isActive) throw new Error('extension did not become active');
  });

  const cmds = await vscode.commands.getCommands(true);
  for (const c of ['voyager.openSelector', 'voyager.openTimeline',
                   'voyager.openComposer', 'voyager.overview',
                   'voyager.bundlePreview', 'voyager.switchThread']) {
    await check('command registered: ' + c, () => {
      if (!cmds.includes(c)) throw new Error('missing command ' + c);
    });
  }

  // The launcher resolves the real Python executable and writes the synthetic
  // workspace/index paths into the isolated host environment. Apply them as
  // workspace settings before any bridge is spawned.
  const py = process.env.VOYAGER_E2E_PYTHON;
  const workspace = process.env.VOYAGER_E2E_WORKSPACE;
  const index = process.env.VOYAGER_E2E_INDEX;
  await check('launcher supplied isolated Python, workspace and index', () => {
    if (!py || !workspace || !index) {
      throw new Error('missing VOYAGER_E2E_PYTHON/WORKSPACE/INDEX');
    }
  });
  await check('extension uses the isolated Python and repo path', async () => {
    const cfg = vscode.workspace.getConfiguration('voyager');
    await cfg.update('pythonPath', py, vscode.ConfigurationTarget.Workspace);
    await cfg.update('repoPath', workspace, vscode.ConfigurationTarget.Workspace);
    const applied = vscode.workspace.getConfiguration('voyager');
    if (applied.get('pythonPath') !== py) throw new Error('pythonPath not applied');
    if (applied.get('repoPath') !== workspace) throw new Error('repoPath not applied');
  });
  await check('synthetic index exists inside the isolated home', async () => {
    await vscode.workspace.fs.stat(vscode.Uri.file(index));
  });

  await check('one real WebviewPanel opens and closes (smoke only)', async () => {
    await vscode.commands.executeCommand('voyager.openSelector');
    await vscode.commands.executeCommand('workbench.action.closeActiveEditor');
  });

  const failed = results.filter((r) => !r.ok);
  console.log('\nREAL HOST SMOKE (automated subset): ' +
    (results.length - failed.length) + '/' + results.length + ' passed');
  console.log('This is NOT a three-workflow E2E verdict. See README checklist.');
  if (failed.length) {
    throw new Error(failed.length + ' real-host smoke checks failed');
  }
}

module.exports = { run };
