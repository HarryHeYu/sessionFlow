# Try sessionFlow in five minutes

sessionFlow is for one specific moment: you used one coding agent, switched to another, and the second agent cannot see what already happened.

## 10-second fit

If you have ever said “the previous agent already knew this, why am I explaining it again?”, this tool is for you. It is local-first: it reads the session history your agents already wrote to disk and compiles the part that matters into a continuation the next agent can read.

It is **not** a cloud memory service and **not** just a search box over old transcripts.

## Safe demo — synthetic only

No real history, no provider login, no credential, no GUI. `--db` keeps the demo index out of the real index.

```sh
voyager demo --db "$HOME/.voyager/try-demo.db"
voyager --db "$HOME/.voyager/try-demo.db" search "authentication"
voyager --db "$HOME/.voyager/try-demo.db" show codex:demo-auth-01
```

On Windows PowerShell, replace `$HOME` with `$env:USERPROFILE`. If `voyager` is not on PATH, use `python -m voyager.cli` in place of `voyager`.

What you should notice:

- one WorkThread spans four agents;
- search finds old decisions, not just raw text;
- every line in `show` carries a `#seq/digest` pointer back to the source event;
- `[source missing]` is expected in the synthetic demo — its events exist only in the index, not in transcript files on disk.

## See the actual value

Read the [public continuity proof](../examples/continuity-proof/README.md). It is a clean fixture where a critical requirement exists only in a prior agent session. The control condition misses it; the sessionFlow condition recovers it. The claim is deliberately small and reproducible — not a statistical completion-rate claim.

## Real use — only when you are ready

Read-only first:

```sh
voyager scan --platform codex
voyager list
voyager preview
```

`scan` is incremental and opens provider session files read-only. `preview` compiles the same context a handoff would use but takes no lease and writes no bundle.

Then, for an actual handoff:

```sh
voyager continue --no-launch -o ./handoff.md
```

`continue` is a write: it refreshes the index, takes the WorkThread lease and writes a bundle. `--no-launch` only suppresses starting the next agent; it does not suppress the write. If the work is still in that agent, use `voyager thread unlock <id>` only when you intentionally want another writer to take over.

## Where data lives

- Real index: `~/.voyager/index.db`.
- Demo index: the `--db` path you chose above.
- Bundles and dashboards are generated from the index and should be treated as private.

sessionFlow itself does not upload session content and does not collect telemetry. The agent you choose to launch may still make its own provider calls.

## Feedback

Found a broken provider, a confusing step, or a handoff that missed the point? Open an issue from the [issue chooser](https://github.com/HarryHeYu/sessionFlow/issues/new/choose). Please redact real session text, paths and secrets.
