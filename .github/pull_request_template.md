# Pull request

## What

<!-- One or two sentences: what changes and why. Link the issue if one exists. -->

## How

<!-- The layer(s) touched: adapter / store / continuity / timeline / doctor / verification / CLI / docs. -->

## Checklist

- [ ] Tests added or updated (a storage-format change must fail loudly, not silently)
- [ ] Documentation updated where user-facing behavior changed (README.md **and** README.zh-CN.md)
- [ ] No breaking API / bridge-schema / CLI-flag changes without prior discussion in an issue
- [ ] No real user data included (sessions, paths, message content — synthetic fixtures only)
- [ ] Full test suite passed locally: `python -m pytest tests/ -q`
- [ ] Provider-specific logic stays inside `voyager/adapters/` (if applicable)

## Honesty check

<!-- Delete the line that does not apply. -->

- This PR does not change any capability claim (README table / capability matrix / verification states).
- This PR changes capability claims, and every claim is backed by evidence or a test as documented in [CONTRIBUTING.md](../CONTRIBUTING.md).
