"""A local dashboard: what is going on, at a glance.

Deliberately not a chat client.  This is an observation and control panel for the
continuity layer, and it is a **single self-contained HTML file** -- no server, no
CDN, no network requests, no JavaScript dependencies.  Open it, read it, close it.

It answers the questions a person actually has:

    which projects have work in them?
    which WorkThread is active, and for which repo?
    which agents took part, and what did the last one say?
    is continuity healthy right now?

Everything shown is derived from the same sources as the CLI (`doctor`,
`thread_brief`, the capability matrix), so the dashboard cannot tell a different
story from the commands.
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

#: How many recent turns the activity panel shows.
RECENT_TURNS = 60

#: How many threads are listed per project.
THREADS_PER_PROJECT = 12

#: O3: how many timeline events the page carries.  A timeline is a
#: lifecycle/milestone view, not a transcript dump -- the newest events are
#: the first screen, and the rest is one `voyager thread timeline` away.
TIMELINE_LIMIT = 40


def _q(store, sql: str, args: tuple = ()) -> List[Any]:
    try:
        return list(store.q(sql, args))
    except Exception:
        return []


def _short(text: Optional[str], n: int = 220) -> str:
    if not text:
        return ""
    return " ".join(str(text).split())[:n]


def build(store, repo: Optional[str] = None) -> Dict[str, Any]:
    """Collect everything the page shows. Read-only and deterministic."""
    from .doctor import run as doctor_run
    from .thread_brief import activity

    health = doctor_run(repo)
    threads = _q(store, "SELECT t.*, COUNT(ts.session_id) AS members "
                        "FROM threads t LEFT JOIN thread_sessions ts "
                        "ON ts.thread_id = t.id GROUP BY t.id "
                        "ORDER BY t.updated_at DESC")

    projects: Dict[str, Dict[str, Any]] = {}
    for t in threads:
        key = t["repo_root"] or "(unknown repo)"
        p = projects.setdefault(key, {"repo_root": key, "threads": [],
                                      "active": 0, "last_activity": 0})
        p["threads"].append({
            "id": t["id"], "title": t["title"], "goal": t["goal"],
            "status": t["status"], "members": t["members"],
            "updated_at": t["updated_at"],
        })
        if t["status"] == "active":
            p["active"] += 1
        p["last_activity"] = max(p["last_activity"], t["updated_at"] or 0)
    for p in projects.values():
        p["threads"].sort(key=lambda x: -(x["updated_at"] or 0))
        p["threads"] = p["threads"][:THREADS_PER_PROJECT]

    # the active thread for this repo, else the most recently touched one
    focus = None
    if repo:
        for t in threads:
            if t["repo_root"] == repo and t["status"] == "active":
                focus = t["id"]
                break
    if not focus and threads:
        focus = threads[0]["id"]

    focus_data: Dict[str, Any] = {}
    if focus:
        try:
            focus_data = activity(store, focus, limit=30)
        except Exception:
            focus_data = {}

    # O3: the timeline comes from the canonical model, never from a second
    # aggregation built here -- the dashboard is a consumer like any other.
    focus_timeline: Dict[str, Any] = {}
    if focus:
        try:
            from .timeline import build_thread_timeline
            focus_timeline = build_thread_timeline(store, focus,
                                                   limit=TIMELINE_LIMIT)
        except Exception:
            focus_timeline = {}

    recent: List[Dict[str, Any]] = []
    for row in _q(store, "SELECT e.ts, e.kind, e.content, e.origin, s.provider, "
                         "e.sid FROM events e LEFT JOIN sessions s ON e.sid = s.id "
                         "WHERE e.kind IN ('user','assistant') "
                         "AND COALESCE(e.content,'') <> '' "
                         "ORDER BY e.ts DESC LIMIT ?", (RECENT_TURNS,)):
        recent.append({
            "ts": row["ts"], "kind": row["kind"], "provider": row["provider"] or "?",
            "origin": row["origin"], "sid": row["sid"],
            "text": _short(row["content"]),
        })

    checkpoints = _q(store, "SELECT id, thread_id, goal, phase, created_at "
                            "FROM checkpoints ORDER BY created_at DESC LIMIT 20")

    return {
        "generated_at": time.time(),
        "focus_thread": focus,
        "focus": focus_data,
        "focus_timeline": focus_timeline,
        "projects": sorted(projects.values(),
                           key=lambda p: -(p["last_activity"] or 0)),
        "recent": recent,
        "checkpoints": [dict(c) for c in checkpoints],
        "health": {
            "store": health.get("store"),
            "continuity": health.get("continuity"),
            "cache": health.get("cache"),
            "leases": health.get("leases"),
            "pending": health.get("pending"),
            "verification": health.get("verification"),
            "providers": {p: {"state": v["state"],
                              "installed": v["installed"],
                              "hook_registered": v["hook_registered"],
                              "hook_fired": v["hook_fired"]}
                          for p, v in (health.get("providers") or {}).items()},
            "blocking": health.get("blocking") or [],
            "external": health.get("external") or [],
            "non_blocking": health.get("non_blocking") or [],
            "warnings": health.get("warnings") or [],
        },
    }


_CSS = """
:root { color-scheme: light dark; --fg:#1c1c1e; --muted:#6b6b70; --bg:#ffffff;
        --panel:#f6f6f8; --line:#e2e2e6; --accent:#0a5cff; --warn:#b26a00;
        --bad:#b3261e; --good:#1a7f37; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e8ea; --muted:#9a9aa0; --bg:#111113; --panel:#1b1b1f;
          --line:#2c2c31; --accent:#6aa2ff; --warn:#e0a458; --bad:#ff6b6b;
          --good:#5dd07a; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
       font:14px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:15px; margin:0 0 10px;
     letter-spacing:.02em; text-transform:uppercase; color:var(--muted); }
