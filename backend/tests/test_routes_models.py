"""Tests for backend/routes/models.py — model lifecycle endpoints.

Covers every route in the file plus the two filesystem helpers
(``_get_dir_size`` and ``_copy_with_progress``). The tests run a minimal
FastAPI app through ``TestClient`` against an on-disk temp HuggingFace
cache and real ``ProgressManager`` / ``TaskManager`` singletons (reset
per test). The only test doubles are at the external/ML-backend boundary
— ``backend.services.tts`` (real torch load is out of scope for a unit
test) and ``backend.backends`` registry lookups that would otherwise
walk the real ``huggingface_hub`` cache and import real backend
classes.

Routes covered:
- POST   /models/load
- POST   /models/unload
- POST   /models/{model_name}/unload
- GET    /models/progress/{model_name}
- GET    /models/cache-dir
- POST   /models/migrate
- GET    /models/migrate/progress
- GET    /models/status
- POST   /models/download
- POST   /models/download/cancel
- DELETE /models/{model_name}
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routes import models as models_route
from backend.routes.models import (
    _copy_with_progress,
    _get_dir_size,
    router as models_router,
)
from backend.utils import progress as progress_mod
from backend.utils import tasks as tasks_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_progress(monkeypatch):
    """Force a brand-new ProgressManager singleton for each test."""
    monkeypatch.setattr(progress_mod, "_progress_manager", None)
    yield progress_mod.get_progress_manager()
    monkeypatch.setattr(progress_mod, "_progress_manager", None)


@pytest.fixture()
def fresh_tasks(monkeypatch):
    """Force a brand-new TaskManager singleton for each test."""
    monkeypatch.setattr(tasks_mod, "_task_manager", None)
    yield tasks_mod.get_task_manager()
    monkeypatch.setattr(tasks_mod, "_task_manager", None)


@pytest.fixture()
def hf_cache(tmp_path, monkeypatch):
    """Point huggingface_hub's HF_HUB_CACHE at a temp directory for the test.

    Returns the temp Path. The route reads HF_HUB_CACHE via
    ``huggingface_hub.constants``, so we patch the constant on that module.
    """
    cache = tmp_path / "hf-cache"
    cache.mkdir(parents=True, exist_ok=True)

    from huggingface_hub import constants as hf_constants

    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(cache))
    return cache


@pytest.fixture()
def client(fresh_progress, fresh_tasks, hf_cache):
    """FastAPI TestClient with just the models router mounted."""
    app = FastAPI()
    app.include_router(models_router)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers — minimal fakes for the backend registry boundary
# ---------------------------------------------------------------------------


def _make_config(
    *,
    model_name: str = "qwen-tts-1.7B",
    display_name: str = "Qwen TTS 1.7B",
    engine: str = "qwen",
    hf_repo_id: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    model_size: str = "1.7B",
):
    """Build a duck-typed ModelConfig (the routes only read attributes)."""
    return SimpleNamespace(
        model_name=model_name,
        display_name=display_name,
        engine=engine,
        hf_repo_id=hf_repo_id,
        model_size=model_size,
        size_mb=3500,
    )


def _write_blob(repo_cache_dir: Path, *, filename: str = "model.safetensors", size: int = 32):
    """Drop a snapshots-rooted weight file inside an HF-shaped cache layout.

    Real HF cache layout: cache/models--<owner>--<repo>/snapshots/<sha>/<file>
    The route walks ``snapshots/`` recursively for known weight extensions.
    """
    snap = repo_cache_dir / "snapshots" / "abc123"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / filename).write_bytes(b"\x00" * size)
    # also create empty blobs dir — route inspects it for .incomplete files
    (repo_cache_dir / "blobs").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# _get_dir_size — sums recursively, ignores directories
# ---------------------------------------------------------------------------


def test_get_dir_size_sums_file_bytes_recursively(tmp_path):
    """Recurses into subdirs and sums only regular-file sizes."""
    (tmp_path / "a.bin").write_bytes(b"x" * 10)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 7)
    (sub / "deep").mkdir()
    (sub / "deep" / "c.bin").write_bytes(b"z" * 3)

    assert _get_dir_size(tmp_path) == 20


def test_get_dir_size_returns_zero_for_empty_tree(tmp_path):
    """An empty tree (only empty directories) sums to 0 bytes."""
    (tmp_path / "empty_sub").mkdir()
    assert _get_dir_size(tmp_path) == 0


# ---------------------------------------------------------------------------
# _copy_with_progress — copies tree + reports byte-level progress
# ---------------------------------------------------------------------------


def test_copy_with_progress_replicates_tree_and_returns_bytes_copied(tmp_path, fresh_progress):
    """Copies every file into the destination and returns the running byte total."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "top.bin").write_bytes(b"A" * 5)
    (src / "sub").mkdir()
    (src / "sub" / "nested.bin").write_bytes(b"B" * 11)

    total = 16
    final = _copy_with_progress(src, dst, fresh_progress, copied_so_far=0, total_bytes=total)

    assert final == total
    assert (dst / "top.bin").read_bytes() == b"A" * 5
    assert (dst / "sub" / "nested.bin").read_bytes() == b"B" * 11

    # Progress manager should have recorded the final state for "migration".
    snapshot = fresh_progress.get_progress("migration")
    assert snapshot is not None
    assert snapshot["current"] == total
    assert snapshot["total"] == total
    assert snapshot["status"] == "downloading"


