"""Tests for backend/routes/cuda.py — CUDA backend management endpoints.

Covers all four routes:
    * GET    /backend/cuda-status     — surfaces service-layer status dict
    * POST   /backend/download-cuda   — schedules download, rejects duplicates
    * DELETE /backend/cuda            — deletes archive, rejects active backend
    * GET    /backend/cuda-progress   — SSE stream from progress manager

The CUDA service module is treated as the boundary: filesystem / network
interactions live behind `backend.services.cuda` (a third-party-style
collaborator). For routing/HTTP behavior tests we substitute lightweight
fakes for that service via the lazy-import `monkeypatch` of attributes on
the imported module — the route handlers re-import the module on each
call (``from ..services import cuda``), so patching attributes on the
module object exercises the real handler code without hitting GitHub or
the local filesystem.
"""

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routes.cuda import router as cuda_router
from backend.services import cuda as cuda_service
from backend.utils import progress as progress_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_progress_manager(monkeypatch):
    """Replace the global ProgressManager with a fresh one per test.

    The progress manager is a process-global singleton; without a reset
    state leaks across tests (notably the 'cuda-backend' progress entry).
    """
    monkeypatch.setattr(progress_module, "_progress_manager", None)
    return progress_module.get_progress_manager()


@pytest.fixture()
def app(fresh_progress_manager):
    """Build a minimal FastAPI app mounting only the CUDA router."""
    a = FastAPI()
    a.include_router(cuda_router)
    return a


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def fake_cuda(monkeypatch):
    """Patch the cuda service module with controllable fakes.

    Returns a namespace whose attributes the tests can flip to drive
    behavior. The route handlers import the service lazily via
    ``from ..services import cuda`` which always picks up the patched
    attributes on the live module object.
    """

    class State:
        binary_path: Path | None = None
        active: bool = False
        download_called: bool = False
        download_should_raise: Exception | None = None
        delete_called: bool = False
        delete_returns: bool = True
        status_payload: dict = {
            "available": False,
            "active": False,
            "binary_path": None,
            "cuda_libs_version": None,
            "downloading": False,
            "download_progress": None,
        }

    state = State()

    def fake_get_cuda_status():
        return state.status_payload

    def fake_get_cuda_binary_path():
        return state.binary_path

    def fake_is_cuda_active():
        return state.active

    async def fake_download_cuda_binary():
        state.download_called = True
        if state.download_should_raise is not None:
            raise state.download_should_raise

    async def fake_delete_cuda_binary():
        state.delete_called = True
        return state.delete_returns

    monkeypatch.setattr(cuda_service, "get_cuda_status", fake_get_cuda_status)
    monkeypatch.setattr(cuda_service, "get_cuda_binary_path", fake_get_cuda_binary_path)
    monkeypatch.setattr(cuda_service, "is_cuda_active", fake_is_cuda_active)
    monkeypatch.setattr(cuda_service, "download_cuda_binary", fake_download_cuda_binary)
    monkeypatch.setattr(cuda_service, "delete_cuda_binary", fake_delete_cuda_binary)
    return state


# ---------------------------------------------------------------------------
# GET /backend/cuda-status
# ---------------------------------------------------------------------------


class TestGetCudaStatus:
    def test_returns_service_payload_verbatim(self, client, fake_cuda):
        fake_cuda.status_payload = {
            "available": True,
            "active": False,
            "binary_path": "/tmp/voiceit-server-cuda",
            "cuda_libs_version": "cu128-v1",
            "downloading": False,
            "download_progress": None,
        }

        response = client.get("/backend/cuda-status")

        assert response.status_code == 200
        assert response.json() == fake_cuda.status_payload

    def test_status_unavailable_when_not_downloaded(self, client, fake_cuda):
        fake_cuda.status_payload = {
            "available": False,
            "active": False,
            "binary_path": None,
            "cuda_libs_version": None,
            "downloading": False,
            "download_progress": None,
        }

        response = client.get("/backend/cuda-status")

        assert response.status_code == 200
        body = response.json()
        assert body["available"] is False
        assert body["binary_path"] is None


# ---------------------------------------------------------------------------
# POST /backend/download-cuda
# ---------------------------------------------------------------------------


