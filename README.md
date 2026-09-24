# Voyager 🧭

[![tests](https://github.com/HarryHeYu/sessionFlow/actions/workflows/test.yml/badge.svg)](https://github.com/HarryHeYu/sessionFlow/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

**One index across every AI coding agent on your machine.**

Cross-agent continuity is built in: merge context across sessions and continue in any agent (`voyager merge` / `switch` / `continue`) — see [docs/ROADMAP.md](docs/ROADMAP.md) for the shipped scope and [docs/POST-1.0.md](docs/POST-1.0.md) for what's next.

[中文说明](README.zh-CN.md)

Voyager reads the local session data your agents already write — Codex,
Claude Code, ZCode, DSH (DeepSeek Harness), and more — and turns it into a
single searchable, exportable, resumable index. Pure local, no accounts,
no cloud, no telemetry.

![Voyager architecture: 8 agents, 8 storage formats, one index](docs/screenshots/architecture.png)

Eight agents keep eight different formats; Voyager normalizes them into one
SQLite index you can search, resume from, hand off to another agent, or query
straight from inside an agent over MCP.

![Voyager in action](docs/screenshots/usage.png)

```
$ voyager list
ID                                    PROV   UPDATED            MSG TOOL  TITLE
a1b2c3d4-...                          zcode  2026-09-13 01:13   162  206  重构存储引擎的写入路径
9f8e7d6c-...                          codex  2026-07-06 22:52    76  168  修复多线程下载器的竞态条件
5e4d3c2b-...                          claude 2026-07-17 12:46    89   70  分析数据集结构并设计评测脚本
```

## Why

Every agent keeps its own history in its own format: Codex writes rollout
JSONL, Claude Code writes project JSONL plus a file-version chain, ZCode
uses SQLite, DSH compresses JSONL with zstd. You work across all of them —
Voyager makes that history *one* thing you can query.

- **Cross-agent timeline per repo** — what did all your agents do to this
  project, and when?
- **Full-text search everywhere** — find the session where someone ran
  that one command or touched that one file (CJK substring search works).
- **Real exports** — human-readable Markdown, or lossless JSON with both
  the normalized and the raw events.
- **Resume where you left off** — Voyager knows each platform's resume
  command and runs it for you.

## Install

```sh
# isolated CLI install — no virtualenv juggling (recommended)
pipx install "voyager[all] @ git+https://github.com/HarryHeYu/sessionFlow.git"

# or with pip (user-level)
pip install "voyager[all] @ git+https://github.com/HarryHeYu/sessionFlow.git"

# once the release is on PyPI (tracked in CHANGELOG.md)
pipx install voyager
```

`[all]` = DSH support (`zstandard`) + MCP server (`mcp`). The two are
optional and only needed for those features:

```sh
pip install "voyager @ git+https://github.com/HarryHeYu/sessionFlow.git"          # core
pip install "voyager[dsh] @ git+https://github.com/HarryHeYu/sessionFlow.git"     # + DSH
pip install "voyager[mcp] @ git+https://github.com/HarryHeYu/sessionFlow.git"     # + MCP server
```

Working on Voyager itself:

```sh
git clone https://github.com/HarryHeYu/sessionFlow && cd sessionFlow
pip install -e ".[all,dev]"    # editable + extras + pytest
python -m pytest tests/ -q     # full dev env: 374 collected, 372 passed, 2 skipped
```

Python ≥ 3.10. Windows / macOS / Linux. If `voyager` is not on your PATH,
run it as `python -m voyager.cli`.

## Usage

First run: `voyager scan` walks every supported agent's local storage and
builds the index at `~/.voyager/index.db`. After that, re-run `scan`
whenever you want to pick up new sessions — it is incremental and only
re-reads what changed. To make syncing fully automatic, keep a watcher
running (or put it in a scheduled task):

```sh
voyager watch --interval 300    # re-scan every 5 minutes, forever
```

Put the command in your OS autostart (or the provided
`~/.voyager/watch.vbs` in the Windows Startup folder) and the index stays
current with zero manual steps.

```sh
voyager scan                # discover + index every supported agent
voyager list                # all sessions, newest first
voyager list --repo myproj  # sessions for one repo
voyager show <id>           # full message / tool-call timeline
voyager search "tensorboard"
voyager repo E:/code/myproj # cross-agent timeline for a repository
voyager export <id> --format md   # or --format json (includes raw events)
voyager resume <id>         # launches the native agent on that session
voyager handoff <id> --to codex   # context package for another agent
voyager continue            # one command to pick your latest work back up
voyager thread list         # WorkThreads: task-centric session groups
voyager switch codex        # switch the active thread to another agent
voyager skill install       # teach other agents about voyager
voyager brief               # 48h digest of what every agent is doing
voyager files <id>          # files the session touched
voyager diff <id>           # Claude sessions: rebuilt before/after diffs
voyager stats               # index statistics
```

**Cross-agent handoff** is **work continuation**, not session migration
(the other agent cannot inherit hidden tool state or cached reasoning).
`voyager handoff <id> --to codex` extracts the session into a Context
Package (goal, instructions, files touched, commands, errors, where the
work stopped) and shows the launch command; add `--launch` to start it.
The target is told to read the package file and continue — works for
`claude`, `codex` and `grok`; other targets get the file to paste.
Same-provider pickup still uses native resume (`codex resume`, …).

**One command to continue**: `voyager continue` picks your newest session
and does the right thing — native resume for codex/claude/dsh/grok,
automatic handoff package for the rest. `voyager continue --repo myproj
--launch` goes straight back into a specific project; multi-session
synthesis (`voyager merge A B C`) groups the work into a **WorkThread**
and cross-agent switch is one command (`voyager switch codex` — lease
aware, see [docs/ROADMAP.md](docs/ROADMAP.md)).

**Goal-conditioned & budgeted**: add `--goal "finish adapter tests"` to
rank the evidence, and `--budget compact|balanced|full|Nk` to cap the
bundle size (estimate printed). Without `--goal`/`--budget` the output is
unchanged.

**Transcript mode (opt-in, experimental)**: `voyager switch codex --mode
transcript` writes a NEW native session containing a flattened
user/assistant transcript (tools/state dropped) so the target resumes
natively. Only codex/grok pass the resume gate today (claude timed out,
dsh unverified) — the default remains the Continuation Bundle. This is
work continuation, not session teleportation: hidden tool state and
provider runtime state never move.

**Everyday flow** — `brief` to see what's moving, `export` to read one
session in full (a 2,915-message DSH session → a 20 MB Markdown file),
`continue` or `handoff` to pick it back up. Full recipes in
[docs/WORKFLOWS.md](docs/WORKFLOWS.md).

Session ids are matched by prefix; if a prefix is ambiguous Voyager lists
the candidates and exits. `resume` runs the native agent's own command
(e.g. `codex resume <id>`); providers without a CLI resume path say so
explicitly instead of pretending.

## Startup Continuity — product status

**Core functionality**: Complete and operational.  
**Runtime auto-trigger**: Claude Code has a native `SessionStart` hook, and Voyager registers one. Whether Claude Code actually fires it is **not yet verified on a live run**.

The `startup_continuity()` function correctly discovers WorkThreads, auto-attaches sessions, and compiles continuation context. What varies per provider is whether a session start can reach that function *without the user doing anything*.

**Provider classification**:

| Provider | Skill | MCP | Status                | What it means                          |
|----------|-------|-----|-----------------------|----------------------------------------|
| Claude   | Y     | R   | `H` — hook registered | Native `SessionStart` hook installed; trigger not yet observed live |
| Codex    | Y     | R   | `A` — startup-assisted | No native hook; needs an explicit call |
| Grok CLI | Y     | N   | `N` — no mechanism    | Best effort                            |
| DSH      | Y     | N   | `N` — no mechanism    | Best effort                            |

Legend: **Y** = installed, **R** = registered, **N** = unsupported, **A** = available/manual setup needed.

Startup status: **`Y`** = zero-touch verified live, **`H`** = native hook registered but the live trigger is **not yet verified**, **`A`** = startup-assisted, **`N`** = no hook. Only `Y` claims the provider fires the hook by itself; nothing currently claims `Y`. Run `voyager integrate status` for the live answer on your machine.

### How Claude Code hooks work here

Claude Code reads `SessionStart` hooks from `~/.claude/settings.json`. Voyager writes the documented shape:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {"type": "command", "command": "\"<abs python>\" \"<abs>/claude_session_start.py\"", "timeout": 120}
        ]
      }
    ]
  }
}
```

`voyager integrate install claude` writes this for you (it is additive — your own hooks are preserved, and the file is backed up first). The installed command uses absolute paths, so it does not depend on the interpreter being on `PATH`.

**Two things are verified, one is not.** The handler is verified end-to-end: it emits a protocol-valid payload, caps the injected context at 9,000 UTF-16 code units, spills the full bundle to `~/.voyager/context/`, and exits 0. The *registration* is verified: `voyager integrate status` reads the file back. What is **not** verified is Claude Code itself invoking the hook — that needs one manual run:

```sh
claude --debug hooks --init-only     # expect: Found 1 hook matchers in settings
```

Until someone sees that line, treat the trigger as unproven. See [claude_continuity_verdict.md](claude_continuity_verdict.md) for the full evidence trail.

### What works right now ✅

- `startup_continuity()` handles discovery, attach, staleness detection
- Auto-attach works when conditions are safe (exact repo match, single thread)
- Context compilation reuses existing ranker+budget+continuation pipeline
- A compiled context bundle is cached in the store and reused across session starts (5-minute TTL, invalidated by git changes or thread membership changes)
- Ambiguity protection: explicit error if multiple active threads
- Auto-registration creates config files for Codex/Claude (`voyager integrate install <provider>`), including the native Claude Code hook

### How to use today 🔧

Recommended workflows:

1. **Native hook (Claude Code)**: `voyager integrate install claude`, then restart Claude Code. No further action — if the hook fires, context is injected before your first turn.
2. **Explicit commands**: `voyager switch <agent>` or `voyager continue [id]`
3. **MCP-assisted**: in-agent tool call `voyager_startup(provider="codex", cwd="$PWD")`
4. **Skill guidance**: read `SKILL.md` in the agent's skill directory

**`A` (startup-assisted) means**: the runtime will not reach Voyager on its own. Codex does not automatically call `voyager_startup` at session start — you must invoke it via one of the workflows above. MCP registration is fully automated; startup invocation is not.

```sh
voyager integrate install claude   # skill + MCP + native SessionStart hook
voyager integrate install codex    # skill + MCP (~/.codex/config.toml)
voyager integrate status           # per-provider truth, including whether the hook is registered
voyager integrate remove claude    # removes only Voyager's entries, leaves your hooks alone
```

See [docs/DOGFOOD.md](docs/DOGFOOD.md) for the detailed verification procedure.

## Integration — teach agents about Voyager

Install the Skill file into known agent directories:

```sh
voyager skill install      # installs SKILL.md at ~/.{agent}/skills/voyager/SKILL.md
```

The Skill instructs agents when to use Voyager commands and when NOT to (never export full sessions).

To register Voyager as an MCP server so agents can query it with native tools:

```sh
voyager integrate install codex    # skill + MCP (~/.codex/config.toml)
voyager integrate install claude   # skill + MCP + native SessionStart hook (~/.claude/settings.json)
voyager integrate status           # per-provider truth, incl. whether the hook is registered
voyager integrate remove <provider>  # undo every step (skill + mcp + bootstrap + hook)
```

These commands now **auto-create** config files if they don't exist - no manual setup needed for first-time installation. Re-running is idempotent, and `install` merges rather than overwrites: your own hooks and settings are preserved, and the file is backed up before it is rewritten.

For verification of actual zero-touch startup behavior, see [docs/DOGFOOD.md](docs/DOGFOOD.md) and [claude_continuity_verdict.md](claude_continuity_verdict.md).

## Supported platforms

| Platform | Source | Messages | Tool calls | Shell exit | File diffs | Tokens | Resume |
|---|---|---|---|---|---|---|---|
| Codex (CLI/VSCode/Desktop) | rollout JSONL | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ `codex resume` |
| Claude Code | project JSONL + file-history | ✅ | ✅ | ✅ | ✅ version chain | ✅ | ✅ `claude --resume` |
| ZCode | SQLite (`~/.zcode/cli/db`) | ✅ | ✅ | ✅ | ⚠️ file events (edits stay in raw) | ✅ usage tables | ❌ desktop only |
| DSH | zstd JSONL (`~/.dsh/sessions`) | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ `dsh --resume` |
| Grok CLI | `chat_history.jsonl` + `summary.json` | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ `grok -r` |
| Cursor | `state.vscdb` (SQLite) | ✅ | ✅ | ❌ | ⚠️ in raw | ⚠️ | ❌ IDE only |
| Kiro IDE | workspace-session JSON | ✅ | ❌ not persisted | ❌ | ❌ | ❌ | ❌ IDE only |
| Antigravity | conversation SQLite (protobuf) | ⚠️ heuristic | ⚠️ heuristic | ⚠️ text | ⚠️ snapshots on disk | ❌ | ❌ IDE only |

Cursor and Antigravity adapters are marked experimental: Cursor reads its
key-value store read-only and Antigravity decodes protobuf blobs
heuristically (no public schema). Full per-field availability matrix and
data-source paths for every tool are in [docs/RECON.md](docs/RECON.md).

## Tests & CI

Adapters are the part of Voyager that breaks when a vendor ships a storage
change, so every platform has a regression test against a **synthetic**
fixture — no real session data, no agent installation needed:

```
tests/
├── fixtures/          # codex/claude/dsh/grok/kiro JSON+JSONL, zcode/cursor/antigravity SQL seeds
├── conftest.py        # builds tmp trees (incl. zstd + SQLite) and repoints adapters at them
├── test_codex.py  test_claude.py  test_zcode.py  test_dsh.py  test_grok.py
├── test_cursor.py  test_kiro.py  test_antigravity.py  test_adapters.py
├── test_store.py  test_export.py  test_handoff.py  test_cli.py  test_mcp.py
├── test_claude_session_start_hook.py   # the SessionStart entrypoint: protocol, size cap, spill, logging
└── test_context_cache.py               # the persisted context cache, incl. hostile input
```

```sh
python -m pytest tests/ -q                # full dev env: 374 collected, 372 passed, 2 skipped — adapters, store, continuity, budget, leases, switch, skill, API, MCP, integration
python scripts/run_tests_core_only.py     # core-only simulated: 374 collected, 356 passed, 18 skipped
```

The two environments skip for different reasons, and the two numbers are not
interchangeable. With the full dev extras the only 2 skips are
**data-dependent**: `tests/test_unicode_preservation.py` reads the default local
index and skips when it holds no Chinese-titled session (`:33`) or no sessions
at all (`:103`). The core-only run adds 16 **dependency-gated** skips — `mcp`,
`zstandard` and `PIL` are absent, so `test_continuity_tools.py`, `test_mcp.py`,
`test_diagram.py` and `test_dsh.py` skip. Neither class is a platform gate.

CI ([.github/workflows/test.yml](.github/workflows/test.yml)) runs the suite
on Python 3.10–3.13 (Linux) and 3.10/3.13 (Windows — the adapters deal with
`%APPDATA%`, drive letters and backslashes), plus a core-only job proving the
CLI works with zero optional dependencies. The store tests cover the
"don't wreck my thousands of sessions" contract: repeated scans never
duplicate, changed sources are re-parsed, vanished sources are pruned.

## Design

Provider files are read-only. Adapters translate each platform's events into
one normalized model (`Session` / `Event`) while keeping the raw provider
event alongside — nothing is lost, giant blobs are truncated with a pointer
back to the source. Everything lands in a local SQLite index with FTS5
(trigram, so CJK substring search works). Scans are idempotent: sources are
tracked by `(mtime, size)` and re-parsed only when they change; sessions
whose source files vanish are pruned.

Details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/DECISIONS.md](docs/DECISIONS.md), [docs/API.md](docs/API.md) and
[docs/FAQ.md](docs/FAQ.md) and everyday recipes in
[docs/WORKFLOWS.md](docs/WORKFLOWS.md).

## What's next

The Continuity Engine core is complete (see
[docs/ROADMAP.md](docs/ROADMAP.md) for the full close-out). The
post-1.0 backlog — VS Code Context Composer UI, auto-clustering
research, Claude/DSH transcript gates, scoped scan, fs-event watcher,
PyPI/packaging polish — lives in [docs/POST-1.0.md](docs/POST-1.0.md).

Phased plan, CLI sketches, and the issue list:
[docs/ROADMAP.md](docs/ROADMAP.md) · [中文](docs/ROADMAP.zh-CN.md).

## License

MIT — see [LICENSE](LICENSE).
