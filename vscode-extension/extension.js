/**
 * Voyager VS Code extension — SCAFFOLD (roadmap Phase 7 / issue #8).
 *
 * This is a thin client over the voyager local API (`voyager api serve`,
 * stdio JSON-lines). It contains NO business logic: every operation is a
 * request to the same core the CLI uses.
 *
 * Implemented (working): overview / thread_detail / sessions /
 * bundle_preview requests over the stdio bridge, shown in an output
 * channel and a basic WorkThreads tree view.
 * Scaffold / pending: Context Composer webview (checkboxes -> live bundle
 * preview -> launch), one-click Switch. Launch intentionally stays in the
 * CLI for now (`voyager continue --launch` / `voyager switch <agent>`).
 */

const vscode = require("vscode");
const { spawn } = require("child_process");
const composer = require("./composer");
const timelineView = require("./timeline");
const selectorView = require("./selector");

let bridge = null;
let requestSeq = 0;

function pythonPath() {
  return vscode.workspace.getConfiguration("voyager").pythonPath || "python";
}

function repoPath() {
  return (
    vscode.workspace.getConfiguration("voyager").repoPath ||
    (vscode.workspace.workspaceFolders &&
      vscode.workspace.workspaceFolders[0].uri.fsPath) ||
    ""
  );
}

/**
 * JSON-lines stdio bridge: spawn `python -m voyager.api serve` once and
 * round-trip {id, op, params} requests. (The long-lived spawn is part of
 * the scaffold; contract tests live in the voyager repo's test_bridge.py.)
 */
function getBridge(context) {
  if (bridge) return bridge;
  const child = spawn(pythonPath(), ["-m", "voyager.api", "serve"], {
    cwd: repoPath() || undefined,
  });
  // One channel for the bridge's lifetime. The scaffold created a channel per
  // stderr chunk, so a chatty child leaked a new channel on every write.
  const stderrChannel = vscode.window.createOutputChannel("Voyager");
  context.subscriptions.push(stderrChannel);
  child.stderr.on("data", (d) =>
    stderrChannel.appendLine("[voyager api stderr] " + d.toString())
  );

  const pending = new Map();
  let buffer = "";
  let dead = null;

  function failPending(reason) {
    for (const [id, entry] of pending) {
      clearTimeout(entry.timer);
      pending.delete(id);
      entry.reject(reason);
    }
  }

  function retire(reason) {
    if (dead) return;
    dead = reason instanceof Error ? reason : new Error(reason);
    failPending(dead);
    // Never hand an exited/failed child to the next caller: a later request
    // would write into a dead stdin and hang until its own timeout.
    if (bridge && bridge.child === child) bridge = null;
  }

  child.on("error", (e) =>
    retire(new Error("voyager api spawn failed: " + (e && e.message ? e.message : e)))
  );
  child.on("exit", (code, signal) =>
    retire(new Error("voyager api exited (code=" + code + ", signal=" + signal + ")"))
  );
  child.on("close", (code, signal) =>
    retire(new Error("voyager api closed (code=" + code + ", signal=" + signal + ")"))
  );

  child.stdout.on("data", (d) => {
    buffer += d.toString();
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        const entry = pending.get(msg.id);
        if (entry) {
          pending.delete(msg.id);
          clearTimeout(entry.timer);
          entry.resolve(msg);
        }
      } catch (e) {
        /* ignore partial lines */
      }
    }
  });
  bridge = {
    child,
    request(op, params) {
      if (dead) return Promise.reject(dead);
      return new Promise((resolve, reject) => {
        const id = ++requestSeq;
        const timer = setTimeout(() => {
          if (pending.has(id)) {
            pending.delete(id);
            reject(new Error("voyager api timeout: " + op));
          }
        }, 30000);
        pending.set(id, { resolve, reject, timer });
        try {
          child.stdin.write(
            JSON.stringify({ id, op, params: params || {} }) + "\n"
          );
        } catch (e) {
          clearTimeout(timer);
          pending.delete(id);
          reject(e);
        }
      });
    },
  };
  context.subscriptions.push({ dispose: () => {
    retire(new Error("voyager api bridge disposed"));
    child.kill();
  } });
  return bridge;
}

