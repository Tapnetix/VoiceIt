"""Tests for backend/routes/tasks.py.

The router exposes:

  - POST /tasks/clear   — clear all task + progress state
  - POST /cache/clear   — clear voice-prompt caches (memory + disk)
  - GET  /tasks/active  — list active downloads + generations

The route handlers compose the real TaskManager / ProgressManager / cache
utilities — those collaborators are first-party project modules, so we use
the real implementations and only isolate global singletons + on-disk state.
No first-party module mocks are used.
"""

from __future__ import annotations

import pytest
import torch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config as cfg
from backend.routes.tasks import router as tasks_router
from backend.utils import cache as cache_module
from backend.utils import progress as progress_module
from backend.utils import tasks as tasks_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_managers(monkeypatch, tmp_path):
    """Reset task + progress manager singletons and isolate the data dir.

    The managers are process-global; left alone they leak state across
    tests. The cache directory likewise lives on disk and must be
    redirected to ``tmp_path``.
    """
    monkeypatch.setattr(tasks_module, "_task_manager", None)
    monkeypatch.setattr(progress_module, "_progress_manager", None)

    # Reroute the configured data directory so cache files land in tmp.
    monkeypatch.setattr(cfg, "_data_dir", tmp_path.resolve())

    # Reset the in-memory cache (process-global dict).
    cache_module._memory_cache.clear()

    return {
        "task_manager": tasks_module.get_task_manager(),
        "progress_manager": progress_module.get_progress_manager(),
        "tmp_path": tmp_path,
    }