# ---------------------------------------------------------------------------
# POST /models/load — happy path & error path
# ---------------------------------------------------------------------------


def test_load_model_returns_success_message_when_backend_loads(client, monkeypatch):
    """Returns the documented success message after the backend's async load resolves."""
    calls = []

    class FakeTTS:
        async def load_model_async(self, size):
            calls.append(size)

    from backend.services import tts as tts_service

    monkeypatch.setattr(tts_service, "get_tts_model", lambda: FakeTTS())

    r = client.post("/models/load", params={"model_size": "0.6B"})

    assert r.status_code == 200
    assert r.json() == {"message": "Model 0.6B loaded successfully"}
    assert calls == ["0.6B"]


def test_load_model_returns_500_when_backend_raises(client, monkeypatch):
    """Backend failures surface as HTTP 500 with the exception message."""

    class FakeTTS:
        async def load_model_async(self, size):
            raise RuntimeError("out of memory")

    from backend.services import tts as tts_service

    monkeypatch.setattr(tts_service, "get_tts_model", lambda: FakeTTS())

    r = client.post("/models/load")

    assert r.status_code == 500
    assert r.json() == {"detail": "out of memory"}


# ---------------------------------------------------------------------------
# POST /models/unload — happy & error
# ---------------------------------------------------------------------------


def test_unload_default_model_returns_success_message(client, monkeypatch):
    """Delegates to ``tts.unload_tts_model`` and returns the success message."""
    called = []

    from backend.services import tts as tts_service

    monkeypatch.setattr(tts_service, "unload_tts_model", lambda: called.append(True))

    r = client.post("/models/unload")

    assert r.status_code == 200
    assert r.json() == {"message": "Model unloaded successfully"}
    assert called == [True]


def test_unload_default_model_returns_500_on_failure(client, monkeypatch):
    """Errors from the backend bubble up as HTTP 500 with the message."""
    from backend.services import tts as tts_service

    def boom():
        raise RuntimeError("cannot unload")

    monkeypatch.setattr(tts_service, "unload_tts_model", boom)

    r = client.post("/models/unload")

    assert r.status_code == 500
    assert r.json() == {"detail": "cannot unload"}


# ---------------------------------------------------------------------------
# POST /models/{model_name}/unload — by-name with config lookup
# ---------------------------------------------------------------------------


def test_unload_by_name_returns_400_when_model_unknown(client, monkeypatch):
    """An unknown model_name yields HTTP 400 with the documented detail."""
    import backend.backends as backends_mod

    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: None)

    r = client.post("/models/does-not-exist/unload")

    assert r.status_code == 400
    assert r.json() == {"detail": "Unknown model: does-not-exist"}


