# Security policy

sessionFlow is a **local-only** tool: it makes no network calls, has no
accounts, and never transmits your session data anywhere. Its attack
surface is deliberately small.

## Supported versions

| Version | Supported |
|---|---|
| latest tag on `main` | ✅ |
| older tags | best effort |

## Reporting a vulnerability

Please do **not** open a public issue for anything exploitable.

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**), or contact the maintainer via a
GitHub account email. Include:

- affected commit / tag
- the command sequence that triggers it (synthetic data only)
- the impact you believe it has

You will get a response within a few days. Fixes land as a patch release
and the advisory is published once a fixed version is available.

## What counts as a vulnerability here

Anything that lets sessionFlow (or one of its bridges) **write** to
provider data directories, send session content off the machine, execute
attacker-controlled input from indexed files, or break the `doctor --fix`
repair boundary (canonical history must never be modified by a repair).
