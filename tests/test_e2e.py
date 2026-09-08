"""Real sockets, CLI processes, script execution, server restarts, and downloads."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from taskport.client import Client

pytestmark = pytest.mark.e2e
REPO = Path(__file__).resolve().parents[1]
HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def eventually(predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Condition did not become true before timeout")


def stop(process):
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
                **HIDDEN,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class Cluster:
    def __init__(self, root, lease_seconds=12):
        self.root = root
        self.root.mkdir(parents=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.lease_seconds = lease_seconds
        self.processes = []
        self.handles = []
        self.env = os.environ.copy()
        self.env["TASKPORT_SERVER"] = self.url
        self.env["TASKPORT_TOKEN"] = "integration-token"
        self.env["PYTHONUNBUFFERED"] = "1"
        self.client = Client(
            self.url,
            token="integration-token",
            request_timeout=0.4,
            poll_interval=0.05,
            retry_timeout=2,
        )
        self.server = None

    def spawn(self, *arguments):
        log = (self.root / f"process-{len(self.processes)}.log").open("ab", buffering=0)
        self.handles.append(log)
        process = subprocess.Popen(
            [sys.executable, "-m", "taskport", *map(str, arguments)],
            cwd=REPO,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **HIDDEN,
        )
        self.processes.append(process)
        return process

    def start(self):
        self.server = self.spawn(
            "serve",
            "--data",
            self.root / "data",
            "--port",
            self.port,
            "--lease-seconds",
            self.lease_seconds,
        )

        def ready():
            if self.server.poll() is not None:
                raise AssertionError(f"Server exited: {self.logs()}")
            try:
                return self.client.http.get("/health").status_code == 200
            except httpx.TransportError:
                return False

        eventually(ready)
        return self

    def worker(self, task="demo", *, once=False):
        arguments = [
            "worker",
            "--task",
            task,
            "--work-dir",
            self.root / "workers",
            "--poll-interval",
            "0.05",
        ]
        if once:
            arguments.append("--once")
        return self.spawn(*arguments)

    def cli(self, *arguments, expected=0):
        response = subprocess.run(
            [sys.executable, "-m", "taskport", *map(str, arguments)],
            cwd=REPO,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            **HIDDEN,
        )
        assert response.returncode == expected, response.stdout + response.stderr + self.logs()
        return response

    def script_started(self, task_id):
        return list((self.root / "workers").glob(f"*/runs/{task_id}/output/started.txt"))

    def logs(self):
        return "\n".join(
            p.read_text(encoding="utf-8", errors="replace") for p in self.root.glob("*.log")
        )

    def close(self):
        for process in reversed(self.processes):
            stop(process)
        for handle in self.handles:
            handle.close()
        self.client.close()


@pytest.fixture
def cluster_factory(tmp_path):
    clusters = []

    def create(lease_seconds=12):
        cluster = Cluster(tmp_path / f"cluster-{len(clusters)}", lease_seconds)
        clusters.append(cluster)
        return cluster.start()

    yield create
    for cluster in clusters:
        cluster.close()


def test_two_workers_pinned_packages_cli_and_central_artifacts(cluster_factory, task_package):
    cluster = cluster_factory()
    first = json.loads(cluster.cli("functions", "publish", task_package()).stdout)
    assert first["version"] == "1"
    tasks = [cluster.client.submit("demo", message=f"message-{n}", delay=0.6) for n in range(4)]
    cluster.client.publish(task_package(version="2"))
    workers = [cluster.worker(), cluster.worker()]
    results = [task.result(timeout=20) for task in tasks]
    assert {r["version"] for r in results} == {"1"}
    assert {r["result"]["version"] for r in results} == {"1"}
    assert len({r["worker_id"] for r in results}) == 2
    for worker_id in {r["worker_id"] for r in results}:
        executions = sorted(
            [r["result"] for r in results if r["worker_id"] == worker_id],
            key=lambda item: item["started"],
        )
        assert all(
            a["finished"] <= b["started"] for a, b in zip(executions, executions[1:], strict=False)
        )

    response = cluster.cli("call", "demo", "--message", "a value with spaces", "--wait")
    latest = json.loads(response.stdout)
    assert latest["version"] == "2"
    assert latest["result"]["message"] == "a value with spaces"
    assert "Task ID:" in response.stderr
    assert json.loads(cluster.cli("functions", "list").stdout)[0]["name"] == "demo"

    # Neither a live worker nor its filesystem is needed to retrieve completed results.
    for worker in workers:
        stop(worker)
    stop(cluster.server)
    cluster.start()
    restored = json.loads(cluster.cli("status", tasks[0].id).stdout)
    assert restored["status"] == "succeeded"
    target = cluster.root / "downloads"
    cluster.cli("download", tasks[0].id, "--output", target)
    assert (target / "nested" / "message.txt").read_text(encoding="utf-8") == "message-0"
    assert (target / "binary.bin").read_bytes() == bytes(range(256)) * 1024
    assert "diagnostic" in (target / "_logs" / "stderr.log").read_text()


def test_wait_survives_server_restart_during_execution(cluster_factory, task_package):
    cluster = cluster_factory(lease_seconds=12)
    cluster.client.publish(task_package())
    task = cluster.client.submit("demo", message="survived", delay=4)
    cluster.worker()
    eventually(lambda: cluster.script_started(task.id))
    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(task.result, 20)
        stop(cluster.server)
        time.sleep(0.3)
        assert not waiting.done()
        cluster.start()
        result = waiting.result(timeout=20)
    assert result["status"] == "succeeded", cluster.logs()
    assert result["result"]["message"] == "survived"
    assert len(cluster.client.request("GET", "/tasks").json()) == 1
    with Client(cluster.url, token="integration-token") as reconnected:
        assert reconnected.get_call(task.id).result(timeout=1)["id"] == task.id


def test_script_failure_timeout_and_explicit_retry(cluster_factory, task_package):
    cluster = cluster_factory()
    cluster.client.publish(task_package(timeout=0.4))
    worker = cluster.worker()
    failed = cluster.client.submit("demo", exit_code=7).wait(timeout=15)
    assert failed["status"] == "failed", cluster.logs()
    assert failed["exit_code"] == 7
    assert any(a["name"] == "_logs/stderr.log" for a in failed["artifacts"])
    timed_out = cluster.client.submit("demo", delay=10).wait(timeout=15)
    assert timed_out["status"] == "failed", cluster.logs()
    assert timed_out["exit_code"] == 124
    assert "timed out" in timed_out["error"]
    cluster.client.publish(task_package(version="2", timeout=10))
    retry = cluster.client.retry(failed["id"])
    assert retry.id != failed["id"]
    assert retry.wait(timeout=10)["version"] == "1"
    assert worker.poll() is None


def test_cancellation_stops_script_and_worker_serves_next_call(cluster_factory, task_package):
    cluster = cluster_factory(lease_seconds=3)
    cluster.client.publish(task_package())
    task = cluster.client.submit("demo", delay=20)
    cluster.worker()
    eventually(lambda: cluster.script_started(task.id))
    assert task.cancel()["status"] == "cancelled"
    next_call = cluster.client.submit("demo", message="next")
    assert next_call.result(timeout=15)["result"]["message"] == "next", cluster.logs()
    assert task.status()["status"] == "cancelled"


def test_dead_worker_is_reported_lost_and_does_not_trigger_automatic_retry(
    cluster_factory, task_package
):
    cluster = cluster_factory(lease_seconds=3)
    cluster.client.publish(task_package())
    task = cluster.client.submit("demo", delay=20)
    worker = cluster.worker()
    eventually(lambda: cluster.script_started(task.id))
    stop(worker)
    lost = task.wait(timeout=10)
    assert lost["status"] == "lost"
    other = cluster.worker(once=True)
    other.wait(timeout=10)
    assert other.returncode == 0, cluster.logs()
    assert task.status()["status"] == "lost"
    assert len(cluster.client.request("GET", "/tasks").json()) == 1


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is not installed")
def test_real_powershell_task_and_literal_arguments(cluster_factory):
    cluster = cluster_factory()
    cluster.client.publish(REPO / "examples" / "powershell")
    message = 'spaces; "quotes"; $env:USERNAME; $(Write-Output unexpected)'
    task = cluster.client.submit("powershell-demo", message=message)
    worker = cluster.worker(task="powershell-demo", once=True)
    result = task.result(timeout=15)
    worker.wait(timeout=10)
    assert worker.returncode == 0, cluster.logs()
    assert result["result"]["message"] == message
    cluster.client.download_artifacts(task.id, cluster.root / "download")
    assert (cluster.root / "download" / "message.txt").read_text(encoding="utf-8") == message


def test_heartbeats_keep_long_execution_alive(cluster_factory, task_package):
    cluster = cluster_factory(lease_seconds=3)
    cluster.client.publish(task_package())
    task = cluster.client.submit("demo", delay=4.5)
    worker = cluster.worker(once=True)
    assert task.result(timeout=15)["status"] == "succeeded", cluster.logs()
    worker.wait(timeout=10)
    assert worker.returncode == 0


def test_builds_actual_git_revision_and_downloads_compiled_artifact(cluster_factory, tmp_path):
    import zipfile

    repository = tmp_path / "source-repo"
    repository.mkdir()
    git_config = tmp_path / "empty-gitconfig"
    git_config.write_text("", encoding="utf-8")
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(git_config),
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def git(*arguments):
        response = subprocess.run(
            [
                "git",
                "-c",
                "core.excludesFile=",
                "-c",
                "core.autocrlf=false",
                "-c",
                "user.name=taskport Tests",
                "-c",
                "user.email=taskport-test@example.invalid",
                *arguments,
            ],
            cwd=repository,
            env=git_env,
            capture_output=True,
            text=True,
            check=True,
            **HIDDEN,
        )
        return response.stdout.strip()

    git("init", "--quiet")
    (repository / "hello.py").write_text(
        "print('first revision')\n", encoding="utf-8", newline="\n"
    )
    git("add", "hello.py")
    git("commit", "--quiet", "-m", "First revision")
    revision = git("rev-parse", "HEAD")
    (repository / "hello.py").write_text(
        "print('later revision')\n", encoding="utf-8", newline="\n"
    )
    git("add", "hello.py")
    git("commit", "--quiet", "-m", "Later revision")

    cluster = cluster_factory()
    cluster.env.update({key: git_env[key] for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")})
    cluster.client.publish(REPO / "examples" / "python-build")
    task = cluster.client.submit("python-build", repository=str(repository), hash=revision)
    worker = cluster.worker(task="python-build", once=True)
    result = task.result(timeout=20)
    worker.wait(timeout=10)
    assert worker.returncode == 0, cluster.logs()
    assert result["result"]["commit"] == revision, cluster.logs()
    cluster.client.download_artifacts(task.id, cluster.root / "download")
    with zipfile.ZipFile(cluster.root / "download" / "build.zip") as archive:
        assert archive.read("hello.py").decode() == "print('first revision')\n"
        assert any(name.endswith(".pyc") for name in archive.namelist())
