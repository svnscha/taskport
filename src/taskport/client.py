"""Synchronous SDK with bounded HTTP requests and resumable task handles."""

import hashlib
import os
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx

from taskport.packages import digest_file, pack_directory, safe_name
from taskport.protocol import TERMINAL, guid

TRANSIENT = {408, 429, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, status: int, detail):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class Unavailable(ConnectionError):
    def __init__(self, message, task_id=None):
        self.task_id = task_id
        super().__init__(message + (f" (task {task_id}; resume using this ID)" if task_id else ""))


class WaitTimeout(TimeoutError):
    def __init__(self, task_id):
        self.task_id = task_id
        super().__init__(f"Stopped waiting for task {task_id}; remote execution was not cancelled")


class TaskFailed(RuntimeError):
    def __init__(self, task):
        self.task = task
        self.task_id = task["id"]
        super().__init__(
            f"Task {task['id']} is {task['status']} "
            f"(exit code {task['exit_code']}): {task['error'] or 'see task logs'}"
        )


def check_response(response):
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except (ValueError, AttributeError):
            detail = response.text
        raise ApiError(response.status_code, detail)


@dataclass(frozen=True)
class TaskHandle:
    client: "Client"
    id: str

    def status(self):
        return self.client.status(self.id)

    def wait(self, timeout: float | None = None):
        """Wait for any terminal state; return the full task record."""
        return self.client.wait(self.id, timeout=timeout)

    def result(self, timeout: float | None = None):
        """Return the completed task record or raise TaskFailed."""
        task = self.wait(timeout)
        if task["status"] != "succeeded":
            raise TaskFailed(task)
        return task

    def cancel(self):
        return self.client.cancel(self.id)


