# Quick Start

**Ten minutes, from nothing to continuing another agent's work — and nothing
reads your own history until step 4.** Every command below is exercised against
v1.3.0; the sample output is from that build.

- **sessionFlow** (this repo, CLI command `voyager`) — indexes and searches your
  local agent history.
- **dsh-sessionflow** — a DeepSeek Harness plugin that exposes that history as six
  tools. Optional; install it only if you use DSH.

The order is deliberate. Steps 2–3 run against a **synthetic** index that cannot
see your agents. Step 4 is the first one that opens your own session files.

---

## 1. Install the core (1 min)

```sh
pipx install "voyager[all] @ git+https://github.com/HarryHeYu/sessionFlow.git@v1.3.0"
```

`[all]` pulls in the DSH (`zstandard`) and MCP (`mcp`) extras. Drop it for a bare
install. The `@v1.3.0` pins a release tag so a reinstall reproduces the same
build; drop it to follow `main`. No `pipx`? Use pip — `python -m pip` on
Windows, where `pip` is often not on `PATH`:

```sh
python -m pip install "voyager[all] @ git+https://github.com/HarryHeYu/sessionFlow.git@v1.3.0"
```

**Not on PyPI.** The `voyager` name there is an unrelated library, so a bare
`pip install voyager` installs the wrong project — always keep the `@ git+…`.

If `voyager` is not on your `PATH`, every command below also works as
`python -m voyager.cli <command>`.

Requirements: **Python ≥ 3.10**. Windows, macOS and Linux.

---

## 2. Look at a synthetic index first (2 min)

No agents, no real data, nothing to configure:

```sh
voyager demo
```

It seeds `~/.voyager/demo.db` — four agents working one "authentication flow"
thread across a single day. That file is **separate** from your real index
(`~/.voyager/index.db`), and deleting it costs you nothing.

```sh
voyager --db ~/.voyager/demo.db list
voyager --db ~/.voyager/demo.db search "authentication"
voyager --db ~/.voyager/demo.db show codex:demo-auth-01
```

`--db` is a global option: put it **before** the subcommand. (Some subcommands
also accept it afterwards; `dashboard` and `evidence` do not.)

Two things in that output are expected, not errors:

- every session shows `[source missing]` — the demo's events live only in the
  index, because there are no synthetic transcript files on disk. The same label
  appears for real retained history after an agent rotates its files: still
  searchable, not resumable.
- `show` prints a `#seq/digest` pointer on every event line. That is the
  reference an Evidence Verify checks — see step 7.

---

## 3. Dashboard (30 s)

```sh
voyager --db ~/.voyager/demo.db dashboard --out ~/.voyager/demo.html
```

One self-contained HTML file: projects, WorkThreads, recent activity, checkpoints
and provider health. No server, no CDN, no network requests. Its filter is
client-side over **what it rendered** — whole-index search is `voyager search`,
not the page.

---

## 4. Scan your own history — only when you decide to (1 min)

**Read this first.** `voyager scan` opens the session files your agents already
wrote to disk — `~/.codex`, `~/.claude`, and the rest — and builds a local index
at `~/.voyager/index.db`. Nothing is uploaded and the provider files are opened
read-only, but that history can contain source code, prompts, command output and
file contents. The dashboard and every continuation package are generated **from
that index**, so they can contain the same material: treat `dashboard.html`,
`handoff-*.md` and anything under `~/.voyager/bundles/` as private, and do not
paste them into issues or chats.

Start with one agent rather than all of them:

```sh
voyager scan --platform codex     # codex | claude | zcode | dsh, comma-separated
```

When you are ready for everything:

```sh
voyager scan
```

It is incremental — only changed files are re-read. Sample output:

```
  codex: 1 sessions indexed (1 new/refreshed, 0 unchanged)
  claude: 1 sessions indexed (1 new/refreshed, 0 unchanged)
  zcode: no sources found
  ...
scan complete: 2 sessions, 15 events (new/refreshed: 2, unchanged: 0)
```

