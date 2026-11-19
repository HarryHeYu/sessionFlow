/**
 * Tests that run INSIDE the real Extension Development Host (CommonJS).
 *
 * Entry point for @vscode/test-electron `extensionTestsPath`, loaded with
 * require() inside a live Extension Host. It does NOT run in the sandbox that
 * authored this file (launching a VS Code window is prohibited there — Core
 * #59 Environment note).
 *
 * Automated here (host-side contract): the extension is discoverable,
 * activates, every contributed command has a handler, and a real WebviewPanel
 * opens and closes without throwing. NOT automated (honest boundary): the
 * webview inline JS renders in a real iframe and needs an operator to click
 * through the manual checklist in ../README.md. No DOM assertion is faked.
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

  await check('opens a real WebviewPanel and closes it', async () => {
    await vscode.commands.executeCommand('voyager.openSelector');
    await vscode.commands.executeCommand('workbench.action.closeActiveEditor');
  });

  const failed = results.filter((r) => !r.ok);
  console.log('\nREAL HOST E2E (automated host-side subset): ' +
    (results.length - failed.length) + '/' + results.length + ' passed');
  if (failed.length) {
    throw new Error(failed.length + ' real-host checks failed');
  }
}

module.exports = { run };
