import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taskport.packages import pack_directory
from taskport.server import create_app

TASK_SCRIPT = """import json
import os
import sys
import time
from pathlib import Path

inputs = json.loads(Path(os.environ["TASKPORT_INPUTS_FILE"]).read_text(encoding="utf-8"))
output = Path(os.environ["TASKPORT_OUTPUT_DIR"])
started = time.monotonic()
print("Started", flush=True)
(output / "started.txt").write_text(str(os.getpid()), encoding="utf-8")
time.sleep(inputs.get("delay", 0))
message = inputs.get("message", "hello")
(output / "nested").mkdir()
(output / "nested" / "message.txt").write_text(message, encoding="utf-8")
(output / "binary.bin").write_bytes(bytes(range(256)) * 1024)
result = {"message": message, "version": VERSION, "started": started,
          "finished": time.monotonic(), "worker": os.environ["TASKPORT_WORKER_ID"]}
Path(os.environ["TASKPORT_RESULT_FILE"]).write_text(json.dumps(result), encoding="utf-8")
print(message)
print("diagnostic", file=sys.stderr)
sys.exit(inputs.get("exit_code", 0))
"""


@pytest.fixture
def task_package(tmp_path):
    counter = 0

    def make(name="demo", version="1", timeout=30, script=None):
        nonlocal counter
        counter += 1
        directory = tmp_path / f"package-{counter}"
        directory.mkdir()
        manifest = {
            "name": name,
            "version": version,
            "description": "Test function",
            "command": ["{python}", "task.py"],
            "inputs": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "delay": {"type": "number", "minimum": 0},
                    "exit_code": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            "timeout_seconds": timeout,
        }
        (directory / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
        code = script if script is not None else TASK_SCRIPT.replace("VERSION", repr(version))
        (directory / "task.py").write_text(code, encoding="utf-8")
        return directory

    return make


@pytest.fixture
def api(tmp_path):
    with TestClient(create_app(tmp_path / "server", token="test-token", lease_seconds=3)) as client:
        client.headers["Authorization"] = "Bearer test-token"
        yield client


def publish(api, directory: Path):
    archive = directory.parent / f"{directory.name}.zip"
    pack_directory(directory, archive)
    response = api.post("/functions", content=archive.read_bytes())
    assert response.status_code == 201, response.text
    return response.json()