---

## 5. Browse and search (30 s)

```sh
voyager list                    # every session, newest first
voyager list --repo myproj      # only sessions for one project
voyager repo E:/code/myproj     # cross-agent timeline for a repository
voyager search "strict"         # full-text, CJK substring included
voyager search "pytest" --since 7d
voyager show <id-or-prefix>     # one session, in full
```

`voyager repo` is the one that shows the point of the tool — every agent that
touched a project, in order:

```
E:/code/myproj  (2 sessions)
  2026-10-12 17:01  claude  "Add a --strict flag …"  2 msgs / 3 tools  branch:main  commit:70e7d3d7fb
  2026-10-11 23:01  codex   "Set up config loading …"  2 msgs / 1 tools  branch:main
```

The query is always treated as literal text, so `pytest -q`, `a:b` and
`"unbalanced` do not need escaping.

---

## 6. Pick the task, preview it, then continue (2 min)

Continuation is a **write**, not a read: it refreshes the index, takes a lease and
produces a file. So pick the task first, with commands that only read:

```sh
voyager thread list                  # active WorkThreads
voyager thread show <thread>         # members, lease, pending attaches
voyager thread summarize <thread>    # one brief across every agent
```

`thread summarize` is the preview: the title, the agents that worked there, what
each of them last said, and the open items — all read-only.

On the **demo** index those three work, but `continue --thread` does not: the
demo's sessions have no transcript files on disk, so they count as retained
history (`SOURCE_MISSING`) and continuity only runs on live members. It says so
rather than guessing:

```
thread thr_… has no live member sessions (4 retained: every source vanished)
```

That is the retention rule, not a bug — a session whose provider file vanished is
still searchable, but not resumable. To watch a package get built on synthetic
data, continue a session instead:

```sh
voyager --db ~/.voyager/demo.db continue codex:demo-auth-01 --no-launch -o ./handoff-demo.md
```

On your own scan, use the thread form — the task, not the newest session, is what
you want to pick up.

When the task you want is the one you are looking at:

```sh
voyager continue --thread <thread> --no-launch -o ./handoff-<thread>.md
```

What `continue` does, in this order, in v1.3.0:

1. **refreshes the index first** — an incremental scan of your provider
   directories. `VOYAGER_NO_SYNC=1` in the environment skips it, which is what you
   want when you are inspecting an old index offline.
2. **takes the single-writer lease** on the WorkThread while it works. A second
   agent is refused by name until it is released — `voyager thread unlock <id>
   --steal` is the explicit override.
3. **writes the package.** `handoff` and the handoff path of `continue` write
   `handoff-<provider>-<session>.md` into the **current directory**; `merge` and
   forced bundles write under `~/.voyager/bundles/`. `-o` always wins, and
   `--bundles-dir` redirects the directory for one run.
4. prints the launch command. `--launch` runs it instead.

Cross-agent handoff is the same engine with a named target:

```sh
voyager handoff <id> --to codex          # a Context Package for another agent
voyager merge <id-a> <id-b>              # group sessions into a WorkThread
```

The package is a **Context Package** — goal, files touched, commands, errors, and
where the work stopped — that the target agent reads and continues from:

```markdown
## User goal / instructions
Add a --strict flag to app.py that fails on unknown config keys, then add a test for it.

## Where the work stopped (last assistant message)
I added --strict to load_config() and a test, but test_strict_rejects_unknown_keys is
still failing: strict mode does not raise on unknown keys yet. TODO: raise on unknown keys.
```

Add `--goal "…"` to rank the evidence and `--budget compact|balanced|full` to cap
the package size.

> **Continuity is not session migration.** The target agent gets the goal, the
> recent real work and the repository state — not the previous session's hidden
> tool state or cached reasoning.

---

## 7. Check a claim before you trust it

Every claim and tool observation in a package carries a reference to the event it
came from, and `voyager show` prints the same pointer on every line:

