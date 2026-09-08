"""taskport HTTP service. Execution always happens in separate workers."""

import asyncio
import hashlib
import os
import secrets
import tempfile
import zipfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi import Path as ApiPath
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from taskport import __version__
from taskport.packages import MAX_PACKAGE_BYTES, inspect_package, safe_name
from taskport.protocol import Claim, Completion, Problem, Submission
from taskport.store import Store


def install_blob(source: Path, target: Path) -> None:
    """Publish only completely received, flushed files."""
    # Immutable, content-addressed blobs may already be open for download on Windows.
    if target.exists():
        source.unlink()
        return
    try:
        os.replace(source, target)
    except PermissionError:
        if not target.exists():
            raise
        source.unlink()
    if os.name != "nt":
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def create_app(
    data: str | Path = "data",
    token: str | None = None,
    lease_seconds: float = 60,
    max_artifact_bytes: int = 1024 * 1024 * 1024,
) -> FastAPI:
    store = Store(Path(data), lease_seconds)
    if max_artifact_bytes <= 0:
        raise ValueError("Artifact size limit must be positive")

    def authorize(authorization: Annotated[str | None, Header()] = None):
        if token and not secrets.compare_digest(authorization or "", f"Bearer {token}"):
            raise Problem(401, "A valid bearer token is required")

    @asynccontextmanager
    async def lifespan(app):
        async def reap():
            while True:
                await asyncio.sleep(min(5, lease_seconds / 3))
                await run_in_threadpool(store.expire)

        monitor = asyncio.create_task(reap())
        try:
            yield
        finally:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor

    app = FastAPI(
        title="taskport",
        version=__version__,
        description="Named remote calls, one-slot workers, and central artifact storage.",
        lifespan=lifespan,
        dependencies=[Depends(authorize)],
    )
    app.state.store = store

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @asynccontextmanager
    async def receive_file(request: Request, maximum: int):
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > maximum:
                    raise Problem(413, f"Upload exceeds {maximum} bytes")
            except ValueError as exc:
                raise Problem(400, "Invalid Content-Length") from exc
        descriptor, filename = tempfile.mkstemp(dir=store.data / "incoming", suffix=".part")
        path = Path(filename)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                async for block in request.stream():
                    size += len(block)
                    if size > maximum:
                        raise Problem(413, f"Upload exceeds {maximum} bytes")
                    digest.update(block)
                    await run_in_threadpool(stream.write, block)
                await run_in_threadpool(stream.flush)
                await run_in_threadpool(os.fsync, stream.fileno())
            expected = request.headers.get("x-content-sha256")
            if expected and not secrets.compare_digest(expected, digest.hexdigest()):
                raise Problem(422, "Upload checksum does not match")
            yield path, digest.hexdigest(), size
        finally:
            path.unlink(missing_ok=True)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/functions")
    def list_functions():
        return store.functions()

    @app.get("/functions/{name}")
    def describe(name: str, version: str | None = None):
        return store.describe(name, version)

    @app.post("/functions", status_code=201)
    async def publish(request: Request):
        async with receive_file(request, MAX_PACKAGE_BYTES) as (path, digest, size):
            try:
                manifest = await run_in_threadpool(inspect_package, path)
            except (ValueError, zipfile.BadZipFile, RuntimeError, OSError) as exc:
                raise Problem(422, f"Invalid task package: {exc}") from exc
            await run_in_threadpool(install_blob, path, store.data / "packages" / f"{digest}.zip")
            return await run_in_threadpool(store.publish, manifest, digest)

    @app.get("/packages/{digest}")
    def package(digest: Annotated[str, ApiPath(pattern=r"^[0-9a-f]{64}$")]):
        path = store.data / "packages" / f"{digest}.zip"
        if not path.is_file():
            raise Problem(404, "Package not found")
        return FileResponse(
            path,
            filename=f"{digest}.zip",
            media_type="application/zip",
            headers={"X-Content-SHA256": digest},
        )

    @app.put("/tasks/{task_id}", status_code=202)
    def submit(task_id: UUID, body: Submission):
        return store.submit(str(task_id), body)

    @app.get("/tasks")
    def list_tasks(limit: Annotated[int, Query(ge=1, le=1000)] = 100):
        return store.tasks(limit)

    @app.get("/tasks/{task_id}")
    def status(task_id: UUID):
        return store.get(str(task_id))

    @app.post("/tasks/{task_id}/cancel")
    def cancel(task_id: UUID):
        return store.cancel(str(task_id))

    @app.get("/workers")
    def workers():
        return store.workers()

    @app.post("/workers/{worker_id}/claim")
    def claim(worker_id: UUID, body: Claim):
        assigned = store.claim(str(worker_id), body.function, str(body.request_id))
        if assigned is None:
            return Response(status_code=204)
        return assigned

    @app.post("/tasks/{task_id}/heartbeat")
    def heartbeat(task_id: UUID, x_taskport_lease: Annotated[str, Header()]):
        return store.heartbeat(str(task_id), x_taskport_lease)

    @app.post("/tasks/{task_id}/complete")
    def complete(task_id: UUID, body: Completion, x_taskport_lease: Annotated[str, Header()]):
        return store.complete(str(task_id), x_taskport_lease, body)

    @app.put("/tasks/{task_id}/artifacts")
    async def upload_artifact(
        task_id: UUID,
        request: Request,
        x_taskport_lease: Annotated[str, Header()],
        name: str,
    ):
        try:
            safe_name(name)
        except ValueError as exc:
            raise Problem(422, str(exc)) from exc
        await run_in_threadpool(store.validate_lease, str(task_id), x_taskport_lease)
        async with receive_file(request, max_artifact_bytes) as (path, digest, size):
            await run_in_threadpool(install_blob, path, store.data / "artifacts" / digest)
            # Re-check ownership after the upload, which may have taken a while.
            return await run_in_threadpool(
                store.add_artifact, str(task_id), x_taskport_lease, name, digest, size
            )

    @app.get("/tasks/{task_id}/artifacts/{artifact_id}")
    def download_artifact(task_id: UUID, artifact_id: UUID):
        artifact = store.artifact(str(task_id), str(artifact_id))
        path = store.data / "artifacts" / artifact["digest"]
        if not path.is_file():
            raise Problem(410, "Artifact data is missing from server storage")
        return FileResponse(
            path,
            filename=artifact["name"].rsplit("/", 1)[-1],
            media_type="application/octet-stream",
            headers={"X-Content-SHA256": artifact["digest"]},
        )

    return app
