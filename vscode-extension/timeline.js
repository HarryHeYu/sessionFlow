/**
 * Voyager WorkThread Timeline — webview markup (roadmap O3).
 *
 * Same safety base as the Context Composer: one self-contained document, a
 * nonce CSP, no CDN, no framework, no network.  Every field it shows came out
 * of an agent's transcript (titles, checkpoint text, repo paths), so it is all
 * untrusted and goes in through textContent — never innerHTML.
 *
 * It also never switches anything.  The handoff engine
 * (`continuity.handoff_thread`) is the one place that decides who may write a
 * WorkThread (D13/D14), so this view hands the user the canonical CLI command
 * instead of growing a second switch path — the same choice the Context
 * Composer makes.
 *
 * Contract with extension.js:
 *   webview -> host : {type:"ready"}
 *                     {type:"timeline", thread_id, provider, kind, state}
 *                     {type:"copy", text}
 *   host -> webview : {type:"threads", items:[...]}
 *                     {type:"timeline", ok, data|error}
 */

function html(nonce) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>Voyager Timeline</title>
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
  .row { display: flex; gap: var(--gap); align-items: center; margin-bottom: var(--gap); flex-wrap: wrap; }
  select {
    background: var(--vscode-dropdown-background, var(--vscode-input-background));
    color: var(--vscode-dropdown-foreground, var(--vscode-input-foreground));
    border: 1px solid var(--vscode-dropdown-border, transparent);
    padding: 3px 6px; font-family: inherit; font-size: inherit;
  }
  button {
    background: var(--vscode-button-background);
    color: var(--vscode-button-foreground);
    border: none; padding: 4px 10px; border-radius: 2px;
    font-family: inherit; font-size: inherit; cursor: pointer;
  }
  button.secondary {
    background: var(--vscode-button-secondaryBackground, transparent);
    color: var(--vscode-button-secondaryForeground, inherit);
    border: 1px solid var(--vscode-button-border, rgba(128,128,128,.4));
  }
  button:disabled { opacity: .5; cursor: default; }
  #meta { opacity: .75; font-size: .85em; margin-bottom: 6px; }
  ol { list-style: none; margin: 0; padding: 0; }
  li.ev {
    border-left: 3px solid var(--vscode-panel-border, rgba(128,128,128,.35));
    padding: 6px 8px; margin-bottom: 2px;
  }
  li.ev.retained { border-left-color: var(--vscode-editorWarning-foreground, #cca700); }
  .when { opacity: .7; font-size: .82em; font-variant-numeric: tabular-nums; }
  .kind {
    font-family: var(--vscode-editor-font-family, monospace);
    font-size: .85em; margin: 0 6px;
  }
  .prov { opacity: .85; font-family: var(--vscode-editor-font-family, monospace); }
  .sum { display: block; margin-top: 2px; }
  .tag {
    font-size: .78em; margin-left: 6px; padding: 0 6px; border-radius: 999px;
    color: var(--vscode-editorWarning-foreground, #cca700);
    border: 1px solid currentColor;
  }
  .acts { margin-top: 4px; }
  .acts button { font-size: .82em; padding: 1px 6px; margin-right: 4px; }
  .empty { opacity: .6; padding: 10px; }
  .err { color: var(--vscode-errorForeground, #f48771); padding: 8px; }
  footer { margin-top: var(--gap); opacity: .75; font-size: .85em; }
</style>
</head>
<body>
  <div class="row">
    <label for="thread">WorkThread</label>
    <select id="thread"></select>
    <label for="prov">Provider</label>
    <select id="prov"><option value="">all</option></select>
    <label for="kind">Event</label>
    <select id="kind"><option value="">all</option></select>
    <label for="state">State</label>
    <select id="state">
      <option value="">all</option>
      <option value="LIVE">live</option>
      <option value="SOURCE_MISSING">retained</option>
    </select>
    <button id="refresh" class="secondary">Refresh</button>
  </div>

  <div id="meta"></div>
  <ol id="events"></ol>
  <footer>
    Switching stays in the CLI: the handoff engine is the one place that
    decides who may write a WorkThread. Use the buttons to copy the command.
  </footer>

<script nonce="${nonce}">
(function () {
  "use strict";
  const vscode = acquireVsCodeApi();
  const state = { threads: [], data: null };

  const $ = (id) => document.getElementById(id);
  const eventsEl = $("events");

  function fmtTime(t) {
    if (!t) return "---------- --:--";
    const d = new Date(t * 1000);
    if (isNaN(d.getTime())) return "---------- --:--";
    return d.toISOString().slice(0, 16).replace("T", " ");
  }

  function copy(text) {
    if (text) vscode.postMessage({ type: "copy", text: text });
  }

  function renderThreads() {
    const sel = $("thread");
    const keep = sel.value;
    sel.textContent = "";
    state.threads.forEach(function (t) {
      const o = document.createElement("option");
      o.value = t.id;
      o.textContent = t.id + "  [" + t.status + "]  " + (t.title || "");
      sel.appendChild(o);
    });
    if (keep) sel.value = keep;
  }

  function renderFilters(data) {
    const events = (data && data.events) || [];
    function fill(id, values) {
      const sel = $(id);
      const keep = sel.value;
      while (sel.options.length > 1) sel.remove(1);
      values.forEach(function (v) {
        const o = document.createElement("option");
        o.value = v; o.textContent = v;
        sel.appendChild(o);
      });
      sel.value = keep;
    }
    const provs = {};
    const kinds = {};
    events.forEach(function (e) {
      if (e.provider) provs[e.provider] = 1;
      if (e.event_type) kinds[e.event_type] = 1;
    });
    fill("prov", Object.keys(provs).sort());
    fill("kind", Object.keys(kinds).sort());
  }

  function render() {
    const data = state.data;
    eventsEl.textContent = "";
    if (!data) { $("meta").textContent = ""; return; }
    if (data.error) {
      const d = document.createElement("div");
      d.className = "err";
      d.textContent = data.error;
      eventsEl.appendChild(d);
      $("meta").textContent = "";
      return;
    }
    $("meta").textContent = (data.thread ? data.thread.id : "") +
      "  showing " + data.shown + " of " + data.total + " event(s)";

    const prov = $("prov").value;
    const kind = $("kind").value;
    const st = $("state").value;

    (data.events || []).forEach(function (e) {
      if (prov && e.provider !== prov) return;
      if (kind && e.event_type !== kind) return;
      if (st && (e.source_state || "") !== st) return;

      const li = document.createElement("li");
      li.className = "ev" + (e.source_state === "SOURCE_MISSING" ? " retained" : "");

      const when = document.createElement("span");
      when.className = "when";
      when.textContent = fmtTime(e.timestamp);

      const k = document.createElement("span");
      k.className = "kind";
      k.textContent = e.event_type;

      li.appendChild(when);
      li.appendChild(k);

      if (e.provider) {
        const p = document.createElement("span");
        p.className = "prov";
        p.textContent = e.provider;
        li.appendChild(p);
      }
      if (e.source_state === "SOURCE_MISSING") {
        const t = document.createElement("span");
        t.className = "tag";
        t.textContent = "retained \\u00b7 source unavailable";
        li.appendChild(t);
      }

      const sum = document.createElement("span");
      sum.className = "sum";
      sum.textContent = e.summary || "";
      li.appendChild(sum);

      const acts = document.createElement("span");
      acts.className = "acts";

      if (e.session_id) {
        const b = document.createElement("button");
        b.className = "secondary";
        b.textContent = "Copy session id";
        b.addEventListener("click", function () { copy(e.session_id); });
        acts.appendChild(b);
      }
      if (e.session_id) {
        // A retained session is history, not a place to switch into: its
        // provider source is gone, so the command offered is the thread-level
        // one that goes through the handoff engine.
        const b = document.createElement("button");
        b.className = "secondary";
        b.textContent = "Copy switch command";
        b.addEventListener("click", function () {
          copy("voyager switch codex --thread " +
               (data.thread ? data.thread.id : ""));
        });
        acts.appendChild(b);
      }
      if (acts.childNodes.length) li.appendChild(acts);
      eventsEl.appendChild(li);
    });

    if (!eventsEl.childNodes.length) {
      const d = document.createElement("div");
      d.className = "empty";
      d.textContent = "no events match these filters";
      eventsEl.appendChild(d);
    }
  }

  function request() {
    const tid = $("thread").value;
    if (!tid) return;
    vscode.postMessage({ type: "timeline", thread_id: tid });
  }

  $("thread").addEventListener("change", request);
  $("prov").addEventListener("change", render);
  $("kind").addEventListener("change", render);
  $("state").addEventListener("change", render);
  $("refresh").addEventListener("click", function () {
    vscode.postMessage({ type: "ready" });
  });

  window.addEventListener("message", function (event) {
    const msg = event.data;
    if (msg.type === "threads") {
      state.threads = msg.items || [];
      renderThreads();
      request();
    } else if (msg.type === "timeline") {
      state.data = msg.ok ? msg.data : { error: msg.error || "unknown error" };
      renderFilters(state.data);
      render();
    }
  });

  vscode.postMessage({ type: "ready" });
})();
</script>
</body>
</html>`;
}

module.exports = { html };
