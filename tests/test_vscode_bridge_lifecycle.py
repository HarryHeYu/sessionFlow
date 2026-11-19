"""Node fault injection for the VS Code extension's long-lived stdio bridge.

This is deliberately not an Extension Host test. It executes the shipped
extension.js in a node:vm context with a deterministic child_process/vscode
stub, emits lifecycle events in a chosen order, and records what actually
happens to pending promises, timer handles and spawned processes.

The trace is checked by assertions so a future "simplification" that drops an
error listener, stops rejecting pending requests or leaks a timer turns RED.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parent.parent / "vscode-extension" / "extension.js"


_DRIVER = r"""
const fs = require('fs'), vm = require('vm');

function newEnv(src) {
  const children = [];
  const timers = new Map();
  let timerSeq = 0;

  function fakeChild(label) {
    const listeners = {};
    const child = {
      label,
      killCount: 0,
      stdin: { writes: [], write(s) { this.writes.push(String(s)); return true; } },
      stdout: { listeners: {}, on(t, fn) { (this.listeners[t] || (this.listeners[t] = [])).push(fn); return this; },
                emit(t, arg) { (this.listeners[t] || []).forEach(fn => fn(arg)); } },
      stderr: { listeners: {}, on(t, fn) { (this.listeners[t] || (this.listeners[t] = [])).push(fn); return this; } },
      on(t, fn) { (listeners[t] || (listeners[t] = [])).push(fn); return child; },
      emit(t, arg) { (listeners[t] || []).forEach(fn => fn(arg)); },
      kill() { this.killCount += 1; return true; },
      _listeners: listeners,
    };
    children.push(child);
    return child;
  }

  const vscode = {
    workspace: {
      getConfiguration: () => ({ pythonPath: 'python' }),
      workspaceFolders: [{ uri: { fsPath: '/repo' } }],
    },
    window: {
      createOutputChannel: () => ({ appendLine() {}, clear() {}, show() {}, dispose() {} }),
      showErrorMessage() {}, showInformationMessage() {},
      createTerminal: () => ({ show() {}, sendText() {} }),
      createWebviewPanel: () => ({
        webview: { html: '', postMessage() {}, onDidReceiveMessage() {} },
        onDidDispose() {}, reveal() {},
      }),
    },
    commands: { registerCommand: () => ({ dispose() {} }), executeCommand() {} },
    EventEmitter: class { constructor() { this.event = () => ({ dispose() {} }); } fire() {} },
    TreeItem: class { constructor(label) { this.label = label; } },
    ViewColumn: { Beside: 2 },
    env: { clipboard: { writeText() {} } },
  };
  const stubs = {
    './composer': { html: () => '' },
    './timeline': { html: () => '' },
    './selector': { html: () => '' },
  };

  const ctx = {
    require(name) {
      if (name === 'vscode') return vscode;
      if (name === 'child_process') {
        return { spawn: () => fakeChild('child' + (children.length + 1)) };
      }
      if (Object.prototype.hasOwnProperty.call(stubs, name)) return stubs[name];
      return require(name);
    },
    module: { exports: {} },
    exports: {},
    console,
    process,
    setTimeout(fn, ms) { const id = ++timerSeq; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.createContext(ctx);
  vm.runInContext(src, ctx, { filename: 'extension.js' });
  return {
    ctx,
    context: { subscriptions: [] },
    children,
    activeTimers: () => timers.size,
    requestId: (child) => {
      const line = child.stdin.writes[child.stdin.writes.length - 1] || '';
      const m = line.match(/"id":(\d+)/);
      return m ? Number(m[1]) : null;
    },
    reply: (child, id, result) => {
      child.stdout.emit('data', JSON.stringify({ id, result }) + '\n');
    },
  };
}

async function settle(promise, ms) {
  let state = 'pending', value = null;
  promise.then(v => { state = 'resolved'; value = v; },
               e => { state = 'rejected'; value = e && e.message ? e.message : String(e); });
  await new Promise(resolve => setTimeout(resolve, ms));
  return { state, value };
}

function newContext() { return { subscriptions: [] }; }

async function run(src) {
  const out = {};

  // 1. spawn error: a failed spawn must reject the pending request instead of
  //    throwing an unhandled EventEmitter 'error' and killing the host.
  {
    const env = newEnv(src), context = env.context;
    let result;
    try {
      const promise = env.ctx.getBridge(context).request('overview', {});
      env.children[0].emit('error', new Error('spawn ENOENT'));
      result = await settle(promise, 20);
    } catch (e) {
      result = { state: 'threw', value: e && e.message };
    }
    out.spawnError = { result, timers: env.activeTimers() };
  }

  // 2. process exits while a request is pending: reject promptly, not after
  //    the 30s timeout.
  {
    const env = newEnv(src), context = env.context;
    const promise = env.ctx.getBridge(context).request('overview', {});
    env.children[0].emit('exit', 3);
    out.requestExit = { result: await settle(promise, 20), timers: env.activeTimers() };
  }

  // 3. recovery: after an exit, getBridge must not return the dead child.
  {
    const env = newEnv(src), context = env.context;
    const first = env.ctx.getBridge(context);
    const p1 = first.request('overview', {});
    env.children[0].emit('close', 0);
    const r1 = await settle(p1, 20);
    const second = env.ctx.getBridge(context);
    const p2 = second.request('overview', {});
    env.reply(env.children[env.children.length - 1], env.requestId(env.children[env.children.length - 1]), { ok: 2 });
    const r2 = await settle(p2, 20);
    out.recovery = { spawnCount: env.children.length, first: r1, second: r2 };
  }

  // 4. dispose: reject pending requests, kill the child, clear timers and
  //    allow a later request to spawn a fresh process.
  {
    const env = newEnv(src), context = env.context;
    const promise = env.ctx.getBridge(context).request('overview', {});
    context.subscriptions.forEach(s => s.dispose());
    const r1 = await settle(promise, 20);
    const b2 = env.ctx.getBridge(context);
    const p2 = b2.request('overview', {});
    env.reply(env.children[env.children.length - 1], env.requestId(env.children[env.children.length - 1]), { ok: 3 });
    const r2 = await settle(p2, 20);
    out.dispose = { pending: r1, killCount: env.children[0].killCount,
                    spawnCount: env.children.length, later: r2, timers: env.activeTimers() };
  }

  // 5. duplicate processes: concurrent callers must share one child.
  {
    const env = newEnv(src), context = env.context;
    env.ctx.getBridge(context); env.ctx.getBridge(context);
    out.duplicate = { spawnCount: env.children.length };
  }

  // 6. timer hygiene: a completed request must not leave a pending timeout.
  {
    const env = newEnv(src), context = env.context;
    const b = env.ctx.getBridge(context);
    const promise = b.request('overview', {});
    env.reply(env.children[0], env.requestId(env.children[0]), { ok: 1 });
    out.timers = { result: await settle(promise, 20), active: env.activeTimers() };
  }

  return out;
}

const src = fs.readFileSync(process.argv[2], 'utf8');
run(src).then(v => { console.log(JSON.stringify(v)); process.exit(0); },
              e => { console.log(JSON.stringify({ fatal: e && e.stack || String(e) })); process.exit(1); });
"""


@pytest.fixture(scope="module")
def trace(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    driver = tmp_path_factory.mktemp("bridge-lifecycle") / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    proc = subprocess.run([node, str(driver), str(EXT)],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_spawn_error_rejects_pending_request(trace):
    got = trace["spawnError"]
    assert got["result"]["state"] == "rejected", (
        "spawn error was not surfaced to the pending request: %r" % got)


def test_process_exit_rejects_pending_request_promptly(trace):
    got = trace["requestExit"]
    assert got["result"]["state"] == "rejected", (
        "an exited bridge left a pending request unresolved: %r" % got)


def test_bridge_recovers_after_process_exit(trace):
    got = trace["recovery"]
    assert got["spawnCount"] == 2, "the dead bridge was reused: %r" % got
    assert got["second"]["state"] == "resolved", "recovery request failed: %r" % got


def test_dispose_rejects_pending_and_stops_the_child(trace):
    got = trace["dispose"]
    assert got["pending"]["state"] == "rejected", "dispose did not reject pending: %r" % got
    assert got["killCount"] == 1, "dispose did not kill the child: %r" % got
    assert got["later"]["state"] == "resolved", "no fresh child after dispose: %r" % got


def test_getbridge_does_not_spawn_duplicate_children(trace):
    assert trace["duplicate"]["spawnCount"] == 1, trace["duplicate"]


def test_completed_request_does_not_leave_a_timer(trace):
    got = trace["timers"]
    assert got["result"]["state"] == "resolved", got
    assert got["active"] == 0, "timeout handle leaked after a completed request: %r" % got