```
- `pytest -q`  # exit 1 · seq 2 · #cbd4d070
All 42 tests pass. · seq 3 · #c5009a39
```

Ask whether that reference still lands on the same content:

```sh
voyager evidence verify <session> <seq> <digest>
```

Five outcomes, each with its own exit code:

| State | Exit | What it means |
|---|---|---|
| `MATCH` | 0 | the event at that position is the one the reference was made from |
| `MISMATCH` | 1 | something else is there now — the transcript changed under the reference |
| `NOT_FOUND` | 2 | no event at that position (or no such session) |
| `UNVERIFIABLE` | 3 | the reference carried no digest, so only the position could be checked |
| `INVALID_REFERENCE` | 4 | the reference is malformed — e.g. a digest that is not 8 hex characters |

You can see all five on the synthetic index, without touching real data. The demo
is deterministic, so these digests are the ones you will get:

```sh
voyager --db ~/.voyager/demo.db evidence verify codex:demo-auth-01 1 2198ad71   # MATCH, exit 0
voyager --db ~/.voyager/demo.db evidence verify codex:demo-auth-01 1 deadbeef   # MISMATCH, exit 1
voyager --db ~/.voyager/demo.db evidence verify codex:demo-auth-01 999 2198ad71 # NOT_FOUND, exit 2
voyager --db ~/.voyager/demo.db evidence verify codex:demo-auth-01 1            # UNVERIFIABLE, exit 3
voyager --db ~/.voyager/demo.db evidence verify codex:demo-auth-01 1 zzzz       # INVALID_REFERENCE, exit 4
```

Add `--json` for the same answer as an object (`status`, `exit_code`, `session_id`,
`seq`, `digest`, `found`, …). `voyager evidence verify --help` does **not** list
these five states or their codes — this table and the command's own output do.

**This is not fact-checking.** `MATCH` says the indexed event is the one the
reference was made from; it does not say the claim is true. The fingerprint is
eight hex characters, not a signature, and the source check compares `mtime` and
`size` rather than bytes. Both limits are printed by the command itself.

---

## 8. Continue inside DeepSeek Harness (1 min, optional)

```sh
dsh plugin --profile web add github:HarryHeYu/dsh-sessionflow
```

pnpm blocks build scripts for git-hosted packages until you allow them. The first
`add` stops and prints the exact key it wants — put **that** key (it includes the
resolved commit) in the profile's `pnpm-workspace.yaml` and run the same `add`
again:

```yaml
allowBuilds:
  dsh-sessionflow@git+https://github.com/HarryHeYu/dsh-sessionflow.git#<sha>: true
```

Then verify and start:

```sh
dsh --profile web --dump-config | grep dsh-sessionflow   # expect: - id: dsh-sessionflow
dsh --profile web
```

Ask in plain language — no tool name needed:

> Continue the work I was doing in Claude Code on this repo.

The agent calls `sessionflow_current_work` / `sessionflow_continue` and picks up
the goal, files and open items. The six tools:

| Tool | Answers |
|---|---|
| `sessionflow_search` | "Find the session where I fixed the parser." |
| `sessionflow_recent` | "What have I been doing this week?" |
| `sessionflow_session` | "Show me what that one session was about." |
| `sessionflow_current_work` | "Where did I leave off on *this* project?" |
| `sessionflow_continue` | "Pick the work back up." |
| `sessionflow_merge` | "Put these sessions together and let me continue." |

**Phrase it as a lookup, not just an action.** Measured, the model recognises the
need to search history far more reliably when you say what you are looking *for*
than when you only say "continue":

> What was I working on before? Continue from there.
> Continue the unfinished Codex work in this project.
> Find the previous Claude Code work on this repository.

A bare *"Continue the unfinished work in this project"* is the weakest of these: with
nothing in the repository marking what is left, the agent may simply read the files and
start writing. It will not invent history, but it can miss work that only the earlier
sessions record — which is exactly the case this plugin exists for.