function showError(msg) {
  vscode.window.showErrorMessage("Voyager: " + msg);
}

async function showOverview(context) {
  try {
    const b = getBridge(context);
    const res = await b.request("overview", { repo: repoPath() });
    if (res.error) return showError(res.error);
    const ch = vscode.window.createOutputChannel("Voyager Overview");
    ch.clear();
    ch.appendLine("threads (active): " + res.result.threads.length);
    for (const t of res.result.threads) {
      ch.appendLine(`  ${t.id}  [${t.status}]  members:${t.members}  ${t.title || ""}`);
    }
    ch.appendLine("recent sessions: " + res.result.recent_sessions.length);
    for (const s of res.result.recent_sessions) {
      ch.appendLine(`  [${s.provider}] ${s.native_session_id}  ${s.title || s.last_user}`);
    }
    ch.show();
  } catch (e) {
    showError(e.message);
  }
}

async function previewBundle(context) {
  try {
    const b = getBridge(context);
    const sessionId = await vscode.window.showInputBox({
      prompt: "Session id/prefix to include (first of the bundle)",
    });
    if (!sessionId) return;
    const goal = await vscode.window.showInputBox({
      prompt: "Goal (optional; ranks the evidence)",
    });
    const budget = await vscode.window.showQuickPick(
      ["", "compact", "balanced", "full", "auto"],
      { placeHolder: "Context budget (optional)" }
    );
    const res = await b.request("bundle_preview", {
      session_refs: [sessionId],
      goal: goal || null,
      budget: budget || null,
    });
    if (res.error) return showError(res.error);
    const doc = await vscode.workspace.openTextDocument({
      content: res.result.bundle,
      language: "markdown",
    });
    await vscode.window.showTextDocument(doc);
    vscode.window.showInformationMessage(
      `Voyager: ~${res.result.estimated_tokens} tokens estimated` +
        (res.result.dropped.length
          ? ` (dropped: ${res.result.dropped.join(", ")})`
          : "")
    );
  } catch (e) {
    showError(e.message);
  }
}

let selectorPanel = null;

/**
 * WorkThread Selector (UI MVP).
 *
 * Read-only consumer of `overview` and `thread_detail`. It does not infer a
 * "current" thread, scan, write, merge, hand off or switch. The handoff engine
 * remains the only path that may write a WorkThread.
 */
function openSelector(context) {
  if (selectorPanel) {
    selectorPanel.reveal(vscode.ViewColumn.Beside);
    return;
  }
  const panel = vscode.window.createWebviewPanel(
    "voyager.selector",
    "Voyager: WorkThread Selector",
    vscode.ViewColumn.Beside,
    { enableScripts: true, retainContextWhenHidden: true }
  );
  selectorPanel = panel;
  panel.webview.html = selectorView.html(
    String(Date.now()) + String(Math.random()).slice(2)
  );

  let disposed = false;

  function post(msg) {
    if (!disposed && panel === selectorPanel) panel.webview.postMessage(msg);
  }

  async function sendOverview(generation) {
    try {
      const res = await getBridge(context).request("overview", {
        repo: repoPath(),
      });
      if (disposed || panel !== selectorPanel) return;
      if (res.error) {
        post({ type: "overview", ok: false, error: res.error,
               generation: generation });
        return;
      }
      post({
        type: "overview", ok: true, data: res.result, repo: repoPath(),
        generation: generation,
      });
    } catch (e) {
      if (!disposed) showError(e.message);
    }
  }

  panel.webview.onDidReceiveMessage(async (msg) => {
    if (disposed || panel !== selectorPanel) return;
    try {
      if (msg.type === "ready") {
        await sendOverview(msg.generation);
      } else if (msg.type === "select") {
        // Echo the webview's generation token verbatim. The webview compares
        // it with its current generation and thread selection before rendering,
        // so a late response for an older click is discarded even when both
        // requests named the same thread.
        const requested = msg.thread_id;
        const res = await getBridge(context).request("thread_detail", {
          thread_id: requested,
        });
        if (disposed || panel !== selectorPanel) return;
        if (res.error) {
          post({
            type: "detail", ok: false, thread_id: requested, error: res.error,
            generation: msg.generation,
          });
          return;
        }
        post({
          type: "detail", ok: true, thread_id: requested, data: res.result,
          generation: msg.generation,
        });
      } else if (msg.type === "copy") {
        await vscode.env.clipboard.writeText(msg.text || "");
        vscode.window.showInformationMessage("Voyager: copied to clipboard.");
      }
    } catch (e) {
      if (!disposed) showError(e.message);
    }
  });

  panel.onDidDispose(() => {
    disposed = true;
    selectorPanel = null;
  });
}

