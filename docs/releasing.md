# Releasing taskport

The package is published at <https://pypi.org/project/taskport/>. Users install it
with `python -m pip install taskport` on Python 3.11 or newer.

## One-time setup

The GitHub repository is `svnscha/taskport`. Its `pypi` environment accepts release
tags matching `v*`. The workflow uses [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
instead of a stored API token.

For the first release, add a pending publisher at
<https://pypi.org/manage/account/publishing/> with these exact values:

| Field | Value |
| --- | --- |
| PyPI project name | `taskport` |
| GitHub owner | `svnscha` |
| Repository | `taskport` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

PyPI creates the project when the first successful upload uses this publisher.
Later releases reuse it. The publisher is attached to `publish.yml`, which owns
the upload job; the reusable build workflow does not request publishing credentials.
No `PYPI_API_TOKEN` secret is needed.

## Publish a version

1. Update `__version__` in `src/taskport/__init__.py`. Hatch reads package metadata
   from this single version source.
2. Commit and push to `main`, then wait for CI to pass. Write the release notes in
   a local file such as `work/release-notes.md`.
3. Tag that tested commit and create a GitHub release. For version 0.1.0:

   ```console
   git tag -a v0.1.0 -m "taskport 0.1.0"
   git push origin v0.1.0
   gh release create v0.1.0 --verify-tag --title "taskport 0.1.0" --notes-file work/release-notes.md
   ```

`publish.yml` checks that the tag matches the package version, builds a wheel and
source distribution, validates their metadata and README, and tests the installed
wheel on Windows, Linux, and macOS with Python 3.11 through 3.14. Only after every
matrix job passes does the `pypi` job obtain a short-lived publishing credential.
The same distributions are uploaded to PyPI and attached to the GitHub release.
The publishing workflow is pinned to the release tag's commit.

Inspect progress with `gh run list --workflow publish.yml` and
`gh run view <run-id>`. Once published, verify from a fresh virtual environment:

```console
python -m pip install --index-url https://pypi.org/simple taskport==0.1.0
taskport --help
python -m taskport --help
```

If a run fails before upload, fix the cause and rerun its failed jobs with
`gh run rerun <run-id> --failed`. To start another attempt at an existing release,
use `gh workflow run publish.yml --ref v0.1.0`. Do not dispatch against `main`:
the version check and environment tag policy reject it.

PyPI release files cannot be replaced. If code needs changing after publication,
use a new version and tag. If PyPI succeeded but attaching GitHub assets failed,
rerun only the failed job; avoid republishing distributions that already exist.

## Local checks

```console
python -m pip install -e '.[dev]'
python -m ruff check src tests examples
python -m ruff format --check src tests examples
python -m pytest -q -p no:cacheprovider
python -m build
python -m twine check --strict dist/*
```

Build outputs, environments, and working files stay outside version control.
