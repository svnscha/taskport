"""SQLite is the authority for submissions, ownership, and completion.

Every state change uses a short BEGIN IMMEDIATE transaction. No network transfer
or task execution occurs while a transaction is open.
"""

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid5

from taskport.protocol import Completion, Manifest, Problem, Submission, canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS definitions (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    manifest TEXT NOT NULL,
    digest TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS functions (
    name TEXT PRIMARY KEY,
    latest_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    function TEXT NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    submission TEXT NOT NULL,
    function TEXT NOT NULL,
    version TEXT NOT NULL,
    inputs TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('queued', 'running', 'succeeded', 'failed', 'lost', 'cancelled')),
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    worker_id TEXT,
    claim_id TEXT UNIQUE,
    lease_token TEXT,
    lease_expires REAL,
    completion TEXT,
    error TEXT,
    FOREIGN KEY (function, version) REFERENCES definitions(name, version)
);
CREATE INDEX IF NOT EXISTS task_queue ON tasks(function, status, created);
CREATE UNIQUE INDEX IF NOT EXISTS worker_slot ON tasks(worker_id) WHERE status='running';
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    name TEXT NOT NULL COLLATE NOCASE,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    UNIQUE (task_id, name)
);
"""


class Store:
    def __init__(self, data: Path, lease_seconds: float = 60):
        if lease_seconds < 3:
            raise ValueError("Lease duration must be at least 3 seconds")
        self.data = data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.path = self.data / "taskport.sqlite"
        self.lease_seconds = lease_seconds
        for name in ("packages", "artifacts", "incoming"):
            (self.data / name).mkdir(exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _expire(connection, now):
        connection.execute(
            "UPDATE tasks SET status='lost', finished=?, error=? "
            "WHERE status='running' AND lease_expires <= ?",
            (
                now,
                "Worker heartbeat expired. Execution state is unknown; retry explicitly if safe.",
                now,
            ),
        )

    def expire(self):
        with self.transaction() as connection:
            self._expire(connection, time.time())

    @staticmethod
    def _task(connection, task_id):
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise Problem(404, "Task not found")
        return row

    @staticmethod
    def _artifact_view(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "size": row["size"],
            "sha256": row["digest"],
            "url": f"/tasks/{row['task_id']}/artifacts/{row['id']}",
        }

    def _view(self, connection, row):
        completion = json.loads(row["completion"]) if row["completion"] else {}
        return {
            "id": row["id"],
            "function": row["function"],
            "version": row["version"],
            "inputs": json.loads(row["inputs"]),
            "status": row["status"],
            "created": row["created"],
            "started": row["started"],
            "finished": row["finished"],
            "worker_id": row["worker_id"],
            "exit_code": completion.get("exit_code"),
            "result": completion.get("result"),
            "error": row["error"],
            "artifacts": [
                self._artifact_view(item)
                for item in connection.execute(
                    "SELECT * FROM artifacts WHERE task_id=? ORDER BY name", (row["id"],)
                )
            ],
        }

    @staticmethod
    def _definition(connection, name, version=None):
        if version is None:
            latest = connection.execute(
                "SELECT latest_version FROM functions WHERE name=?", (name,)
            ).fetchone()
            if latest:
                version = latest[0]
        row = connection.execute(
            "SELECT * FROM definitions WHERE name=? AND version=?", (name, version)
        ).fetchone()
        if row is None:
            raise Problem(404, "Function or version not found")
        return row

    def publish(self, manifest: Manifest, digest: str):
        document = canonical(manifest.model_dump())
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM definitions WHERE name=? AND version=?",
                (manifest.name, manifest.version),
            ).fetchone()
            if existing:
                if existing["digest"] != digest or existing["manifest"] != document:
                    raise Problem(409, "This version is immutable; publish a new version")
                # Retrying an old publication must not move the latest pointer backwards.
            else:
                connection.execute(
                    "INSERT INTO definitions VALUES (?, ?, ?, ?, ?)",
                    (manifest.name, manifest.version, document, digest, time.time()),
                )
                connection.execute(
                    "INSERT INTO functions VALUES (?, ?) ON CONFLICT(name) "
                    "DO UPDATE SET latest_version=excluded.latest_version",
                    (manifest.name, manifest.version),
                )
        return self.describe(manifest.name, manifest.version)

    def describe(self, name, version=None):
        with self.transaction() as connection:
            row = self._definition(connection, name, version)
            return {
                **json.loads(row["manifest"]),
                "package_sha256": row["digest"],
                "package_url": f"/packages/{row['digest']}",
            }

    def functions(self):
        with self.transaction() as connection:
            now = time.time()
            self._expire(connection, now)
            results = []
            for item in connection.execute("SELECT * FROM functions ORDER BY name").fetchall():
                name = item["name"]
                definition = self._definition(connection, name)
                workers = connection.execute(
                    "SELECT COUNT(*) FROM workers WHERE function=? AND last_seen>?",
                    (name, now - self.lease_seconds),
                ).fetchone()[0]
                busy = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE function=? AND status='running'", (name,)
                ).fetchone()[0]
                queued = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE function=? AND status='queued'", (name,)
                ).fetchone()[0]
                results.append(
                    {
                        "name": name,
                        "latest_version": item["latest_version"],
                        "description": json.loads(definition["manifest"])["description"],
                        "versions": [
                            v[0]
                            for v in connection.execute(
                                "SELECT version FROM definitions WHERE name=? ORDER BY created",
                                (name,),
                            )
                        ],
                        "online_workers": workers,
                        "busy_workers": busy,
                        "queued_calls": queued,
                    }
                )
            return results

    def submit(self, task_id: str, submission: Submission):
        request = canonical(submission.model_dump())
        with self.transaction() as connection:
            existing = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if existing:
                if existing["submission"] != request:
                    raise Problem(409, "Request ID already exists with different arguments")
                return self._view(connection, existing)
            definition = self._definition(connection, submission.function, submission.version)
            manifest = Manifest.model_validate_json(definition["manifest"])
            try:
                manifest.validate_arguments(submission.inputs)
            except Exception as exc:
                raise Problem(422, f"Invalid task arguments: {exc}") from exc
            connection.execute(
                "INSERT INTO tasks (id, submission, function, version, inputs, status, created) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                (
                    task_id,
                    request,
                    submission.function,
                    definition["version"],
                    canonical(submission.inputs),
                    time.time(),
                ),
            )
            return self._view(connection, self._task(connection, task_id))

    def get(self, task_id):
        with self.transaction() as connection:
            self._expire(connection, time.time())
            return self._view(connection, self._task(connection, task_id))

    def tasks(self, limit=100):
        with self.transaction() as connection:
            self._expire(connection, time.time())
            return [
                self._view(connection, row)
                for row in connection.execute(
                    "SELECT * FROM tasks ORDER BY created DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def claim(self, worker_id, function, request_id):
        now = time.time()
        with self.transaction() as connection:
            self._expire(connection, now)
            self._definition(connection, function)
            worker = connection.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
            if worker and worker["function"] != function:
                raise Problem(409, "A worker ID can only serve one function")
            connection.execute(
                "INSERT INTO workers VALUES (?, ?, ?) ON CONFLICT(id) "
                "DO UPDATE SET last_seen=excluded.last_seen",
                (worker_id, function, now),
            )
            previous = connection.execute(
                "SELECT * FROM tasks WHERE claim_id=?", (request_id,)
            ).fetchone()
            if previous:
                if previous["worker_id"] != worker_id or previous["function"] != function:
                    raise Problem(409, "Claim request ID already belongs to another worker")
                if previous["status"] != "running":
                    raise Problem(409, "This claim is no longer active")
                row = previous
            else:
                if connection.execute(
                    "SELECT 1 FROM tasks WHERE worker_id=? AND status='running'", (worker_id,)
                ).fetchone():
                    raise Problem(409, "Worker already owns its one execution slot")
                row = connection.execute(
                    "SELECT * FROM tasks WHERE function=? AND status='queued' "
                    "ORDER BY created, id LIMIT 1",
                    (function,),
                ).fetchone()
                if row is None:
                    return None
                connection.execute(
                    "UPDATE tasks SET status='running', started=?, worker_id=?, claim_id=?, "
                    "lease_token=?, lease_expires=? WHERE id=? AND status='queued'",
                    (
                        now,
                        worker_id,
                        request_id,
                        secrets.token_urlsafe(32),
                        now + self.lease_seconds,
                        row["id"],
                    ),
                )
                row = self._task(connection, row["id"])
            definition = self._definition(connection, row["function"], row["version"])
            return {
                "task": self._view(connection, row),
                "manifest": json.loads(definition["manifest"]),
                "package_sha256": definition["digest"],
                "package_url": f"/packages/{definition['digest']}",
                "lease_token": row["lease_token"],
                "lease_seconds": self.lease_seconds,
                "lease_remaining": max(0, row["lease_expires"] - now),
            }

    @staticmethod
    def _check_lease(row, token):
        if (
            row["status"] != "running"
            or not secrets.compare_digest(row["lease_token"] or "", token)
            or row["lease_expires"] <= time.time()
        ):
            raise Problem(409, "Execution lease is no longer valid")

    def validate_lease(self, task_id, token):
        with self.transaction() as connection:
            self._check_lease(self._task(connection, task_id), token)

    def heartbeat(self, task_id, token):
        now = time.time()
        with self.transaction() as connection:
            row = self._task(connection, task_id)
            self._check_lease(row, token)
            connection.execute(
                "UPDATE tasks SET lease_expires=? WHERE id=?",
                (now + self.lease_seconds, task_id),
            )
            connection.execute("UPDATE workers SET last_seen=? WHERE id=?", (now, row["worker_id"]))
        return {"lease_seconds": self.lease_seconds}

    def complete(self, task_id, token, completion: Completion):
        document = canonical(completion.model_dump())
        with self.transaction() as connection:
            row = self._task(connection, task_id)
            if row["completion"] is not None and secrets.compare_digest(
                row["lease_token"] or "", token
            ):
                if row["completion"] != document:
                    raise Problem(409, "Task already completed with a different result")
                return self._view(connection, row)
            self._check_lease(row, token)
            status = "succeeded" if completion.exit_code == 0 and not completion.error else "failed"
            connection.execute(
                "UPDATE tasks SET status=?, completion=?, error=?, finished=? WHERE id=?",
                (status, document, completion.error, time.time(), task_id),
            )
            return self._view(connection, self._task(connection, task_id))

    def cancel(self, task_id):
        with self.transaction() as connection:
            row = self._task(connection, task_id)
            if row["status"] in {"queued", "running"}:
                connection.execute(
                    "UPDATE tasks SET status='cancelled', finished=?, error=? WHERE id=?",
                    (time.time(), "Cancelled by client", task_id),
                )
            return self._view(connection, self._task(connection, task_id))

    def add_artifact(self, task_id, token, name, digest, size):
        with self.transaction() as connection:
            self._check_lease(self._task(connection, task_id), token)
            artifact_id = str(uuid5(UUID(task_id), name.casefold()))
            existing = connection.execute(
                "SELECT * FROM artifacts WHERE task_id=? AND (id=? OR name=?)",
                (task_id, artifact_id, name),
            ).fetchone()
            if existing:
                if existing["name"] != name:
                    raise Problem(409, "Artifact names must be unique ignoring case")
                if existing["digest"] != digest or existing["size"] != size:
                    raise Problem(409, "An artifact with this name already has different contents")
                return self._artifact_view(existing)
            connection.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?)",
                (artifact_id, task_id, name, digest, size),
            )
            return self._artifact_view(
                connection.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            )

    def artifact(self, task_id, artifact_id):
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE task_id=? AND id=?", (task_id, artifact_id)
            ).fetchone()
            if row is None:
                raise Problem(404, "Artifact not found")
            return dict(row)

    def workers(self):
        with self.transaction() as connection:
            now = time.time()
            self._expire(connection, now)
            return [
                {
                    "id": row["id"],
                    "function": row["function"],
                    "online": row["last_seen"] > now - self.lease_seconds,
                    "last_seen": row["last_seen"],
                    "task_id": row["task_id"],
                }
                for row in connection.execute(
                    "SELECT workers.*, tasks.id AS task_id FROM workers LEFT JOIN tasks "
                    "ON workers.id=tasks.worker_id AND tasks.status='running' "
                    "ORDER BY workers.last_seen DESC"
                )
            ]