let composerPanel = null;

/**
 * Context Composer — the remaining half of Phase 7 / issue #8.
 *
 * Checkbox session picker -> live Continuation Bundle preview -> token
 * estimate -> a CLI command you can copy. It deliberately does NOT launch:
 * `bundle_preview` writes nothing, and the lease flow (D13) is what decides
 * who may write a WorkThread, so starting an agent from a webview would route
 * around the one safety property the core actually guarantees.
 */
function openComposer(context) {
  if (composerPanel) {
    composerPanel.reveal(vscode.ViewColumn.Beside);
    return;
  }
  const panel = vscode.window.createWebviewPanel(
    "voyager.composer",
    "Voyager: Context Composer",
    vscode.ViewColumn.Beside,
    { enableScripts: true, retainContextWhenHidden: true }
  );
  composerPanel = panel;
  panel.webview.html = composer.html(
    String(Date.now()) + String(Math.random()).slice(2)
  );

  async function sendSessions() {
    try {
      const res = await getBridge(context).request("sessions", {
        repo: repoPath(),
        limit: 200,
      });
      if (res.error) return showError(res.error);
      panel.webview.postMessage({ type: "sessions", items: res.result || [] });
    } catch (e) {
      showError(e.message);
    }
  }

  panel.webview.onDidReceiveMessage(async (msg) => {
    try {
      if (msg.type === "ready") {
        await sendSessions();
      } else if (msg.type === "preview") {
        if (!msg.refs || !msg.refs.length) {
          panel.webview.postMessage({
            type: "preview", ok: true, bundle: "", tokens: null,
          });
          return;
        }
        const res = await getBridge(context).request("bundle_preview", {
          session_refs: msg.refs,
          goal: msg.goal || null,
          budget: msg.budget || null,
        });
        if (res.error) {
          panel.webview.postMessage({
            type: "preview", ok: false, error: res.error,
          });
          return;
        }
        const r = res.result;
        panel.webview.postMessage({
          type: "preview", ok: true, bundle: r.bundle,
          tokens: r.estimated_tokens, budget: r.budget,
          dropped: r.dropped || [], trimmed: r.trimmed || [],
        });
      } else if (msg.type === "copy") {
        await vscode.env.clipboard.writeText(msg.text || "");
        vscode.window.showInformationMessage("Voyager: copied to clipboard.");
      }
    } catch (e) {
      showError(e.message);
    }
  });

  panel.onDidDispose(() => {
    composerPanel = null;
  });
}

let timelinePanel = null;

/**
 * WorkThread Timeline (O3.5).
 *
 * A pure consumer of the `thread_timeline` API op, which is a pass-through to
 * the canonical `voyager.timeline.build_thread_timeline` — the same function
 * the CLI and the dashboard use.  This view aggregates nothing itself, and it
 * never switches: switching goes through the handoff engine, so the view hands
 * the user the canonical command instead of growing a second path.
 */
