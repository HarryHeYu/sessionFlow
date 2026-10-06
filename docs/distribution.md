# Distribution roadmap

How sessionFlow reaches users, now and planned. Nothing on this page is
published ahead of its status line.

## Current (works today)

```sh
git clone https://github.com/HarryHeYu/sessionFlow
cd sessionFlow
pip install -e ".[all]"      # core + DSH + MCP extras
voyager demo                 # 2-minute synthetic tour, no agents needed
voyager scan                 # then index YOUR agents
```

The [dsh-sessionflow](https://github.com/HarryHeYu/dsh-sessionflow)
plugin installs from its GitHub repository through DSH's plugin
mechanism:

```
dsh plugin --profile <profile> install HarryHeYu/dsh-sessionflow
```

## Planned — PyPI

Package name: **undecided** — the `voyager` name on PyPI is taken
(Peter Sobot's nearest-neighbour search library), so the final name
needs a maintainer decision; `sessionflow` and `voyager-sessionflow`
were available as of 2026-10-06 (see `docs/pypi-checklist.md`).

Status: **not published.** The build is validated (`python -m build`,
`twine check` PASSED, clean-venv wheel smoke), and a TestPyPI dry-run
checklist is in [pypi-checklist.md](pypi-checklist.md).

Future command (do not expect it to work yet):

```
pip install voyager          # or the finally chosen name
```

## Planned — npm

The DSH plugin's npm packaging is validated (`npm pack --dry-run`,
`npm publish --dry-run`), but npm publishing is **not done** — DSH users
should install from GitHub as above.

Future command (do not expect it to work yet):

```
npm install -g dsh-sessionflow   # or DSH-managed install
```

## Compatibility contract

Whatever the distribution channel, the bridge protocol stays versioned:
the core reports `BRIDGE_SCHEMA_VERSION` (currently `1`), the plugin
requires `>= 1`, and additive changes never bump it. See
[architecture.md](architecture.md).
