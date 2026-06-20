"""Unit tests for ``backend/services/transcribe.py`` (U-py-044).

The service module is a thin wrapper that delegates STT-backend
construction and teardown to ``backend.backends.get_stt_backend``. Two
behaviors matter to callers:

1. ``get_whisper_model()`` returns the STT backend that
   ``get_stt_backend()`` provides — including the cache semantics of
   that factory (repeated calls hand back the same instance).
2. ``unload_whisper_model()`` calls ``unload_model()`` on whatever
   instance the factory currently provides, so a subsequent
   ``get_whisper_model()`` still sees a backend (the cache is not
   wiped by an unload).

We exercise the real ``backend.backends`` module rather than mocking it,
substituting only the concrete backend class behind the factory by
clearing the cached ``_stt_backend`` and stubbing the platform-dispatch
``get_backend_type()`` indirection so we never touch torch / mlx at
import time. The fake backend exposes the same Protocol surface but
records observable side effects (unload count) so the tests assert
behavior, not call shape on a mock.
"""

from __future__ import annotations

from typing import Optional

import pytest

from backend import backends as backends_mod
from backend.services import transcribe


# ---------------------------------------------------------------------------
# Fake STT backend
# ---------------------------------------------------------------------------


class _FakeSTTBackend:
    """Records ``unload_model`` invocations and reports loaded state.

    The real STTBackend Protocol also defines ``load_model`` /
    ``transcribe`` / ``is_loaded`` — we provide async stubs for
    completeness even though the service-under-test only calls
    ``unload_model``.
    """

    def __init__(self) -> None:
        self.unload_count = 0
        self._loaded = False
        self.model_size = "turbo"

    async def load_model(self, model_size: str) -> None:  # pragma: no cover - unused by service
        self.model_size = model_size
        self._loaded = True

    async def transcribe(  # pragma: no cover - unused by service
        self,
        audio_path: str,
        language: Optional[str] = None,
        model_size: Optional[str] = None,
    ) -> str:
        return ""

    def unload_model(self) -> None:
        self.unload_count += 1
        self._loaded = False

    def is_loaded(self) -> bool:
        return self._loaded


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_stt(monkeypatch) -> _FakeSTTBackend:
    """Install a fake STT backend into the ``backends`` module cache.

    ``backends.get_stt_backend()`` lazily constructs a singleton based on
    ``get_backend_type()``. Pre-seeding ``_stt_backend`` short-circuits
    that construction path so we never import torch / mlx and so every
    ``get_stt_backend()`` call returns *our* fake. Reset to None after
    the test so other tests get a fresh state.
    """
    fake = _FakeSTTBackend()
    monkeypatch.setattr(backends_mod, "_stt_backend", fake)
    return fake


# ---------------------------------------------------------------------------
# get_whisper_model
# ---------------------------------------------------------------------------


def test_get_whisper_model_returns_backend_instance_from_factory(fake_stt):
    """``get_whisper_model()`` returns whatever ``get_stt_backend()``
    currently holds — same identity, no copy or wrapper."""
    result = transcribe.get_whisper_model()

    assert result is fake_stt


def test_get_whisper_model_returns_same_instance_across_calls(fake_stt):
    """The wrapper inherits the factory's singleton behavior: repeated
    calls hand back the same backend object rather than constructing a
    new one each time. Callers rely on this so model state (loaded /
    cached weights) is preserved between requests."""
    first = transcribe.get_whisper_model()
    second = transcribe.get_whisper_model()

    assert first is second is fake_stt


def test_get_whisper_model_constructs_backend_when_cache_is_empty(monkeypatch):
    """When no STT backend has been built yet, ``get_whisper_model()``
    triggers construction via the factory. We force the PyTorch branch
    (so the test is deterministic regardless of host hardware) and
    confirm the returned object exposes the STT Protocol surface."""
    monkeypatch.setattr(backends_mod, "_stt_backend", None)

    # Force the platform dispatch to choose PyTorch — but stub the import
    # by providing a fake module before the factory reaches it.
    monkeypatch.setattr(
        backends_mod, "get_backend_type", lambda: "pytorch"
    )

    fake = _FakeSTTBackend()

    import sys
    import types

    fake_pytorch_module = types.ModuleType("backend.backends.pytorch_backend")
    fake_pytorch_module.PyTorchSTTBackend = lambda: fake  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "backend.backends.pytorch_backend", fake_pytorch_module
    )

    try:
        result = transcribe.get_whisper_model()
    finally:
        # Ensure we don't leak the fake into the global cache for other tests.
        backends_mod._stt_backend = None

    assert result is fake
    assert hasattr(result, "unload_model")
    assert hasattr(result, "is_loaded")


# ---------------------------------------------------------------------------
# unload_whisper_model
# ---------------------------------------------------------------------------


def test_unload_whisper_model_invokes_unload_on_current_backend(fake_stt):
    """``unload_whisper_model()`` resolves the current backend through
    the factory and calls its ``unload_model``. After the call the
    backend reports as not loaded — the observable outcome callers care
    about (free the model from memory)."""
    # Simulate a loaded model first so we can observe the transition.
    fake_stt._loaded = True

    transcribe.unload_whisper_model()

    assert fake_stt.unload_count == 1
    assert fake_stt.is_loaded() is False


def test_unload_whisper_model_returns_none(fake_stt):
    """The function is declared without a return value; callers chain it
    in cleanup contexts and treat ``None`` as success. Pin the contract."""
    assert transcribe.unload_whisper_model() is None


def test_unload_whisper_model_uses_same_backend_get_whisper_model_returns(fake_stt):
    """Unloading must operate on the backend that ``get_whisper_model``
    would hand to a caller — otherwise an unload would leave a stale
    loaded instance accessible. This guards against future refactors
    that introduce per-call construction in one path but not the other."""
    backend_seen_by_caller = transcribe.get_whisper_model()

    transcribe.unload_whisper_model()

    assert backend_seen_by_caller is fake_stt
    assert fake_stt.unload_count == 1


def test_unload_whisper_model_then_get_returns_same_instance(fake_stt):
    """Unloading frees weights but does NOT evict the backend from the
    singleton cache. A subsequent ``get_whisper_model()`` returns the
    same object (now in an unloaded state), so the next request can
    re-load it in place rather than paying a full reconstruction cost."""
    transcribe.unload_whisper_model()
    after = transcribe.get_whisper_model()

    assert after is fake_stt
    assert after.is_loaded() is False


def test_unload_whisper_model_is_idempotent_against_fake_backend(fake_stt):
    """Two consecutive unloads each delegate to the backend; the service
    layer does not short-circuit the second call. If a future change
    needs to introduce idempotency, it should live in the backend, not
    in this wrapper — this test pins that boundary."""
    transcribe.unload_whisper_model()
    transcribe.unload_whisper_model()

    assert fake_stt.unload_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
