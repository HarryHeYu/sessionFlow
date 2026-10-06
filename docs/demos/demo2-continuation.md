# Demo 2 — cross-agent continuation

One WorkThread spans three agents. The timeline is the lifecycle:
who attached, when, in what order — and `voyager continue` hands the
whole story to the next agent.

### voyager thread timeline <thread>

```
$ voyager thread timeline thr_49fe96ee1b
WorkThread thr_49fe96ee1b  [active]  payments idempotency
repo: E:/proj/payments

2026-10-06 11:27  THREAD_CREATED     WorkThread created — payments idempotency
2026-10-06 11:27  SESSION_ATTACHED   [codex] codex attached
      session: codex:mig-01
2026-10-06 11:27  SESSION_ATTACHED   [claude] claude attached
      session: claude:rev-01
2026-10-06 11:27  SESSION_ATTACHED   [dsh] dsh attached
      session: dsh:dbg-01
```
