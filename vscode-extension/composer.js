/**
 * Voyager Context Composer — webview markup (roadmap Phase 7 / issue #8).
 *
 * One self-contained document: no external stylesheet, no CDN, no framework.
 * The extension passes a nonce and the document declares a CSP that allows
 * only that nonce, so nothing here can reach the network or the filesystem.
 *
 * Contract with extension.js:
 *   webview -> host : {type:"ready"}
 *                     {type:"preview", refs:[...], goal, budget}
 *                     {type:"copy", text}
 *   host -> webview : {type:"sessions", items:[...]}
 *                     {type:"preview", ok, bundle, tokens, budget, dropped,
 *                      trimmed, error}
 *
 * The Composer never launches and never writes. `bundle_preview` renders in
 * memory, so the worst a click can do is produce text — which is why the
 * "run this" affordance copies a CLI command to the clipboard instead of
 * starting an agent (see docs/POST-1.0.md §1: unattended launch is risky
 * while the lease flow is what decides who may write).
 */

function html(nonce) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>Voyager Context Composer</title>
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
  .row > label { flex: 0 0 auto; opacity: .8; }
  input[type="text"], select {
    background: var(--vscode-input-background);
    color: var(--vscode-input-foreground);
    border: 1px solid var(--vscode-input-border, transparent);
    padding: 4px 6px; border-radius: 2px; font-family: inherit; font-size: inherit;
  }
  input[type="text"] { flex: 1 1 auto; min-width: 0; }
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
  button:disabled { opacity: .5; cursor: default; }
  main { display: grid; grid-template-columns: minmax(240px, 1fr) 2fr; gap: var(--gap); }
  @media (max-width: 820px) { main { grid-template-columns: 1fr; } }
  section {
    border: 1px solid var(--vscode-panel-border, rgba(128,128,128,.35));
    border-radius: 3px; min-height: 220px; display: flex; flex-direction: column;
  }
  section > h2 {
    margin: 0; padding: 6px 8px; font-size: .85em; text-transform: uppercase;
    letter-spacing: .06em; opacity: .75;
    border-bottom: 1px solid var(--vscode-panel-border, rgba(128,128,128,.35));
  }
  .scroll { overflow: auto; flex: 1 1 auto; max-height: 46vh; }
  ul { list-style: none; margin: 0; padding: 4px; }
  li { display: flex; gap: 8px; padding: 4px 4px; align-items: baseline; }
  li:hover { background: var(--vscode-list-hoverBackground, rgba(128,128,128,.12)); }
  li label { flex: 1 1 auto; cursor: pointer; min-width: 0; }
  .meta { opacity: .65; font-size: .85em; white-space: nowrap; }
  .title { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .prov { font-family: var(--vscode-editor-font-family, monospace); opacity: .9; }
  #stats { padding: 6px 8px; border-bottom: 1px solid var(--vscode-panel-border, rgba(128,128,128,.35)); }
  #stats .n { font-weight: 600; }
  #stats .warn { color: var(--vscode-editorWarning-foreground, #cca700); }
  pre {
    margin: 0; padding: 8px; white-space: pre-wrap; word-break: break-word;
    font-family: var(--vscode-editor-font-family, monospace);
    font-size: var(--vscode-editor-font-size, .9em);
    flex: 1 1 auto; overflow: auto; max-height: 46vh;
  }
  footer { display: flex; gap: var(--gap); margin-top: var(--gap); align-items: center; }
  #hint { opacity: .7; font-size: .85em; }
  .empty { padding: 12px; opacity: .6; }
</style>
</head>
<body>
  <div class="row">
    <label for="goal">Goal</label>
    <input id="goal" type="text" placeholder="optional — ranks the evidence">
    <label for="budget">Budget</label>
    <select id="budget">
      <option value="">none</option>
      <option value="compact">compact</option>
      <option value="balanced">balanced</option>
      <option value="full">full</option>
      <option value="auto">auto</option>
    </select>
    <label for="target">Target</label>
    <select id="target">
      <option value="claude">claude</option>
      <option value="codex">codex</option>
      <option value="grok">grok</option>
    </select>
    <button id="refresh" class="secondary">Refresh</button>
  </div>

  <main>
    <section>
      <h2>Sessions</h2>
      <div class="scroll"><ul id="sessions"></ul></div>
    </section>
    <section>
      <h2>Bundle preview</h2>
      <div id="stats">Select one or more sessions.</div>
      <div class="scroll"><pre id="bundle"></pre></div>
    </section>
  </main>

  <footer>
    <button id="copyCli" disabled>Copy CLI command</button>
    <button id="copyBundle" class="secondary" disabled>Copy bundle</button>
    <span id="hint">The Composer never launches an agent — it hands you the command.</span>
  </footer>

<script nonce="${nonce}">
(function () {
  "use strict";
  const vscode = acquireVsCodeApi();
  const state = { items: [], selected: new Set(), bundle: "", tokens: null,
                  dropped: [], trimmed: [], error: null, pending: null };

  const $ = (id) => document.getElementById(id);
  const sessionsEl = $("sessions");
  const statsEl = $("stats");
  const bundleEl = $("bundle");
  const copyCliEl = $("copyCli");
  const copyBundleEl = $("copyBundle");

  function fmtTime(t) {
    if (!t) return "";
    const d = new Date(t * 1000);
    if (isNaN(d.getTime())) return "";
    return d.toISOString().slice(0, 16).replace("T", " ");
  }

  function renderSessions() {
    sessionsEl.textContent = "";
    if (!state.items.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "No indexed sessions for this repo. Run voyager scan.";
      sessionsEl.appendChild(li);
      return;
    }
    state.items.forEach(function (s) {
      const li = document.createElement("li");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = state.selected.has(s.id);
      cb.addEventListener("change", function () {
        if (cb.checked) state.selected.add(s.id); else state.selected.delete(s.id);
        schedulePreview();
      });
      const label = document.createElement("label");
      // Every field below is untrusted (it comes from an agent's transcript),
      // so it goes in as text, never as markup.
      const title = document.createElement("span");
      title.className = "title";
      title.textContent = s.title || "(untitled)";
      const meta = document.createElement("span");
      meta.className = "meta";
      const prov = document.createElement("span");
      prov.className = "prov";
      prov.textContent = s.provider + ":" + String(s.native_id || "").slice(0, 12);
      meta.appendChild(prov);
      meta.appendChild(document.createTextNode(
        "  " + fmtTime(s.updated_at) +
        "  " + (s.message_count || 0) + "msg/" + (s.tool_count || 0) + "tool"));
      label.appendChild(title);
      label.appendChild(meta);
      li.appendChild(cb);
      li.appendChild(label);
      sessionsEl.appendChild(li);
    });
  }

  function renderPreview() {
    const n = state.selected.size;
    if (state.error) {
      statsEl.textContent = "Error: " + state.error;
      bundleEl.textContent = "";
      copyBundleEl.disabled = true;
      copyCliEl.disabled = true;
      return;
    }
    if (!n) {
      statsEl.textContent = "Select one or more sessions.";
      bundleEl.textContent = "";
      copyBundleEl.disabled = true;
      copyCliEl.disabled = true;
      return;
    }
    const parts = [];
    const strong = document.createElement("span");
    strong.className = "n";
    strong.textContent = String(n);
    parts.push(strong, document.createTextNode(
      " session" + (n === 1 ? "" : "s") + " selected"));
    if (state.tokens != null) {
      const t = document.createElement("span");
      t.className = "n";
      t.textContent = "~" + state.tokens;
      parts.push(document.createTextNode("  ·  "), t,
                 document.createTextNode(" tokens estimated"));
    }
    if (state.dropped.length) {
      parts.push(document.createTextNode("  ·  "));
      const w = document.createElement("span");
      w.className = "warn";
      w.textContent = "dropped: " + state.dropped.join(", ");
      parts.push(w);
    }
    if (state.trimmed.length) {
      parts.push(document.createTextNode("  ·  "));
      const w = document.createElement("span");
      w.className = "warn";
      w.textContent = "trimmed: " + state.trimmed.join(", ");
      parts.push(w);
    }
    statsEl.textContent = "";
    parts.forEach(function (p) { statsEl.appendChild(p); });

    bundleEl.textContent = state.bundle || "";
    copyBundleEl.disabled = !state.bundle;
    copyCliEl.disabled = !n;
  }

  function schedulePreview() {
    if (state.pending) clearTimeout(state.pending);
    state.pending = setTimeout(requestPreview, 250);
  }

  function requestPreview() {
    state.pending = null;
    const refs = state.items
      .filter(function (s) { return state.selected.has(s.id); })
      .map(function (s) { return s.id; });
    vscode.postMessage({
      type: "preview", refs: refs,
      goal: $("goal").value, budget: $("budget").value,
    });
  }

  function cliCommand() {
    const refs = state.items
      .filter(function (s) { return state.selected.has(s.id); })
      .map(function (s) { return String(s.native_id || s.id).slice(0, 12); });
    if (!refs.length) return "";
    const goal = $("goal").value.trim();
    let cmd = refs.length === 1
      ? "voyager handoff " + refs[0]
      : "voyager merge " + refs.join(" ");
    if (goal) {
      // Escape a double quote by building the backslash from its code point:
      // this file is itself a JS template literal, so a literal
      // backslash-quote here would be eaten one level of escaping earlier and
      // silently become a no-op.
      const BS = String.fromCharCode(92);
      cmd += ' --goal "' + goal.split('"').join(BS + '"') + '"';
    }
    const budget = $("budget").value;
    if (budget) cmd += " --budget " + budget;
    cmd += " --to " + $("target").value;
    return cmd;
  }

  $("goal").addEventListener("input", schedulePreview);
  $("budget").addEventListener("change", schedulePreview);
  $("refresh").addEventListener("click", function () {
    vscode.postMessage({ type: "ready" });
  });
  copyCliEl.addEventListener("click", function () {
    const cmd = cliCommand();
    if (cmd) vscode.postMessage({ type: "copy", text: cmd });
  });
  copyBundleEl.addEventListener("click", function () {
    if (state.bundle) vscode.postMessage({ type: "copy", text: state.bundle });
  });

  window.addEventListener("message", function (event) {
    const msg = event.data;
    if (msg.type === "sessions") {
      state.items = msg.items || [];
      // Drop selections whose session vanished from the index.
      const live = new Set(state.items.map(function (s) { return s.id; }));
      state.selected.forEach(function (id) {
        if (!live.has(id)) state.selected.delete(id);
      });
      renderSessions();
      renderPreview();
    } else if (msg.type === "preview") {
      if (msg.ok) {
        state.bundle = msg.bundle || "";
        state.tokens = msg.tokens;
        state.dropped = msg.dropped || [];
        state.trimmed = msg.trimmed || [];
        state.error = null;
      } else {
        state.bundle = ""; state.tokens = null;
        state.dropped = []; state.trimmed = [];
        state.error = msg.error || "unknown error";
      }
      renderPreview();
    }
  });

  renderPreview();
  vscode.postMessage({ type: "ready" });
})();
</script>
</body>
</html>`;
}

module.exports = { html };