def test_unload_by_name_reports_not_loaded_when_backend_says_so(client, monkeypatch):
    """When the backend says the model wasn't loaded, the response says so."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: cfg)
    monkeypatch.setattr(backends_mod, "unload_model_by_config", lambda c: False)

    r = client.post("/models/luxtts/unload")

    assert r.status_code == 200
    assert r.json() == {"message": "Model luxtts is not loaded"}


def test_unload_by_name_returns_success_when_unloaded(client, monkeypatch):
    """When unload_model_by_config returns True, the response confirms unload."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: cfg)
    monkeypatch.setattr(backends_mod, "unload_model_by_config", lambda c: True)

    r = client.post("/models/luxtts/unload")

    assert r.status_code == 200
    assert r.json() == {"message": "Model luxtts unloaded successfully"}


def test_unload_by_name_returns_500_when_backend_raises(client, monkeypatch):
    """Unexpected backend errors surface as 500."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: cfg)

    def kaboom(c):
        raise RuntimeError("nope")

    monkeypatch.setattr(backends_mod, "unload_model_by_config", kaboom)

    r = client.post("/models/luxtts/unload")

    assert r.status_code == 500
    assert r.json() == {"detail": "nope"}


# ---------------------------------------------------------------------------
# GET /models/cache-dir
# ---------------------------------------------------------------------------


def test_cache_dir_returns_huggingface_hub_cache_path(client, hf_cache):
    """Returns the path huggingface_hub.constants.HF_HUB_CACHE points at."""
    r = client.get("/models/cache-dir")

    assert r.status_code == 200
    assert r.json() == {"path": str(hf_cache)}


# ---------------------------------------------------------------------------
# GET /models/progress/{model_name} — SSE
# ---------------------------------------------------------------------------
#
# The SSE handlers are invoked directly with ``asyncio.run`` rather than via
# TestClient. The real ProgressManager.subscribe body is an infinite heartbeat
# loop with a 1-second timeout, which would block the TestClient socket. The
# direct-call pattern still drives the real handler, the real StreamingResponse
# wiring and the real ProgressManager subscription, but lets us assert the
# response shape and pull the first event off the generator before closing it.


def test_progress_stream_returns_streaming_response_with_sse_headers(fresh_progress):
    """The /models/progress endpoint returns a StreamingResponse with SSE headers."""
    from backend.routes.models import get_model_progress

    response = asyncio.run(get_model_progress("luxtts"))

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"


def test_progress_stream_replays_in_flight_progress_as_first_event(fresh_progress):
    """An in-flight download seeds the first SSE event with the current progress."""
    from backend.routes.models import get_model_progress

    fresh_progress.update_progress(
        "luxtts", current=42, total=100, filename="weights.bin", status="downloading"
    )

    async def _drive() -> str:
        response = await get_model_progress("luxtts")
        body_iter = response.body_iterator
        first = await anext(body_iter)
        await body_iter.aclose()
        return first if isinstance(first, str) else first.decode()

    payload = asyncio.run(_drive())

    assert payload.startswith("data: ")
    assert "luxtts" in payload
    assert "downloading" in payload


# ---------------------------------------------------------------------------
# GET /models/migrate/progress — SSE
# ---------------------------------------------------------------------------


def test_migration_progress_stream_returns_sse_headers(fresh_progress):
    """The /models/migrate/progress endpoint returns a StreamingResponse with SSE headers."""
    from backend.routes.models import get_migration_progress

    response = asyncio.run(get_migration_progress())

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"


# ---------------------------------------------------------------------------
# POST /models/migrate
# ---------------------------------------------------------------------------


def test_migrate_returns_404_when_source_missing(client, monkeypatch, tmp_path):
    """If the current HF cache dir doesn't exist on disk, return 404."""
    missing = tmp_path / "nope"
    from huggingface_hub import constants as hf_constants

    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(missing))

    r = client.post("/models/migrate", json={"destination": str(tmp_path / "elsewhere")})

    assert r.status_code == 404
    assert r.json() == {"detail": "Current model cache directory not found"}


def test_migrate_returns_400_when_source_equals_destination(client, hf_cache):
    """Migrating to the same directory is rejected with 400."""
    r = client.post("/models/migrate", json={"destination": str(hf_cache)})

    assert r.status_code == 400
    assert r.json() == {"detail": "Source and destination are the same directory"}


def test_migrate_returns_400_when_destination_is_inside_source(client, hf_cache):
    """Refuse a destination nested inside the current cache (would recurse)."""
    inside = hf_cache / "child"
    inside.mkdir()

    r = client.post("/models/migrate", json={"destination": str(inside)})

    assert r.status_code == 400
    assert r.json() == {"detail": "Destination cannot be inside the current cache directory"}


