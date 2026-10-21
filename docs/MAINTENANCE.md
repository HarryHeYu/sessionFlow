# Maintenance Guide — sessionFlow v1.3.0

Post-release reference. Nothing here starts new development; it records what is
known, what is supported, and what a maintainer should check.

---

## Known Issues

| # | Issue | Impact | Status |
|---|---|---|---|
| 1 | **No automatic fact-checking.** Nothing decides whether a historical agent claim is correct. | A reader can locate and check evidence; they cannot delegate the judgement. | By design. Not a bug. |
| 2 | **The fingerprint is not a signature.** 8 hex of SHA-256, compared pairwise. | Detects accidental drift; a forger who rewrites a transcript can recompute it. | By design. Stated in every output path. |
| 3 | **No source content hashing.** Only `mtime` + `size` are compared. | A `touch` reads as "possibly changed"; an edit preserving size reads as "unchanged". | Deferred (cost: full re-read of every provider file per scan). |
| 4 | **`(session, seq)` does not survive an edit in the middle of a transcript.** | The seq still resolves — to a different event. | Mitigated: the fingerprint reports MISMATCH instead of silently re-pointing. |
| 5 | **Unicode is not normalised** in the fingerprint. | NFC vs NFD spellings of the same text hash differently. | Deferred: normalising ties the digest to a Unicode DB version, and a false MISMATCH is the safe direction. |
| 6 | **`evidence verify` is CLI-only.** | No API/MCP wrapper. `--json` is the machine surface. | Deferred until something consumes it. |
| 7 | **Dashboard search is snapshot-scoped.** | The page filters what it rendered; whole-index search is `voyager search`. | By design (static HTML, no server). |
| 8 | **Browser verification runs locally, not in CI.** | CI jobs skip where a browser cannot start, with a stated reason. | `CI_BROWSER_NOT_VERIFIED` — declared, not hidden. |
| 9 | **SQLite concurrent-index flake.** | Rare failure in `test_concurrent_creation_of_a_fresh_index`. | **INCONCLUSIVE**: not reproduced in bounded re-runs (40 fresh-index pairs, 160 child processes). Deliberately not masked with sleeps or unbounded retries. |
| 10 | **Native resume is unverified** end to end on every provider. | The command is built and wired; no resume has been observed succeeding. | `NOT_VERIFIED` — never claimed as working. Verifying it needs a paid provider session and writes into a real environment. |
| 11 | **Model-driven tool selection has no repeatable end-to-end suite.** | It has been *observed* in recorded real-model runs — 3/3 when the prompt names the agent or asks "what was I working on", 1/3 on a bare "continue" — but that is one model, one fixture, three attempts. | `OBSERVED, NOT REPEATABLE`. Contract tests cover the six tools; they do not cover the model choosing them. Do not quote a success rate. |
| 12 | **`voyager evidence verify --help` does not list the five outcomes or the exit codes.** | A user writing a script has to read the README (or trigger each state) to learn them. The runtime output *does* state its own limits on every path. | Known, deliberate for now: changing help text is a code change, and no release is open to carry it. Documented in the README; fix belongs in the next release. |

## Supported Platforms

| | |
|---|---|
| Python | 3.10 – 3.13 (CI covers all four on both OS families) |
| OS | Windows, Linux. macOS is supported by the code and the path handling but is **not in CI**. |
| Agents indexed | Codex, Claude Code, DSH, Grok CLI, ZCode, Kiro, Cursor, Antigravity |
| Distribution | GitHub tag install (`@v1.3.0`). **Not on PyPI** — the name `voyager` there is an unrelated library. |

## Compatibility Matrix

| Core | Plugin | Bridge schema | Notes |
|---|---|---|---|
| v1.3.0 | v1.1.0 | 1 | current release; plugin needs no upgrade |
| v1.2.0 | v1.1.0 | 1 | still supported; index readable by v1.3.0 with no migration |
| v1.1.0 | v1.1.0 | 1 | upgrade path verified: no row and no schema object changes |

Bundle compatibility: v1.2.0 bundles are readable by v1.3.0. They carry no
fingerprint, so `evidence verify` answers `UNVERIFIABLE` and offers the digest to
adopt. v1.3.0 never invents a fingerprint for a legacy reference.

## Maintenance Checklist

Run before tagging any future release:

```sh
# PY must be an interpreter that HAS pytest — the base interpreter does not.
# A venv works: python -m venv .venv && .venv/bin/python -m pip install -e ".[all,dev]"
PY=/path/to/venv/bin/python

# 1. version and changelog agree
grep -m1 '^version' pyproject.toml
grep -m1 '^## \[' CHANGELOG.md

# 2. clean tree, in sync
git status --short && git rev-parse --short HEAD origin/main

# 3. full suite (one run at a time — concurrent runs contend on the shared index)
PYTHONPATH= PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest tests/ -q \
    --basetemp=/tmp/sf-bt-$(date +%s)

# 4. plugin
cd /e/code/dsh-sessionflow && npm test

# 5. build and install from the artefact, not the worktree
cd /e/code/voyager
python -m build --wheel --sdist --outdir /tmp/sf-dist
# then: clean venv, pip install the wheel, `voyager --version` must match

# 6. upgrade path: build an index with the PREVIOUS tag, read it with this one
git archive <previous-tag> | tar -x -C /tmp/sf-prev
# write synthetic rows with it, then read/verify with the current tree
```