function openTimeline(context) {
  if (timelinePanel) {
    timelinePanel.reveal(vscode.ViewColumn.Beside);
    return;
  }
  const panel = vscode.window.createWebviewPanel(
    "voyager.timeline",
    "Voyager: WorkThread Timeline",
    vscode.ViewColumn.Beside,
    { enableScripts: true, retainContextWhenHidden: true }
  );
  timelinePanel = panel;
  panel.webview.html = timelineView.html(
    String(Date.now()) + String(Math.random()).slice(2)
  );

  async function sendThreads() {
    try {
      const res = await getBridge(context).request("overview", {
        repo: repoPath(),
      });
      if (res.error) return showError(res.error);
      const threads = (res.result && res.result.threads) || [];
      panel.webview.postMessage({ type: "threads", items: threads });
    } catch (e) {
      showError(e.message);
    }
  }

  panel.webview.onDidReceiveMessage(async (msg) => {
    try {
      if (msg.type === "ready") {
        await sendThreads();
      } else if (msg.type === "timeline") {
        const res = await getBridge(context).request("thread_timeline", {
          thread_id: msg.thread_id,
        });
        if (res.error) {
          panel.webview.postMessage({
            type: "timeline", ok: false, error: res.error,
          });
          return;
        }
        panel.webview.postMessage({
          type: "timeline", ok: true, data: res.result,
        });
      } else if (msg.type === "copy") {
        await vscode.env.clipboard.writeText(msg.text || "");
        vscode.window.showInformationMessage("Voyager: copied to clipboard.");
      }
    } catch (e) {
      showError(e.message);
    }
  });

  panel.onDidDispose(() => {
    timelinePanel = null;
  });
}

class ThreadsProvider {
  constructor(context) {
    this._bridge = getBridge(context);
    this._emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._emitter.event;
  }
  refresh() {
    this._emitter.fire();
  }
  getTreeItem(element) {
    return element;
  }
  async getChildren() {
    try {
      const res = await this._bridge.request("overview", { repo: repoPath() });
      if (res.error) return [];
      return res.result.threads.map((t) => {
        const item = new vscode.TreeItem(
          `${t.id.slice(0, 14)}  members:${t.members}  ${t.title || ""}`
        );
        item.contextValue = "thread";
        item.threadId = t.id;
        item.tooltip = "Right-click for one-click switch";
        return item;
      });
    } catch (e) {
      return [];
    }
  }
}

function activate(context) {
  const provider = new ThreadsProvider(context);
  vscode.window.registerTreeDataProvider("voyager.threads", provider);

  context.subscriptions.push(
    vscode.commands.registerCommand("voyager.overview", () =>
      showOverview(context)
    ),
    vscode.commands.registerCommand("voyager.bundlePreview", () =>
      previewBundle(context)
    ),
    vscode.commands.registerCommand("voyager.openSelector", () =>
      openSelector(context)
    ),
    vscode.commands.registerCommand("voyager.openComposer", () =>
      openComposer(context)
    ),
    vscode.commands.registerCommand("voyager.openTimeline", () =>
      openTimeline(context)
    ),
    vscode.commands.registerCommand("voyager.refresh", () => provider.refresh()),
    vscode.commands.registerCommand("voyager.switchThread", (item) =>
      switchThread(context, item)
    )
  );
}

async function switchThread(context, item) {
  if (!item || !item.threadId) {
    vscode.window.showErrorMessage("Voyager: no WorkThread selected.");
    return;
  }
  const target = await vscode.window.showQuickPick(
    ["codex", "claude", "grok"],
    {
      placeHolder:
        "Voyager one-click switch: hand the WorkThread to which agent?",
      title: "Voyager switch",
    }
  );
  if (!target) return;
  const terminal = vscode.window.createTerminal({
    name: `voyager switch -> ${target}`,
    cwd: repoPath() || undefined,
  });
  terminal.show(true);
  // The tree item is the user's explicit choice. Passing only the target
  // would make the CLI resolve "the current thread" again, which can be a
  // different thread when the repo has more than one active WorkThread.
  // JSON.stringify quotes the id so a surprising character is data, not shell
  // syntax; generated ids are simple, but the boundary should still be safe.
  const selectedThread = JSON.stringify(String(item.threadId));
  terminal.sendText(`voyager switch ${target} --thread ${selectedThread}`);
  vscode.window.showInformationMessage(
    `Voyager: handing the WorkThread to ${target} (lease + continuation + launch).`
  );
  vscode.commands.executeCommand("voyager.refresh");
}

function deactivate() {}

module.exports = { activate, deactivate };