.sub { color:var(--muted); margin-bottom:20px; font-size:12px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
        gap:16px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px;
         padding:16px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line);
        vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; }
tr:last-child td { border-bottom:0; }
code,.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
.pill { display:inline-block; padding:1px 7px; border-radius:999px; font-size:11px;
        border:1px solid var(--line); }
.ok { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); }
.muted { color:var(--muted); }
input[type=search] { width:100%; padding:7px 9px; border-radius:7px;
        border:1px solid var(--line); background:var(--bg); color:var(--fg); }
ul { margin:0; padding-left:18px; } li { margin:2px 0; }
.turn { padding:6px 0; border-bottom:1px solid var(--line); }
.turn:last-child { border-bottom:0; }
.when { color:var(--muted); font-size:11px; }
.empty { color:var(--muted); font-style:italic; }
/* O3: retained history is marked, never hidden.  The wording matters here --
   a rotated source is not a removal (DECISIONS D15), so this page never uses
   that vocabulary, not even in a comment. */
.retained { border-left:3px solid var(--warn); padding-left:8px; }
.tag { font-size:11px; color:var(--warn); border:1px solid var(--warn);
       border-radius:999px; padding:0 6px; margin-left:6px; }
"""

_JS = """
function filterTurns() {
  var q = document.getElementById('q').value.toLowerCase();
  var rows = document.querySelectorAll('#turns .turn');
  for (var i = 0; i < rows.length; i++) {
    var t = rows[i].getAttribute('data-text') || '';
    rows[i].style.display = (q === '' || t.indexOf(q) >= 0) ? '' : 'none';
  }
}

