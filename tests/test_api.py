import hashlib
import io
import json
import stat
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from conftest import publish


def submit(api, **overrides):
    task_id = str(uuid4())
    response = api.put(f"/tasks/{task_id}", json={"function": "demo", "inputs": {}, **overrides})
    assert response.status_code == 202, response.text
    return task_id


def claim(api, worker=None, request_id=None, function="demo"):
    return api.post(
        f"/workers/{worker or uuid4()}/claim",
        json={"function": function, "request_id": str(request_id or uuid4())},
    )


def test_submission_validation_idempotence_and_pinned_versions(api, task_package):
    first = task_package()
    publish(api, first)
    task_id = str(uuid4())
    body = {"function": "demo", "inputs": {"message": "first"}}
    assert api.put(f"/tasks/{task_id}", json=body).json()["version"] == "1"
    publish(api, task_package(version="2"))
    retry = api.put(f"/tasks/{task_id}", json=body)
    assert retry.status_code == 202
    assert retry.json()["version"] == "1"
    assert api.put(f"/tasks/{task_id}", json={**body, "inputs": {}}).status_code == 409
    assert api.put(f"/tasks/{uuid4()}", json={**body, "inputs": {"message": 2}}).status_code == 422
    assert api.put(f"/tasks/{uuid4()}", json={**body, "inputs": {"bogus": 2}}).status_code == 422
    assert api.put(f"/tasks/{uuid4()}", json={**body, "version": "missing"}).status_code == 404
    assert api.put("/tasks/not-a-guid", json=body).status_code == 422
    assert api.get(f"/tasks/{submit(api)}").json()["version"] == "2"
    publish(api, first)  # An old publication retry does not roll back the latest version.
    assert api.get("/functions/demo").json()["version"] == "2"
    assert api.get("/functions").json()[0]["versions"] == ["1", "2"]


def test_published_version_is_immutable(api, task_package):
    package = task_package()
    publish(api, package)
    (package / "task.py").write_text("print('changed')", encoding="utf-8")
    from taskport.packages import pack_directory

    archive = package.parent / "changed.zip"
    pack_directory(package, archive)
    response = api.post("/functions", content=archive.read_bytes())
    assert response.status_code == 409


def test_atomic_claims_and_one_slot_per_worker(api, task_package):
    publish(api, task_package())
    tasks = {submit(api) for _ in range(12)}
    with ThreadPoolExecutor(max_workers=12) as executor:
        responses = list(executor.map(lambda _: claim(api), range(12)))
    assert all(r.status_code == 200 for r in responses)
    assigned = [r.json() for r in responses]
    assert {r["task"]["id"] for r in assigned} == tasks
    assert claim(api).status_code == 204
    worker = assigned[0]["task"]["worker_id"]
    submit(api)
    assert claim(api, worker=worker).status_code == 409
    assert api.get("/functions").json()[0]["busy_workers"] == 12


def test_lost_claim_response_reuses_same_assignment(api, task_package):
    publish(api, task_package())
    submit(api)
    submit(api)
    worker, request = str(uuid4()), str(uuid4())
    first = claim(api, worker, request).json()
    again = claim(api, worker, request).json()
    assert first["task"]["id"] == again["task"]["id"]
    assert first["lease_token"] == again["lease_token"]
    assert claim(api, request_id=request).status_code == 409
    assert api.get("/functions").json()[0]["queued_calls"] == 1


def test_artifact_integrity_and_idempotent_completion(api, task_package):
    publish(api, task_package())
    task_id = submit(api)
    assigned = claim(api).json()
    headers = {"X-Taskport-Lease": assigned["lease_token"]}
    url = f"/tasks/{task_id}/artifacts?name=directory/payload.bin"
    contents = bytes(range(256)) * 512
    response = api.put(url, content=contents, headers=headers)
    assert response.status_code == 200
    artifact = response.json()
    assert artifact["sha256"] == hashlib.sha256(contents).hexdigest()
    assert api.put(url, content=contents, headers=headers).json()["id"] == artifact["id"]
    assert api.put(url, content=b"changed", headers=headers).status_code == 409
    assert api.get(artifact["url"]).content == contents
    assert (
        api.put(
            f"/tasks/{task_id}/artifacts?name=bad.bin",
            content=b"corrupt",
            headers={**headers, "X-Content-SHA256": "0" * 64},
        ).status_code
        == 422
    )
    for name in (".", "../secret", "C:/secret", "foo\\bar", "CON", "a./b"):
        assert (
            api.put(
                f"/tasks/{task_id}/artifacts", params={"name": name}, content=b"x", headers=headers
            ).status_code
            == 422
        )
    assert not list((api.app.state.store.data / "incoming").iterdir())
    completion = {"exit_code": 0, "result": {"ok": True}}
    done = api.post(f"/tasks/{task_id}/complete", json=completion, headers=headers)
    assert done.json()["status"] == "succeeded"
    assert len(done.json()["artifacts"]) == 1
    assert (
        api.post(f"/tasks/{task_id}/complete", json=completion, headers=headers).json()
        == done.json()
    )
    assert (
        api.post(f"/tasks/{task_id}/complete", json={"exit_code": 7}, headers=headers).status_code
        == 409
    )
    assert api.put(url, content=contents, headers=headers).status_code == 409


