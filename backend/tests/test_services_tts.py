"""Unit tests for ``backend/services/tts.py`` (U-py-049).

The service module is a thin wrapper that delegates TTS-backend
construction and teardown to ``backend.backends.get_tts_backend``, plus
a small WAV-encoding helper. Three behaviors matter to callers:

1. ``get_tts_model()`` returns the TTS backend that
   ``get_tts_backend()`` provides — including the cache semantics of
   that factory (repeated calls hand back the same instance).
2. ``unload_tts_model()`` calls ``unload_model()`` on whatever instance
   the factory currently provides, so a subsequent
   ``get_tts_model()`` still sees a backend (the cache is not wiped
   by an unload).
3. ``audio_to_wav_bytes(audio, sample_rate)`` encodes a numpy float
   array as a valid WAV byte string at the supplied sample rate that
   round-trips through ``soundfile.read``.

We exercise the real ``backend.backends`` module rather than mocking it,
substituting only the concrete backend behind the factory by pre-seeding
the per-engine cache for ``"qwen"`` (which is what ``get_tts_backend()``
asks for). The fake backend exposes the same Protocol surface but
records observable side effects (unload count) so the tests assert
behavior, not call shape on a mock.
"""

from __future__ import annotations

import io
from typing import List, Optional, Tuple

import numpy as np
import pytest
import soundfile as sf

from backend import backends as backends_mod
from backend.services import tts as tts_service


# ---------------------------------------------------------------------------
# Fake TTS backend
# ---------------------------------------------------------------------------


class _FakeTTSBackend:
    """Records ``unload_model`` invocations and reports loaded state.

    The real TTSBackend Protocol also defines ``load_model`` /
    ``create_voice_prompt`` / ``combine_voice_prompts`` / ``generate``
    / ``is_loaded`` / ``_get_model_path`` — we provide stubs for
    completeness even though the service-under-test only calls
    ``unload_model``.
    """

    def __init__(self) -> None:
        self.unload_count = 0
        self._loaded = False
        self.model_size = "1.7B"

    async def load_model(self, model_size: str) -> None:  # pragma: no cover - unused by service
        self.model_size = model_size
        self._loaded = True

    async def create_voice_prompt(  # pragma: no cover - unused by service
        self,
        audio_path: str,
        reference_text: str,
        use_cache: bool = True,
    ) -> Tuple[dict, bool]:
        return ({}, False)

    async def combine_voice_prompts(  # pragma: no cover - unused by service
        self,
        audio_paths: List[str],
        reference_texts: List[str],
    ) -> Tuple[np.ndarray, str]:
        return (np.zeros(1, dtype=np.float32), "")

    async def generate(  # pragma: no cover - unused by service
        self,
        text: str,
        voice_prompt: dict,
        language: str = "en",
        seed: Optional[int] = None,
        instruct: Optional[str] = None,
    ) -> Tuple[np.ndarray, int]:
        return (np.zeros(1, dtype=np.float32), 24000)

    def unload_model(self) -> None:
        self.unload_count += 1
        self._loaded = False

    def is_loaded(self) -> bool:
        return self._loaded

    def _get_model_path(self, model_size: str) -> str:  # pragma: no cover - unused by service
        return f"/fake/{model_size}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_tts(monkeypatch) -> _FakeTTSBackend:
    """Install a fake TTS backend into the ``backends`` per-engine cache.

    ``backends.get_tts_backend()`` is a thin wrapper around
    ``get_tts_backend_for_engine("qwen")`` which lazily constructs a
    singleton based on ``get_backend_type()``. Pre-seeding the
    ``_tts_backends`` dict for the ``"qwen"`` key short-circuits that
    construction path so we never import torch / mlx and so every
    ``get_tts_backend()`` call returns *our* fake. ``monkeypatch`` swaps
    the dict for an isolated copy so other tests see fresh state.
    """
    fake = _FakeTTSBackend()
    monkeypatch.setattr(backends_mod, "_tts_backends", {"qwen": fake})
    return fake


# ---------------------------------------------------------------------------
# get_tts_model
# ---------------------------------------------------------------------------


def test_get_tts_model_returns_backend_instance_from_factory(fake_tts):
    """``get_tts_model()`` returns whatever ``get_tts_backend()`` currently
    holds — same identity, no copy or wrapper."""
    result = tts_service.get_tts_model()

    assert result is fake_tts


def test_get_tts_model_returns_same_instance_across_calls(fake_tts):
    """The wrapper inherits the factory's singleton behavior: repeated
    calls hand back the same backend object rather than constructing a
    new one each time. Callers rely on this so model state (loaded /
    cached weights) is preserved between requests."""
    first = tts_service.get_tts_model()
    second = tts_service.get_tts_model()

    assert first is second is fake_tts


def test_get_tts_model_constructs_backend_when_cache_is_empty(monkeypatch):
    """When no Qwen TTS backend has been built yet,
    ``get_tts_model()`` triggers construction via the factory. We force
    the PyTorch branch (so the test is deterministic regardless of host
    hardware) and stub the lazy ``pytorch_backend`` import so we never
    touch real torch. The returned object must expose the TTS Protocol
    surface (``unload_model`` / ``is_loaded``)."""
    monkeypatch.setattr(backends_mod, "_tts_backends", {})

    # Force the platform dispatch to choose PyTorch.
    monkeypatch.setattr(
        backends_mod, "get_backend_type", lambda: "pytorch"
    )

    fake = _FakeTTSBackend()

    import sys
    import types

    fake_pytorch_module = types.ModuleType("backend.backends.pytorch_backend")
    fake_pytorch_module.PyTorchTTSBackend = lambda: fake  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "backend.backends.pytorch_backend", fake_pytorch_module
    )

    try:
        result = tts_service.get_tts_model()
    finally:
        # Don't leak the fake into the global cache for other tests.
        backends_mod._tts_backends.clear()

    assert result is fake
    assert hasattr(result, "unload_model")
    assert hasattr(result, "is_loaded")


