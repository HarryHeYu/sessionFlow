# TestPyPI upload checklist

`twine check` passes on the built artifacts; the only missing piece for a
dry-run upload is a TestPyPI token, which must come from the maintainer.
Do **not** put the token in the repository or in the shell history.

## One-time setup (maintainer)

1. Create a TestPyPI account at https://test.pypi.org (separate from PyPI).
2. Generate an API token: https://test.pypi.org/manage/account/token/
   (scope: project `voyager`).
3. Configure the token locally — either

   ```powershell
   # session-only (preferred; nothing persisted)
   $env:TWINE_USERNAME = "__token__"
   $env:TWINE_PASSWORD = "pypi-<your-testpypi-token>"
   ```

   or in `%USERPROFILE%\.pypirc` under `[testpypi]`.

## Dry-run upload (after `python -m build`)

```powershell
python -m twine upload --repository testpypi dist/*
```

Then verify at https://test.pypi.org/project/voyager/ :

- [ ] README renders (images resolve — they are absolute raw.githubusercontent URLs)
- [ ] project description, license, authors, keywords correct
- [ ] `voyager-1.0.0rc2` version string matches the tag
- [ ] extras listed: dsh / mcp / all / dev
- [ ] entry points present (`voyager`, `voyager-mcp`)

## Install-from-TestPyPI smoke

```powershell
python -m venv E:\sessionflow-scratch\tmp\pypitest-venv
E:\sessionflow-scratch\tmp\pypitest-venv\Scripts\pip install `
  --index-url https://test.pypi.org/simple/ `
  --extra-index-url https://pypi.org/simple `
  voyager==1.0.0rc2
E:\sessionflow-scratch\tmp\pypitest-venv\Scripts\voyager demo
```

(The `--extra-index-url` is needed because the `all`/`dev` extras pull
`zstandard`/`mcp` from real PyPI.)

## Real PyPI

Same steps without `--repository testpypi`, using a **real PyPI** token.
Package name `voyager` on real PyPI must be confirmed free/owned before
the first upload — check https://pypi.org/project/voyager/ first.