class TestDownloadCuda:
    def test_starts_download_when_no_binary_and_no_progress(
        self, client, fake_cuda, fresh_progress_manager
    ):
        fake_cuda.binary_path = None

        response = client.post("/backend/download-cuda")

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "message": "CUDA backend download started",
            "progress_key": "cuda-backend",
        }

    def test_rejects_when_binary_already_present(self, client, fake_cuda):
        fake_cuda.binary_path = Path("/tmp/voiceit-server-cuda")

        response = client.post("/backend/download-cuda")

        assert response.status_code == 409
        assert response.json()["detail"] == "CUDA backend already downloaded"
        assert fake_cuda.download_called is False

    def test_rejects_when_download_already_in_progress(
        self, client, fake_cuda, fresh_progress_manager
    ):
        fake_cuda.binary_path = None
        fresh_progress_manager.update_progress(
            cuda_service.PROGRESS_KEY,
            current=10,
            total=100,
            filename="Downloading CUDA server",
            status="downloading",
        )

        response = client.post("/backend/download-cuda")

        assert response.status_code == 409
        assert response.json()["detail"] == "CUDA backend download already in progress"
        assert fake_cuda.download_called is False

    def test_allows_restart_after_previous_completion(
        self, client, fake_cuda, fresh_progress_manager
    ):
        """A stale 'complete' entry must not block a new download."""
        fake_cuda.binary_path = None
        fresh_progress_manager.update_progress(
            cuda_service.PROGRESS_KEY,
            current=100,
            total=100,
            filename="done",
            status="downloading",
        )
        fresh_progress_manager.mark_complete(cuda_service.PROGRESS_KEY)

        response = client.post("/backend/download-cuda")

        assert response.status_code == 200

    def test_background_task_runs_download(self, client, fake_cuda):
        fake_cuda.binary_path = None

        response = client.post("/backend/download-cuda")

        assert response.status_code == 200
        # Allow the background task to run on the test client's loop.
        # TestClient runs sync, but background tasks are scheduled on its
        # internal loop; we wait briefly for the fake to record the call.
        async def _wait():
            for _ in range(50):
                if fake_cuda.download_called:
                    return
                await asyncio.sleep(0.01)

        asyncio.run(_wait())
        assert fake_cuda.download_called is True

    def test_download_exception_is_swallowed_and_logged(self, client, fake_cuda, caplog):
        """Errors from the background download must not crash the server."""
        fake_cuda.binary_path = None
        fake_cuda.download_should_raise = RuntimeError("network down")

        with caplog.at_level("ERROR"):
            response = client.post("/backend/download-cuda")

        assert response.status_code == 200

        async def _wait():
            for _ in range(50):
                if fake_cuda.download_called:
                    return
                await asyncio.sleep(0.01)

        asyncio.run(_wait())
        assert fake_cuda.download_called is True
        # The handler installs an error log on failure.
        assert any("CUDA download failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# DELETE /backend/cuda
# ---------------------------------------------------------------------------


class TestDeleteCuda:
    def test_deletes_when_present_and_inactive(self, client, fake_cuda):
        fake_cuda.active = False
        fake_cuda.delete_returns = True

        response = client.delete("/backend/cuda")

        assert response.status_code == 200
        assert response.json() == {"message": "CUDA backend deleted"}
        assert fake_cuda.delete_called is True

    def test_rejects_delete_while_cuda_backend_active(self, client, fake_cuda):
        fake_cuda.active = True

        response = client.delete("/backend/cuda")

        assert response.status_code == 409
        assert "Switch to CPU first" in response.json()["detail"]
        assert fake_cuda.delete_called is False

    def test_returns_404_when_nothing_to_delete(self, client, fake_cuda):
        fake_cuda.active = False
        fake_cuda.delete_returns = False

        response = client.delete("/backend/cuda")

        assert response.status_code == 404
        assert response.json()["detail"] == "No CUDA backend found to delete"


# ---------------------------------------------------------------------------
# GET /backend/cuda-progress
# ---------------------------------------------------------------------------


class TestCudaProgressStream:
    """The progress endpoint exposes the cuda-backend stream via SSE.

    We exercise the handler directly (rather than through TestClient) to
    avoid the SSE stream's infinite heartbeat loop blocking the test
    client. This still runs the real handler code, the real
    StreamingResponse, and the real ProgressManager.subscribe() body —
    just without an HTTP socket round-trip.
    """

    def test_returns_streaming_response_with_sse_headers(self):
        from backend.routes.cuda import get_cuda_download_progress

        response = asyncio.run(get_cuda_download_progress())

        assert response.status_code == 200
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["connection"] == "keep-alive"
        assert response.headers["x-accel-buffering"] == "no"

    def test_emits_initial_payload_for_in_flight_download(
        self, fresh_progress_manager
    ):
        """When a download is in flight, opening the stream replays the
        latest progress as the first SSE event.
        """
        from backend.routes.cuda import get_cuda_download_progress

        fresh_progress_manager.update_progress(
            cuda_service.PROGRESS_KEY,
            current=42,
            total=100,
            filename="Downloading CUDA server",
            status="downloading",
        )

        async def _drive() -> str:
            response = await get_cuda_download_progress()
            body_iter = response.body_iterator
            # Pull the first event off the SSE generator; the
            # ProgressManager replays the in-flight progress synchronously
            # before entering its heartbeat loop, so this completes
            # without waiting on the 1s timeout.
            first = await anext(body_iter)
            await body_iter.aclose()
            return first if isinstance(first, str) else first.decode()

        first_event = asyncio.run(_drive())

        assert first_event.startswith("data: ")
        assert "cuda-backend" in first_event
        assert "downloading" in first_event
        assert "Downloading CUDA server" in first_event

    def test_does_not_replay_stale_complete_status(self, fresh_progress_manager):
        """A previously 'complete' download must not be replayed as the
        initial SSE event — only in-flight progress is replayed.
        """
        from backend.routes.cuda import get_cuda_download_progress

        fresh_progress_manager.update_progress(
            cuda_service.PROGRESS_KEY,
            current=100,
            total=100,
            filename="done",
            status="downloading",
        )
        fresh_progress_manager.mark_complete(cuda_service.PROGRESS_KEY)

        async def _drive() -> str | None:
            response = await get_cuda_download_progress()
            body_iter = response.body_iterator
            try:
                # First yield will be a heartbeat (": heartbeat\n\n")
                # because the stale 'complete' status is skipped.
                first = await asyncio.wait_for(anext(body_iter), timeout=2.0)
            finally:
                await body_iter.aclose()
            return first if isinstance(first, str) else first.decode()

        first_event = asyncio.run(_drive())

        # Should be a heartbeat, not a data payload with stale 'complete'.
        assert first_event.startswith(":")
        assert "data:" not in first_event
