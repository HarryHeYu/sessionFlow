/**
 * Voyager WorkThread Selector — read-only webview (UI MVP).
 *
 * It is a thin consumer of two existing API ops:
 *   - overview      -> active threads + recent sessions
 *   - thread_detail -> members, source state and lease state
 *
 * It never scans, writes, merges, hands off or switches.  The handoff engine
 * remains the only path that may write a WorkThread.
 *
 * Contract with extension.js:
 *   webview -> host : {type:"ready"}
 *                     {type:"select", thread_id}
 *                     {type:"copy", text}
 *   host -> webview : {type:"overview", ok, generation, repo, data|error}
 *                     {type:"detail", ok, generation, thread_id, data|error}
 *                     {type:"dispose"}
 *
 * The `thread_id` on every detail message is a correlation token: a response
 * is applied only if it belongs to the currently selected thread. The
 * `generation` token additionally distinguishes two requests for the *same*
 * thread, so a late A1 can never overwrite a newer A2. Responses missing
 * either token are un-attributable and are discarded.
 */

function html(nonce) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>Voyager WorkThread Selector</title>
<style>
  :root { --gap: 10px; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: var(--gap);
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    background: var(--vscode-editor-background);
  }
  .row { display: flex; gap: var(--gap); align-items: center; margin-bottom: var(--gap); }
  .row > label { opacity: .75; }
  #repo { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; opacity: .8; }
  button {
    background: var(--vscode-button-background);
    color: var(--vscode-button-foreground);
    border: none; padding: 5px 10px; border-radius: 2px;
    font-family: inherit; font-size: inherit; cursor: pointer;
  }
  button.secondary {
    background: var(--vscode-button-secondaryBackground, transparent);
    color: var(--vscode-button-secondaryForeground, inherit);
    border: 1px solid var(--vscode-button-border, rgba(128,128,128,.4));
  }
  main { display: grid; grid-template-columns: minmax(260px, 1fr) 2fr; gap: var(--gap); }
  @media (max-width: 820px) { main { grid-template-columns: 1fr; } }
  section {
    border: 1px solid var(--vscode-panel-border, rgba(128,128,128,.35));
    border-radius: 3px; min-height: 260px; display: flex; flex-direction: column;
  }
  section > h2 {
    margin: 0; padding: 6px 8px; font-size: .85em; text-transform: uppercase;
    letter-spacing: .06em; opacity: .75;
    border-bottom: 1px solid var(--vscode-panel-border, rgba(128,128,128,.35));
  }
  .scroll { overflow: auto; flex: 1 1 auto; max-height: 56vh; padding: 6px; }
  .thread {
    border: 1px solid var(--vscode-panel-border, rgba(128,128,128,.25));
    border-radius: 3px; padding: 7px 8px; margin-bottom: 6px; cursor: pointer;
  }
  .thread:hover { background: var(--vscode-list-hoverBackground, rgba(128,128,128,.12)); }
  .thread.selected { border-color: var(--vscode-focusBorder, #007acc); }
  .thread .title { display: block; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { display: block; opacity: .72; font-size: .84em; margin-top: 2px; }
  .mono { font-family: var(--vscode-editor-font-family, monospace); }
  .tag {
    display: inline-block; font-size: .78em; margin-left: 6px; padding: 0 6px;
    border-radius: 999px; color: var(--vscode-editorWarning-foreground, #cca700);
    border: 1px solid currentColor;
  }
  .detail-block { margin-bottom: 12px; }
  .detail-block h3 { margin: 0 0 4px; font-size: .9em; opacity: .8; }
  .kv { margin: 2px 0; }
  .kv .k { opacity: .7; display: inline-block; min-width: 90px; padding-right: 6px; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { padding: 3px 0; border-bottom: 1px solid var(--vscode-panel-border, rgba(128,128,128,.18)); }
  .empty { opacity: .6; padding: 10px; }
  .err { color: var(--vscode-errorForeground, #f48771); padding: 8px; }
  .warn { color: var(--vscode-editorWarning-foreground, #cca700); }
  footer { margin-top: var(--gap); opacity: .75; font-size: .85em; }
</style>
</head>
<body>
  <div class="row">
    <label>Repo</label>
    <span id="repo">(not configured)</span>
    <button id="refresh" class="secondary">Refresh</button>
  </div>
  <div id="banner"></div>

  <main>
    <section>
      <h2>WorkThreads</h2>
      <div id="threads" class="scroll"></div>
    </section>
    <section>
      <h2>Selected WorkThread</h2>
      <div id="detail" class="scroll"></div>
    </section>
  </main>

  <footer>
    Read-only: this view uses overview / thread_detail. It never scans, writes,
    merges, hands off or switches a WorkThread.
  </footer>

<script nonce="${nonce}">
(function () {
  "use strict";
  const vscode = acquireVsCodeApi();
  const state = { overview: null, detail: null, selected: null, repo: "",
                  error: null, overviewError: null, detailGeneration: 0,
                  overviewGeneration: 0, disposed: false };

  const $ = (id) => document.getElementById(id);

  function fmtTime(t) {
    if (!t) return "-";
    const d = new Date(t * 1000);
    if (isNaN(d.getTime())) return "-";
    return d.toISOString().slice(0, 16).replace("T", " ");
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function kv(label, value) {
    const p = el("div", "kv");
    p.appendChild(el("span", "k", label));
    p.appendChild(el("span", "", value === null || value === undefined ? "-" : String(value)));
    return p;
  }

  function renderBanner() {
    const banner = $("banner");
    banner.textContent = "";
    const err = state.overviewError || state.error;
    if (err) {
      banner.appendChild(el("div", "err", "Error: " + err));
    }
  }

  function renderThreads() {
    const box = $("threads");
    box.textContent = "";
    const threads = (state.overview && state.overview.threads) || [];
    if (!threads.length) {
      // A failed/empty overview is not the same state as "no WorkThreads
      // indexed". Never suggest running a scan when the query itself failed.
      if (state.overviewError) {
        box.appendChild(el("div", "empty", "WorkThreads could not be loaded."));
      } else {
        box.appendChild(el("div", "empty", "No active WorkThreads for this repo. Run voyager scan first."));
      }
      return;
    }
    threads.forEach(function (t) {
      const card = el("div", "thread" + (state.selected === t.id ? " selected" : ""));
      card.appendChild(el("span", "title", t.title || "(untitled)"));
      const meta = el("span", "meta");
      meta.appendChild(el("span", "mono", t.id));
      meta.appendChild(document.createTextNode(
        "  [" + (t.status || "?") + "]  members:" + (t.members || 0) +
        "  " + fmtTime(t.updated_at)));
      card.appendChild(meta);
      if (t.repo_root) card.appendChild(el("span", "meta mono", t.repo_root));
      card.addEventListener("click", function () {
        state.selected = t.id;
        // Invalidate any in-flight detail request for the previous selection
        // and drop the stale detail immediately, so the panel never shows a
        // thread that is no longer selected while the new one loads.
        state.detailGeneration += 1;
        state.detail = null;
        renderThreads();
        renderDetail();
        vscode.postMessage({ type: "select", thread_id: t.id,
                             generation: state.detailGeneration });
      });
      box.appendChild(card);
    });
  }

  function renderDetail() {
    const box = $("detail");
    box.textContent = "";
    if (!state.selected) {
      box.appendChild(el("div", "empty", "Select a WorkThread to inspect its members and source state."));
      return;
    }
    if (state.detail && state.detail.error) {
      box.appendChild(el("div", "err", state.detail.error));
      return;
    }
    const d = state.detail;
    if (!d) {
      box.appendChild(el("div", "empty", "Loading thread detail..."));
      return;
    }
    const t = d.thread || {};
    const block = el("div", "detail-block");
    block.appendChild(el("h3", "", "Identity"));
    block.appendChild(kv("ID", t.id));
    block.appendChild(kv("Title", t.title || "(untitled)"));
    block.appendChild(kv("Repo", t.repo_root));
    block.appendChild(kv("Status", t.status));
    block.appendChild(kv("Members", t.members));
    block.appendChild(kv("Last activity", fmtTime(t.updated_at)));
    box.appendChild(block);

    const lease = d.lease || {};
    const leaseBlock = el("div", "detail-block");
    leaseBlock.appendChild(el("h3", "", "Lease"));
    leaseBlock.appendChild(kv("Held", lease.held ? "yes" : "no"));
    leaseBlock.appendChild(kv("Expired", lease.expired ? "yes" : "no"));
    leaseBlock.appendChild(kv("Holder", lease.holder || "-"));
    box.appendChild(leaseBlock);

    const members = d.members || [];
    const memberBlock = el("div", "detail-block");
    memberBlock.appendChild(el("h3", "", "Members and source state"));
    if (!members.length) {
      memberBlock.appendChild(el("div", "empty", "No member sessions."));
    } else {
      const ul = el("ul");
      members.forEach(function (m) {
        const li = el("li");
        li.appendChild(el("span", "mono", (m.provider || "?") + ":" + (m.native_id || "?")));
        li.appendChild(el("span", "", "  " + (m.title || "(untitled)")));
        const stateText = m.source_state || "ACTIVE_SOURCE";
        li.appendChild(el("span", stateText === "SOURCE_MISSING" ? "tag" : "meta",
                          stateText === "SOURCE_MISSING" ? "retained - source unavailable" : stateText));
        li.appendChild(el("span", "meta", "  updated " + fmtTime(m.updated_at)));
        ul.appendChild(li);
      });
      memberBlock.appendChild(ul);
    }
    box.appendChild(memberBlock);

    const recent = (state.overview && state.overview.recent_sessions) || [];
    const recentBlock = el("div", "detail-block");
    recentBlock.appendChild(el("h3", "", "Recent sessions (overview)"));
    if (!recent.length) {
      recentBlock.appendChild(el("div", "empty", "No recent sessions in the overview window."));
    } else {
      const ul = el("ul");
      recent.forEach(function (s) {
        const li = el("li");
        li.appendChild(el("span", "mono", (s.provider || "?") + ":" + (s.native_session_id || "?")));
        li.appendChild(el("span", "", "  " + (s.title || s.last_user || "(untitled)")));
        if (s.source_state === "SOURCE_MISSING") {
          li.appendChild(el("span", "tag", "retained - source unavailable"));
        }
        ul.appendChild(li);
      });
      recentBlock.appendChild(ul);
    }
    box.appendChild(recentBlock);
  }

  $("refresh").addEventListener("click", function () {
    state.error = null;
    state.overviewError = null;
    state.overviewGeneration += 1;
    state.detailGeneration += 1;   // in-flight detail responses are now stale
    state.detail = null;
    renderDetail();
    vscode.postMessage({ type: "ready", generation: state.overviewGeneration });
  });

  window.addEventListener("message", function (event) {
    if (state.disposed) return;
    const msg = event.data;
    if (msg.type === "dispose") {
      state.disposed = true;
      return;
    }
    if (msg.type === "overview") {
      // Overview responses are correlated too: a late failed/successful
      // overview from before Refresh must not rewrite the current view.
      if (msg.generation !== state.overviewGeneration) {
        return;
      }
      if (msg.ok) {
        state.overview = msg.data || {};
        state.overviewError = null;
        state.repo = msg.repo || "";
        state.error = null;
        $("repo").textContent = state.repo || "(not configured)";
        if (!state.selected && state.overview.threads && state.overview.threads.length) {
          const first = state.overview.threads[0].id;
          state.selected = first;
          state.detailGeneration += 1;
          vscode.postMessage({ type: "select", thread_id: first,
                               generation: state.detailGeneration });
        }
        renderThreads();
        renderDetail();
      } else {
        state.error = msg.error || "unknown error";
        state.overviewError = state.error;
        state.overview = null;
        renderBanner();
        renderThreads();
        renderDetail();
      }
    } else if (msg.type === "detail") {
      // Double correlation: the response must carry both the current
      // generation and the currently selected thread id. This blocks a late
      // A1 from overwriting A2 even when both responses name thread A, and it
      // blocks an in-flight A from landing on a newer B selection.
      if (msg.generation !== state.detailGeneration ||
          !msg.thread_id || msg.thread_id !== state.selected) {
        return;
      }
      state.detail = msg.ok ? (msg.data || {}) : { error: msg.error || "unknown error" };
      renderDetail();
    }
  });

  renderBanner();
  renderThreads();
  renderDetail();
  vscode.postMessage({ type: "ready", generation: state.overviewGeneration });
})();
</script>
</body>
</html>`;
}

module.exports = { html };
