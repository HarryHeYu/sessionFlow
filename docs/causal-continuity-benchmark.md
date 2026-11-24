# Causal continuity benchmark — R17 protocol

This benchmark answers a narrow question: can a continuation bundle give an
incoming Agent information that is not available from the repository itself?
It is deliberately small and falsifiable; it is not a statistical completion
rate and it does not bind the protocol to a particular model vendor.

## Three scenarios

| Task | Hidden information in the prior session | Repo state |
|---|---|---|
| E1 hidden behavioral requirement | Collapse every whitespace run, including tabs, newlines and U+00A0, into one ASCII space | Visible tests green; no TODO or failing test |
| E2 hidden architectural constraint | Accept legacy numeric strings and `Decimal`; stdlib only; signature unchanged; existing output unchanged | Visible tests green; no TODO or failing test |
| E3 hidden rejected approach | `line.split('#', 1)` was tried and failed because it destroys `#` inside values; use a quote-aware parser | Visible tests green; failed attempt rolled back and not in the repo |

## Protocol

1. Run `python scripts/r17_causal_fixture.py --out <dir>` to generate three
   fixture repos, private hidden tests and a machine-readable oracle.
2. Verify deterministic setup: visible tests pass, hidden tests fail.
3. Agent A writes a prior session containing the hidden information without
   modifying the repo. Ingest that transcript through the real Core CLI:
   `scan`, `thread create --attach`, then `continue --bundle --no-launch`.
4. Run two independent Agent B conditions on identical repo copies:
   - `CONTROL`: only the repo and a neutral continuation prompt;
   - `SESSIONFLOW`: the same prompt plus the one continuation bundle.
5. Run `python scripts/r17_causal_fixture.py --out <dir> --action verify` to
   apply the pre-written oracle. The verifier checks hidden tests and structural
   constraints; it does not trust the Agent's self-report.

## Interpretation

- If CONTROL also solves the task, the scenario is SUPPORTIVE at best and must
  not be used as causal evidence.
- If SESSIONFLOW recovers hidden facts and passes the oracle while CONTROL does
  not, the scenario is CAUSAL-EVIDENCE for that fact.
- A scenario whose SESSIONFLOW run fails the oracle stays PARTIAL; do not relax
  the oracle after seeing the output.

## R17 result

CONTROL 0/3 complete; SESSIONFLOW 2/3 complete. E1 and E2 are causal evidence;
E3 recovered the hidden failure history and avoided the rejected approach but
failed one hidden test, so its completion claim was not promoted.
