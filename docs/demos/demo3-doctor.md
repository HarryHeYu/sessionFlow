# Demo 3 — doctor diagnostics and safe repair

The derived search index is corrupted (every FTS row deleted; the
canonical events are untouched). Doctor detects it, the dry run
plans the repair, `--fix` rebuilds the index from canonical rows,
and search works again. Retained/archived history is reported as
informational — never as corruption.

### voyager doctor

```
$ voyager doctor
voyager doctor
==============================================================

store
  path     : E:\sessionflow-scratch\rc1-demo\index.db
  sessions : 3   threads: 1

continuity
  active threads : 1   pending: 0   ambiguous: False
  provenance     : 10 events, 0.0% classified, 0 literal 'unknown'
  cache          : 0 entries, newest None s ago
  context format : tiered-v1

providers
  provider     state                      installed hook     fired  budget  
  --------------------------------------------------------------------------
  codex        UNIT_VERIFIED              Y        Y        N      7600c   
  claude       UNIT_VERIFIED              Y        Y        N      7600c   
  grok         UNIT_VERIFIED              Y        N        N      5000c   
  zcode        UNIT_VERIFIED              Y        Y        N      8000c   
  cursor       UNIT_VERIFIED              Y        Y        N      8000c   
  kiro         UNIT_VERIFIED              Y        N        N      8000c   
  antigravity  UNIT_VERIFIED              Y        Y        N      8000c   
  dsh          NOT_FOUND_IN_CURRENT_AUDIT Y        N        N      -       

verification
  provider     declared                   observed                   effective                 
  --------------------------------------------------------------------------------------------
  codex        ZERO_TOUCH_LIVE_VERIFIED   UNIT_VERIFIED              UNIT_VERIFIED             
  claude       ZERO_TOUCH_LIVE_VERIFIED   UNIT_VERIFIED              UNIT_VERIFIED             
  grok         ZERO_TOUCH_LIVE_VERIFIED   UNIT_VERIFIED              UNIT_VERIFIED             
  zcode        UNIT_VERIFIED              UNIT_VERIFIED              UNIT_VERIFIED             
  cursor       UNIT_VERIFIED              UNIT_VERIFIED              UNIT_VERIFIED             
  kiro         UNIT_VERIFIED              UNIT_VERIFIED              UNIT_VERIFIED             
  antigravity  UNIT_VERIFIED              UNIT_VERIFIED              UNIT_VERIFIED             
  dsh          NOT_FOUND_IN_CURRENT_AUDIT UNIT_VERIFIED              NOT_FOUND_IN_CURRENT_AUDIT

  blocked reasons:
    codex        no evidence recorded yet (waiting for natural trigger)
    claude       no evidence recorded yet (waiting for natural trigger)
    grok         hook not registered
    zcode        no evidence recorded yet (waiting for natural trigger)
    cursor       no evidence recorded yet (waiting for natural trigger)
    kiro         hook not registered
    antigravity  no evidence recorded yet (waiting for natural trigger)
    dsh          no startup surface

warning (1)
  [FTS_INCONSISTENT] FTS index holds 0 row(s) against 10 canonical event(s)

external (1)
  [EXTERNAL_CODEX_APPSERVER_CONPTY_POPUP] the managed Codex app-server launches git through ConPTY, which can surface Windows Terminal windows; not a sessionFlow bug

non-blocking debt (10)
  [VERIFICATION_GAP_CODEX] codex: declared ZERO_TOUCH_LIVE_VERIFIED, observed UNIT_VERIFIED (effective UNIT_VERIFIED)
  [VERIFICATION_GAP_CLAUDE] claude: declared ZERO_TOUCH_LIVE_VERIFIED, observed UNIT_VERIFIED (effective UNIT_VERIFIED)
  [VERIFICATION_GAP_GROK] grok: declared ZERO_TOUCH_LIVE_VERIFIED, observed UNIT_VERIFIED (effective UNIT_VERIFIED)
  [L1_STRONG_FAIRNESS] the per-session minimum is best-effort; a very small budget can still starve the newest STRONG session
  [PROVIDER_HANDLER_DUPLICATION] _log_dir/_log_event/_read_stdin are duplicated across handlers and the log-trim logic has already drifted
  [PROVENANCE_NORMALISE_DEAD_CODE] provenance.normalise() has no caller; DB NULL stays the single representation of unclassified
  [LIVE_VERIFICATION_BLOCKED_BY_PROVIDER_UI] zcode/cursor/kiro/antigravity need their own UI to fire a hook; they stay UNIT_VERIFIED until that is observed
  [GIT_STALE_REFLOG_MISSING_OBJECT] a local dangling commit's tree is missing and a reflog entry points at it; reachable history is healthy and is deliberately left alone
  [OVERVIEW_RECENT_SESSIONS_N1] overview's recent-sessions query is N+1 on thread membership; not fixed because the wall-clock cost is dominated by sandbox spawn overhead, not the query
  [SCAN_MATERIALISE_ALL_BEFORE_WRITE] scan materialises all parsed events before writing; a streaming write would reduce peak memory, but the current batch is bounded and the cost is startup, not per-event

no blocking issues
```
### voyager doctor --fix --dry-run

