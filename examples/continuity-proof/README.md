# Public continuity proof

This is a small, privacy-safe demonstration of the idea behind sessionFlow:

> A critical requirement exists only in a prior agent session. The repository is clean, the visible tests pass, and there is no TODO revealing what remains. Agent B sees only the repository in the control condition; in the sessionFlow condition it also receives a continuation compiled from the prior session.

This is a falsifiable scenario, not a statistical benchmark. It uses no real user history, credentials, provider login or GUI.

## 1. Set up two fresh runs

Run from the Core repository root. Use a different output directory for each condition; the scripts refuse to reuse a non-empty directory and delete nothing.

```bash
python examples/continuity-proof/setup.py --out .continuity-proof/control
python examples/continuity-proof/setup.py --out .continuity-proof/sessionflow
```

Each run creates:

- `app/` — the only directory Agent B may edit;
- `home/.codex/sessions/...` — a synthetic prior session from Agent A;
- `evaluator/` — oracle, hidden test and setup evidence (evaluator-only);
- `agent-b-control.md` / `agent-b-sessionflow.md` — use the prompt files from this directory.

Setup must print:

```text
visible_rc=0 hidden_rc=1 leaked=False
```

That means the visible tests pass, the hidden contract fails before any handoff, and the hidden requirement is not present in the visible repository.

## 2. Control condition

Give an agent this prompt, then let it work only in `.continuity-proof/control/app/`:

> See `agent-b-control.md` in this directory.

The control prompt deliberately contains no information about the remaining requirement.

Verify:

```bash
python examples/continuity-proof/verify.py --out .continuity-proof/control --condition control
```

Expected result: visible tests still pass, hidden test fails, and the verifier prints `VERIFIER_PASS`. If the hidden test passes here, the control was contaminated by a leaked requirement or an unexpected guess.

## 3. sessionFlow condition

First compile a continuation from the seeded prior session using the real Core CLI:

```bash
python examples/continuity-proof/make_continuation.py --out .continuity-proof/sessionflow
```

This writes `.continuity-proof/sessionflow/continuation.md` using `scan`, `thread create` and `continue --bundle --no-launch` against the isolated demo home and database. No real HOME is read.

Give an agent this prompt, then let it work only in `.continuity-proof/sessionflow/app/`:

> See `agent-b-sessionflow.md` in this directory; read `continuation.md` first.

Verify:

```bash
python examples/continuity-proof/verify.py --out .continuity-proof/sessionflow --condition sessionflow
```

Expected result: visible tests pass, hidden test passes, and the verifier prints `VERIFIER_PASS`.

## What the verifier checks

- `setup-evidence.json`: the pristine fixture had green visible tests, a red hidden test and no hidden requirement in the visible repo.
- `verify-control.json` / `verify-sessionflow.json`: visible tests and hidden test outcomes for the condition.
- `continuation-evidence.json`: the sessionFlow condition really generated a continuation through the Core CLI.

The hidden test is evaluator-only. Both agent prompts forbid reading `evaluator/`. This keeps the run deterministic and inspectable; it is not a blind or statistically significant study.

## Using an installed Core instead of this checkout

The scripts auto-detect this repository root. If you run them elsewhere, pass the Python environment that has `voyager` installed:

```bash
python examples/continuity-proof/setup.py --out <run-dir> --python /path/to/python
python examples/continuity-proof/make_continuation.py --out <run-dir> --python /path/to/python
python examples/continuity-proof/verify.py --out <run-dir> --condition sessionflow --python /path/to/python
```
