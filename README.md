# TaskPort

Run named functions on intranet machines and collect their results and files on one server.

**One Python server. One task name and one execution slot per worker process.**

The server owns immutable task packages (a manifest plus scripts), a SQLite queue,
results, and artifacts. Workers download the assigned package version and execute
it. Clients discover available functions, submit calls, and wait by polling a task GUID.
Every HTTP request is independent; disconnecting a client does not cancel its task.

TaskPort uses FastAPI, SQLite, and HTTPX. It does not require RabbitMQ, Redis,
ZeroMQ, Docker, a shared worker filesystem, or a CI server.

## Quick start

Requires Python 3.11+ on the server, workers, and Python clients. These commands
use PowerShell on Windows; the same CLI works on other platforms. This repository
has been tested on Windows with Python 3.12 and PowerShell 7.

Install from this checkout on each participating machine:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\Activate.ps1
```

Alternatively, install the wheel from `dist/` after building it. Each worker also
needs the tools used by its function, such as PowerShell, Git, or a compiler.
TaskPort itself does not install task-specific tools.

**1. Start the server.** For a local demonstration:

```powershell
taskport serve --data ./data
```

For intranet access, choose a token and set it in every participating terminal:

```powershell
$env:TASKPORT_TOKEN = 'replace-with-a-long-random-secret'
taskport serve --host 0.0.0.0 --port 8080 --data ./data
```

Keep `data` on the server's local disk. On workers and clients, set:

```powershell
$env:TASKPORT_SERVER = 'http://buildbox:8080'
$env:TASKPORT_TOKEN = 'replace-with-a-long-random-secret'
```

For the local demonstration, the default address is `http://127.0.0.1:8080`
and no token is needed. The server requires a token when binding beyond loopback,
unless `--allow-unauthenticated` is explicitly supplied.

**2. Publish a task package to the server.** The included echo task needs only Python:

```powershell
taskport functions publish ./examples/echo
taskport functions list
taskport functions describe echo
```

**3. Start workers in separate terminals or on separate machines.** Both serve the same task:

```powershell
taskport worker --task echo
```

Each worker process has exactly one slot. Run another process to add another
slot. A worker requires its task name and server connection; it downloads scripts
from the server automatically. `--work-dir` selects its local cache and working area.
On Windows, choose a short path such as `--work-dir C:/tp-work` when build tools
have path-length limits. The Git build example enables Git's long-path support.

**4. Call it and retrieve files:**

```powershell
taskport call echo --message 'Hello from a remote worker' --wait
taskport download <task-guid> --output ./downloads
```

`call` prints the GUID to stderr before submission. Its final JSON includes status,
return data, exit code, and artifact URLs. Downloads contain `message.txt` plus
`_logs/stdout.log` and `_logs/stderr.log`.

## Calls, waiting, and reconnecting

```powershell
taskport call echo --message hello                  # Submit and return its GUID
taskport status <task-guid>
taskport wait <task-guid> --timeout 600
taskport tasks
taskport workers
taskport cancel <task-guid>
taskport retry <task-guid>                          # A NEW execution and GUID
```

`call --wait` and `wait` poll with short HTTP requests. A temporary connection
failure is retried while waiting. `--timeout` ends the wait without cancelling
execution; omitting it waits until a terminal status or user interruption.
Use the printed GUID to resume from a different process or device.

If the initial submission response is lost, repeat the same invocation with
`--request-id <original-guid>`. The server deduplicates that GUID and verifies
the original function, version selection, and inputs. Reusing an ID with different
arguments returns a conflict. Do not generate a new GUID to recover an uncertain submission.

CLI exit codes: `0` for success, `1` for failed/lost/cancelled execution or an API
error, `2` for argparse usage errors, `124` for a waiting timeout, `130` for an
interrupted client. JSON results are written to stdout; progress/errors to stderr.

## Python client