`PYTHONPATH=` matters on machines with a delete-guard shim injected through
`PYTHONPATH`: with it in place, bulk `unlink()` raises and tests fail for a reason
that has nothing to do with the code.

Release mechanics:

1. bump `pyproject.toml` **and** the CHANGELOG heading together;
2. push, wait for CI to be green **on that commit**;
3. `git tag -a vX.Y.Z -m "..."` and push the tag;
4. `gh release create vX.Y.Z --title ... --notes-file ... <wheel> <sdist>`;
5. build the artefacts **from the tag** (`git archive vX.Y.Z`), not from the
   worktree, so the attachment and the tag cannot drift;
6. install from the tag in a clean venv and confirm `--version`;
7. **only then** repoint the READMEs — docs must never point at a tag that does
   not exist yet.

Never: move or delete a published tag, edit a published release's assets, or
`--force` push.

## Bug Report / Feature Request

The GitHub issue forms are the templates — do not paste a second copy here, and do
not add a parallel set of files:

- [`.github/ISSUE_TEMPLATE/bug_report.yml`](../.github/ISSUE_TEMPLATE/bug_report.yml)
- [`.github/ISSUE_TEMPLATE/feature_request.yml`](../.github/ISSUE_TEMPLATE/feature_request.yml)

Two things the bug form asks for that are specific to this project:

```sh
voyager --version
voyager doctor          # paste the "blocking" section if any
```

`doctor` prints the store path — redact what you consider private. **Never paste
real session content**; use `voyager demo` or synthetic text.

If the report is about evidence, also include:

- the reference as printed (e.g. `seq 3 · #c5009a39`)
- `voyager evidence verify <session> <seq> <digest>` and its exit code
- whether `voyager show <session>` shows the same `#seq/digest`

## Shortest path from zero

Two stages, because the second one reads the user's own history. Do not merge
them into one `&&` chain — `scan`, `dashboard` and `continue` are not the same
kind of operation and `continue` is a write.

```sh
# --- stage 1: synthetic only, nothing real is opened ---
pipx install "voyager[all] @ git+https://github.com/HarryHeYu/sessionFlow.git@v1.3.0"
voyager --version        # must print 1.3.0
voyager demo             # seeds ~/.voyager/demo.db, separate from the real index
voyager --db ~/.voyager/demo.db search "authentication"
voyager --db ~/.voyager/demo.db show codex:demo-auth-01
voyager --db ~/.voyager/demo.db dashboard --out ~/.voyager/demo.html
voyager --db ~/.voyager/demo.db evidence verify codex:demo-auth-01 1 2198ad71

# --- stage 2: the user's own history, after they say so ---
voyager scan --platform codex            # one agent first; drop --platform for all
voyager thread list                      # read-only
voyager thread summarize <thread>        # read-only preview of the task
voyager continue --thread <thread> --no-launch -o ./handoff-<thread>.md
```

`continue` refreshes the index (a scan), takes the WorkThread lease and writes a
package; `handoff`/`continue` put `handoff-*.md` in the **current directory**,
`merge` writes under `~/.voyager/bundles/`. The demo's sessions show
`[source missing]` because their transcript files were never written to disk —
expected, and the same label real retained history gets after a rotation. One
consequence worth knowing before writing a demo script: **`continue --thread`
refuses on the demo** (`has no live member sessions`), because continuity only
runs on live members; continue a session id there instead.

`voyager doctor` **exits 1 before the first `scan`**, with `index database not
found; run voyager scan`. That is the command working — it reports the missing
index rather than pretending the install is healthy.

## Roadmap

Five directions, in the order they would pay off. Nothing here is in progress.

1. **Easier Python distribution.** A `sessionflow` package on PyPI would replace
   the `@ git+…@v1.3.0` pin with `pip install sessionflow`. The name is free;
   the work is release automation and a migration note, not code.
2. **Dashboard whole-history search.** Today the page filters its snapshot. Real
   search needs either a generated search index or a small read-only localhost
   service — a design decision, not a patch.
3. **Optional dsh-memory interoperability.** The boundary is already written
   down (evidence reference / extracted claim / retention consent / reviewer
   identity / verification verdict, kept separate). What is missing is an
   adapter, and a reason to build one.
4. **Native resume in a real environment.** The code path exists; verifying it
   needs a paid provider session. Until then it stays `NOT_VERIFIED`.
5. **Cross-agent continuation improvements driven by actual use.** The repo
   deliberately does not guess at this one — the last few rounds showed that
   every real defect came from using the thing, not from reading it.

## What this release deliberately does not do

- No automatic truth judgement of historical claims.
- No cryptographic attestation.
- No continuous hashing of every provider file.
- No long-term memory system.
- No LLM-based task identification.
- No new dashboard architecture.
- No paid-model E2E.
