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
  child.stderr.on("data", (d) => {
    const ch = vscode.window.createOutputChannel("Voyager");
    ch.appendLine("[voyager api stderr] " + d.toString());
  });
  const pending = new Map();
  let buffer = "";
  child.stdout.on("data", (d) => {
    buffer += d.toString();
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        const resolve = pending.get(msg.id);
        if (resolve) {
          pending.delete(msg.id);
          resolve(msg);
        }
      } catch (e) {
        /* ignore partial lines */
      }
    }
  });
  bridge = {
    request(op, params) {
      return new Promise((resolve, reject) => {
        const id = ++requestSeq;
        pending.set(id, resolve);
        child.stdin.write(
          JSON.stringify({ id, op, params: params || {} }) + "\n"
        );
        setTimeout(() => {
          if (pending.has(id)) {
            pending.delete(id);
            reject(new Error("voyager api timeout: " + op));
          }
        }, 30000);
      });
    },
  };
  context.subscriptions.push({ dispose: () => child.kill() });
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
      return res.result.threads.map(
        (t) =>
          new vscode.TreeItem(
            `${t.id.slice(0, 14)}  members:${t.members}  ${t.title || ""}`
          )
      );
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
    vscode.commands.registerCommand("voyager.refresh", () => provider.refresh())
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