```python
from taskport import Client

with Client("http://buildbox:8080", token="your-token") as client:
    # Returns a full task record: result, artifacts, exit_code, status, etc.
    completed = client.call("echo", message="hello", timeout=600)
    print(completed["result"])
    client.download_artifacts(completed["id"], "downloads")

    # Keep the handle when submission and waiting should be separate.
    call = client.submit("echo", message="another message")
    print(call.id)
    completed = call.result(timeout=600)

    # A saved GUID can be used after restarting the client.
    completed = client.get_call(call.id).result(timeout=600)
```

`TaskHandle.wait()` returns any terminal task record. `TaskHandle.result()` and
`Client.call()` raise `TaskFailed` for failed, lost, or cancelled calls; the exception
contains `.task` and `.task_id`. `WaitTimeout` and `Unavailable` expose a `.task_id`
when applicable. `Client` accepts `poll_interval`, `request_timeout`, and
`retry_timeout`; defaults are 2, 5, and 30 seconds. The submission retry window is
separate from waiting for an accepted task.

For names that conflict with SDK options, use `inputs={...}`. On the CLI,
`--input-json '{"message":"hello"}'`, `--input-json @inputs.json`, and
`--arg message=hello` are also supported. Task argument flags are discovered from
the server schema. JSON Schema defaults are descriptive; provide values explicitly.

## Defining functions

Create a directory containing `task.json`, a script, and any supporting files:

```json
{
  "name": "build",
  "version": "1",
  "description": "Build a particular revision",
  "command": ["pwsh", "-NoProfile", "-File", "Build.ps1", "-Hash", "{hash}"],
  "inputs": {
    "type": "object",
    "properties": {"hash": {"type": "string"}},
    "required": ["hash"],
    "additionalProperties": false
  },
  "timeout_seconds": 3600
}
```

Publish with `taskport functions publish ./my-build-task`. A version is immutable:
change `version` before changing scripts or metadata. Repeating an identical
publication is safe. The most recently published **new** version becomes the default;
versions are labels, not an automatically sorted semantic-version range.

Each submitted call is pinned to a version before acknowledging acceptance.
Updating the default does not change queued or running calls. Use
`taskport call build --version 1 ...` to explicitly select a version.

Workers configured for `build` serve all its versions, downloading and verifying
each package by SHA-256. Keep the tools needed by those versions installed.
Client schemas and scripts always come from the same server-owned definition.

Commands run in a fresh extracted package directory with an argument list and
`shell=False`. Input placeholders occupy literal argument strings; they are not
evaluated as shell commands. Use an explicit interpreter for scripts. Available
reserved placeholders are `{python}`, `{inputs}`, `{output}`, and `{result}`.
`{python}` selects the Python interpreter running the worker.

Every script receives:

| Environment variable | Meaning |
| --- | --- |
| `TASKPORT_TASK_ID` | Call GUID |
| `TASKPORT_WORKER_ID` | Worker process identity |
| `TASKPORT_INPUTS_FILE` | UTF-8 JSON file containing the submitted inputs |
| `TASKPORT_OUTPUT_DIR` | Fresh directory for arbitrary output files |
| `TASKPORT_RESULT_FILE` | Optional UTF-8 JSON return value written by the script |

The worker uploads the output directory recursively and captures stdout/stderr
under `_logs/`. The script's exit code determines success; a timeout, invalid return
JSON, or an artifact transfer failure makes the call fail. A failed script's available
logs and outputs are still uploaded. `_logs` is reserved; outputs cannot contain
symlinks, junctions, or filenames that are unsafe on Windows.

Scripts must wait for their own work before exiting. TaskPort is not a sandbox:
scripts run as the worker's OS account and can use its installed tools and credentials.
The TaskPort token is not passed through the script environment.

## Examples