class Client:
    def __init__(
        self,
        server="http://127.0.0.1:8080",
        *,
        token=None,
        poll_interval=2.0,
        request_timeout=5.0,
        retry_timeout=30.0,
        transport=None,
    ):
        if poll_interval <= 0 or request_timeout <= 0 or retry_timeout < 0:
            raise ValueError("Polling/request timeouts must be positive; retry timeout nonnegative")
        self.server = server.rstrip("/")
        self.poll_interval = poll_interval
        self.request_timeout = request_timeout
        self.retry_timeout = retry_timeout
        self.http = httpx.Client(
            base_url=self.server,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=request_timeout,
            transport=transport,
            trust_env=False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.http.close()

    @staticmethod
    def _path(path):
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ValueError("API and download paths must be relative to the taskport server")
        return path

    def request(
        self,
        method,
        path,
        *,
        retry_for=None,
        task_id=None,
        file: Path | None = None,
        timeout=None,
        **kwargs,
    ):
        """Retry only operations whose request IDs or semantics make retries safe."""
        path = self._path(path)
        retry_for = self.retry_timeout if retry_for is None else retry_for
        deadline = time.monotonic() + retry_for
        delay = min(0.2, self.poll_interval)
        while True:
            try:
                with file.open("rb") if file else nullcontext(None) as stream:
                    response = self.http.request(
                        method,
                        path,
                        content=stream,
                        timeout=self.request_timeout if timeout is None else timeout,
                        **kwargs,
                    )
                check_response(response)
                return response
            except (httpx.TransportError, ApiError) as exc:
                if isinstance(exc, ApiError) and exc.status not in TRANSIENT:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Unavailable(str(exc), task_id) from exc
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 2.0)

    def functions(self):
        return self.request("GET", "/functions").json()

    def describe(self, function, version=None):
        return self.request(
            "GET", f"/functions/{function}", params={"version": version} if version else {}
        ).json()

    def publish(self, directory):
        with tempfile.TemporaryDirectory(prefix="taskport-package-") as temporary:
            archive = Path(temporary) / "package.zip"
            pack_directory(Path(directory), archive)
            return self.request(
                "POST",
                "/functions",
                file=archive,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": str(archive.stat().st_size),
                    "X-Content-SHA256": digest_file(archive),
                },
            ).json()

    def submit(self, function, *, inputs=None, version=None, request_id=None, **arguments):
        if inputs is not None and arguments:
            raise ValueError("Use either inputs= or keyword arguments")
        task_id = guid(request_id) if request_id else str(uuid4())
        self.request(
            "PUT",
            f"/tasks/{task_id}",
            task_id=task_id,
            json={"function": function, "version": version, "inputs": inputs or arguments},
        )
        return TaskHandle(self, task_id)

    def call(self, function, *, timeout=None, **kwargs):
        return self.submit(function, **kwargs).result(timeout)

    def get_call(self, task_id):
        return TaskHandle(self, guid(task_id))

    def status(self, task_id):
        return self.request("GET", f"/tasks/{guid(task_id)}", task_id=task_id).json()

    def wait(self, task_id, timeout=None):
        task_id = guid(task_id)
        if timeout is not None and timeout < 0:
            raise ValueError("Wait timeout must be nonnegative")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise WaitTimeout(task_id)
            try:
                task = self.request(
                    "GET",
                    f"/tasks/{task_id}",
                    retry_for=0,
                    task_id=task_id,
                    timeout=min(self.request_timeout, remaining)
                    if remaining is not None
                    else self.request_timeout,
                ).json()
                if task["status"] in TERMINAL:
                    return task
            except Unavailable:
                pass
            delay = self.poll_interval
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            time.sleep(delay)

    def cancel(self, task_id):
        return self.request("POST", f"/tasks/{guid(task_id)}/cancel", task_id=task_id).json()

    def retry(self, task_id, *, request_id=None):
        """Explicitly create another execution, pinned to the original task version."""
        task = self.status(task_id)
        if task["status"] not in TERMINAL:
            raise ValueError("Only terminal tasks can be retried")
        return self.submit(
            task["function"],
            version=task["version"],
            inputs=task["inputs"],
            request_id=request_id,
        )

    def upload(self, task_id, lease_token, name, file, *, retry_for=None):
        file = Path(file)
        safe_name(name)
        return self.request(
            "PUT",
            f"/tasks/{guid(task_id)}/artifacts",
            task_id=task_id,
            file=file,
            retry_for=retry_for,
            params={"name": name},
            headers={
                "X-Taskport-Lease": lease_token,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file.stat().st_size),
                "X-Content-SHA256": digest_file(file),
            },
        ).json()

    def download_file(self, url, destination, sha256, *, retry_for=None):
        """Stream to a temporary file, verify the hash, then atomically install it."""
        self._path(url)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4()}.part")
        deadline = time.monotonic() + (self.retry_timeout if retry_for is None else retry_for)
        try:
            while True:
                try:
                    with self.http.stream("GET", url) as response:
                        if response.is_error:
                            response.read()
                            check_response(response)
                        digest = hashlib.sha256()
                        with temporary.open("wb") as stream:
                            for block in response.iter_bytes(1024 * 1024):
                                digest.update(block)
                                stream.write(block)
                            stream.flush()
                            os.fsync(stream.fileno())
                    if digest.hexdigest() != sha256:
                        raise ValueError("Downloaded file checksum does not match")
                    os.replace(temporary, destination)
                    return destination
                except (httpx.TransportError, ApiError) as exc:
                    if isinstance(exc, ApiError) and exc.status not in TRANSIENT:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Unavailable(str(exc)) from exc
                    time.sleep(min(self.poll_interval, remaining))
        finally:
            temporary.unlink(missing_ok=True)

    def download_artifacts(self, task_id, directory):
        task = self.status(task_id)
        root = Path(directory).resolve()
        paths = []
        for artifact in task["artifacts"]:
            name = safe_name(artifact["name"])
            destination = root.joinpath(*name.split("/"))
            if not destination.resolve().is_relative_to(root):
                raise ValueError("Artifact destination escapes its output directory")
            paths.append(self.download_file(artifact["url"], destination, artifact["sha256"]))
        return paths