def test_migrate_with_no_models_returns_zero_moved(client, hf_cache, tmp_path, fresh_progress):
    """An empty cache short-circuits to ``{moved: 0}`` and marks migration complete."""
    destination = tmp_path / "new-cache"

    r = client.post("/models/migrate", json={"destination": str(destination)})

    assert r.status_code == 200
    body = r.json()
    assert body["moved"] == 0
    assert body["errors"] == []
    assert body["source"] == str(hf_cache)
    assert body["destination"] == str(destination)


def test_migrate_with_models_returns_source_and_destination(client, hf_cache, tmp_path):
    """When models exist, the route returns the source/destination pair and queues a job.

    The actual copy runs as a background task; this test asserts the
    synchronous response shape and that the destination directory is
    created (a prerequisite the route does up front).
    """
    # Create a fake model dir matching the ``models--`` prefix the route filters on.
    model_dir = hf_cache / "models--Qwen--Qwen3-TTS-12Hz-1.7B-Base"
    model_dir.mkdir(parents=True)
    (model_dir / "weights.bin").write_bytes(b"x" * 32)

    destination = tmp_path / "new-cache"

    r = client.post("/models/migrate", json={"destination": str(destination)})

    assert r.status_code == 200
    body = r.json()
    assert body["source"] == str(hf_cache)
    assert body["destination"] == str(destination)
    assert destination.exists()


# ---------------------------------------------------------------------------
# GET /models/status
# ---------------------------------------------------------------------------


def test_status_reports_undownloaded_model_when_cache_empty(client, monkeypatch, hf_cache):
    """A registered model with no files on disk reports downloaded=False, loaded=False."""
    import backend.backends as backends_mod

    cfg = _make_config(
        model_name="luxtts",
        display_name="LuxTTS (Fast, CPU-friendly)",
        engine="luxtts",
        hf_repo_id="YatharthS/LuxTTS",
    )
    monkeypatch.setattr(backends_mod, "get_all_model_configs", lambda: [cfg])
    monkeypatch.setattr(backends_mod, "check_model_loaded", lambda c: False)

    r = client.get("/models/status")

    assert r.status_code == 200
    items = r.json()["models"]
    assert len(items) == 1
    item = items[0]
    assert item["model_name"] == "luxtts"
    assert item["display_name"] == "LuxTTS (Fast, CPU-friendly)"
    assert item["hf_repo_id"] == "YatharthS/LuxTTS"
    assert item["downloaded"] is False
    assert item["downloading"] is False
    assert item["loaded"] is False
    assert item["size_mb"] is None


def test_status_reports_downloaded_model_with_size_when_weights_present(
    client, monkeypatch, hf_cache
):
    """When a snapshot directory holds a weight file, the route reports downloaded=True with size_mb."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_all_model_configs", lambda: [cfg])
    monkeypatch.setattr(backends_mod, "check_model_loaded", lambda c: False)

    # Lay down an HF-cache-shaped directory with a weights file the route recognises.
    repo_dir = hf_cache / "models--YatharthS--LuxTTS"
    _write_blob(repo_dir, filename="model.safetensors", size=2 * 1024 * 1024)

    r = client.get("/models/status")

    assert r.status_code == 200
    item = r.json()["models"][0]
    assert item["downloaded"] is True
    assert item["size_mb"] is not None
    assert item["size_mb"] >= 1.0  # ~2 MiB


def test_status_reports_loaded_true_when_backend_says_loaded(client, monkeypatch, hf_cache):
    """``loaded`` is sourced from ``check_model_loaded`` per config."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_all_model_configs", lambda: [cfg])
    monkeypatch.setattr(backends_mod, "check_model_loaded", lambda c: True)

    r = client.get("/models/status")

    assert r.json()["models"][0]["loaded"] is True


