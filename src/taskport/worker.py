"""One worker process serves one named function and executes one call at a time."""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from taskport.client import ApiError, Client, Unavailable
from taskport.packages import digest_file, extract_package, is_link, safe_name
from taskport.protocol import PLACEHOLDER, Completion, Manifest, canonical


class ProcessCleanupError(RuntimeError):
    """Cannot establish that a task's process tree has stopped."""


def stop_process_tree(process):
    """Cancel the script and its compiler/test descendants."""
    if os.name == "nt":
        if process.poll() is None:
            result = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=15,
                check=False,
            )
            if result.returncode and process.poll() is None:
                raise ProcessCleanupError("Windows denied process-tree termination")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


class LeaseKeeper:
    def __init__(self, server, token, claim):
        self.task_id = claim["task"]["id"]
        self.lease_token = claim["lease_token"]
        self.seconds = claim["lease_seconds"]
        self.deadline = time.monotonic() + claim["lease_remaining"]
        self.lost = threading.Event()
        self.stopped = threading.Event()
        self.client = Client(
            server,
            token=token,
            request_timeout=min(2.0, self.seconds / 4),
            retry_timeout=0,
        )
        self.thread = threading.Thread(target=self._run, name="taskport-heartbeat", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=5)
        self.client.close()

    def valid(self):
        return not self.lost.is_set() and time.monotonic() < self.deadline

    def _run(self):
        delay = 0
        while not self.stopped.wait(delay):
            started = time.monotonic()
            if not self.valid():
                self.lost.set()
                return
            try:
                self.client.request(
                    "POST",
                    f"/tasks/{self.task_id}/heartbeat",
                    headers={"X-Taskport-Lease": self.lease_token},
                    retry_for=0,
                )
                self.deadline = started + self.seconds
                delay = min(10, self.seconds / 3)
            except Unavailable:
                delay = min(0.5, self.seconds / 6)
            except ApiError:
                self.lost.set()
                return