```
$ voyager doctor --fix --dry-run
doctor --fix (dry run)
==============================================================
safe repairs: 1 fixable
  [WOULD] FTS_INCONSISTENT:
```
### voyager doctor --fix

```
$ voyager doctor --fix
doctor --fix
==============================================================
safe repairs: 1 fixable
  [OK] FTS_INCONSISTENT: rebuilt FTS index from canonical events (10 rows)
```
### search works again

```
$ voyager search "idempotency" --json
[
  {
    "id": "codex:mig-01",
    "provider": "codex",
    "native_id": "mig-01",
    "title": "Add payments.idempotency_key column + index",
    "started_at": 1791162000.0,
    "updated_at": 1791162600.0,
    "cwd": "E:/proj/payments",
    "repo_root": "E:/proj/payments",
    "git_remote": null,
    "git_branch": null,
    "git_commit": null,
    "model": null,
    "message_count": 5,
    "tool_count": 1,
    "can_resume": 1,
    "can_fork": 0,
    "resume_cmd": "codex resume mig-01",
    "metadata_json": "{}",
    "raw_metadata_json": "{}",
    "source_state": "ACTIVE_SOURCE",
    "source_missing_since": null,
    "_kind": "user",
    "_origin": null,
    "_ts": 1791162000.0,
    "_tool": null,
    "_file": null,
    "_sid": "codex:mig-01",
    "snippet": "We need an >>>ide<<<…"
  },
  {
    "id": "dsh:dbg-01",
    "provider": "dsh",
    "native_id": "dbg-01",
    "title": "Debug duplicate charge on retry",
    "started_at": 1791201600.0,
    "updated_at": 1791202200.0,
    "cwd": "E:/proj/payments",
    "repo_root": "E:/proj/payments",
    "git_remote": null,
    "git_branch": null,
    "git_commit": null,
    "model": null,
    "message_count": 3,
    "tool_count": 0,
    "can_resume": 1,
    "can_fork": 0,
    "resume_cmd": "dsh resume dbg-01",
    "metadata_json": "{}",
    "raw_metadata_json": "{}",
    "source_state": "ACTIVE_SOURCE",
    "source_missing_since": null,
    "_kind": "reasoning",
    "_origin": null,
    "_ts": 1791201900.0,
    "_tool": null,
    "_file": null,
    "_sid": "dsh:dbg-01",
    "snippet": "… >>>idempotency<<<_k…"
  },
  {
    "id": "codex:mig-01",
    "provider": "codex",
    "native_id": "mig-01",
    "title": "Add payments.idempotency_key column + index",
    "started_at": 1791162000.0,
    "updated_at": 1791162600.0,
    "cwd": "E:/proj/payments",
    "repo_root": "E:/proj/payments",
    "git_remote": null,
    "git_branch": null,
    "git_commit": null,
    "model": null,
    "message_count": 5,
    "tool_count": 1,
    "can_resume": 1,
    "can_fork": 0,
    "resume_cmd": "codex resume mig-01",
    "metadata_json": "{}",
    "raw_metadata_json": "{}",
    "source_state": "ACTIVE_SOURCE",
    "source_missing_since": null,
    "_kind": "reasoning",
    "_origin": null,
    "_ts": 1791162300.0,
    "_tool": null,
    "_file": null,
    "_sid": "codex:mig-01",
    "snippet": "… >>>idempotency<<<_k…"
  }
]
```