// O3.4 filters: provider, event type, live/retained.  Pure client-side --
// the page still makes no requests.
function filterTimeline() {
  var p = document.getElementById('tlProv').value;
  var k = document.getElementById('tlKind').value;
  var s = document.getElementById('tlState').value;
  var rows = document.querySelectorAll('#timeline .turn');
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var ok = (p === '' || r.getAttribute('data-provider') === p)
          && (k === '' || r.getAttribute('data-kind') === k)
          && (s === '' || r.getAttribute('data-state') === s);
    r.style.display = ok ? '' : 'none';
  }
}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _when(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _state_class(state: str) -> str:
    if state in ("ZERO_TOUCH_LIVE_VERIFIED", "LIVE_VERIFIED"):
        return "ok"
    if state == "UNIT_VERIFIED":
        return "warn"
    return "muted"


def render_html(data: Dict[str, Any]) -> str:
    """One self-contained page. No external requests of any kind."""
    out: List[str] = []
    a = out.append

    a("<!doctype html><html><head><meta charset='utf-8'>")
    a("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    a("<title>sessionFlow dashboard</title><style>%s</style></head><body>" % _CSS)
    a("<h1>sessionFlow</h1>")
    a("<div class='sub'>generated %s &middot; local only &middot; nothing here is "
      "fetched from the network</div>" % _e(_when(data.get("generated_at"))))

    health = data.get("health") or {}
    store = health.get("store") or {}
    cont = health.get("continuity") or {}

    # --- top line: is it healthy -------------------------------------------
    blocking = health.get("blocking") or []
    a("<div class='panel' style='margin-bottom:16px'>")
    if blocking:
        a("<h2>Continuity: <span class='bad'>%d blocking issue(s)</span></h2>"
          % len(blocking))
        a("<ul>")
        for i in blocking:
            a("<li><code>%s</code> %s</li>" % (_e(i.get("id")), _e(i.get("detail"))))
        a("</ul>")
    else:
        a("<h2>Continuity: <span class='ok'>no blocking issues</span></h2>")
    a("<div class='muted'>%s sessions &middot; %s threads &middot; %s events "
      "&middot; %s external &middot; %s tracked debt</div>"
      % (_e(store.get("sessions")), _e(store.get("threads")),
         _e((cont.get("coverage") or {}).get("events")),
         len(health.get("external") or []), len(health.get("non_blocking") or [])))
    # O4: show leases + pending in the health summary, reusing doctor's model
    leases = health.get("leases") or {}
    if leases.get("total"):
        a("<div class='muted'>leases: %s total, %s active, %s expired</div>"
          % (_e(leases.get("total")), _e(leases.get("active")),
             _e(leases.get("expired"))))
    pending = health.get("pending") or {}
    if pending.get("open"):
        a("<div class='muted'>pending attach: %s open, %s stale</div>"
          % (_e(pending.get("open")),
             _e(len(pending.get("stale", [])))))
    a("</div>")

    a("<div class='grid'>")

    # --- providers ----------------------------------------------------------
    a("<div class='panel'><h2>Providers</h2><table>")
    a("<tr><th>provider</th><th>state</th><th>installed</th><th>hook</th>"
      "<th>fired</th></tr>")
    for p, v in sorted((health.get("providers") or {}).items()):
        a("<tr><td class='mono'>%s</td><td class='%s'>%s</td><td>%s</td>"
          "<td>%s</td><td>%s</td></tr>"
          % (_e(p), _state_class(v.get("state")), _e(v.get("state")),
             "Y" if v.get("installed") else "N",
             "Y" if v.get("hook_registered") else "N",
             "Y" if v.get("hook_fired") else "N"))
    a("</table></div>")

    # --- projects -----------------------------------------------------------
    a("<div class='panel'><h2>Projects</h2>")
    if not data.get("projects"):
        a("<div class='empty'>no projects indexed yet</div>")
    for p in data["projects"]:
        a("<div style='margin-bottom:10px'>")
        a("<div><code>%s</code> <span class='pill'>%d active</span></div>"
          % (_e(p["repo_root"]), p["active"]))
        a("<table>")
        for t in p["threads"]:
            cls = "ok" if t["status"] == "active" else "muted"
            a("<tr><td class='mono'>%s</td><td>%s</td><td class='%s'>%s</td>"
              "<td class='muted'>%s</td><td class='when'>%s</td></tr>"
              % (_e(t["id"]), _e(_short(t["title"], 60)), cls, _e(t["status"]),
                 "members %s" % _e(t["members"]), _e(_when(t["updated_at"]))))
        a("</table></div>")
    a("</div>")

    # --- focus thread -------------------------------------------------------
    focus = data.get("focus") or {}
    a("<div class='panel'><h2>Active WorkThread</h2>")
    if not data.get("focus_thread"):
        a("<div class='empty'>none</div>")
    else:
        a("<div><code>%s</code></div>" % _e(data["focus_thread"]))
        if focus.get("title"):
            a("<div>%s</div>" % _e(focus["title"]))
        if focus.get("goal"):
            a("<div class='muted'>goal: %s</div>" % _e(focus["goal"]))
        a("<table style='margin-top:8px'>")
        a("<tr><th>agent</th><th>band</th><th>events</th><th>human</th>"
          "<th>last said</th></tr>")
        for c in (focus.get("contributions") or [])[:12]:
            a("<tr><td class='mono'>%s</td><td>%s</td><td>%s</td><td>%s</td>"
              "<td class='muted'>%s</td></tr>"
              % (_e(c.get("provider")), _e(c.get("band")), _e(c.get("events")),
                 _e(c.get("human_turns")), _e(_short(c.get("last_line"), 90))))
        a("</table>")
    a("</div>")

    # --- timeline (O3) ------------------------------------------------------
    # Rendered from data["focus_timeline"], which comes from the canonical
    # voyager/timeline.py -- the page aggregates nothing itself.
    tl = data.get("focus_timeline") or {}
    a("<div class='panel'><h2>WorkThread timeline</h2>")
    if not tl or not tl.get("events"):
        a("<div class='empty'>no timeline events &mdash; "
          "<code>voyager thread timeline &lt;id&gt;</code> prints the same model"
          "</div>")
    else:
        a("<div class='muted'>%s &middot; showing %s of %s event(s)</div>"
          % (_e((tl.get("thread") or {}).get("id")), _e(tl.get("shown")),
             _e(tl.get("total"))))
        provs = sorted({e["provider"] for e in tl["events"] if e.get("provider")})
        kinds = sorted({e["event_type"] for e in tl["events"]})
        a("<div style='margin:8px 0'>")
        a("<select id='tlProv' onchange='filterTimeline()'>"
          "<option value=''>all providers</option>%s</select> "
          % "".join("<option value='%s'>%s</option>" % (_e(p), _e(p))
                    for p in provs))
        a("<select id='tlKind' onchange='filterTimeline()'>"
          "<option value=''>all event types</option>%s</select> "
          % "".join("<option value='%s'>%s</option>" % (_e(k), _e(k))
                    for k in kinds))
        a("<select id='tlState' onchange='filterTimeline()'>"
          "<option value=''>all states</option>"
          "<option value='LIVE'>live</option>"
          "<option value='SOURCE_MISSING'>retained</option></select>")
        a("</div>")
        a("<div id='timeline'>")
        for e in tl["events"]:
            state = e.get("source_state") or ""
            is_retained = state == "SOURCE_MISSING"
            a("<div class='turn%s' data-provider='%s' data-kind='%s' "
              "data-state='%s'><span class='when'>%s</span> "
              "<span class='pill'>%s</span> %s%s%s</div>"
              % (" retained" if is_retained else "",
                 _e(e.get("provider") or ""), _e(e["event_type"]), _e(state),
                 _e(_when(e.get("timestamp"))), _e(e["event_type"]),
                 ("<span class='mono'>%s</span> " % _e(e["provider"]))
                 if e.get("provider") else "",
                 _e(e.get("summary")),
                 "<span class='tag'>retained &middot; source unavailable</span>"
                 if is_retained else ""))
        a("</div>")
    a("</div>")

    # --- recent activity (with a client-side filter) ------------------------
    a("<div class='panel'><h2>Recent activity</h2>")
    a("<input id='q' type='search' placeholder='filter these turns "
      "(the CLI does full-text search: voyager search)' oninput='filterTurns()'>")
    a("<div id='turns' style='margin-top:8px'>")
    if not data.get("recent"):
        a("<div class='empty'>nothing recent</div>")
    for r in data["recent"]:
        a("<div class='turn' data-text='%s'><span class='when'>%s</span> "
          "<span class='pill'>%s</span> <span class='mono'>%s</span><br>%s</div>"
          % (_e(r["text"].lower()), _e(_when(r["ts"])), _e(r["kind"]),
             _e(r["provider"]), _e(r["text"])))
    a("</div></div>")

    # --- checkpoints --------------------------------------------------------
    a("<div class='panel'><h2>Checkpoints</h2>")
    if not data.get("checkpoints"):
        a("<div class='empty'>none recorded &mdash; `voyager thread checkpoint "
          "create` writes one</div>")
    else:
        a("<table><tr><th>thread</th><th>phase</th><th>goal</th><th>when</th></tr>")
        for c in data["checkpoints"]:
            a("<tr><td class='mono'>%s</td><td>%s</td><td>%s</td>"
              "<td class='when'>%s</td></tr>"
              % (_e(c.get("thread_id")), _e(c.get("phase")),
                 _e(_short(c.get("goal"), 60)), _e(_when(c.get("created_at")))))
        a("</table>")
    a("</div>")

    # --- debt + warnings (O4 canonical model) -------------------------------
    a("<div class='panel'><h2>Open items</h2><table>")
    for group, items in (("external", health.get("external") or []),
                         ("warning", health.get("warnings") or []),
                         ("debt", health.get("non_blocking") or [])):
        for i in items:
            a("<tr><td class='mono'>%s</td><td class='muted'>%s</td>"
              "<td class='when'>%s</td></tr>"
              % (_e(i.get("id")), _e(_short(i.get("detail"), 150)), _e(group)))
    a("</table></div>")

    a("</div>")                       # /grid
    a("<script>%s</script>" % _JS)
    a("</body></html>")
    return "".join(out)


def write(store, out_path: Path, repo: Optional[str] = None) -> Path:
    """Render the dashboard to one file."""
    data = build(store, repo=repo)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(data), encoding="utf-8")
    return out_path
