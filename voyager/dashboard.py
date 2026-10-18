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

#: How much of one message or tool output the detail panel shows.  Truncated
#rather than unlimited: a page that embeds a whole 100k-token log is a page that
#cannot be opened, and the full text lives in the index and the source transcript.
DETAIL_BODY = 1200

#: How many *additional* sessions a --repo view pulls into the explorer, so a
#: session with no message text is reachable too.
EXTRA_DETAIL_SESSIONS = 20

#: How many threads are listed per project.
THREADS_PER_PROJECT = 12

#: How many threads per project get a continuation preview.  Bounded so the page
#cost is what it displays, not what the index holds.
PREVIEW_THREADS_PER_PROJECT = 6

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


def _session_detail(store, sid: str) -> Dict[str, Any]:
    """One session, whole, for the detail panel.

    Everything here is untrusted text: session content is whatever an agent
    said, and a tool's output is whatever a tool printed.  Neither is ever
    treated as HTML -- `render_html` escapes on output, and that is the only
    place it is turned into one.

    Sessions that no longer exist in the store (retained history whose source
    rotated away) are marked, not dropped: D15 marks, never hides.
    """
    srow = store.con.execute(
        "SELECT id, provider, native_id, title, repo_root, cwd, started_at, "
        "updated_at, source_state FROM sessions WHERE id = ?", (sid,)).fetchone()
    evs = _q(store, "SELECT seq, ts, kind, role, tool_name, tool_call_id, "
                    "command, tool_output, exit_code, content, origin "
                    "FROM events WHERE sid = ? ORDER BY seq", (sid,))

    turns = []
    tools = []
    for ev in evs:
        kind = ev["kind"]
        if kind in ("user", "assistant", "reasoning") and ev["content"]:
            turns.append({
                "kind": kind, "when": _when(ev["ts"]),
                "text": _short(ev["content"], DETAIL_BODY),
                "truncated": len(ev["content"]) > DETAIL_BODY,
            })
        elif kind == "tool_call" and (ev["command"] or ev["tool_name"]):
            tools.append({
                "when": _when(ev["ts"]),
                "name": ev["tool_name"] or "tool",
                "command": _short(ev["command"], DETAIL_BODY),
                "call_id": ev["tool_call_id"],
                "exit_code": None,
                "output": "",
            })
        elif kind == "tool_result":
            # The result matches its call by call id: that join is what lets the
            # page show `pytest -q  # exit 1` instead of a bare command.
            for t in tools:
                if t["call_id"] and t["call_id"] == ev["tool_call_id"]:
                    t["exit_code"] = ev["exit_code"]
                    t["output"] = _short(ev["tool_output"], DETAIL_BODY)
                    break
            else:
                tools.append({
                    "when": _when(ev["ts"]), "name": "tool",
                    "command": "", "call_id": ev["tool_call_id"],
                    "exit_code": ev["exit_code"],
                    "output": _short(ev["tool_output"], DETAIL_BODY),
                })

    threads = [row["thread_id"] for row in _q(
        store, "SELECT thread_id FROM thread_sessions WHERE session_id = ?", (sid,))]

    return {
        "sid": sid,
        "provider": (srow["provider"] if srow else None) or "?",
        "native_id": (srow["native_id"] if srow else None) or "",
        "title": (srow["title"] if srow else None) or "",
        "repo": (srow["repo_root"] or srow["cwd"]) if srow else "",
        "started_at": _when(srow["started_at"]) if srow else "-",
        "updated_at": _when(srow["updated_at"]) if srow else "-",
        "source_state": (srow["source_state"] if srow else "SOURCE_MISSING"),
        "threads": threads,
        "turns": turns,
        "tools": tools,
        "origin": (evs[0]["origin"] if evs else None),
    }


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

    # --- session detail ----------------------------------------------------
    # One entry per session shown in the recent panel, so a reader can open one
    # and see the whole conversation -- tool calls and their exit codes
    # included, because that is where a claim and the evidence that contradicts
    # it sit side by side.
    #
    # Bounded on purpose: a page that embeds every session is a page nobody can
    # open.  The details carried are the ones the current page omits; the recent
    # panel stays the summary view.
    # `recent` only carries sessions with user/assistant text, so an empty
    # session would otherwise be unreachable from the page.  Include the ones
    # this repo owns as well, capped, so the explorer is complete for what is
    # shown rather than silently skipping the odd case.
    seen = {r["sid"] for r in recent}
    extra: List[str] = []
    if repo:
        for row in _q(store, "SELECT s.id FROM sessions s WHERE "
                             "COALESCE(s.repo_root, s.cwd) = ? "
                             "ORDER BY s.updated_at DESC LIMIT ?",
                      (repo, EXTRA_DETAIL_SESSIONS)):
            if row["id"] not in seen:
                extra.append(row["id"])
    details: Dict[str, Dict[str, Any]] = {}
    for sid in [r["sid"] for r in recent] + extra:
        if sid in details:
            continue
        details[sid] = _session_detail(store, sid)

    # --- searchable snapshot ---------------------------------------------
    # Searched client-side over the turns the page already has.  It is a snapshot
    # of the recent window, NOT a live FTS query over the whole index: the
    # placeholder and the footer both say so, because implying more than it is
    # would be the kind of lie that makes a tool untrustworthy.
    search_corpus = [
        {"sid": r["sid"], "provider": r["provider"], "kind": r["kind"],
         "when": _when(r["ts"]), "text": r["text"],
         "title": details.get(r["sid"], {}).get("title") or ""}
        for r in recent
    ]

    # --- continuation preview, for every project's threads ---------------
    # One read-only `api.continue_context` call per thread.  Never a target: no
    # lease, no pending attach, no bundle written.  This is exactly what the Core
    # compiles for `voyager continue --thread <id> --json`, so the page cannot
    # disagree with the command about what the next agent would receive.
    #
    # Salary-cap bounded: a project can show at most THREADS_PER_PROJECT threads,
    # and the page already truncates there, so the number of calls is bounded by
    # what is on screen rather than by the size of the index.
    from .api import continue_context as _continue_context

    previews: Dict[str, Dict[str, Any]] = {}
    for p in projects.values():
        for t in p["threads"][:PREVIEW_THREADS_PER_PROJECT]:
            try:
                res = _continue_context(db=store.db_path, thread_id=t["id"])
            except Exception:
                res = {}
            previews[t["id"]] = {
                "selected": res.get("scope") or [],
                "selected_ids": res.get("selected_session_ids") or [],
                "reason": res.get("selection_reason") or "",
                "scope": res.get("selection_scope") or "thread",
                "ambiguous": bool(res.get("thread_ambiguous")),
                "candidates": res.get("thread_candidates") or [],
                "excluded": res.get("excluded_session_count"),
                "tokens": res.get("estimated_tokens"),
                "dropped": res.get("dropped") or [],
                "bundle": res.get("bundle") or "",
                "error": res.get("error"),
            }

    # --- which WorkThreads in this repo are actually in contention -------
    # A repo with several active threads is the case where picking one silently
    # is worst, so the page states it rather than letting the first row act as
    # an implied choice.
    ambiguous_threads: List[Dict[str, Any]] = []
    for p in projects.values():
        active = [t for t in p["threads"] if t["status"] == "active"]
        if len(active) > 1:
            ambiguous_threads.append({
                "repo_root": p["repo_root"], "threads": active,
            })

    return {
        "generated_at": time.time(),
        "focus_thread": focus,
        "focus": focus_data,
        "focus_timeline": focus_timeline,
        "previews": previews,
        "ambiguous_threads": ambiguous_threads,
        "projects": sorted(projects.values(),
                           key=lambda p: -(p["last_activity"] or 0)),
        "recent": recent,
        "details": details,
        "search_corpus": search_corpus,
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
  var shown = 0;
  for (var i = 0; i < rows.length; i++) {
    var t = rows[i].getAttribute('data-text') || '';
    // the session id is searchable too, so a copied id finds its own turns
    var sid = rows[i].getAttribute('data-sid') || '';
    var ok = (q === '' || t.indexOf(q) >= 0 || sid.indexOf(q) >= 0);
    rows[i].style.display = ok ? '' : 'none';
    if (ok) { shown++; }
  }
  var note = document.getElementById('qnote');
  if (note) {
    if (q !== '' && shown === 0) {
      note.textContent = 'no match in the recent window — '
        + 'this page only holds a snapshot; '
        + 'voyager search "' + q + '" searches the whole index';
    } else {
      note.textContent = '';
    }
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
    previews = data.get("previews") or {}
    ambiguous = {a["repo_root"] for a in (data.get("ambiguous_threads") or [])}
    a("<div class='panel'><h2>Projects</h2>")
    if not data.get("projects"):
        a("<div class='empty'>no projects indexed yet</div>")
    for p in data["projects"]:
        a("<div style='margin-bottom:10px'>")
        a("<div><code>%s</code> <span class='pill'>%d active</span></div>"
          % (_e(p["repo_root"]), p["active"]))
        if p["repo_root"] in ambiguous:
            # More than one active task in this repo.  Say so, and say that
            # nothing here picks one: a list that looks like a default is how a
            # user ends up continuing the wrong task.
            a("<div class='warn' style='font-size:12px;margin:4px 0'>"
              "%d active WorkThreads &middot; nothing is selected for you &mdash; "
              "choose one in <a href='#continue'>Continuation preview</a></div>"
              % p["active"])
        a("<table>")
        for t in p["threads"]:
            cls = "ok" if t["status"] == "active" else "muted"
            a("<tr><td class='mono'>%s</td><td>%s</td><td class='%s'>%s</td>"
              "<td class='muted'>%s</td><td class='when'>%s</td></tr>"
              % (_e(t["id"]), _e(_short(t["title"], 60)), cls, _e(t["status"]),
                 "members %s" % _e(t["members"]), _e(_when(t["updated_at"]))))
        a("</table></div>")
    a("</div>")

    # --- continuation preview ---------------------------------------------
    # The Core already compiles this.  The page only shows what it would send,
    # so nothing here takes a lease, writes a bundle or starts an agent.
    a("<div class='panel' id='continue'><h2>Continuation preview</h2>")
    if not previews:
        a("<div class='empty'>no WorkThread to preview</div>")
    else:
        for p in data["projects"]:
            for t in p["threads"]:
                pv = previews.get(t["id"])
                if not pv:
                    continue
                a("<div class='panel' style='margin:0 0 10px'>")
                a("<div><code>%s</code> %s <span class='pill'>%s</span></div>"
                  % (_e(t["id"]), _e(_short(t["title"], 70)), _e(t["status"])))
                if pv.get("goal_hint"):
                    a("<div class='muted'>%s</div>" % _e(pv["goal_hint"]))
                if pv.get("error"):
                    a("<div class='bad'>%s</div>" % _e(pv["error"]))
                else:
                    a("<table style='margin-top:6px'>")
                    a("<tr><th>scope</th><td>%s</td></tr>"
                      % _e(pv.get("scope")))
                    a("<tr><th>sessions</th><td class='mono'>%s</td></tr>"
                      % _e(", ".join(pv.get("selected_ids") or []) or "-"))
                    if pv.get("reason"):
                        a("<tr><th>why</th><td class='muted'>%s</td></tr>"
                          % _e(_short(pv["reason"], 200)))
                    if pv.get("tokens"):
                        a("<tr><th>context</th><td>~%s tokens</td></tr>"
                          % _e(pv.get("tokens")))
                    if pv.get("dropped"):
                        a("<tr><th>dropped</th><td class='warn'>%s</td></tr>"
                          % _e(", ".join(str(d) for d in pv["dropped"][:3])))
                    a("</table>")
                    if pv.get("bundle"):
                        # Rendered as text, not HTML: the bundle quotes what an
                        # agent said, so its author is not trusted here either.
                        a("<pre style='max-height:220px;overflow:auto;"
                          "white-space:pre-wrap;font-size:11px'>%s</pre>"
                          % _e(_short(pv["bundle"], 4000)))
                    a("<div class='muted' style='font-size:11px'>"
                      "To continue this task:<br>"
                      "<code>voyager continue --thread %s</code></div>"
                      % _e(t["id"]))
                a("</div>")
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
          "<option value='ACTIVE_SOURCE'>active</option>"
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
      "(snapshot of the recent window; the CLI does full-text search: "
      "voyager search)' oninput='filterTurns()'>")
    a("<div id='turns' style='margin-top:8px'>")
    if not data.get("recent"):
        a("<div class='empty'>nothing recent</div>")
    details = data.get("details") or {}
    for r in data["recent"]:
        # The session id is the anchor into the detail panel, so a reader can get
        # from any turn to the whole conversation in one click.
        a("<div class='turn' data-text='%s' data-sid='%s'>"
          "<span class='when'>%s</span> "
          "<span class='pill'>%s</span> <span class='pill'>%s</span> "
          "<a href='#s-%s' class='mono'>%s</a><br>%s</div>"
          % (_e(r["text"].lower()), _e(r["sid"]), _e(_when(r["ts"])),
             _e(r["kind"]), _e(r["provider"]), _e(r["sid"]), _e(r["sid"]),
             _e(r["text"])))
    a("</div></div>")

    # A place for the "no match" note, and a line stating the scope rather than
    # letting a search box imply it holds the whole index.
    a("<div id='qnote' class='warn' style='font-size:12px;margin:4px 0'></div>")
    a("<div class='muted' style='font-size:11px;margin:2px 0 8px'>"
      "The filter above searches only the recent window shown here — it is a "
      "snapshot, not the whole index. Full-text search over every indexed "
      "session: <code>voyager search &lt;term&gt;</code></div>")

    # --- session explorer ---------------------------------------------------
    # Each turn above links here.  The whole conversation, with tool calls and
    # their exit codes, escaped and truncated.
    a("<div class='panel' id='sessions'><h2>Sessions</h2>")
    if not details:
        a("<div class='empty'>no sessions to open</div>")
    for sid, sdet in details.items():
        a("<div class='panel' id='s-%s' style='margin:0 0 10px'>" % _e(sid))
        a("<div><code>%s</code> <span class='pill'>%s</span></div>"
          % (_e(sid), _e(sdet["provider"])))
        a("<table style='margin-top:6px'>")
        a("<tr><th>title</th><td>%s</td></tr>" % _e(_short(sdet["title"], 80)))
        a("<tr><th>repo</th><td class='mono'>%s</td></tr>" % _e(sdet["repo"]))
        a("<tr><th>window</th><td>%s &rarr; %s</td></tr>"
          % (_e(sdet["started_at"]), _e(sdet["updated_at"])))
        if sdet["threads"]:
            a("<tr><th>WorkThread</th><td class='mono'>%s</td></tr>"
              % _e(", ".join(sdet["threads"])))
        else:
            a("<tr><th>WorkThread</th><td class='muted'>none &mdash; "
              "no task links this session</td></tr>")
        a("<tr><th>origin</th><td class='muted'>%s</td></tr>" % _e(sdet["origin"]))
        if sdet["source_state"] and sdet["source_state"] != "SOURCE_OK":
            a("<tr><th>source</th><td class='warn'>%s</td></tr>"
              % _e(sdet["source_state"]))
        a("</table>")

        # Committed to text, not markup: a tool call shown as an executable
        # button would be a remote-code hole in a local file.
        if sdet["tools"]:
            a("<h2>Tool calls</h2>")
            a("<table><tr><th>when</th><th>tool</th><th>command</th>"
              "<th>exit</th><th>output</th></tr>")
            for t in sdet["tools"]:
                ec = t["exit_code"]
                ecls = "bad" if ec not in (0, None) else "muted"
                a("<tr><td class='when'>%s</td><td>%s</td>"
                  "<td class='mono'>%s</td><td class='%s'>%s</td>"
                  "<td class='muted'>%s</td></tr>"
                  % (_e(t["when"]), _e(t["name"]), _e(t["command"]), ecls,
                     _e(ec if ec is not None else "-"), _e(t["output"])))
            a("</table>")

        a("<h2>Conversation</h2>")
        for t in sdet["turns"]:
            note = (" <span class='warn'>(truncated)</span>"
                    if t.get("truncated") else "")
            a("<div class='turn'><span class='when'>%s</span> "
              "<span class='pill'>%s</span> %s%s</div>"
              % (_e(t["when"]), _e(t["kind"]), _e(t["text"]), note))
        a("</div>")
    a("</div>")

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
