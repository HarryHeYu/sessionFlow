# Cold-start acceptance matrix — v1.4.0 (2026-11-14)

AUDIT — frozen. Records what a brand-new user sees on a clean, fully
isolated environment per platform, executing the 14 steps of
[Issue #29](https://github.com/HarryHeYu/sessionFlow/issues/29) against the
**released v1.4.0 assets**. It is a measurement of one round, not a
description of the product; it is not updated.

## Environment

| | Windows | Linux |
|---|---|---|
| OS | Windows 10.0.26300 (Git Bash host) | Ubuntu 22.04.5 (WSL2) |
| Interpreter (wheel install) | Python 3.14.2, clean venv | Python 3.10.12, clean venv |
| Interpreter (pipx install) | Python 3.12.7 | Python 3.10.12 |
| Assets | `voyager-1.4.0-py3-none-any.whl`, `voyager-1.4.0.tar.gz` — sha256 verified against the Release API digest on **both** platforms (`855326c7…` / `2f0630fb…`) | same files, re-hashed after copy |
| pipx | pipx 1.18.16, `PIPX_HOME`/`PIPX_BIN_DIR` inside the scratch tree (verified) | pipx 1.17.12, same redirect (verified) |
| macOS | **NOT VERIFIED** — no machine available; not inferred from Linux | |

Isolation: every `voyager` invocation ran with throwaway
`HOME`/`USERPROFILE`/`HOMEDRIVE`/`HOMEPATH`/`APPDATA`/`LOCALAPPDATA`/
`XDG_CONFIG_HOME`/`XDG_CACHE_HOME`/`XDG_DATA_HOME`/`XDG_STATE_HOME`/
`TMP`/`TEMP`/`TMPDIR`, plus `PIP_CACHE_DIR`; the real user profile was never
on any path. Confirmed in-product: `voyager --help` prints the default db
path under the redirected home. Synthetic canary sessions (2 codex, 1 claude;
one repo with Chinese characters + a space) were placed in the isolated home
before the first run.

Environment note for reproduction: on this Windows host, `git clone` inside
pipx needed a scratch `[http] sslbackend = schannel` gitconfig (the network
sits behind a TLS-intercepting proxy; the bundled OpenSSL CA does not trust
it). No user git configuration was read (`GIT_CONFIG_SYSTEM=/dev/null`).

## Step × platform matrix

Each PASS names the command and the observed output; the evidence log holds
the full stdout/stderr of every step. FAIL cells link the filed issue.

| # | Step | Windows | Linux |
|---|---|---|---|
| 1a | install from the Release wheel | PASS — `pip install <wheel>` → `pip show voyager` = 1.4.0; `voyager --version` → `voyager 1.4.0` | PASS — same |
| 1b | install `voyager[all] @ git+…@v1.4.0` via pipx | PASS — `voyager 1.4.0`; `import zstandard, mcp` OK (zstandard 0.25.0); app venv inside scratch | PASS — same |
| 2 | first run, no configuration | PASS — `doctor` exit 1: `index database not found; run 'voyager scan'`; `status` → `NO_THREAD`; `list` → `no sessions (run 'voyager scan' first?)` | PASS — identical wording |
| 3 | default run does **not** index real history | PASS — with the canary on disk and no scan run: `search "parser"` → `no matches`; `~/.voyager` not created (see isolation section) | PASS — same |
| 4 | explicit scan after consent | PASS — `voyager scan` → `codex: 2 sessions indexed`, `claude: 1`, 8 other providers `no sources found`; `scan complete: 3 sessions, 6 events`; `index.db` created | PASS — identical counts |
| 5 | index + search incl. CJK | PASS — `search "parser"` → 2 hits; `search "解析器"` → 2 hits; `search "审查"` → 3 hits across 2 sessions; `repo` timelines for the ASCII and the CJK+space repo | PASS — identical |
| 6 | show / session summary | PASS — `show codex:<id>` and `show claude:<id>` print metadata, `#seq/digest` pointers; a wrong provider prefix (`cla:`) fails cleanly with `session not found` | PASS — identical |
| 7 | export | PASS — `export claude:cla-sfacc1 --format md -o "…/导出 目录/session-export.md"`; json format writes `codex-<id>.json` into the **cwd** (observation, not filed) | PASS — same behaviour |
| 8 | WorkThread creation + `thread list` | PASS — `thread create --attach … --attach …` → `Thread thr_… created` (2 cross-provider members); `thread list` / `thread show` correct | PASS — same |
| 9 | Continuation Bundle with explicit `-o` | PASS with caveat — `continue --thread T -o f.md` prints the D7 native-resume command and writes **no file**; `continue --thread T --to codex --bundle --no-launch -o f.md` writes the bundle (claims labelled, evidence refs). Doc drift filed as [#37](https://github.com/HarryHeYu/sessionFlow/issues/37) | PASS with caveat — identical |
| 10 | agent handoff (file check only) | PASS — `handoff claude:cla-sfacc1 --to codex -o …md` → context package written, CJK intact, `seq 1 · #cdfb02a7` reference present, nothing launched | PASS — same |
| 11a | absent index | PASS — rename db away → `list`/`search` empty answers, `doctor` exit 1 | PASS — same |
| 11b | corrupt index | **FAIL** — `list`/`status`/`search`/`scan`/`continue` print a raw `sqlite3.DatabaseError` traceback (`store.py:415`), no guidance, and `scan` cannot rebuild over it → [#34](https://github.com/HarryHeYu/sessionFlow/issues/34). `doctor` and `db check` degrade correctly | **FAIL** — identical traceback on Python 3.10 |
| 11c | missing source | PASS — transcript moved away + rescan → `[source missing]` in `list`, retention note printed, `search` still finds it; restored + rescan → live again | PASS — same |
| 11d | read-only database (write refused) | PASS — `attrib +R` on the db + pending new session → `scan` exit 0 with `4 session(s) could not be written and will be retried on the next scan; the index is consistent and nothing was half-written`; nothing silently indexed | PASS — `chmod 444`, same message (`1 session(s) could not be written …`) |
| 11d′ | recovery after permissions restored | PASS — retry scan indexes the pending session (`4 sessions, 8 events`) | **FAIL** — the scan left `index.db-shm` at mode 444; the retry promise never succeeds and names no cause → [#36](https://github.com/HarryHeYu/sessionFlow/issues/36) |
| 12 | read paths never create the database | **FAIL** — `voyager thread list` in a fresh home **creates** an empty `index.db` (`cli.py:1607` uses the read-write `Store()`) → [#35](https://github.com/HarryHeYu/sessionFlow/issues/35). All other read commands verified clean by per-command bisect in fresh homes (`list`, `search`, `status`, `doctor`, `stats`; plus `--db <nonexistent>/x.db list` creates nothing) | **FAIL** — same reproduction |
| 13 | empty state, no history at all | PASS — after `scan` with zero provider data: `no sessions (run 'voyager scan' first?)`, `no active threads`, `NO_THREAD`, `doctor` shows `sessions: 0 threads: 0` | PASS — same |
| 14 | Chinese characters, space, non-ASCII | PASS — CJK repo path `E:/验收 测试/演示项目` (Windows) / `/tmp/sfacc/work/验收 测试/演示项目` (Linux) timelines; export to a CJK+space directory; CJK full-text search; CJK titles throughout | PASS — same on ext4 |

## Isolation verification (independent of the product's own reports)

- **Real-profile snapshot diff** (`tests/iso_guard.py` root list: `.voyager`,
  `.codex`, `.claude`, `.dsh`, `.grok`, `.zcode`, `.cursor`, `.gemini`,
  `.config`, `.local/share/opencode`, `%APPDATA%\Kiro`): before/after
  manifests (path, size, mtime_ns) over ~195,600 entries on Windows.
  - Windows: 45 diffs, **all** in roots with concurrently running agent
    processes (ZCode harness artifacts incl. this session's id, DSH session
    files growing, Grok/Codex logs, and the maintainer's own live voyager
    index). No change is attributable to the acceptance commands, but the
    host is not a single-process machine, so this is recorded as attribution
    analysis, not proof of absence.
  - Linux: **0 diffs** (real home re-snapshotted with the genuine
    environment).
- **`sources.path` audit**: the final index was opened read-only on both
  platforms; all 8 rows (4 sources × 2 platforms) point inside the scratch
  tree. **0 rows name a real user directory.**
- **SQLite WAL/shm side effects at rest**: journal mode is `wal`; after
  runs, zero-byte `-wal` and small `-shm` files persist beside the index on
  both platforms (no torn state — `voyager db check` is fully `[OK]` × 6 on
  both final indexes after every abuse scenario).
- **Read-never-create**: fresh-home bisect — `list`, `search`, `status`,
  `doctor`, `stats` create nothing; `thread list` does (filed as #35).

## Defects found → issues

| Issue | Severity | Summary |
|---|---|---|
| [#34](https://github.com/HarryHeYu/sessionFlow/issues/34) | P2, both platforms | corrupt index → raw traceback on every read/write command; no recovery guidance |
| [#35](https://github.com/HarryHeYu/sessionFlow/issues/35) | P2, both platforms | read-only `thread` subcommands create an empty `index.db`, breaking `open_read`'s documented guarantee |
| [#36](https://github.com/HarryHeYu/sessionFlow/issues/36) | P2, Linux | read-only scan leaves a read-only `-shm`; the promised retry never succeeds and says why not |
| [#37](https://github.com/HarryHeYu/sessionFlow/issues/37) | P3, docs | QUICKSTART §6 `-o` example silently writes nothing on the D7 native-resume path |

## Explicitly not verified

- macOS — no machine; nothing above transfers to it by inference.
- Native resume was never launched (`--no-launch` everywhere; the written
  files were inspected, no agent was started) — consistent with the README's
  `NOT VERIFIED` stance on resume.
- No real agent history was indexed anywhere in this round; every session in
  every index is the synthetic canary. The v2.0.29 OpenCode real install on
  the host was probed for existence/version only (see the #30 record), never
  scanned.
- `voyager demo`, the MCP server and the DSH plugin are outside #29's step
  list and were not exercised here.
