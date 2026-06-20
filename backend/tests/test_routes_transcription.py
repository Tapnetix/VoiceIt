"""Tests for ``backend/routes/transcription.py``.

The ``POST /transcribe`` endpoint accepts a multipart upload, persists it to
a temp file, decodes the duration via :func:`load_audio`, then dispatches to
the Whisper STT backend. Three branches matter for behavior:

1. The request model size is invalid -> 400 with the supported list echoed.
2. The model is not loaded and not yet cached on disk -> 202 with a download
   notice; a background download task is started; the temp file is cleaned up.
3. The model is ready (loaded or already cached) -> 200 with the transcribed
   text and the audio's duration in seconds.

A generic exception inside the route surfaces as 500 with the message
preserved (so the caller can diagnose decode/transcribe failures rather than
seeing a bare 500).

Strategy: wire a minimal FastAPI app around the real router, stub the
``transcribe.get_whisper_model()`` factory with a fake backend, swap
``load_audio`` for a deterministic in-memory return, and replace the
fire-and-forget ``create_background_task`` with one that runs the coroutine
inline so the test can observe its effects. No first-party module's behavior
is mocked beyond those seams — assertions check the HTTP response, the
TaskManager state, and that the uploaded temp file is unlinked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake Whisper STT backend
# ---------------------------------------------------------------------------


class _FakeWhisperBackend:
    """Stand-in for the real Whisper STT backend.

    ``model_size`` defaults to ``"turbo"`` to match the route's behavior of
    falling back to the backend's current size when the request omits one.
    Each of ``is_loaded`` / ``_is_model_cached`` / ``load_model_async`` /
    ``transcribe`` is configurable per test so we can exercise the three
    branches of the route.
    """

    def __init__(
        self,
        *,
        model_size: str = "turbo",
        loaded: bool = True,
        cached_sizes: set[str] | None = None,
        transcribe_result: str = "hello world",
        load_raises: BaseException | None = None,
        transcribe_raises: BaseException | None = None,
    ):
        self.model_size = model_size
        self._loaded = loaded
        self._cached_sizes = cached_sizes if cached_sizes is not None else {model_size}
        self._transcribe_result = transcribe_result
        self._load_raises = load_raises
        self._transcribe_raises = transcribe_raises
        self.load_calls: list[str] = []
        self.transcribe_calls: list[tuple[str, str | None, str | None]] = []

    def is_loaded(self) -> bool:
        return self._loaded

    def _is_model_cached(self, model_size: str) -> bool:
        return model_size in self._cached_sizes

    async def load_model_async(self, model_size: str) -> None:
        self.load_calls.append(model_size)
        if self._load_raises is not None:
            raise self._load_raises
        self._loaded = True
        self.model_size = model_size
        self._cached_sizes.add(model_size)

    async def transcribe(
        self,
        audio_path: str,
        language: str | None,
        model_size: str | None,
    ) -> str:
        self.transcribe_calls.append((audio_path, language, model_size))
        if self._transcribe_raises is not None:
            raise self._transcribe_raises
        return self._transcribe_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_task_manager(monkeypatch):
    """Replace the module-global TaskManager with a fresh instance per test.

    The route reads it via ``get_task_manager()`` so swapping the global
    ensures cross-test isolation of ``start_download``/``complete_download``
    state. Returns the fresh TaskManager so tests can introspect it.
    """
    from backend.utils import tasks as tasks_mod

    fresh = tasks_mod.TaskManager()
    monkeypatch.setattr(tasks_mod, "_task_manager", fresh)
    return fresh


@pytest.fixture()
def inline_background_task(monkeypatch):
    """Run ``create_background_task`` coroutines synchronously inline.

    The production code fires the download as a detached task to keep the
    response latency-free. For tests we want the download side-effects
    (TaskManager state changes) to be observable before the test exits, so
    we replace the helper with one that awaits the coroutine on the running
    loop.
    """
    started: list[asyncio.Task] = []

    def _inline(coro):
        loop = asyncio.get_event_loop()
        task = loop.create_task(coro)
        started.append(task)
        return task

    import backend.routes.transcription as route_mod

    monkeypatch.setattr(route_mod, "create_background_task", _inline)
    return started


@pytest.fixture()
def fake_load_audio(monkeypatch):
    """Stub the lazy ``load_audio`` import so we don't decode real WAV files.

    Returns a 24000-sample mono buffer at 24 kHz (== 1.0 seconds) by default.
    Tests can override by re-monkeypatching ``backend.utils.audio.load_audio``.
    """

    def _stub(path: str, *args, **kwargs):
        return np.zeros(24000, dtype=np.float32), 24000

    import backend.utils.audio as audio_mod

    monkeypatch.setattr(audio_mod, "load_audio", _stub)
    return _stub


@pytest.fixture()
def backend_factory(monkeypatch):
    """Install a fake Whisper backend behind ``transcribe.get_whisper_model``.

    Yields a setter the test calls with its configured _FakeWhisperBackend;
    afterward, the route's ``transcribe.get_whisper_model()`` returns it.
    """
    holder: dict[str, _FakeWhisperBackend] = {}

    def _install(fake: _FakeWhisperBackend) -> _FakeWhisperBackend:
        holder["backend"] = fake
        import backend.services.transcribe as transcribe_svc

        monkeypatch.setattr(transcribe_svc, "get_whisper_model", lambda: fake)
        return fake

    return _install


@pytest.fixture()
def client(fresh_task_manager, inline_background_task, fake_load_audio, backend_factory):
    """Minimal FastAPI app exposing only the transcription router."""
    from backend.routes.transcription import router as transcription_router

    app = FastAPI()
    app.include_router(transcription_router)
    with TestClient(app) as c:
        yield c


def _wav_upload(content: bytes = b"RIFFfake-wav-bytes") -> dict:
    return {"file": ("clip.wav", content, "audio/wav")}


# ---------------------------------------------------------------------------
# Validation: invalid model size
# ---------------------------------------------------------------------------


def test_transcribe_returns_400_when_model_size_unsupported(client, backend_factory):
    """An unknown model name surfaces as 400 with the supported list in the detail.

    The route is the only layer that gates on the WHISPER_HF_REPOS keys, so
    a typo like ``ginormous`` must come back as an actionable error message
    (not a generic 500 from a downstream KeyError).
    """
    backend_factory(_FakeWhisperBackend())

    resp = client.post(
        "/transcribe",
        files=_wav_upload(),
        data={"model": "ginormous"},
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "ginormous" in detail
    # The supported sizes must be advertised so the caller can self-correct.
    for size in ("base", "small", "medium", "large", "turbo"):
        assert size in detail


# ---------------------------------------------------------------------------
# Happy path: model already loaded
# ---------------------------------------------------------------------------


def test_transcribe_returns_text_and_duration_when_model_ready(client, backend_factory):
    """When the requested model is loaded, the route returns text + duration.

    Duration is computed as ``len(audio)/sr`` from the decoded waveform —
    the stub returns 24000 samples at 24000 Hz, so the response must report
    exactly 1.0 seconds.
    """
    fake = backend_factory(
        _FakeWhisperBackend(
            model_size="turbo",
            loaded=True,
            transcribe_result="captured speech",
        )
    )

    resp = client.post(
        "/transcribe",
        files=_wav_upload(),
        data={"model": "turbo", "language": "en"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "captured speech"
    assert body["duration"] == pytest.approx(1.0)

    # The route hands the temp file path, language, and resolved model size
    # to the backend.
    assert len(fake.transcribe_calls) == 1
    _, language_arg, model_arg = fake.transcribe_calls[0]
    assert language_arg == "en"
    assert model_arg == "turbo"


def test_transcribe_falls_back_to_backend_default_model_when_form_omits_it(
    client, backend_factory
):
    """No ``model`` form field -> the route uses the backend's current size.

    The backend stub reports ``model_size="small"`` and is loaded, so the
    transcribe call must receive ``"small"`` as the model argument.
    """
    fake = backend_factory(
        _FakeWhisperBackend(model_size="small", loaded=True, transcribe_result="ok")
    )

    resp = client.post(
        "/transcribe",
        files=_wav_upload(),
        # No model field on purpose.
        data={"language": "en"},
    )

    assert resp.status_code == 200, resp.text
    assert fake.transcribe_calls[0][2] == "small"


def test_transcribe_passes_through_null_language_when_form_omits_it(
    client, backend_factory
):
    """When ``language`` is omitted, the backend receives ``None`` (auto-detect)."""
    fake = backend_factory(
        _FakeWhisperBackend(model_size="turbo", loaded=True, transcribe_result="auto")
    )

    resp = client.post("/transcribe", files=_wav_upload())

    assert resp.status_code == 200, resp.text
    assert fake.transcribe_calls[0][1] is None


def test_transcribe_reports_full_duration_for_multi_second_audio(
    client, backend_factory, monkeypatch
):
    """Duration is derived from the decoded audio, not the uploaded byte size.

    Override ``load_audio`` to return 72000 samples at 24 kHz (== 3.0 s).
    """
    import backend.utils.audio as audio_mod

    monkeypatch.setattr(
        audio_mod,
        "load_audio",
        lambda *_a, **_kw: (np.zeros(72000, dtype=np.float32), 24000),
    )
    backend_factory(_FakeWhisperBackend(loaded=True, transcribe_result="ok"))

    resp = client.post("/transcribe", files=_wav_upload())

    assert resp.status_code == 200, resp.text
    assert resp.json()["duration"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Model needs downloading -> 202 + background task
# ---------------------------------------------------------------------------


def test_transcribe_returns_202_when_requested_model_not_cached(
    client, backend_factory, fresh_task_manager
):
    """When the requested model isn't loaded and isn't cached, the route
    refuses to block on the download and instead returns 202 with a hint."""
    backend_factory(
        _FakeWhisperBackend(
            model_size="base",
            loaded=False,
            cached_sizes=set(),  # nothing cached
        )
    )

    resp = client.post(
        "/transcribe",
        files=_wav_upload(),
        data={"model": "small"},
    )

    assert resp.status_code == 202, resp.text
    detail = resp.json()["detail"]
    assert detail["downloading"] is True
    assert detail["model_name"] == "whisper-small"
    assert "small" in detail["message"]


def test_transcribe_202_path_starts_download_task_in_task_manager(
    client, backend_factory, fresh_task_manager
):
    """The 202 path must register a download in the TaskManager so the SSE
    progress endpoint and the UI's checklist surface it.

    The fixture's ``inline_background_task`` runs the coroutine inline, so
    by the time the response returns, the download has either completed
    (TaskManager entry removed) or errored. We assert that ``start_download``
    was recorded first.
    """
    # Track ``start_download`` calls by spying on the TaskManager itself.
    started: list[str] = []
    original_start = fresh_task_manager.start_download

    def _spy_start(name: str) -> None:
        started.append(name)
        original_start(name)

    fresh_task_manager.start_download = _spy_start  # type: ignore[method-assign]

    backend_factory(
        _FakeWhisperBackend(
            model_size="base",
            loaded=False,
            cached_sizes=set(),
        )
    )

    resp = client.post(
        "/transcribe", files=_wav_upload(), data={"model": "medium"}
    )

    assert resp.status_code == 202
    assert started == ["whisper-medium"]


def test_transcribe_download_task_marks_error_when_load_fails(
    client, backend_factory, fresh_task_manager
):
    """If the background load raises, the TaskManager entry transitions to
    the ``error`` status with the exception message recorded for the UI.

    This catches a regression where the route accidentally swallows the
    exception before recording the failure on the task — the checklist
    would otherwise be stuck on 'downloading' forever.
    """
    backend_factory(
        _FakeWhisperBackend(
            model_size="base",
            loaded=False,
            cached_sizes=set(),
            load_raises=RuntimeError("disk full"),
        )
    )

    resp = client.post(
        "/transcribe", files=_wav_upload(), data={"model": "large"}
    )

    assert resp.status_code == 202

    # Drain pending background tasks so the inline-task's exception handler
    # runs and writes the error to the TaskManager.
    async def _drain():
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.get_event_loop().run_until_complete(_drain())

    active = fresh_task_manager.get_active_downloads()
    assert len(active) == 1
    assert active[0].model_name == "whisper-large"
    assert active[0].status == "error"
    assert "disk full" in (active[0].error or "")


def test_transcribe_skips_download_when_already_loaded_with_same_size(
    client, backend_factory, fresh_task_manager
):
    """The already-loaded short-circuit must not register a download task,
    even if the model isn't reported as cached (loaded > cached).

    This pins the precedence: a model held in memory by the running process
    is treated as ready regardless of on-disk cache state.
    """
    fake = backend_factory(
        _FakeWhisperBackend(
            model_size="turbo",
            loaded=True,
            cached_sizes=set(),  # not on disk, but loaded in memory
            transcribe_result="memory-served",
        )
    )

    resp = client.post(
        "/transcribe", files=_wav_upload(), data={"model": "turbo"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == "memory-served"
    assert fresh_task_manager.get_active_downloads() == []
    assert fake.load_calls == []


def test_transcribe_skips_download_when_model_already_cached(
    client, backend_factory, fresh_task_manager
):
    """The already-cached short-circuit: a model present on disk but not yet
    loaded still goes straight to ``transcribe`` (the backend's transcribe
    method handles its own lazy load); no 202 is issued."""
    backend_factory(
        _FakeWhisperBackend(
            model_size="base",
            loaded=False,
            cached_sizes={"medium"},  # caller asks for medium, which is cached
            transcribe_result="from cache",
        )
    )

    resp = client.post(
        "/transcribe", files=_wav_upload(), data={"model": "medium"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == "from cache"
    assert fresh_task_manager.get_active_downloads() == []


# ---------------------------------------------------------------------------
# Generic failures -> 500 with message preserved
# ---------------------------------------------------------------------------


def test_transcribe_returns_500_with_message_when_backend_raises(
    client, backend_factory
):
    """An unexpected exception inside ``transcribe`` is mapped to a 500 whose
    detail carries the exception message — so the desktop client can show a
    diagnostic instead of an opaque "Internal Server Error"."""
    backend_factory(
        _FakeWhisperBackend(
            model_size="turbo",
            loaded=True,
            transcribe_raises=RuntimeError("cuda oom"),
        )
    )

    resp = client.post("/transcribe", files=_wav_upload())

    assert resp.status_code == 500
    assert "cuda oom" in resp.json()["detail"]


def test_transcribe_returns_500_when_audio_decode_fails(
    client, backend_factory, monkeypatch
):
    """A decode error from ``load_audio`` is caught and re-raised as 500.

    The route does not let the BaseException propagate to the ASGI layer
    (which would return a generic 500 with no detail), so we must see the
    underlying error message in ``detail``.
    """
    backend_factory(_FakeWhisperBackend(loaded=True))

    def _boom(*_a, **_kw):
        raise ValueError("bad wav header")

    import backend.utils.audio as audio_mod

    monkeypatch.setattr(audio_mod, "load_audio", _boom)

    resp = client.post("/transcribe", files=_wav_upload())

    assert resp.status_code == 500
    assert "bad wav header" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Temp file cleanup
# ---------------------------------------------------------------------------


def test_transcribe_unlinks_uploaded_temp_file_on_success(
    client, backend_factory, monkeypatch
):
    """The uploaded bytes are spilled to a NamedTemporaryFile that the route
    must delete in its ``finally`` block. We capture the path via load_audio
    (which the route calls with the temp path) and confirm the file is gone
    after the response is returned.
    """
    captured_paths: list[str] = []

    def _capture(path: str, *args, **kwargs):
        captured_paths.append(path)
        # Touch the file so we can verify it existed before cleanup.
        assert Path(path).exists()
        return np.zeros(24000, dtype=np.float32), 24000

    import backend.utils.audio as audio_mod

    monkeypatch.setattr(audio_mod, "load_audio", _capture)
    backend_factory(_FakeWhisperBackend(loaded=True, transcribe_result="ok"))

    resp = client.post("/transcribe", files=_wav_upload(b"RIFFcleanup-test"))

    assert resp.status_code == 200, resp.text
    assert len(captured_paths) == 1
    assert not Path(captured_paths[0]).exists(), (
        "Temp upload file must be unlinked after the response is built"
    )


def test_transcribe_unlinks_uploaded_temp_file_on_error(
    client, backend_factory, monkeypatch
):
    """Even when the backend raises, the temp file must be cleaned up —
    otherwise repeated failed uploads leak disk space."""
    captured_paths: list[str] = []

    def _capture(path: str, *args, **kwargs):
        captured_paths.append(path)
        return np.zeros(24000, dtype=np.float32), 24000

    import backend.utils.audio as audio_mod

    monkeypatch.setattr(audio_mod, "load_audio", _capture)
    backend_factory(
        _FakeWhisperBackend(
            loaded=True, transcribe_raises=RuntimeError("model crashed")
        )
    )

    resp = client.post("/transcribe", files=_wav_upload())

    assert resp.status_code == 500
    assert len(captured_paths) == 1
    assert not Path(captured_paths[0]).exists()


# ---------------------------------------------------------------------------
# Streaming upload (chunked reads)
# ---------------------------------------------------------------------------


def test_transcribe_persists_full_upload_even_when_larger_than_chunk_size(
    client, backend_factory, monkeypatch
):
    """The route reads the upload in 1 MB chunks. A payload larger than the
    chunk size must still arrive at disk completely — assert the spilled
    bytes match the upload exactly."""
    captured_bytes: list[bytes] = []

    def _capture(path: str, *args, **kwargs):
        captured_bytes.append(Path(path).read_bytes())
        return np.zeros(24000, dtype=np.float32), 24000

    import backend.utils.audio as audio_mod

    monkeypatch.setattr(audio_mod, "load_audio", _capture)
    backend_factory(_FakeWhisperBackend(loaded=True, transcribe_result="ok"))

    # 1.5 MB payload — forces at least two chunk reads.
    payload = b"A" * (1024 * 1024 + 512 * 1024)
    resp = client.post("/transcribe", files=_wav_upload(payload))

    assert resp.status_code == 200, resp.text
    assert len(captured_bytes) == 1
    assert captured_bytes[0] == payload


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