def test_status_reports_downloading_when_active_download_matches_repo(
    client, monkeypatch, hf_cache, fresh_tasks
):
    """An active download task for a registered model flips downloading=True and downloaded=False."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_all_model_configs", lambda: [cfg])
    monkeypatch.setattr(backends_mod, "check_model_loaded", lambda c: False)

    # Even though weights exist on disk, an in-flight download must mask
    # the "downloaded" signal so the UI shows the spinner, not "Ready".
    repo_dir = hf_cache / "models--YatharthS--LuxTTS"
    _write_blob(repo_dir, filename="model.safetensors", size=1024)

    fresh_tasks.start_download("luxtts")

    r = client.get("/models/status")

    item = r.json()["models"][0]
    assert item["downloading"] is True
    assert item["downloaded"] is False
    assert item["size_mb"] is None


# ---------------------------------------------------------------------------
# POST /models/download — happy & error
# ---------------------------------------------------------------------------


def test_download_returns_400_when_model_unknown(client, monkeypatch):
    """An unregistered model_name returns 400 with the documented detail."""
    import backend.backends as backends_mod

    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: None)

    r = client.post("/models/download", json={"model_name": "ghost"})

    assert r.status_code == 400
    assert r.json() == {"detail": "Unknown model: ghost"}


def test_download_starts_task_and_seeds_initial_progress(
    client, monkeypatch, fresh_tasks, fresh_progress
):
    """A known model triggers task_manager.start_download and seeds a 'Connecting...' progress entry."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: cfg)

    # A load func that yields a coroutine which suspends indefinitely; this
    # keeps the background download task in the "started" state long enough
    # for us to observe ``is_download_active`` without racing against the
    # complete_download cleanup.
    async def fake_load():
        await asyncio.Event().wait()  # never resolves

    monkeypatch.setattr(backends_mod, "get_model_load_func", lambda c: fake_load)

    r = client.post("/models/download", json={"model_name": "luxtts"})

    assert r.status_code == 200
    assert r.json() == {"message": "Model luxtts download started"}
    # The route registered the model in the active-download set.
    assert fresh_tasks.is_download_active("luxtts")
    # Initial progress entry was seeded with the "Connecting..." filename.
    snapshot = fresh_progress.get_progress("luxtts")
    assert snapshot is not None
    assert snapshot["filename"] == "Connecting to HuggingFace..."
    assert snapshot["status"] == "downloading"


# ---------------------------------------------------------------------------
# POST /models/download/cancel
# ---------------------------------------------------------------------------


def test_download_cancel_reports_no_task_when_nothing_active(client, fresh_tasks, fresh_progress):
    """Cancelling with no active task or progress returns the 'No active task' message."""
    r = client.post("/models/download/cancel", json={"model_name": "nobody"})

    assert r.status_code == 200
    assert r.json() == {"message": "No active task found for nobody"}


def test_download_cancel_removes_task_and_progress(client, fresh_tasks, fresh_progress):
    """An active download is cancelled and its progress entry is purged."""
    fresh_tasks.start_download("luxtts")
    fresh_progress.update_progress("luxtts", current=10, total=100, filename="weights.bin")

    r = client.post("/models/download/cancel", json={"model_name": "luxtts"})

    assert r.status_code == 200
    assert r.json() == {"message": "Download task for luxtts cancelled"}
    assert not fresh_tasks.is_download_active("luxtts")
    assert fresh_progress.get_progress("luxtts") is None


def test_download_cancel_purges_progress_even_without_active_task(client, fresh_progress):
    """A stale/errored progress entry (no active task) can still be dismissed."""
    fresh_progress.update_progress("luxtts", current=0, total=0, status="error")

    r = client.post("/models/download/cancel", json={"model_name": "luxtts"})

    assert r.status_code == 200
    assert r.json() == {"message": "Download task for luxtts cancelled"}
    assert fresh_progress.get_progress("luxtts") is None


# ---------------------------------------------------------------------------
# DELETE /models/{model_name}
# ---------------------------------------------------------------------------


def test_delete_returns_400_when_model_unknown(client, monkeypatch):
    """Unregistered model_name returns 400."""
    import backend.backends as backends_mod

    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: None)

    r = client.delete("/models/ghost")

    assert r.status_code == 400
    assert r.json() == {"detail": "Unknown model: ghost"}


