# Archived audits — O5 and O6 (2026-10-03)

Two completed one-off audits, moved out of `docs/` on 2026-10-14 so that
`docs/` holds only documents that are still maintained. Neither file is
referenced by path anywhere in the repository, and neither had a markdown link
pointing at it; the move rewrote nothing.

| File | What it was | Where the outcome lives now |
|---|---|---|
| `O5_INVENTORY.md` | Provider verification inventory, written **before** O5. Its own header says it describes the repo as it was when written, including gaps that have since closed. | `docs/DECISIONS.md` D18 (evidence-only verification); `voyager verify` |
| `O6_AUDIT.md` | Architecture hygiene audit, status "Complete". | `docs/DECISIONS.md` D19 (shared helpers in `voyager/util.py`); `docs/architecture.md` |

**Read these as history, not as current state.** They are reproduced verbatim —
the paths and line references inside them point at the layout of 2026-10-03, and
the numbers in them were true then. Nothing here is maintained, and nothing here
should be quoted as a description of the code today.

For the current picture see [../../INDEX.md](../../INDEX.md).
