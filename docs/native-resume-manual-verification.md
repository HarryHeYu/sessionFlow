# Native Resume — manual verification procedure

Status: **NOT VERIFIED**. This document describes a safe procedure for a user
who chooses to verify Native Resume with their own provider account. It does
not grant permission to run authenticated provider sessions in CI or in an
automated sandbox, and it does not change the public status to VERIFIED.

## Principle

A provider CLI knowing a resume command is not evidence. Verification requires
a unique fact that exists only in the original session, a real resumed session
with a checkable identity, and a post-resume answer that recovers the fact.

## Procedure

1. Create a disposable project or use a throwaway branch. Do not use a private
   repository containing secrets.
2. Start a new session with the chosen provider. Give it a unique marker, for
   example `R17_NATIVE_RESUME_MARKER=<random value>`, and ask it to remember and
   repeat the value later. Use your own account only if you explicitly choose
   to do so.
3. Let the provider write its native session file. Capture the provider session
   id or the conversation identity if the CLI exposes one.
4. Run `voyager scan --force` in the project, then locate the indexed session
   with `voyager list` / `voyager recent`.
5. Run `voyager resume --print <session>` to inspect the exact native command.
   Confirm that the adapter names the same provider session id from step 3.
6. Run the printed native resume command yourself. Do not paste credentials
   into issue trackers, logs or test fixtures.
7. Ask the resumed session for the unique marker. Record whether it answers
   with the exact value.
8. Compare session identity: the resumed CLI session should be the same native
   session, not merely a new session with copied prose.
9. Record only redacted evidence: dates, provider, session id, command shape,
   pass/fail and the marker equality result. Never commit credentials, tokens
   or private conversation text.

## Acceptance

A provider may be claimed as `LIVE VERIFIED end to end` only after steps 1–8
are observed by a human and the redacted record is attached to the relevant
issue or release audit. Until then the status remains `NOT VERIFIED`, regardless
of adapter source, CLI help output or startup-hook evidence.