def test_delete_returns_404_when_repo_cache_missing(client, monkeypatch, hf_cache):
    """A registered model with no on-disk cache returns 404 with the documented detail."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: cfg)
    monkeypatch.setattr(backends_mod, "unload_model_by_config", lambda c: False)

    r = client.delete("/models/luxtts")

    assert r.status_code == 404
    assert r.json() == {"detail": "Model luxtts not found in cache"}


def test_delete_removes_repo_cache_directory_and_returns_success(client, monkeypatch, hf_cache):
    """When the cache directory exists, it is removed and the success message returned."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: cfg)
    monkeypatch.setattr(backends_mod, "unload_model_by_config", lambda c: True)

    repo_dir = hf_cache / "models--YatharthS--LuxTTS"
    _write_blob(repo_dir, filename="model.safetensors", size=8)
    assert repo_dir.exists()

    r = client.delete("/models/luxtts")

    assert r.status_code == 200
    assert r.json() == {"message": "Model luxtts deleted successfully"}
    assert not repo_dir.exists()


def test_delete_returns_500_when_unload_step_raises(client, monkeypatch, hf_cache):
    """A failure inside unload_model_by_config (non-HTTP) is surfaced as 500."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: cfg)

    def kaboom(c):
        raise RuntimeError("unload broken")

    monkeypatch.setattr(backends_mod, "unload_model_by_config", kaboom)

    # Cache dir must exist so we don't short-circuit on the 404 branch — but the
    # error is raised before we reach the cache check anyway.
    (hf_cache / "models--YatharthS--LuxTTS").mkdir(parents=True)

    r = client.delete("/models/luxtts")

    assert r.status_code == 500
    assert "unload broken" in r.json()["detail"]


# ---------------------------------------------------------------------------
# /models/status — scan_cache_dir paths and resilience
# ---------------------------------------------------------------------------


def test_status_uses_scan_cache_dir_when_repo_lists_weight_files(
    client, monkeypatch, hf_cache
):
    """If huggingface_hub.scan_cache_dir reports the repo, the route uses its
    size_on_disk total rather than walking the snapshots directory itself."""
    import backend.backends as backends_mod
    import huggingface_hub as hh

    cfg = _make_config(
        model_name="luxtts",
        display_name="LuxTTS",
        engine="luxtts",
        hf_repo_id="YatharthS/LuxTTS",
    )
    monkeypatch.setattr(backends_mod, "get_all_model_configs", lambda: [cfg])
    monkeypatch.setattr(backends_mod, "check_model_loaded", lambda c: False)

    fake_file = SimpleNamespace(file_name="model.safetensors")
    fake_rev = SimpleNamespace(files=[fake_file], size_on_disk=4 * 1024 * 1024)
    fake_repo = SimpleNamespace(repo_id="YatharthS/LuxTTS", revisions=[fake_rev])
    fake_cache = SimpleNamespace(repos=[fake_repo])

    monkeypatch.setattr(hh, "scan_cache_dir", lambda: fake_cache)

    r = client.get("/models/status")

    assert r.status_code == 200
    item = r.json()["models"][0]
    assert item["downloaded"] is True
    # 4 MiB / (1024 * 1024) == 4.0
    assert item["size_mb"] == pytest.approx(4.0)


def test_status_treats_incomplete_blob_as_not_downloaded(client, monkeypatch, hf_cache):
    """A .incomplete file in the blobs dir means the download is mid-flight — the
    route must NOT report the model as downloaded even though scan_cache_dir
    listed weight files."""
    import backend.backends as backends_mod
    import huggingface_hub as hh

    cfg = _make_config(
        model_name="luxtts",
        display_name="LuxTTS",
        engine="luxtts",
        hf_repo_id="YatharthS/LuxTTS",
    )
    monkeypatch.setattr(backends_mod, "get_all_model_configs", lambda: [cfg])
    monkeypatch.setattr(backends_mod, "check_model_loaded", lambda c: False)

    fake_file = SimpleNamespace(file_name="model.safetensors")
    fake_rev = SimpleNamespace(files=[fake_file], size_on_disk=1024)
    fake_repo = SimpleNamespace(repo_id="YatharthS/LuxTTS", revisions=[fake_rev])
    fake_cache = SimpleNamespace(repos=[fake_repo])

    monkeypatch.setattr(hh, "scan_cache_dir", lambda: fake_cache)

    # Create the blobs dir with a .incomplete marker.
    blobs = hf_cache / "models--YatharthS--LuxTTS" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "partial.bin.incomplete").write_bytes(b"")

    r = client.get("/models/status")

    item = r.json()["models"][0]
    assert item["downloaded"] is False


def test_status_recovers_when_scan_cache_dir_raises(client, monkeypatch, hf_cache):
    """A raised scan_cache_dir falls back to the path-based detection without crashing."""
    import backend.backends as backends_mod
    import huggingface_hub as hh

    cfg = _make_config(
        model_name="luxtts",
        display_name="LuxTTS",
        engine="luxtts",
        hf_repo_id="YatharthS/LuxTTS",
    )
    monkeypatch.setattr(backends_mod, "get_all_model_configs", lambda: [cfg])
    monkeypatch.setattr(backends_mod, "check_model_loaded", lambda c: False)

    def boom():
        raise RuntimeError("scan boom")

    monkeypatch.setattr(hh, "scan_cache_dir", boom)

    # Lay down weights on disk so the path-based fallback can find them.
    repo_dir = hf_cache / "models--YatharthS--LuxTTS"
    _write_blob(repo_dir, filename="model.safetensors", size=1024)

    r = client.get("/models/status")

    assert r.status_code == 200
    item = r.json()["models"][0]
    assert item["downloaded"] is True


def test_status_isolates_failures_to_a_single_config(client, monkeypatch, hf_cache):
    """If processing one config raises, that row still surfaces (downloaded=False)
    while other rows remain unaffected — the per-row try/except is the safety net."""
    import backend.backends as backends_mod

    good = _make_config(
        model_name="good",
        display_name="Good",
        engine="luxtts",
        hf_repo_id="org/good",
    )
    bad = _make_config(
        model_name="bad",
        display_name="Bad",
        engine="luxtts",
        hf_repo_id="org/bad",
    )

    monkeypatch.setattr(backends_mod, "get_all_model_configs", lambda: [good, bad])

    def loaded_check(cfg):
        if cfg.model_name == "bad":
            # ``check_model_loaded`` is wrapped in try/except in both the
            # happy and outer-fallback paths; raising here exercises the
            # ``except Exception: loaded = False`` branches.
            raise RuntimeError("loaded check failed")
        return False

    monkeypatch.setattr(backends_mod, "check_model_loaded", loaded_check)

    r = client.get("/models/status")

    assert r.status_code == 200
    items = {item["model_name"]: item for item in r.json()["models"]}
    assert items["good"]["loaded"] is False
    assert items["bad"]["loaded"] is False
    assert items["bad"]["downloaded"] is False


# ---------------------------------------------------------------------------
# Download background task — error path
# ---------------------------------------------------------------------------


def test_download_marks_task_errored_when_load_func_raises(
    client, monkeypatch, fresh_tasks, fresh_progress
):
    """If the load function raises, the background task flips the download to error state.

    Run as an async test so the background task created by the route handler
    actually executes inside our event loop before we inspect ``fresh_tasks``.
    """
    import backend.backends as backends_mod
    from backend.routes.models import trigger_model_download
    from backend.models import ModelDownloadRequest

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: cfg)

    async def fake_load():
        raise RuntimeError("download exploded")

    monkeypatch.setattr(backends_mod, "get_model_load_func", lambda c: fake_load)

    async def _drive():
        result = await trigger_model_download(ModelDownloadRequest(model_name="luxtts"))
        # The route schedules ``download_in_background()`` as a background
        # task. Give the loop one full pass so that coroutine runs to the
        # except branch.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return result

    body = asyncio.run(_drive())

    assert body == {"message": "Model luxtts download started"}
    active = {task.model_name: task for task in fresh_tasks.get_active_downloads()}
    assert "luxtts" in active
    assert active["luxtts"].status == "error"
    assert "download exploded" in (active["luxtts"].error or "")


def test_download_completes_task_when_load_func_resolves(
    client, monkeypatch, fresh_tasks, fresh_progress
):
    """When the load function resolves successfully, the background task removes
    the active-download entry via ``complete_download``."""
    import backend.backends as backends_mod
    from backend.routes.models import trigger_model_download
    from backend.models import ModelDownloadRequest

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: cfg)

    async def fake_load():
        return None

    monkeypatch.setattr(backends_mod, "get_model_load_func", lambda c: fake_load)

    async def _drive():
        await trigger_model_download(ModelDownloadRequest(model_name="luxtts"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drive())

    assert not fresh_tasks.is_download_active("luxtts")


# ---------------------------------------------------------------------------
# Migrate background — exercises the in-process copy + cleanup
# ---------------------------------------------------------------------------


def test_migrate_same_fs_moves_models_via_background_task(
    monkeypatch, hf_cache, tmp_path, fresh_progress
):
    """A same-filesystem migration moves each ``models--*`` dir to the destination
    and the background task marks migration complete.

    Driven via ``asyncio.run`` rather than TestClient so we can yield to the
    event loop and let the route's ``create_background_task`` coroutine run
    to completion before asserting on the destination filesystem.
    """
    a = hf_cache / "models--org--a"
    a.mkdir()
    (a / "file.bin").write_bytes(b"hello")
    b = hf_cache / "models--org--b"
    b.mkdir()
    (b / "file.bin").write_bytes(b"world")

    destination = tmp_path / "new-cache"

    from backend.routes.models import migrate_models
    from backend.models import ModelMigrateRequest

    async def _drive():
        result = await migrate_models(ModelMigrateRequest(destination=str(destination)))
        # Yield repeatedly so the background migrate_background() coroutine runs.
        for _ in range(20):
            await asyncio.sleep(0)
        return result

    body = asyncio.run(_drive())

    assert body["source"] == str(hf_cache)
    assert body["destination"] == str(destination)
    moved_a = destination / "models--org--a" / "file.bin"
    moved_b = destination / "models--org--b" / "file.bin"
    assert moved_a.read_bytes() == b"hello"
    assert moved_b.read_bytes() == b"world"
    # Source dirs got moved away.
    assert not a.exists()
    assert not b.exists()
    # Final migration progress is "complete".
    snapshot = fresh_progress.get_progress("migration")
    assert snapshot is not None
    assert snapshot["status"] == "complete"


def test_migrate_same_fs_records_per_model_errors_without_aborting(
    monkeypatch, hf_cache, tmp_path, fresh_progress
):
    """A per-model shutil.move failure is collected in the errors list rather
    than aborting the whole migration."""
    a = hf_cache / "models--org--a"
    a.mkdir()
    (a / "file.bin").write_bytes(b"ok")
    b = hf_cache / "models--org--bad"
    b.mkdir()
    (b / "file.bin").write_bytes(b"bad")

    destination = tmp_path / "new-cache"

    import shutil as shutil_mod

    real_move = shutil_mod.move

    def fake_move(src, dst):
        if "bad" in str(src):
            raise OSError("permission denied")
        return real_move(src, dst)

    monkeypatch.setattr("backend.routes.models.shutil.move", fake_move)

    from backend.routes.models import migrate_models
    from backend.models import ModelMigrateRequest

    async def _drive():
        await migrate_models(ModelMigrateRequest(destination=str(destination)))
        for _ in range(20):
            await asyncio.sleep(0)

    asyncio.run(_drive())

    # Good model still arrived at the destination.
    assert (destination / "models--org--a" / "file.bin").exists()
    # Background task finishes (marked complete) despite the per-model error.
    snapshot = fresh_progress.get_progress("migration")
    assert snapshot is not None
    assert snapshot["status"] == "complete"


# ---------------------------------------------------------------------------
# DELETE — rmtree raises OSError
# ---------------------------------------------------------------------------


def test_delete_returns_500_when_rmtree_raises_oserror(client, monkeypatch, hf_cache):
    """A shutil.rmtree OSError surfaces as a 500 with the documented detail."""
    import backend.backends as backends_mod

    cfg = _make_config(model_name="luxtts", engine="luxtts", hf_repo_id="YatharthS/LuxTTS")
    monkeypatch.setattr(backends_mod, "get_model_config", lambda name: cfg)
    monkeypatch.setattr(backends_mod, "unload_model_by_config", lambda c: True)

    repo_dir = hf_cache / "models--YatharthS--LuxTTS"
    repo_dir.mkdir(parents=True)

    def fake_rmtree(path):
        raise OSError("filesystem busy")

    monkeypatch.setattr("backend.routes.models.shutil.rmtree", fake_rmtree)

    r = client.delete("/models/luxtts")

    assert r.status_code == 500
    assert "filesystem busy" in r.json()["detail"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