def test_artifact_names_are_portable_across_case_sensitive_filesystems(api, task_package):
    publish(api, task_package())
    task_id = submit(api)
    assigned = claim(api).json()
    headers = {"X-Taskport-Lease": assigned["lease_token"]}
    for first, second in (("A.txt", "a.txt"), ("ss.txt", "ß.txt")):
        url = f"/tasks/{task_id}/artifacts"
        assert (
            api.put(url, params={"name": first}, content=b"x", headers=headers).status_code == 200
        )
        assert (
            api.put(url, params={"name": second}, content=b"x", headers=headers).status_code == 409
        )


def test_expired_lease_is_lost_never_automatically_requeued(api, task_package):
    publish(api, task_package())
    task_id = submit(api)
    assigned = claim(api).json()
    headers = {"X-Taskport-Lease": assigned["lease_token"]}
    assert api.post(f"/tasks/{task_id}/heartbeat", headers=headers).status_code == 200
    with api.app.state.store.transaction() as connection:
        connection.execute(
            "UPDATE tasks SET lease_expires=? WHERE id=?", (time.time() - 1, task_id)
        )
    assert api.get(f"/tasks/{task_id}").json()["status"] == "lost"
    assert claim(api).status_code == 204
    assert api.post(f"/tasks/{task_id}/heartbeat", headers=headers).status_code == 409
    assert (
        api.post(f"/tasks/{task_id}/complete", json={"exit_code": 0}, headers=headers).status_code
        == 409
    )
    assert (
        api.put(
            f"/tasks/{task_id}/artifacts?name=late.txt", content=b"late", headers=headers
        ).status_code
        == 409
    )


def test_cancel_queued_and_running_tasks(api, task_package):
    publish(api, task_package())
    queued = submit(api)
    assert api.post(f"/tasks/{queued}/cancel").json()["status"] == "cancelled"
    assert claim(api).status_code == 204
    running = submit(api)
    assigned = claim(api).json()
    assert api.post(f"/tasks/{running}/cancel").json()["status"] == "cancelled"
    assert (
        api.post(
            f"/tasks/{running}/heartbeat",
            headers={"X-Taskport-Lease": assigned["lease_token"]},
        ).status_code
        == 409
    )


def test_authentication_covers_metadata_execution_and_files(api, task_package):
    definition = publish(api, task_package())
    headers = {"Authorization": "Bearer wrong"}
    for path in ("/functions", "/workers", "/tasks", definition["package_url"]):
        assert api.get(path, headers=headers).status_code == 401
    assert api.post("/functions", content=b"zip", headers=headers).status_code == 401


@pytest.mark.parametrize("entry", ["../escape.py", "/absolute.py", "C:/drive.py", "NUL", "a\\b"])
def test_rejects_unsafe_packages(api, entry):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "task.json", json.dumps({"name": "demo", "version": "1", "command": ["x"]})
        )
        info = zipfile.ZipInfo()
        info.filename = entry  # Bypass Windows ZipInfo's normalizing constructor.
        archive.writestr(info, b"bad")
    assert api.post("/functions", content=stream.getvalue()).status_code == 422


def test_rejects_zip_links_and_external_schema_references(api):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        link = zipfile.ZipInfo("link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "outside")
    assert api.post("/functions", content=stream.getvalue()).status_code == 422
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "task.json",
            json.dumps(
                {
                    "name": "demo",
                    "version": "1",
                    "command": ["x"],
                    "inputs": {"type": "object", "$ref": "https://untrusted.invalid/schema"},
                }
            ),
        )
    assert api.post("/functions", content=stream.getvalue()).status_code == 422
