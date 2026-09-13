# Voyager 🧭

**One index across every AI coding agent on your machine.**

[中文说明](README.zh-CN.md)

Voyager reads the local session data your agents already write — Codex,
Claude Code, ZCode, DSH (DeepSeek Harness), and more — and turns it into a
single searchable, exportable, resumable index. Pure local, no accounts,
no cloud, no telemetry.

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
pip install -e .            # core (Codex / Claude / ZCode adapters)
pip install -e ".[dsh]"     # + DSH (needs zstandard)
```

Python ≥ 3.10. Windows / macOS / Linux. If `voyager` is not on your PATH,
run it as `python -m voyager.cli`.

## Usage

```sh
voyager scan                # discover + index every supported agent
voyager list                # all sessions, newest first
voyager list --repo myproj  # sessions for one repo
voyager show <id>           # full message / tool-call timeline
voyager search "tensorboard"
voyager repo E:/code/myproj # cross-agent timeline for a repository
voyager export <id> --format md   # or --format json (includes raw events)
voyager resume <id>         # launches the native agent on that session
voyager files <id>          # files the session touched
voyager diff <id>           # Claude sessions: rebuilt before/after diffs
voyager stats               # index statistics
```

Session ids are matched by prefix; if a prefix is ambiguous Voyager lists
the candidates and exits.

## Supported platforms

| Platform | Source | Messages | Tool calls | Shell exit | File diffs | Tokens | Resume |
|---|---|---|---|---|---|---|---|
| Codex (CLI/VSCode/Desktop) | rollout JSONL | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ `codex resume` |
| Claude Code | project JSONL + file-history | ✅ | ✅ | ✅ | ✅ version chain | ✅ | ✅ `claude --resume` |
| ZCode | SQLite (`~/.zcode/cli/db`) | ✅ | ✅ | ✅ | ⚠️ file events (edits stay in raw) | ✅ usage tables | ❌ desktop only |
| DSH | zstd JSONL (`~/.dsh/sessions`) | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ `dsh --resume` |

Adapters for Grok, Cursor, Antigravity and Kiro are designed in
[docs/RECON.md](docs/RECON.md) (a full survey of where each tool keeps its
local data, and what it records) and are straightforward to add.

## Design

Provider files are read-only. Adapters translate each platform's events into
one normalized model (`Session` / `Event`) while keeping the raw provider
event alongside — nothing is lost, giant blobs are truncated with a pointer
back to the source. Everything lands in a local SQLite index with FTS5
(trigram, so CJK substring search works). Scans are idempotent: sources are
tracked by `(mtime, size)` and re-parsed only when they change; sessions
whose source files vanish are pruned.

Details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/DECISIONS.md](docs/DECISIONS.md).

## Ideas welcome

Natural next steps: an interactive TUI, more platform adapters, cross-agent
handoff (`voyager handoff <id> --to codex` → generated Context Package), and
a recording layer that captures agent actions as they happen so any session
can be replayed or re-run from a chosen step.

## License

MIT — see [LICENSE](LICENSE).
