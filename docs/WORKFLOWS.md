# Workflows — everyday recipes

Real flows tested against a live index (8 platforms, ~200 sessions,
~140k events). Session ids below are from that index; replace with yours.

## 1. "What have all my agents been doing?"

```sh
voyager brief --hours 48            # everything active in the last 2 days
voyager brief --repo black_box      # or scoped to one repository
```

Or from inside an agent (MCP tools): ask for `voyager_brief`.

## 2. Read one session in full detail

`voyager brief` gives one line per session. To read everything:

```sh
voyager list --platform dsh         # find the id (check message_count —
                                    # some sessions are hollow: created but
                                    # never used, 0 msgs)
voyager export session-8f08216f --format md -o dsh_full.md
```

A 2,915-message / 13,654-tool-call session exports to a ~20 MB Markdown
file — every user message, assistant analysis (LaTeX intact), command and
output, in order. Open it in an editor and read top to bottom.

Variants:

```sh
voyager show session-8f08216f             # scroll the timeline in-terminal
voyager export session-8f08216f --format json   # normalized + raw events
```

## 3. Find where something was said/done

```sh
voyager search "unlearning"         # substring search, CJK works too
voyager search "chenjunkai14"
```

Hits show the session, provider and a highlighted snippet; dive in with
`voyager show <id>`.

## 4. Cross-agent timeline for one repository

```sh
voyager repo black_box
```

Groups every provider's sessions under the repo, with branch/commit and
message counts — the answer to "what did codex vs dsh do on this project?"

## 5. Continue a task

```sh
voyager continue                    # newest session: native resume when the
                                    # platform supports it, otherwise an
                                    # automatic handoff package for Claude
voyager continue --repo black_box --launch
```

This is work continuation, not session teleportation. Same-provider
pickup uses the native CLI (`codex resume`, `claude --resume`, …).
Multi-session merge / WorkThread: [ROADMAP.md](ROADMAP.md).

## 6. Hand a task to a different agent

```sh
voyager handoff session-8f08216f --to claude          # package + command
voyager handoff session-8f08216f --to claude --launch # package + start
```

The Context Package contains the goal, every follow-up instruction, the
last assistant message, files touched, commands with exit codes, errors
and a condensed timeline. Launchable targets: `claude`, `codex`, `grok`;
other targets get the package file to paste manually. The new session
does not inherit the previous agent's hidden tool state — it reads the
package and continues the *work*.

## 7. From inside an agent (MCP)

If your agent has the voyager MCP server enabled (Codex `config.toml`,
Claude Code `claude mcp add`, Cursor `mcp.json`), ask in natural language:

> "用 voyager_brief 看看我所有 agent 最近在忙什么"
> "用 voyager_show 把 dsh 那个 CAP-U 会话展开"

Tools: `voyager_brief` / `voyager_search` / `voyager_list` /
`voyager_show` / `voyager_handoff`.

## 8. Keep it current

```sh
voyager watch --interval 300        # or put it in OS autostart
```

## Gotchas

- **Hollow sessions exist.** Some platforms create a session file before
  any message is sent — check `message_count` in `voyager list` before
  exporting.
- **Exports are snapshots.** A session that is still active needs another
  `scan` + re-export to include newer messages.
- **Event bodies are capped** (600 KB) in the index; the full original
  always remains in the provider's own files, and `sources.path` records
  exactly where.
