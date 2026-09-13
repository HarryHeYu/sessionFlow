# Contributing to Voyager

Thanks for your interest! Voyager is intentionally small; the fastest way
to help is adding or improving a **platform adapter**.

## Project layout in 30 seconds

```
voyager/
├── cli.py          # argparse commands; orchestrates adapters + store
├── store.py        # SQLite schema, idempotent upserts, FTS5
├── model.py        # normalized Session/Event field whitelist
├── handoff.py      # context-package builder
├── export.py       # Markdown/JSON rendering
└── adapters/
    ├── base.py     # Adapter protocol, registry, git helpers
    └── *.py        # one module per platform
```

Core rule: **provider-specific logic stays inside `adapters/`**. The store
and CLI only know the normalized model (`model.py`). Every event keeps its
`raw_event` — normalization must never lose information.

## Adding a platform adapter

1. Read `docs/RECON.md` first — it documents where each known tool keeps
   sessions, and which fields (tool calls, diffs, tokens, git info) are
   available. Copy its checklist for your platform.
2. Create `voyager/adapters/<platform>.py` implementing:

   ```python
   class YourAdapter(Adapter):
       provider = "yourplatform"      # unique, lowercase
       can_resume = True/False        # does a resume CLI exist?
       can_fork = False

       def discover(self) -> list[Path]: ...   # source artifacts
       def parse(self, source: Path): ...      # one source -> one session
   ```

   If one artifact holds many sessions (like a SQLite DB), implement
   `scan(self, source_changed) -> list[dict]` instead and return one
   bundle per session, each carrying its own `source_path`.
3. Register it at module bottom: `register(YourAdapter())`, and add the
   module name to `_MODULES` in `adapters/__init__.py`.
4. Emit only the event kinds in `model.py::EVENT_KINDS`; put everything
   the normalized fields can't express into `metadata` / `raw_event`.
5. Optional dependencies (e.g. `zstandard`) go in
   `pyproject.toml :: [project.optional-dependencies]`; import them
   lazily so the core stays dependency-free.

## Ground rules

- **Provider files are read-only.** Never write, move or "clean up"
  anything under an agent's own data directory.
- **Scans are idempotent.** Re-running must not duplicate sessions or
  events; deleted sources must prune; unchanged sources must be skipped.
- **Never fabricate.** If a platform doesn't persist tool calls, emit no
  tool events — say so in the README table instead of inventing data.
- No network calls. No telemetry. No new runtime dependencies in core.

## Testing

There is no mock framework; tests run against real on-disk data:

```sh
python tests/test_store.py      # index-layer regressions (9 cases)
python -m voyager.cli scan      # then exercise the CLI against your
voyager list / show / search    # own real sessions
```

If you add storage-layer logic, extend `tests/test_store.py` — it builds
synthetic fixtures in a temp dir and needs no provider data.

## Docs

User-facing behavior changes must update **both** `README.md` and
`README.zh-CN.md`, and `docs/DECISIONS.md` gets an entry whenever you make
an architectural trade-off (format: Decision / Reason / Alternatives /
Consequences).

## License

By contributing you agree your code is released under the project's MIT
license.
