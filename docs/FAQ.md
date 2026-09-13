# FAQ

## Privacy & safety

**Does voyager upload my sessions anywhere?**

No. Voyager only reads files that already exist on your machine and writes
one local SQLite index at `~/.voyager/index.db`. There is no network code
in the project at all. `voyager handoff` writes a Markdown file locally —
you choose what to do with it.

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
