# FAQ

## Privacy & safety

**Does voyager upload my sessions anywhere?**

No. Voyager only reads files that already exist on your machine and writes
one local SQLite index at `~/.voyager/index.db`. There is no network code
in the project at all. `voyager handoff` writes a Markdown file locally —
you choose what to do with it. The planned multi-session compiler
([ROADMAP.md](ROADMAP.md)) stays the same: deterministic extraction from
the index, no LLM in core.

**Who can see my index?**

Anything running as your user. Treat it like your browser history: it is
as private as your machine.

## Platforms

**My agent isn't listed. Will it be supported?**

The adapter interface (`voyager/adapters/base.py`) is small on purpose:
`discover()` finds the session files, `parse()` turns one into normalized
events. If your tool writes JSON/JSONL/SQLite locally, an adapter is
usually 100–200 lines — see `docs/RECON.md` for how the existing ones were
mapped and `CONTRIBUTING.md` for the walk-through.

**Why does my platform show fewer fields (no tool calls / no tokens)?**

Voyager can only index what the platform persists. For example Kiro IDE
does not store tool calls at all, and DSH does not record exit codes — the
platform tables in the README mark exactly what each source contains.

**Cursor / Antigravity adapters are "experimental" — what does that mean?**

Cursor's data lives in an undocumented key-value schema, and Antigravity
stores events as protobuf blobs without a public schema. Both adapters
work against real data observed on disk, but vendor updates may break them
without notice.

## Handoff & continue

**Does `voyager handoff --to codex` move the Claude session into Codex?**

No. Hidden tool state, system prompts and cached reasoning stay in the
original agent. Voyager writes a Context Package and starts a *new*
session that is told to read it and continue the work. Same-provider
pickup still uses native resume (`codex resume`, `claude --resume`, …).
See [ROADMAP.md](ROADMAP.md).

**Can I finish a chat in Claude and keep the same history visible in Codex?**

Not as the same native session. The eight agents store history in eight
formats (JSONL, zstd JSONL, SQLite, protobuf, VS Code KV); tool names
do not map; hidden tool state and cached reasoning stay behind.
Voyager's path is: refresh the index, compile a Continuation Bundle
from the canonical events, start a *new* Codex session that reads it.
Same-provider continue still uses native resume. Writing a synthetic
transcript into another agent's session directory is not the default
and is not promised ([ROADMAP.md](ROADMAP.md) Phase 1b / #10).

**Does Voyager keep histories in sync automatically?**

It syncs the **index**, not the session files. `voyager watch` already
polls on an interval. Continuity commands (`handoff` / `merge` /
`continue` / `switch`) are planned to run an incremental scan
*before* they compile, so a switch right after a Claude turn still
sees that turn. Sync is one-way (provider files → `~/.voyager/index.db`).
Voyager does not mirror a live Claude JSONL into a Codex rollout.

**What if Claude and Codex both keep writing the same chat?**

They must not. A WorkThread is leased to one live agent at a time
(planned, D13 / issue #11). `voyager switch` acquires the lease or
refuses. While Grok holds it, Voyager only *ingests* Grok's file into
the canonical log; it does not rewrite that file, and it will not
open a Codex writer for the same thread. Sync-on-open happens at
switch time, not as a live fan-out to every agent.

## Usage problems

**`voyager` is not recognized as a command**

The console script lands in your Python `Scripts/` directory, which may
not be on `PATH`. Either add it, or run everything as
`python -m voyager.cli`.

**`scan` says "no sources found" for a platform I have installed**

Voyager looks at the default storage locations (e.g. `~/.codex`,
`~/.claude/projects`, `~/.zcode/cli/db`). If you moved your data elsewhere
the adapter won't find it — file an issue with the layout and we'll add
support.

**`ModuleNotFoundError: zstandard` when scanning DSH**

The DSH format is zstd-compressed. Install the optional dependency:

```sh
pip install -e ".[dsh]"
```

**Search doesn't find a word I know is in a session**

The FTS body is capped (600 bytes per event, see `docs/ARCHITECTURE.md`)
to keep the index small, and trigram search needs ≥3-character queries.
The full text is still in `voyager show` / `export`; only *search
recall* is limited to the head of each event.

**Deleting sessions from my agent — when does the index update?**

On the next `scan` (or `voyager watch` tick). Sources that vanish from
disk release their sessions; unchanged files never re-parse.

**Windows: `scan` prints mojibake for non-ASCII titles**

Voyager sets the console output to UTF-8 itself. If your font can't
render CJK, switch the terminal font (e.g. Windows Terminal + a CJK-capable
font). The underlying data and JSON export are always valid UTF-8.