Numbers from one controlled run (one model, one fixture, three attempts each) are in
[`dsh-sessionflow`'s P13 notes](https://github.com/HarryHeYu/dsh-sessionflow/blob/main/docs/p13-agent-discovery.md);
treat them as that environment's result, not a published success rate.

DSH needs a working model provider; **this plugin does not supply model access.**

---

## 9. When a repo has more than one WorkThread

Nothing picks one for you. `voyager continue` and `voyager switch` refuse, list
every candidate, and exit non-zero; `voyager status` reports `AMBIGUOUS`:

```
error: 2 active WorkThreads for this repo — refusing to guess which one you meant.
  thr_d07d8c7c68  Add a --json output mode
  thr_12f1165131  merge: claude:cccccccc-1 + codex:aaaaaaaa-1
Pick one explicitly: --thread <id>
```

Name the one you mean:

```sh
voyager continue --thread thr_12f1165131
voyager thread close <id>            # if one line of work is finished
```

The DSH tools behave the same way: `sessionflow_current_work` returns
`thread_ambiguous: true` with `thread_candidates`, and labels the one it returns
a *default* rather than a decision.

---

## 10. Health and troubleshooting

```sh
voyager doctor            # is this installation healthy, and why not
voyager verify            # declared / observed / effective per provider
voyager status            # current thread, or AMBIGUOUS
```

`doctor` exits 1 before your first `scan`, saying `index database not found; run
voyager scan`. That is the command working: it reports a missing index instead of
calling the install healthy. Run `scan` (or `demo`) first.

| Symptom | Fix |
|---|---|
| `voyager: command not found` | Run `python -m voyager.cli …`, or add the pipx bin dir to `PATH`. |
| `pip install voyager` installed the wrong thing | It did — that PyPI name is unrelated. Uninstall it; install with `@ git+…`. |
| `dsh plugin add dsh-sessionflow` finds nothing | The plugin is not on npm. Use `github:HarryHeYu/dsh-sessionflow`. |
| `ERR_PNPM_GIT_DEP_PREPARE_NOT_ALLOWED` | Add the exact `allowBuilds` key pnpm printed (see §8). |
| `declares no dsh.bundle` with a local path | A bare path becomes a `link:` dependency, which DSH does not materialise. Use `file:$PWD`. |
| DSH stops with `MISSING_CREDENTIAL` | Configure a model provider in the profile — that is the host's setup, not the plugin's. |
| Two active threads, commands refuse | Pass `--thread <id>`; that is intentional. |
| `refused: thread … is leased to …` | A previous `continue` still holds the lease. `voyager thread unlock <id> --steal`. |
| `scan --platform <name>` reports `0 sessions` | The value is not validated — a typo looks exactly like "you have no history". Check the spelling: `codex`, `claude`, `zcode`, `dsh`. |

---

## 11. What is *not* verified

- **Native resume has not been observed working end to end on any provider.**
  The commands exist and the adapters build them; treat every `resume` as
  `NOT VERIFIED` until the verification table in the README says otherwise.
- Cross-agent continuation has been verified live with a real DSH agent against
  both a synthetic index and real provider session files — see the plugin's
  `docs/p8-live-verification.md` and `docs/p9-live-verification.md`.
- **Evidence is about references, not truth.** `MATCH` means the indexed event is
  the one a claim was recorded from; nothing here decides whether the claim is
  correct, and the fingerprint is not a signature. See §7.
- Provider capability varies: file-edit tracking and token accounting are
  stronger on some agents than others. See the platform table in the README.

Version relationship: the core and the plugin are **versioned independently** —
this page installs core **v1.3.0**, and the current plugin is **v1.1.0**. They
are not required to share a number; what binds them is **bridge schema 1**, which
the plugin declares and the core reports (`voyager integration-info --json`). A
newer core that kept the schema would need no plugin release, and the plugin
refuses an older core by name rather than misbehaving.