@pytest.fixture()
def client(fresh_managers):
    """Minimal FastAPI app mounting only the tasks router."""
    app = FastAPI()
    app.include_router(tasks_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /tasks/clear
# ---------------------------------------------------------------------------


class TestClearAllTasks:
    def test_returns_confirmation_message(self, client):
        response = client.post("/tasks/clear")

        assert response.status_code == 200
        assert response.json() == {"message": "All task state cleared"}

    def test_clears_active_downloads_and_generations(self, client, fresh_managers):
        tm = fresh_managers["task_manager"]
        tm.start_download("qwen-tts-1.7B")
        tm.start_generation(
            task_id="t1", profile_id="p1", text="hello world"
        )
        assert len(tm.get_active_downloads()) == 1
        assert len(tm.get_active_generations()) == 1

        client.post("/tasks/clear")

        assert tm.get_active_downloads() == []
        assert tm.get_active_generations() == []

    def test_clears_progress_manager_state(self, client, fresh_managers):
        pm = fresh_managers["progress_manager"]
        pm.update_progress(
            "qwen-tts-1.7B",
            current=50,
            total=100,
            filename="weights.bin",
            status="downloading",
        )
        assert pm.get_progress("qwen-tts-1.7B") is not None
        assert pm._last_notify_time  # throttle tracking populated

        client.post("/tasks/clear")

        assert pm.get_progress("qwen-tts-1.7B") is None
        assert pm._progress == {}
        assert pm._last_notify_time == {}
        assert pm._last_notify_progress == {}


# ---------------------------------------------------------------------------
# POST /cache/clear
# ---------------------------------------------------------------------------


class TestClearCache:
    def test_returns_success_payload_when_cache_empty(self, client):
        response = client.post("/cache/clear")

        assert response.status_code == 200
        body = response.json()
        assert body["message"] == "Voice prompt cache cleared successfully"
        assert body["files_deleted"] == 0

    def test_deletes_prompt_and_combined_audio_files(
        self, client, fresh_managers
    ):
        cache_dir = fresh_managers["tmp_path"] / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Two prompt files (one real torch tensor on disk, one stub) and
        # one combined-audio file. Only .prompt and combined_*.wav are
        # cleared; an unrelated file must survive.
        torch.save({"prompt": torch.zeros(2)}, cache_dir / "abc.prompt")
        torch.save({"prompt": torch.zeros(2)}, cache_dir / "def.prompt")
        (cache_dir / "combined_p1_xyz.wav").write_bytes(b"RIFFwav")
        (cache_dir / "unrelated.txt").write_text("keep me")

        response = client.post("/cache/clear")

        assert response.status_code == 200
        assert response.json()["files_deleted"] == 3
        assert not (cache_dir / "abc.prompt").exists()
        assert not (cache_dir / "def.prompt").exists()
        assert not (cache_dir / "combined_p1_xyz.wav").exists()
        assert (cache_dir / "unrelated.txt").exists()

    def test_clears_in_memory_cache(self, client):
        cache_module._memory_cache["k1"] = torch.zeros(1)
        cache_module._memory_cache["k2"] = {"prompt": torch.zeros(1)}

        response = client.post("/cache/clear")

        assert response.status_code == 200
        assert cache_module._memory_cache == {}

    def test_returns_500_when_cache_clear_fails(self, client, monkeypatch):
        def boom() -> int:
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(
            "backend.routes.tasks.clear_voice_prompt_cache", boom
        )

        response = client.post("/cache/clear")

        assert response.status_code == 500
        assert "Failed to clear cache" in response.json()["detail"]
        assert "disk on fire" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /tasks/active
# ---------------------------------------------------------------------------


class TestGetActiveTasks:
    def test_returns_empty_lists_when_nothing_active(self, client):
        response = client.get("/tasks/active")

        assert response.status_code == 200
        assert response.json() == {"downloads": [], "generations": []}

    def test_lists_active_downloads_tracked_by_task_manager(
        self, client, fresh_managers
    ):
        tm = fresh_managers["task_manager"]
        tm.start_download("qwen-tts-1.7B")

        response = client.get("/tasks/active")

        assert response.status_code == 200
        body = response.json()
        assert len(body["downloads"]) == 1
        download = body["downloads"][0]
        assert download["model_name"] == "qwen-tts-1.7B"
        assert download["status"] == "downloading"
        # No progress entry was created → progress fields are null.
        assert download["progress"] is None
        assert download["current"] is None
        assert download["total"] is None
        assert download["filename"] is None
        assert download["error"] is None

    def test_merges_progress_data_into_task_manager_download(
        self, client, fresh_managers
    ):
        tm = fresh_managers["task_manager"]
        pm = fresh_managers["progress_manager"]
        tm.start_download("qwen-tts-1.7B")
        pm.update_progress(
            "qwen-tts-1.7B",
            current=30,
            total=100,
            filename="weights.bin",
            status="downloading",
        )

        response = client.get("/tasks/active")

        assert response.status_code == 200
        download = response.json()["downloads"][0]
        assert download["model_name"] == "qwen-tts-1.7B"
        assert download["progress"] == pytest.approx(30.0)
        assert download["current"] == 30
        assert download["total"] == 100
        assert download["filename"] == "weights.bin"

    def test_includes_error_when_task_manager_marks_download_failed(
        self, client, fresh_managers
    ):
        tm = fresh_managers["task_manager"]
        tm.start_download("qwen-tts-1.7B")
        tm.error_download("qwen-tts-1.7B", "network refused")

        response = client.get("/tasks/active")

        assert response.status_code == 200
        download = response.json()["downloads"][0]
        assert download["status"] == "error"
        assert download["error"] == "network refused"

    def test_falls_back_to_progress_manager_error_field(
        self, client, fresh_managers
    ):
        """When the task manager has no error but the progress entry does,
        the route surfaces the progress-manager error."""
        tm = fresh_managers["task_manager"]
        pm = fresh_managers["progress_manager"]
        tm.start_download("qwen-tts-1.7B")
        # Seed progress and mark it as errored via the progress manager.
        pm.update_progress(
            "qwen-tts-1.7B",
            current=10,
            total=100,
            filename="weights.bin",
            status="downloading",
        )
        pm.mark_error("qwen-tts-1.7B", "checksum mismatch")

        response = client.get("/tasks/active")

        assert response.status_code == 200
        download = response.json()["downloads"][0]
        # task manager status stays "downloading"; error is borrowed from PM.
        assert download["error"] == "checksum mismatch"

    def test_lists_progress_only_downloads_with_iso_timestamp(
        self, client, fresh_managers
    ):
        """A model present in progress_manager but absent from task_manager
        is still surfaced; its started_at comes from the progress timestamp."""
        pm = fresh_managers["progress_manager"]
        pm.update_progress(
            "whisper-base",
            current=5,
            total=100,
            filename="tokenizer.json",
            status="downloading",
        )

        response = client.get("/tasks/active")

        assert response.status_code == 200
        downloads = response.json()["downloads"]
        assert len(downloads) == 1
        d = downloads[0]
        assert d["model_name"] == "whisper-base"
        assert d["status"] == "downloading"
        assert d["progress"] == pytest.approx(5.0)
        assert d["filename"] == "tokenizer.json"
        # started_at must be a parseable ISO timestamp.
        assert d["started_at"] is not None

    def test_progress_only_download_uses_now_when_timestamp_missing(
        self, client, fresh_managers
    ):
        """If the progress dict has no timestamp, started_at falls back to now."""
        pm = fresh_managers["progress_manager"]
        # Bypass update_progress so we control the dict shape: no timestamp.
        with pm._lock:
            pm._progress["whisper-base"] = {
                "model_name": "whisper-base",
                "current": 1,
                "total": 10,
                "progress": 10.0,
                "filename": "x.bin",
                "status": "downloading",
            }

        response = client.get("/tasks/active")

        assert response.status_code == 200
        downloads = response.json()["downloads"]
        assert len(downloads) == 1
        assert downloads[0]["model_name"] == "whisper-base"
        assert downloads[0]["started_at"] is not None

    def test_progress_only_download_handles_bad_timestamp(
        self, client, fresh_managers
    ):
        """A malformed timestamp string is tolerated — falls back to now."""
        pm = fresh_managers["progress_manager"]
        with pm._lock:
            pm._progress["whisper-base"] = {
                "model_name": "whisper-base",
                "current": 1,
                "total": 10,
                "progress": 10.0,
                "filename": "x.bin",
                "status": "downloading",
                "timestamp": "not-a-timestamp",
            }

        response = client.get("/tasks/active")

        assert response.status_code == 200
        downloads = response.json()["downloads"]
        assert len(downloads) == 1
        assert downloads[0]["model_name"] == "whisper-base"
        assert downloads[0]["started_at"] is not None

    def test_z_suffixed_iso_timestamp_is_parsed(self, client, fresh_managers):
        """A trailing 'Z' is normalised to '+00:00' before parsing."""
        pm = fresh_managers["progress_manager"]
        with pm._lock:
            pm._progress["whisper-base"] = {
                "model_name": "whisper-base",
                "current": 5,
                "total": 100,
                "progress": 5.0,
                "filename": "x.bin",
                "status": "downloading",
                "timestamp": "2026-06-20T00:00:00Z",
            }

        response = client.get("/tasks/active")

        assert response.status_code == 200
        downloads = response.json()["downloads"]
        assert len(downloads) == 1
        d = downloads[0]
        assert d["status"] == "downloading"
        # The Z timestamp should be parsed without error and surfaced.
        assert d["started_at"].startswith("2026-06-20")

    def test_progress_only_downloads_filter_out_non_active_statuses(
        self, client, fresh_managers
    ):
        """Per ProgressManager.get_all_active() only 'downloading' and
        'extracting' surface — terminal 'error' / 'complete' entries that
        the task manager no longer tracks are not reported as active."""
        pm = fresh_managers["progress_manager"]
        with pm._lock:
            pm._progress["whisper-base"] = {
                "model_name": "whisper-base",
                "current": 0,
                "total": 0,
                "progress": 0,
                "filename": None,
                "status": "error",
                "error": "boom",
                "timestamp": "2026-06-20T00:00:00Z",
            }

        response = client.get("/tasks/active")

        assert response.status_code == 200
        assert response.json()["downloads"] == []

    def test_lists_active_generations(self, client, fresh_managers):
        tm = fresh_managers["task_manager"]
        tm.start_generation(
            task_id="gen-1",
            profile_id="prof-1",
            text="Hello world this is a sample text",
        )

        response = client.get("/tasks/active")

        assert response.status_code == 200
        gens = response.json()["generations"]
        assert len(gens) == 1
        assert gens[0]["task_id"] == "gen-1"
        assert gens[0]["profile_id"] == "prof-1"
        assert gens[0]["text_preview"] == "Hello world this is a sample text"
        assert gens[0]["started_at"] is not None

    def test_truncates_long_generation_text_in_preview(
        self, client, fresh_managers
    ):
        tm = fresh_managers["task_manager"]
        long_text = "x" * 200
        tm.start_generation(task_id="gen-2", profile_id="p", text=long_text)

        response = client.get("/tasks/active")

        gens = response.json()["generations"]
        assert len(gens) == 1
        # TaskManager truncates to first 50 chars + "..."
        assert gens[0]["text_preview"].endswith("...")
        assert len(gens[0]["text_preview"]) == 53

    def test_lists_both_downloads_and_generations_together(
        self, client, fresh_managers
    ):
        tm = fresh_managers["task_manager"]
        pm = fresh_managers["progress_manager"]
        tm.start_download("qwen-tts-1.7B")
        pm.update_progress(
            "qwen-tts-1.7B", current=10, total=100, filename="w.bin"
        )
        tm.start_generation("g1", "p1", "abc")

        response = client.get("/tasks/active")

        body = response.json()
        assert {d["model_name"] for d in body["downloads"]} == {"qwen-tts-1.7B"}
        assert {g["task_id"] for g in body["generations"]} == {"g1"}
