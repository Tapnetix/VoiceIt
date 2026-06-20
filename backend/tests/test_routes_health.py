"""Tests for backend.routes.health.

Covers all five endpoints exposed by the health router:
  * GET /
  * POST /shutdown
  * POST /watchdog/disable
  * GET /health
  * GET /health/filesystem

The /health endpoint normally inspects torch/CUDA/MPS/XPU/DirectML and walks
the HuggingFace cache. To keep these tests deterministic and CPU-only we
patch the small surface that ``backend.routes.health`` actually touches
(``torch.cuda.is_available``, ``torch.backends.mps.is_available``,
``torch.cuda.get_device_name``, ``torch.cuda.memory_allocated``, the
``get_backend_type`` helper, and the TTS model factory) and let the rest
of the function execute against the real implementation.

Per the project test convention (see backend/tests/test_cors.py and
backend/tests/test_books_overview.py) we construct a minimal FastAPI app
that mounts the real ``backend.routes.health`` router and drive it with
``starlette.testclient.TestClient``.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Pre-import shim: backend.routes.health.watchdog_disable does
# ``from backend.server import disable_watchdog`` at call time. Importing
# backend.server would pull in the entire app (torch, transformers, etc.).
# Stub the module before any health import so the call works without it.
# ---------------------------------------------------------------------------
_FAKE_SERVER = types.ModuleType("backend.server")
_disable_calls: list[int] = []


def _fake_disable_watchdog() -> None:
    _disable_calls.append(1)


_FAKE_SERVER.disable_watchdog = _fake_disable_watchdog
sys.modules.setdefault("backend.server", _FAKE_SERVER)


from backend.routes import health as health_module  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTTSModel:
    """Stand-in for the TTS backend used by the /health endpoint."""

    def __init__(self, *, loaded: bool = False, model_size: str | None = None,
                 current_model_size: str | None = None,
                 raise_on_is_loaded: bool = False) -> None:
        self._loaded = loaded
        self._raise = raise_on_is_loaded
        if current_model_size is not None:
            self._current_model_size = current_model_size
        if model_size is not None:
            self.model_size = model_size

    def is_loaded(self) -> bool:
        if self._raise:
            raise RuntimeError("backend went away")
        return self._loaded


def _build_app() -> FastAPI:
    """Build a minimal FastAPI app mounting the real health router."""
    app = FastAPI()
    app.include_router(health_module.router)
    return app


@pytest.fixture()
def cpu_only_health(monkeypatch):
    """Force the /health endpoint into a deterministic CPU-only configuration."""
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.cuda, "memory_allocated", lambda *a, **k: 0)
    monkeypatch.setattr(torch.cuda, "get_device_name", lambda *a, **k: "stub-gpu")

    # torch.backends.mps may or may not exist depending on the build; force it
    # to report unavailable either way.
    if hasattr(torch.backends, "mps"):
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)

    monkeypatch.setattr(health_module, "get_backend_type", lambda: "pytorch")
    monkeypatch.setattr(
        health_module.tts, "get_tts_model", lambda: _FakeTTSModel(loaded=False)
    )
    monkeypatch.delenv("VOICEIT_BACKEND_VARIANT", raising=False)


@pytest.fixture()
def client(cpu_only_health):
    return TestClient(_build_app())


# ---------------------------------------------------------------------------
# GET /  — root endpoint
# ---------------------------------------------------------------------------


class TestRoot:
    def test_returns_api_metadata_when_spa_index_absent(self, client, monkeypatch, tmp_path):
        # Point the SPA directory at an empty tmp dir so index.html is absent
        # regardless of the developer's working copy.
        monkeypatch.setattr(health_module, "_frontend_dir", tmp_path)

        response = client.get("/")

        assert response.status_code == 200
        body = response.json()
        assert body["message"] == "voiceit API"
        # Version must match the package's declared version, not a placeholder.
        from backend import __version__
        assert body["version"] == __version__

    def test_serves_spa_index_html_when_present(self, client, monkeypatch, tmp_path):
        index = tmp_path / "index.html"
        index.write_text("<!doctype html><title>spa</title>")
        monkeypatch.setattr(health_module, "_frontend_dir", tmp_path)

        response = client.get("/")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<title>spa</title>" in response.text


# ---------------------------------------------------------------------------
# POST /shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_returns_acknowledgement_and_signals_self(self, client, monkeypatch):
        # The shutdown handler schedules an async task that calls os.kill with
        # SIGTERM after a brief sleep. Capture the call without actually
        # terminating the pytest process.
        recorded: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            recorded.append((pid, sig))

        monkeypatch.setattr(health_module.os, "kill", fake_kill)
        # Also collapse the sleep so the background task fires promptly.
        async def _instant(_):
            return None
        monkeypatch.setattr(health_module.asyncio, "sleep", _instant)

        response = client.post("/shutdown")

        assert response.status_code == 200
        assert response.json() == {"message": "Shutting down..."}

        # The background task should fire on the next event-loop turn. Run
        # the loop briefly to let it execute.
        import asyncio as _asyncio
        import time

        deadline = time.monotonic() + 1.0
        while not recorded and time.monotonic() < deadline:
            # Pump pending tasks; TestClient ran the request on its own loop
            # so we explicitly wait for them via a fresh sleep call.
            _asyncio.run(_asyncio.sleep(0.01))

        assert recorded, "shutdown task never invoked os.kill"
        pid, sig = recorded[-1]
        import signal as _signal

        assert pid == os.getpid()
        assert sig == _signal.SIGTERM


# ---------------------------------------------------------------------------
# POST /watchdog/disable
# ---------------------------------------------------------------------------


class TestWatchdogDisable:
    def test_invokes_disable_watchdog_and_returns_message(self, client):
        before = len(_disable_calls)

        response = client.post("/watchdog/disable")

        assert response.status_code == 200
        assert response.json() == {"message": "Watchdog disabled"}
        assert len(_disable_calls) == before + 1


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_reports_healthy_with_no_gpu_and_no_model_loaded(self, client):
        response = client.get("/health")

        assert response.status_code == 200
        body = response.json()

        assert body["status"] == "healthy"
        assert body["model_loaded"] is False
        # No GPU available in CPU-only fixture and backend_type=pytorch.
        assert body["gpu_available"] is False
        assert body["gpu_type"] is None
        assert body["vram_used_mb"] is None
        assert body["backend_type"] == "pytorch"
        # No env override and no CUDA -> backend_variant collapses to "cpu".
        assert body["backend_variant"] == "cpu"
        assert body["gpu_compatibility_warning"] is None

    def test_reports_loaded_model_with_current_size_taking_precedence(
        self, client, monkeypatch
    ):
        # When the backend has both `_current_model_size` and `model_size`,
        # `_current_model_size` wins.
        fake = _FakeTTSModel(
            loaded=True, model_size="1.7B", current_model_size="0.6B"
        )
        monkeypatch.setattr(health_module.tts, "get_tts_model", lambda: fake)

        body = client.get("/health").json()

        assert body["model_loaded"] is True
        assert body["model_size"] == "0.6B"

    def test_falls_back_to_model_size_when_current_size_missing(
        self, client, monkeypatch
    ):
        fake = _FakeTTSModel(loaded=True, model_size="1.7B")
        monkeypatch.setattr(health_module.tts, "get_tts_model", lambda: fake)

        body = client.get("/health").json()

        assert body["model_loaded"] is True
        assert body["model_size"] == "1.7B"

    def test_treats_is_loaded_exception_as_not_loaded(self, client, monkeypatch):
        fake = _FakeTTSModel(raise_on_is_loaded=True)
        monkeypatch.setattr(health_module.tts, "get_tts_model", lambda: fake)

        body = client.get("/health").json()

        assert body["model_loaded"] is False
        assert body["model_size"] is None

    def test_reports_cuda_gpu_when_available(self, client, monkeypatch):
        import torch

        monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
        monkeypatch.setattr(torch.cuda, "get_device_name", lambda idx=0: "RTX 4090")
        monkeypatch.setattr(
            torch.cuda, "memory_allocated", lambda *a, **k: 256 * 1024 * 1024
        )

        # Stub compatibility check so we don't depend on real driver info.
        import backend.backends.base as backend_base

        monkeypatch.setattr(
            backend_base, "check_cuda_compatibility", lambda: (True, None)
        )

        body = client.get("/health").json()

        assert body["gpu_available"] is True
        assert body["gpu_type"] == "CUDA (RTX 4090)"
        assert body["vram_used_mb"] == pytest.approx(256.0, rel=0, abs=0.5)
        assert body["backend_variant"] == "cuda"

    def test_surfaces_cuda_compatibility_warning(self, client, monkeypatch):
        import torch

        monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
        monkeypatch.setattr(torch.cuda, "get_device_name", lambda idx=0: "old-gpu")
        monkeypatch.setattr(torch.cuda, "memory_allocated", lambda *a, **k: 0)

        import backend.backends.base as backend_base

        monkeypatch.setattr(
            backend_base,
            "check_cuda_compatibility",
            lambda: (False, "unsupported compute capability"),
        )

        body = client.get("/health").json()

        assert body["gpu_compatibility_warning"] == "unsupported compute capability"

    def test_mlx_backend_marks_gpu_available_via_metal(self, client, monkeypatch):
        monkeypatch.setattr(health_module, "get_backend_type", lambda: "mlx")

        body = client.get("/health").json()

        assert body["backend_type"] == "mlx"
        assert body["gpu_available"] is True
        assert body["gpu_type"] == "Metal (Apple Silicon via MLX)"

    def test_backend_variant_env_override_wins(self, client, monkeypatch):
        monkeypatch.setenv("VOICEIT_BACKEND_VARIANT", "rocm")

        body = client.get("/health").json()

        assert body["backend_variant"] == "rocm"


# ---------------------------------------------------------------------------
# GET /health/filesystem
# ---------------------------------------------------------------------------


class TestFilesystemHealth:
    def test_reports_healthy_when_all_dirs_exist_and_writable(
        self, client, monkeypatch, tmp_path
    ):
        gens = tmp_path / "generations"
        caps = tmp_path / "captures"
        profs = tmp_path / "profiles"
        data = tmp_path / "data"
        for p in (gens, caps, profs, data):
            p.mkdir()

        monkeypatch.setattr(health_module.config, "get_generations_dir", lambda: gens)
        monkeypatch.setattr(health_module.config, "get_captures_dir", lambda: caps)
        monkeypatch.setattr(health_module.config, "get_profiles_dir", lambda: profs)
        monkeypatch.setattr(health_module.config, "get_data_dir", lambda: data)

        body = client.get("/health/filesystem").json()

        assert body["healthy"] is True
        assert body["disk_free_mb"] is not None
        assert body["disk_total_mb"] is not None
        assert body["disk_free_mb"] > 0
        # Every reported directory should exist, be writable, and carry no
        # error string.
        assert len(body["directories"]) == 4
        for entry in body["directories"]:
            assert entry["exists"] is True
            assert entry["writable"] is True
            assert entry["error"] is None
        # The reported paths should match the resolved tmp paths.
        paths = {entry["path"] for entry in body["directories"]}
        assert str(gens.resolve()) in paths
        assert str(caps.resolve()) in paths
        assert str(profs.resolve()) in paths
        assert str(data.resolve()) in paths

    def test_reports_unhealthy_when_directory_missing(
        self, client, monkeypatch, tmp_path
    ):
        existing = tmp_path / "exists"
        existing.mkdir()
        missing = tmp_path / "missing"  # deliberately not created
        data = tmp_path / "data"
        data.mkdir()

        monkeypatch.setattr(health_module.config, "get_generations_dir", lambda: existing)
        monkeypatch.setattr(health_module.config, "get_captures_dir", lambda: existing)
        monkeypatch.setattr(health_module.config, "get_profiles_dir", lambda: missing)
        monkeypatch.setattr(health_module.config, "get_data_dir", lambda: data)

        body = client.get("/health/filesystem").json()

        assert body["healthy"] is False
        missing_entry = next(
            e for e in body["directories"] if e["path"] == str(missing.resolve())
        )
        assert missing_entry["exists"] is False
        assert missing_entry["writable"] is False
        assert missing_entry["error"] == "Directory does not exist"

    def test_reports_permission_error_when_dir_not_writable(
        self, client, monkeypatch, tmp_path
    ):
        # Make one directory unwritable. We can't easily revoke write on a
        # tmp dir cross-platform, so simulate the failure by patching
        # Path.write_text to raise PermissionError.
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        data = tmp_path / "data"
        data.mkdir()

        monkeypatch.setattr(health_module.config, "get_generations_dir", lambda: readonly)
        monkeypatch.setattr(health_module.config, "get_captures_dir", lambda: other)
        monkeypatch.setattr(health_module.config, "get_profiles_dir", lambda: other)
        monkeypatch.setattr(health_module.config, "get_data_dir", lambda: data)

        original_write_text = Path.write_text

        def selective_write(self, *args, **kwargs):
            if readonly in self.parents:
                raise PermissionError("denied")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", selective_write)

        body = client.get("/health/filesystem").json()

        assert body["healthy"] is False
        ro_entry = next(
            e for e in body["directories"] if e["path"] == str(readonly.resolve())
        )
        assert ro_entry["exists"] is True
        assert ro_entry["writable"] is False
        assert ro_entry["error"] == "Permission denied"

    def test_disk_usage_failure_marks_unhealthy(
        self, client, monkeypatch, tmp_path
    ):
        # All dirs healthy, but shutil.disk_usage raises -> overall unhealthy
        # with disk fields left as None.
        good = tmp_path / "good"
        good.mkdir()
        data = tmp_path / "data"
        data.mkdir()

        monkeypatch.setattr(health_module.config, "get_generations_dir", lambda: good)
        monkeypatch.setattr(health_module.config, "get_captures_dir", lambda: good)
        monkeypatch.setattr(health_module.config, "get_profiles_dir", lambda: good)
        monkeypatch.setattr(health_module.config, "get_data_dir", lambda: data)

        import shutil

        def boom(_path):
            raise OSError("disk gone")

        monkeypatch.setattr(shutil, "disk_usage", boom)

        body = client.get("/health/filesystem").json()

        assert body["healthy"] is False
        assert body["disk_free_mb"] is None
        assert body["disk_total_mb"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
