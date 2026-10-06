# Demo 1 — cross-agent search

One query, three agents: the decision, the command and the file
come back from Codex, Claude Code and DSH sessions alike — and
`voyager show` compiles one session into a continuation context.

### voyager search "sqlite migration"

```
$ voyager search "sqlite migration"
[claude] rev-01  2026-10-05 14:10
    review the >>>sql<<<…
[dsh] dbg-01  2026-10-05 20:10
    …qlite migratio…
[codex] mig-01  2026-10-05 09:10
    …QLite migratio…

3 hit(s) across 3 session(s). `voyager show <id>` for detail.
```
### voyager show codex:mig-01

```
$ voyager show codex:mig-01
codex:mig-01
  title   : Add payments.idempotency_key column + index
  time    : 2026-10-05 09:00 -> 2026-10-05 09:10
  cwd     : E:/proj/payments
  repo    : E:/proj/payments  branch=None commit=
  model   : None
  msgs    : 5   tools: 1
  resume  : codex resume mig-01

[2026-10-05 09:00] USER  We need an idempotency key on payments
[2026-10-05 09:05] think SQLite migration: ALTER TABLE payments ADD COLUMN idempotency_key TEXT, then a unique index
[2026-10-05 09:10] TOOL> shell_command python scripts/migrate.py
[2026-10-05 09:15]   out  exit=0 migrated 2 rows
[2026-10-05 09:20] ASST  Migration committed; decided against NOT NULL because backfill must run first
```
