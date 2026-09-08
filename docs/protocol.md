# taskport HTTP protocol

All endpoints use JSON except package and artifact transfers. A configured bearer
token is required on every API request. HTTP errors use `{"detail": ...}`.
Times are Unix seconds. Clients and workers should use finite per-request timeouts.

## Definitions

| Method | Path | Meaning |
| --- | --- | --- |
| GET | `/health` | Liveness and version |
| POST | `/functions` | Upload a ZIP containing `task.json` and scripts |
| GET | `/functions` | Catalog, published versions, online/busy workers, queue counts |
| GET | `/functions/{name}?version=1` | Manifest plus package URL and SHA-256; omit version for latest |
| GET | `/packages/{sha256}` | Download an immutable ZIP |

Publication reads the manifest from the ZIP, validates JSON Schema and portable
filenames, and saves the ZIP before committing its definition. Package names and
version labels are case-sensitive. Repeated publication with identical bytes is
idempotent. Different bytes under the same name/version return `409`.

Publishing another version advances the default for future calls. Repeating an
old publication does not move the default backwards. Existing calls always retain
their resolved version, including when a lost submission acknowledgment is retried.

Input schemas use JSON Schema Draft 2020-12 and have an object root. External
schema references are forbidden. Schema defaults do not mutate submitted inputs.
An argument used in a command placeholder must be supplied.

## Calls

| Method | Path | Meaning |
| --- | --- | --- |
| PUT | `/tasks/{client-generated-guid}` | Persist a submission; returns `202` |
| GET | `/tasks/{guid}` | State, inputs, result, error, timestamps, artifact metadata |
| GET | `/tasks?limit=100` | Recent calls, up to 1,000 |
| POST | `/tasks/{guid}/cancel` | Cancel a queued/running call; idempotent |
| GET | `/tasks/{guid}/artifacts/{artifact-guid}` | Download one finalized artifact |

Submission body:

```json
{"function": "build", "version": null, "inputs": {"hash": "..."}}
```

The server atomically inserts the GUID and canonical original request, resolving
the function version at insertion. If that GUID already exists, the original
request must match exactly in meaning, including whether version was supplied.
Matching requests return the original call; mismatches return `409`.

A call progresses through:

```text
queued -> running -> succeeded
                  -> failed
                  -> lost
queued or running -> cancelled
```

`lost` means the lease expired; actual external execution state is unknown.
Terminal calls are immutable. A retry creates a new GUID and new execution using
the original resolved version and inputs. No implicit retry or cache-by-arguments
is performed. A fresh GUID deliberately requests fresh work.

Results are retained by the server. Waiting is repeated `GET` requests and is
independent of task execution. A client disconnect or wait timeout is not cancellation.
Cancellation immediately invalidates task ownership, but worker process termination
is cooperative with its next heartbeat/lease check, not instantaneous or guaranteed
for an unreachable or forcibly stopped worker.

## Workers and ownership

| Method | Path | Meaning |
| --- | --- | --- |
| GET | `/workers` | Registered workers, recent availability, current task |
| POST | `/workers/{worker-guid}/claim` | Claim one call for one function; `204` if none |
| POST | `/tasks/{guid}/heartbeat` | Renew a valid execution lease |
| PUT | `/tasks/{guid}/artifacts?name=relative/path.bin` | Upload raw artifact bytes |
| POST | `/tasks/{guid}/complete` | Commit the result and terminal state |

Claim body:

```json
{"function": "build", "request_id": "<claim-request-guid>"}
```

Each worker process generates one identity and serves one function name. Claims
use `BEGIN IMMEDIATE` to select and assign the oldest compatible queued call in a
single transaction. A partial unique index also enforces at most one running call
per worker identity. Worker claim polling doubles as an idle heartbeat.

Retry the **same claim request GUID** after a connection failure. If an assignment
was already committed, the server returns it instead of assigning a second call.
After a received `204`, or after finishing execution, use a new claim request GUID.

An assigned response includes the task record, manifest, package URL/hash, an opaque
`lease_token`, `lease_seconds`, and remaining lease time. Subsequent worker writes
require `X-Taskport-Lease: <token>`. Renewals are independent short HTTP requests.
The server checks the token, state, and unexpired lease in a transaction on every write.

The worker downloads/verifies the package, executes it in a fresh workspace, and
continues heartbeats through execution and uploads. If it loses ownership or its
local lease deadline elapses, it stops execution and does not publish a completion.
The server never automatically reassigns an expired task.

Completion body:

```json
{"exit_code": 0, "result": {"answer": 42}, "error": null}
```

An identical completion retried with the original token returns the existing
result, including after the lease is no longer active. A changed completion,
wrong token, or stale attempt returns `409`. Successful completion requires a
zero exit code and no error. The worker uploads artifacts before completion.

## File transfers

Packages and artifacts use raw request bodies, not JSON/base64. A client can send
`X-Content-SHA256`; the server checks it before publication. Uploads are size-limited
and streamed to temporary files under the server data directory, then flushed and
installed under a content hash before SQLite references them.

Artifact ownership is checked both before receiving the body and after the upload.
Each task/name pair is immutable and upload retries with matching contents are
idempotent. Names are portable relative paths, unique ignoring case. Logical names
do not become filesystem paths on the server. Download metadata includes an opaque
artifact GUID, its name, byte size, SHA-256, and an HTTP path on this same server.

Concurrent uploads of identical bytes reuse the same immutable blob. Files are
published before database references, so a crash can leave an unreferenced file,
but normal process restart cannot expose a database record for an unfinished upload.
Clients verify hashes and install local downloads atomically. The current SDK
restarts an interrupted file transfer; it does not resume byte ranges.

## Deployment boundary

Run one server process with SQLite and files on its local disk. Clients and workers
access only its HTTP API. Persistence survives ordinary server process restarts;
availability depends on that server being reachable. Leases expire during extended
outages. SQLite and filesystem persistence do not provide a guarantee against disk
loss, external side effects happening twice, or arbitrary manual changes to stored data.