class Worker:
    def __init__(
        self,
        server,
        task,
        *,
        token=None,
        work_dir=".taskport-worker",
        poll_interval=2.0,
        keep_work=False,
    ):
        self.server = server
        self.token = token
        self.task = task
        self.id = str(uuid4())
        self.root = Path(work_dir).resolve() / self.id
        self.cache = self.root / "cache"
        self.runs = self.root / "runs"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.runs.mkdir(parents=True, exist_ok=True)
        self.poll_interval = poll_interval
        self.keep_work = keep_work
        self.stopped = threading.Event()
        self.client = Client(server, token=token, poll_interval=poll_interval)

    def stop(self):
        self.stopped.set()

    @staticmethod
    def log(message):
        print(message, file=sys.stderr, flush=True)

    def run(self, *, once=False):
        self.log(f"Worker {self.id}: function={self.task}, slots=1")
        claim_id = str(uuid4())
        try:
            while not self.stopped.is_set():
                started = time.monotonic()
                try:
                    response = self.client.request(
                        "POST",
                        f"/workers/{self.id}/claim",
                        json={"request_id": claim_id, "function": self.task},
                        retry_for=0,
                    )
                except Unavailable:
                    # Keep the claim ID: a lost response may already have reserved a task.
                    self.stopped.wait(self.poll_interval)
                    continue
                except ApiError as exc:
                    if exc.status != 409:
                        raise
                    self.log(str(exc))
                    claim_id = str(uuid4())
                    self.stopped.wait(self.poll_interval)
                    continue
                if response.status_code == 204:
                    if once:
                        return
                    claim_id = str(uuid4())
                    self.stopped.wait(self.poll_interval)
                    continue
                claim = response.json()
                claim["lease_remaining"] -= time.monotonic() - started
                self.execute(claim)
                claim_id = str(uuid4())
                if once:
                    return
        finally:
            self.client.close()

    def execute(self, claim):
        task = claim["task"]
        task_id = task["id"]
        self.log(f"Starting {task_id}: {task['function']}@{task['version']}")
        lease = LeaseKeeper(self.server, self.token, claim)
        lease.start()
        run_dir = self.runs / task_id
        package_dir = run_dir / "package"
        output_dir = run_dir / "output"
        logs = run_dir / "logs"
        output_dir.mkdir(parents=True)
        logs.mkdir()
        stdout_path, stderr_path = logs / "stdout.log", logs / "stderr.log"
        stdout_path.touch()
        stderr_path.touch()
        recorded = False
        try:
            completion = Completion(exit_code=127)
            try:
                archive = self.cache / f"{claim['package_sha256']}.zip"
                if not archive.is_file() or digest_file(archive) != claim["package_sha256"]:
                    self.client.download_file(
                        claim["package_url"], archive, claim["package_sha256"]
                    )
                manifest = extract_package(archive, package_dir)
                if manifest.model_dump() != claim["manifest"]:
                    raise ValueError("Package manifest differs from the assigned task definition")
                manifest.validate_arguments(task["inputs"])
                inputs_path, result_path = run_dir / "inputs.json", run_dir / "result.json"
                inputs_path.write_text(canonical(task["inputs"]), encoding="utf-8")
                values = {
                    **task["inputs"],
                    "python": sys.executable,
                    "inputs": str(inputs_path),
                    "output": str(output_dir),
                    "result": str(result_path),
                }
                command = self._command(manifest, values)
                environment = os.environ.copy()
                environment.pop("TASKPORT_TOKEN", None)
                environment.update(
                    {
                        "TASKPORT_TASK_ID": task_id,
                        "TASKPORT_WORKER_ID": self.id,
                        "TASKPORT_OUTPUT_DIR": str(output_dir),
                        "TASKPORT_INPUTS_FILE": str(inputs_path),
                        "TASKPORT_RESULT_FILE": str(result_path),
                        "PYTHONUNBUFFERED": "1",
                        "PYTHONIOENCODING": "utf-8",
                    }
                )
                completion = self._run_process(
                    command, package_dir, environment, stdout_path, stderr_path, manifest, lease
                )
                if result_path.is_file():
                    if result_path.stat().st_size > 1024 * 1024:
                        raise ValueError("Result JSON exceeds 1 MiB; write an artifact instead")
                    completion = Completion(
                        exit_code=completion.exit_code,
                        error=completion.error,
                        result=json.loads(result_path.read_text(encoding="utf-8")),
                    )
            except Exception as exc:
                completion = Completion(exit_code=127, error=str(exc)[:8192])
                with stderr_path.open("a", encoding="utf-8") as stream:
                    stream.write(f"\nTaskPort worker error: {exc}\n")

            if not lease.valid() or self.stopped.is_set():
                self.log(f"Execution ownership lost/stopped for {task_id}; keeping {run_dir}")
                return
            try:
                self._upload_outputs(task_id, claim["lease_token"], output_dir, logs, lease)
            except Exception as exc:
                completion = Completion(
                    exit_code=completion.exit_code or 1,
                    error=f"Artifact upload failed: {exc}"[:8192],
                    result=completion.result,
                )
            if not lease.valid():
                self.log(f"Lease expired during uploads for {task_id}; keeping {run_dir}")
                return
            self.client.request(
                "POST",
                f"/tasks/{task_id}/complete",
                headers={"X-Taskport-Lease": claim["lease_token"]},
                json=completion.model_dump(),
                task_id=task_id,
            )
            recorded = True
            self.log(f"Completed {task_id}: exit_code={completion.exit_code}")
        except (ApiError, Unavailable) as exc:
            self.log(f"Could not record {task_id}: {exc}; keeping {run_dir}")
        finally:
            lease.close()
            if recorded and not self.keep_work:
                # Only remove a generated run directory inside this worker's owned root.
                resolved = run_dir.resolve()
                root = self.runs.resolve()
                if resolved != root and resolved.is_relative_to(root) and not is_link(run_dir):
                    try:
                        shutil.rmtree(run_dir)
                    except OSError as exc:
                        self.log(f"Keeping completed workspace {run_dir}: cleanup failed: {exc}")

    @staticmethod
    def _command(manifest: Manifest, values):
        def replace(match):
            value = values[match.group(1)]
            return value if isinstance(value, str) else canonical(value)

        return [PLACEHOLDER.sub(replace, part) for part in manifest.command]

    def _run_process(self, command, directory, environment, stdout, stderr, manifest, lease):
        if not lease.valid() or self.stopped.is_set():
            raise RuntimeError("Execution lease expired before script startup")
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen(
                command,
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                shell=False,
                **options,
            )
            deadline = time.monotonic() + manifest.timeout_seconds
            try:
                while process.poll() is None:
                    if not lease.valid() or self.stopped.is_set():
                        stop_process_tree(process)
                        return Completion(exit_code=125, error="Execution stopped or lease lost")
                    if time.monotonic() >= deadline:
                        stop_process_tree(process)
                        return Completion(exit_code=124, error="Task execution timed out")
                    time.sleep(0.05)
                return Completion(exit_code=process.returncode)
            except ProcessCleanupError:
                # Never take another slot if a prior script could still be executing.
                self.stopped.set()
                raise
            finally:
                if process.poll() is None:
                    try:
                        stop_process_tree(process)
                    except ProcessCleanupError:
                        self.stopped.set()

    def _upload_outputs(self, task_id, token, output, logs, lease):
        files = [
            ("_logs/stdout.log", logs / "stdout.log"),
            ("_logs/stderr.log", logs / "stderr.log"),
        ]
        for directory, subdirs, names in os.walk(output, followlinks=False):
            for child in subdirs + names:
                if is_link(Path(directory) / child):
                    raise ValueError("Output artifacts cannot be symbolic links or junctions")
            for name in sorted(names):
                path = Path(directory) / name
                relative = safe_name(path.relative_to(output).as_posix())
                if relative.split("/")[0].casefold() == "_logs":
                    raise ValueError("The _logs artifact directory is reserved for worker logs")
                files.append((relative, path))
                if len(files) > 2048:
                    raise ValueError("Too many artifacts; archive a large directory as a ZIP")
        for name, path in files:
            if not lease.valid() or self.stopped.is_set():
                raise RuntimeError("Execution lease lost during artifact upload")
            self.client.upload(task_id, token, name, path)
