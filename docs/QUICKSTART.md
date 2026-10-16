# Quick Start

**Five minutes, from nothing to continuing another agent's work.** Every command
below is exercised against v1.1.0; nothing here is aspirational.

- **sessionFlow** (this repo, CLI command `voyager`) — indexes and searches your
  local agent history.
- **dsh-sessionflow** — a DeepSeek Harness plugin that exposes that history as six
  tools. Optional; install it only if you use DSH.

---

## 1. Install the core (1 min)

```sh
pipx install "voyager[all] @ git+https://github.com/HarryHeYu/sessionFlow.git"
```

`[all]` pulls in the DSH (`zstandard`) and MCP (`mcp`) extras. Drop it for a bare
install. No `pipx`? Use pip:

```sh
pip install "voyager[all] @ git+https://github.com/HarryHeYu/sessionFlow.git"
```

**Not on PyPI.** The `voyager` name there is an unrelated library, so a bare
`pip install voyager` installs the wrong project — always keep the `@ git+…`.

If `voyager` is not on your `PATH`, every command below also works as
`python -m voyager.cli <command>`.

Requirements: **Python ≥ 3.10**. Windows, macOS and Linux.

---

## 2. Index your history (30 s)

```sh
voyager scan
```

This walks each supported agent's local storage and builds a local SQLite index at
`~/.voyager/index.db`. It is incremental — only changed files are re-read. Sample
output:

```
  codex: 1 sessions indexed (1 new/refreshed, 0 unchanged)
  claude: 1 sessions indexed (1 new/refreshed, 0 unchanged)
  zcode: no sources found
  ...
scan complete: 2 sessions, 15 events (new/refreshed: 2, unchanged: 0)
```

Nothing is uploaded; provider files are opened read-only.

**No agent history yet?** Try the synthetic demo instead — it seeds its own index
and never touches yours:

```sh
voyager demo
voyager search --db ~/.voyager/demo.db "authentication"
```

---

## 3. See what you have (30 s)

```sh
voyager list                    # every session, newest first
voyager list --repo myproj      # only sessions for one project
voyager repo E:/code/myproj     # cross-agent timeline for a repository
```

`voyager repo` is the one that shows the point of the tool — every agent that
touched a project, in order:

```
E:/code/myproj  (2 sessions)
  2026-10-12 17:01  claude  "Add a --strict flag …"  2 msgs / 3 tools  branch:main  commit:70e7d3d7fb
  2026-10-11 23:01  codex   "Set up config loading …"  2 msgs / 1 tools  branch:main
```

---

## 4. Search your history (30 s)

```sh
voyager search "strict"                  # full-text, CJK substring included
voyager search "pytest" --since 7d
voyager search "config" --provider claude --json
voyager show <id-or-prefix>              # one session, in full
```

The query is always treated as literal text, so `pytest -q`, `a:b` and
`"unbalanced` do not need escaping.

---

## 5. Continue the work (1 min)

```sh
voyager continue                         # newest session in this repo, resume it
voyager continue --repo myproj
voyager handoff <id> --to codex          # a Context Package for another agent
voyager merge <id-a> <id-b>              # group sessions into a WorkThread
```

`voyager continue` picks the newest session and does the right thing. `handoff`
writes a **Context Package** — goal, files touched, commands, errors, and where
the work stopped — that the target agent reads and continues from:

```markdown
## User goal / instructions
Add a --strict flag to app.py that fails on unknown config keys, then add a test for it.

## Where the work stopped (last assistant message)
I added --strict to load_config() and a test, but test_strict_rejects_unknown_keys is
still failing: strict mode does not raise on unknown keys yet. TODO: raise on unknown keys.
```

Add `--launch` to start the target agent immediately, `--goal "…"` to rank the
evidence, and `--budget compact|balanced|full` to cap the bundle size.

Bundles land in `~/.voyager/bundles/` unless you name the file with `--output`
(which wins) or redirect the directory with `--bundles-dir` — useful for keeping
a scripted or CI run out of your real home:

```sh
voyager merge <id-a> <id-b> --bundles-dir ./tmp-bundles   # this run only
VOYAGER_BUNDLES_DIR=./tmp-bundles voyager continue        # or for a whole shell
```

> **Continuity is not session migration.** The target agent gets the goal, the
> recent real work and the repository state — not the previous session's hidden
> tool state or cached reasoning.

---

## 6. Continue inside DeepSeek Harness (1 min, optional)

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

## 7. When a repo has more than one WorkThread

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

## 8. Health and troubleshooting

```sh
voyager doctor            # is this installation healthy, and why not
voyager verify            # declared / observed / effective per provider
voyager status            # current thread, or AMBIGUOUS
```

| Symptom | Fix |
|---|---|
| `voyager: command not found` | Run `python -m voyager.cli …`, or add the pipx bin dir to `PATH`. |
| `pip install voyager` installed the wrong thing | It did — that PyPI name is unrelated. Uninstall it; install with `@ git+…`. |
| `dsh plugin add dsh-sessionflow` finds nothing | The plugin is not on npm. Use `github:HarryHeYu/dsh-sessionflow`. |
| `ERR_PNPM_GIT_DEP_PREPARE_NOT_ALLOWED` | Add the exact `allowBuilds` key pnpm printed (see §6). |
| `declares no dsh.bundle` with a local path | A bare path becomes a `link:` dependency, which DSH does not materialise. Use `file:$PWD`. |
| DSH stops with `MISSING_CREDENTIAL` | Configure a model provider in the profile — that is the host's setup, not the plugin's. |
| Two active threads, commands refuse | Pass `--thread <id>`; that is intentional. |

---

## 9. What is *not* verified

- **Native resume has not been observed working end to end on any provider.**
  The commands exist and the adapters build them; treat every `resume` as
  `NOT VERIFIED` until the verification table in the README says otherwise.
- Cross-agent continuation has been verified live with a real DSH agent against
  both a synthetic index and real provider session files — see the plugin's
  `docs/p8-live-verification.md` and `docs/p9-live-verification.md`.
- Provider capability varies: file-edit tracking and token accounting are
  stronger on some agents than others. See the platform table in the README.

Version relationship: the core and the plugin are released together, both at
**v1.1.0**, and they speak **bridge schema 1**. The plugin declares the schema it
needs and fails clearly against an older core rather than guessing.