# ---------------------------------------------------------------------------
# unload_tts_model
# ---------------------------------------------------------------------------


def test_unload_tts_model_invokes_unload_on_current_backend(fake_tts):
    """``unload_tts_model()`` resolves the current backend through the
    factory and calls its ``unload_model``. After the call the backend
    reports as not loaded — the observable outcome callers care about
    (free the model from memory)."""
    # Simulate a loaded model first so we can observe the transition.
    fake_tts._loaded = True

    tts_service.unload_tts_model()

    assert fake_tts.unload_count == 1
    assert fake_tts.is_loaded() is False


def test_unload_tts_model_returns_none(fake_tts):
    """The function is declared without a return value; callers chain it
    in cleanup contexts and treat ``None`` as success. Pin the contract."""
    assert tts_service.unload_tts_model() is None


def test_unload_tts_model_uses_same_backend_get_tts_model_returns(fake_tts):
    """Unloading must operate on the backend that ``get_tts_model``
    would hand to a caller — otherwise an unload would leave a stale
    loaded instance accessible. This guards against future refactors
    that introduce per-call construction in one path but not the other."""
    backend_seen_by_caller = tts_service.get_tts_model()

    tts_service.unload_tts_model()

    assert backend_seen_by_caller is fake_tts
    assert fake_tts.unload_count == 1


def test_unload_tts_model_then_get_returns_same_instance(fake_tts):
    """Unloading frees weights but does NOT evict the backend from the
    singleton cache. A subsequent ``get_tts_model()`` returns the same
    object (now in an unloaded state), so the next request can re-load
    it in place rather than paying a full reconstruction cost."""
    tts_service.unload_tts_model()
    after = tts_service.get_tts_model()

    assert after is fake_tts
    assert after.is_loaded() is False


def test_unload_tts_model_is_idempotent_against_fake_backend(fake_tts):
    """Two consecutive unloads each delegate to the backend; the service
    layer does not short-circuit the second call. If a future change
    needs to introduce idempotency, it should live in the backend, not
    in this wrapper — this test pins that boundary."""
    tts_service.unload_tts_model()
    tts_service.unload_tts_model()

    assert fake_tts.unload_count == 2


# ---------------------------------------------------------------------------
# audio_to_wav_bytes
# ---------------------------------------------------------------------------


def test_audio_to_wav_bytes_round_trips_through_soundfile():
    """The encoder produces a WAV byte string that ``soundfile.read``
    can decode back to the same waveform at the supplied sample rate.
    This is the load-bearing behavior — the function exists so HTTP
    responses can carry raw bytes that any audio client will accept."""
    sample_rate = 24000
    duration = 0.05  # 50 ms keeps the test fast
    t = np.linspace(0.0, duration, int(sample_rate * duration), endpoint=False)
    audio = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)

    wav_bytes = tts_service.audio_to_wav_bytes(audio, sample_rate)

    decoded, decoded_sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")

    assert decoded_sr == sample_rate
    assert decoded.shape == audio.shape
    # WAV at this depth/format is lossless for float32 PCM_16 within ~1e-4.
    np.testing.assert_allclose(decoded, audio, atol=1e-3)


def test_audio_to_wav_bytes_starts_with_riff_header():
    """Any valid WAV blob begins with the ``RIFF....WAVE`` magic — pin
    this so the byte string is recognized by every off-the-shelf audio
    consumer (browser <audio>, ffmpeg, libsndfile, etc.), not just by
    soundfile."""
    audio = np.zeros(100, dtype=np.float32)

    wav_bytes = tts_service.audio_to_wav_bytes(audio, 16000)

    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"


def test_audio_to_wav_bytes_encodes_supplied_sample_rate_into_header():
    """The sample rate the caller passes must appear in the WAV fmt
    chunk — otherwise playback would happen at the wrong pitch. We
    encode at 8000 Hz and a distinct 48000 Hz and confirm the decoded
    header matches each."""
    audio = np.zeros(50, dtype=np.float32)

    bytes_8k = tts_service.audio_to_wav_bytes(audio, 8000)
    bytes_48k = tts_service.audio_to_wav_bytes(audio, 48000)

    _, sr_8k = sf.read(io.BytesIO(bytes_8k))
    _, sr_48k = sf.read(io.BytesIO(bytes_48k))

    assert sr_8k == 8000
    assert sr_48k == 48000


def test_audio_to_wav_bytes_returns_non_empty_bytes_for_empty_audio():
    """A zero-length audio array still produces a valid (header-only)
    WAV blob — encoders that strip empty inputs would surprise callers
    that always expect to write _something_ to the response body. Pin
    the non-empty header contract."""
    audio = np.array([], dtype=np.float32)

    wav_bytes = tts_service.audio_to_wav_bytes(audio, 22050)

    # A bare WAV header is 44 bytes; we only assert presence of the magic
    # plus a non-trivial header so the test survives minor libsndfile
    # variations (e.g., extra metadata chunks).
    assert isinstance(wav_bytes, bytes)
    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"
    assert len(wav_bytes) >= 12


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
