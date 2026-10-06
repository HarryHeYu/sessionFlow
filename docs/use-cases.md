# Use cases

Three everyday situations sessionFlow is built for. The transcripts and
sessions referenced here are **fictional examples** — every command works
exactly like this against your own agents' history (or against the
synthetic demo: `voyager demo`).

---

## Case 1 — Lost context

**The problem.** Yesterday Codex designed the authentication flow. Today
Claude Code takes over and needs those decisions — but Claude Code cannot
read Codex's rollout files, and scrolling yesterday's transcript by hand
is slow.

**With sessionFlow:**

```sh
$ voyager search "JWT refresh token"
[codex] demo-auth-01  2026-10-05 09:30
    Decision: use JWT refresh tokens. auth.py issues access tokens ...
[zcode] demo-auth-03  2026-10-05 17:30
    Fixed: token.py now allows 30s leeway ...
```

The decision, the file it landed in and the follow-up fix — across two
agents, one query. `voyager show codex:demo-auth-01` compiles the whole
session into a readable continuation context.

**Outcome:** Claude starts with the design rationale, not from zero.

---

## Case 2 — Agent switching

**The problem.** You start work in Codex, continue in Claude Code, hand
the hardening to DSH in the evening. Each agent keeps its own history in
its own format; none of them knows what the others did. "Which agent is
even the latest holder of this thread?" is a manual archaeology question.

**With sessionFlow:**

```sh
$ voyager repo E:/demo-project
$ voyager thread timeline <thread>
2026-10-05 09:00  THREAD_CREATED     WorkThread created — authentication flow
2026-10-05 09:05  SESSION_ATTACHED  [codex] codex attached
2026-10-05 14:00  SESSION_ATTACHED  [claude] claude attached
2026-10-05 17:00  SESSION_ATTACHED  [zcode] zcode attached
2026-10-05 20:00  SESSION_ATTACHED  [dsh] dsh attached
```

One timeline, four agents, exact order. `voyager switch` / `voyager
continue` compile the context and hand it to the next agent — including a
lease so two agents never write the thread blind.

**Outcome:** switching agents costs one command, not a re-briefing.

---

## Case 3 — Engineering memory

**The problem.** Six months later someone asks *"why is `token.py`
rotating refresh tokens instead of just expiring them?"* The conversation
that decided this is buried in an agent's storage — or gone, if the
provider rotated its own files.

**With sessionFlow:**

```sh
$ voyager search "refresh token rotation"
$ voyager export <id> --format md   # the full reasoning, human-readable
```

And when a provider *does* rotate its storage, the normalized history is
**retained, not deleted**: the session stays searchable and on the
timeline, marked `SOURCE_MISSING` (retained) — history remains, only
native resume becomes unavailable. Explicitly archived sessions
(`ARCHIVED_CANONICAL`) stay searchable forever, too.

**Outcome:** the "why" survives agent churn, provider rotation and time.

---

## Try these yourself

```sh
voyager demo                                   # synthetic index, no agents needed
voyager search --db ~/.voyager/demo.db "authentication"
```

Every example on this page is generated from that synthetic demo — see
[architecture.md](architecture.md) for how it all fits together, or
[../README.md](../README.md#quick-start) for the 2-minute setup.
