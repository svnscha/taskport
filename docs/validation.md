# Validation

Validated on 2026-09-08 on Windows with Python 3.12.14 and PowerShell 7.
The exact Python dependency snapshot is in `requirements-dev.lock.txt`.

## Results

- **30 tests passed**, including 8 end-to-end tests using real local HTTP sockets,
  server/worker subprocesses, and script execution.
- Ruff lint and formatting checks passed.
- All repository text files passed the LF/no-UTF-8-BOM check. Git reports `eol=lf`
  for Python, PowerShell, and Markdown files.
- The source distribution and wheel built successfully with `python -m build --no-isolation`.
- The wheel was installed into a separate target directory; its own installed
  package was imported and its HTTP server and CLI were smoke-tested.
- `pip check` found no broken requirements.

The full suite completed in about 32 seconds on the validation machine. It emitted
two upstream deprecation warnings from Starlette's HTTPX-based TestClient and its
AnyIO type alias. There were no skipped tests; PowerShell was installed.

## End-to-end scenarios

1. Two workers compete for queued calls, execute only one call each at a time,
   and run the package version pinned when each call was submitted. CLI calls,
   discovery, and artifact downloads work. Results survive stopping all workers
   and restarting the server.
2. A waiting client reconnects after the server is stopped and restarted during
   script execution. The existing call completes without resubmission.
3. Nonzero script exits and execution timeouts produce failed calls with logs.
   An explicit retry gets a new GUID and retains the original package version.
4. Cancelling a running call stops its script; the same worker accepts the next call.
5. A dead worker becomes `lost` after lease expiry. Another worker does not
   automatically execute the lost call.
6. A real PowerShell task receives strings containing spaces, quotes, dollar signs,
   and shell-looking text literally, and returns matching artifact contents.
7. Heartbeats keep a script alive beyond the server's lease duration.
8. A worker clones an actual local Git repository, checks out an earlier requested
   commit, compiles its Python source, and uploads a ZIP. The client downloads the
   original revision and compiled bytecode even though the repository has a newer commit.

API/SDK regression tests additionally cover lost submission acknowledgments,
replayed claim IDs, transactional contention, input validation, immutable publication,
stale completion rejection, cancellation, authentication, upload checksums, unsafe
ZIP paths, portable filenames, partial-download retries, and waiting deadlines.

## Reproduce

```powershell
python -m pip install -r requirements-dev.lock.txt
python -m pip install --no-deps --no-build-isolation -e .
python -m ruff check src tests examples
python -m ruff format --check src tests examples
python -m pytest -q -p no:cacheprovider
python -m build --no-isolation
```

Tests use local repositories and loopback networking. Windows process tests need
permission to terminate their own process trees with `taskkill /T`; the restricted
agent sandbox denied that operation, so the full suite was run with local process
permissions. The application was not deployed as an OS service and no firewall,
global Git, or machine-wide Python configuration was changed.

Linux/macOS execution and multiple physical machines were not exercised in this
environment. TaskPort's single-server and explicit-retry limitations are documented
in the README and protocol document.
