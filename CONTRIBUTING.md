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
├── mcp_server.py   # MCP tools (optional [mcp] extra)
└── adapters/
    ├── base.py     # Adapter protocol, registry, git helpers
    └── *.py        # one module per platform

tests/
├── fixtures/       # synthetic provider data (text + SQL seeds)
├── conftest.py     # tmp-tree builders, adapter_of / patch_paths fixtures
└── test_*.py       # one file per adapter + store/export/handoff/cli/mcp

.github/workflows/test.yml   # CI: py3.10-3.13 (linux), 3.10/3.13 (windows), core-only
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

```sh
pip install -e ".[all,dev]"
python -m pytest tests/ -q            # the whole suite (63 tests, ~5s)
python -m pytest tests/test_zcode.py  # one adapter
python scripts/run_tests_core_only.py # simulate `pip install voyager` (no extras)
```

Tests never touch your real `~/.codex`, `~/.claude`, `~/.voyager` or any
provider storage: adapters are pointed at synthetic fixtures built in
`tmp_path` (see `tests/conftest.py`). Provider fixtures are text files /
SQL seeds under `tests/fixtures/<provider>/` — reviewable in diffs, no
binaries, no captured user data. SQLite and zstd artifacts are materialized
from those seeds at test time.

**Adding an adapter means adding its fixture + test**: `tests/fixtures/<p>/…`
plus `tests/test_<p>.py`, using the `adapter_of` and `patch_paths` fixtures.
The test must assert the normalized session fields *and* the event kinds, so
a storage-format change upstream fails loudly instead of silently indexing
garbage. CI (`.github/workflows/test.yml`) runs the suite on Python 3.10–3.13
(Linux) and 3.10/3.13 (Windows).

## Docs

User-facing behavior changes must update **both** `README.md` and
`README.zh-CN.md`, and `docs/DECISIONS.md` gets an entry whenever you make
an architectural trade-off (format: Decision / Reason / Alternatives /
Consequences).

## License

By contributing you agree your code is released under the project's MIT
license.
