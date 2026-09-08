import hashlib
import time
from uuid import uuid4

import httpx
import pytest
from conftest import publish

from taskport.client import ApiError, Client, TaskFailed, WaitTimeout


def adapter(api):
    def handle(request):
        return api.request(
            request.method,
            request.url.path + ("?" + request.url.query.decode() if request.url.query else ""),
            content=request.read(),
            headers=dict(request.headers),
        )

    return handle


def test_lost_submission_acknowledgment_does_not_enqueue_twice(api, task_package):
    publish(api, task_package())
    next_version = task_package(version="2")
    count = 0
    forward = adapter(api)

    def handle(request):
        nonlocal count
        response = forward(request)
        if request.method == "PUT":
            count += 1
            if count == 1:
                publish(api, next_version)
                raise httpx.ReadError("Response lost after server committed", request=request)
        return response

    with Client(
        token="test-token", transport=httpx.MockTransport(handle), poll_interval=0.01
    ) as client:
        task = client.submit("demo", message="once")
        assert task.status()["version"] == "1"
        assert len(api.get("/tasks").json()) == 1
        assert count == 2


def test_wait_recovers_from_network_loss_without_resubmission():
    task_id = str(uuid4())
    calls = []

    def handle(request):
        calls.append(request.method)
        if len(calls) < 3:
            raise httpx.ConnectError("Offline", request=request)
        return httpx.Response(200, json={"id": task_id, "status": "succeeded"})

    with Client(transport=httpx.MockTransport(handle), poll_interval=0.01) as client:
        assert client.wait(task_id, timeout=1)["status"] == "succeeded"
    assert calls == ["GET", "GET", "GET"]


def test_wait_timeout_does_not_cancel_remote_task(api, task_package):
    publish(api, task_package())
    with Client(
        token="test-token", transport=httpx.MockTransport(adapter(api)), poll_interval=0.01
    ) as client:
        task = client.submit("demo")
        with pytest.raises(WaitTimeout) as error:
            task.result(timeout=0.04)
        assert error.value.task_id == task.id
        assert task.status()["status"] == "queued"
        task.cancel()
        with pytest.raises(TaskFailed):
            task.result(timeout=1)


def test_offline_wait_respects_deadline():
    def offline(request):
        raise httpx.ConnectError("Offline", request=request)

    with Client(transport=httpx.MockTransport(offline), poll_interval=0.01) as client:
        started = time.monotonic()
        with pytest.raises(WaitTimeout):
            client.wait(str(uuid4()), timeout=0.05)
        assert time.monotonic() - started < 0.5


def test_permanent_api_errors_are_not_retried():
    calls = []

    def unauthorized(request):
        calls.append(request)
        return httpx.Response(401, json={"detail": "bad token"})

    with Client(transport=httpx.MockTransport(unauthorized)) as client:
        with pytest.raises(ApiError):
            client.wait(str(uuid4()), timeout=1)
    assert len(calls) == 1


def test_download_retries_partial_transfer_and_checks_integrity(tmp_path):
    payload = b"correct binary data\0" * 100
    requests = []

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial contents"
            raise httpx.ReadError("Connection interrupted")

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, stream=BrokenStream())
        return httpx.Response(200, content=payload)

    target = tmp_path / "download.bin"
    with Client(transport=httpx.MockTransport(handle), poll_interval=0.01) as client:
        client.download_file("/artifact", target, hashlib.sha256(payload).hexdigest())
        assert target.read_bytes() == payload
        with pytest.raises(ValueError, match="checksum"):
            client.download_file("/artifact", target, "0" * 64)
        assert target.read_bytes() == payload
    assert not list(tmp_path.glob("*.part"))


def test_retry_pins_original_version_and_uses_new_id(api, task_package):
    publish(api, task_package())
    with Client(token="test-token", transport=httpx.MockTransport(adapter(api))) as client:
        task = client.submit("demo")
        task.cancel()
        publish(api, task_package(version="2"))
        retry = client.retry(task.id)
        assert retry.id != task.id
        assert retry.status()["version"] == "1"