- `examples/echo`: Python-only round trip with a return value and text artifact.
- `examples/powershell`: PowerShell 7 script with literal argument passing.
- `examples/python-build`: Fetch an exact Git commit, compile Python sources,
  and produce a ZIP containing sources and bytecode. Requires Git on the worker.

```powershell
taskport functions publish ./examples/python-build
taskport worker --task python-build
# In another terminal:
taskport call python-build --repository 'https://your-git-server/repo.git' --hash <full-commit-hash> --wait
```

The Git remote must contain the requested commit and be reachable from the worker.
Uncommitted client changes are not included. Replace or extend these example scripts
to implement your builds, tests, or other functions; the server has no build-specific logic.

## Reliability and operation

- SQLite commits submissions before acknowledging them. Client retry IDs prevent
  duplicate submission after a lost acknowledgment.
- A transaction claims one queued task and occupies one worker slot together.
  Claim requests also have retry IDs, so a lost claim response cannot reserve a second task.
- Workers renew execution leases with short heartbeat requests, including during
  package downloads and artifact uploads. The default lease is 60 seconds.
- Expired leases become `lost`, meaning the server cannot confirm execution state.
  They are **not automatically requeued**. `retry` explicitly starts another execution.
- Attempt tokens reject stale uploads and completions. Cancellation invalidates
  the lease; a connected worker stops its process tree when it detects cancellation.
- Artifacts are streamed to temporary files, checked, then published. A task becomes
  successful only after its outputs have uploaded. Downloads verify SHA-256 and
  atomically replace their local destination; interrupted downloads restart from the beginning.
- A short server outage can be tolerated within the execution lease. A longer outage
  results in lost executions. Clients can reconnect independently and read persisted results.

This handles ordinary client disconnects and process restarts. It does not promise
exactly-once external side effects, automatic server failover, or recovery from destroyed
server storage. A forcibly killed worker may leave child processes; a lost connection
does not prove a process has stopped. Check before retrying functions with side effects.

Run one server process, backed by local disk, under your normal service supervisor.
No build work runs in the HTTP server. The SQLite file, packages, and artifacts all
live under `--data`; back up that entire directory with the server stopped.
SQLite's `-wal` and `-shm` files are part of its active state.

Artifacts and task history are retained indefinitely in this first version. Monitor
disk space. Failed/crashed uploads can leave unreferenced blobs or `.part` files;
there is no automatic garbage collector. Workers delete acknowledged run directories
by default, preserve uncertain executions, and keep package caches. `--keep-work`
preserves every execution workspace. A cleanup error is logged and leaves the
remaining workspace for manual cleanup; it does not stop the worker.

Default limits: 64 MiB compressed/expanded task packages, 2,048 package files,
2,048 output files including logs, 1 GiB per artifact, and 1 MiB JSON return values.
Use `--max-artifact-mib` to change the server artifact limit.

The shared token grants full access, including publishing executable task packages.
Use this with trusted users and workers. Use HTTPS through a reverse proxy on networks
where bearer tokens and code should not travel in clear text. Interactive API docs
are at `/docs`; the wire contract is documented in [docs/protocol.md](docs/protocol.md).

## Development and verification

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build
```

`requirements-dev.lock.txt` records the exact dependency versions used for validation.
To reproduce that environment, install it first, then run
`python -m pip install --no-build-isolation --no-deps -e .`.

Tests exercise the API and real HTTP server/worker subprocesses. Coverage includes
competing claims, lost acknowledgment retries, version pinning, reconnects during
a server restart, long-running heartbeats, cancellation, worker loss, execution
timeouts, binary transfers, PowerShell execution (when installed), and an actual
Git-revision build. Process tests need permission to bind loopback ports and terminate
their own child process trees. They do not contact external Git servers.

`.gitattributes` and `.editorconfig` enforce UTF-8/LF text conventions. Runtime data,
virtual environments, test scratch files, and distribution outputs are ignored.

See [docs/validation.md](docs/validation.md) for the tested environment, scenarios,
and results.
