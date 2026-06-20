"""Tests for backend/routes/llm.py — the /llm/generate endpoint.

The endpoint has a few decision branches:

  * If the requested model size is not in the known LLM configs, return 400.
  * If the model isn't cached locally yet, kick off a background download and
    return 202 ("downloading, please try again").
  * If the examples payload has any pair that isn't length 2, return 400.
  * Otherwise, call the LLM backend's ``generate`` coroutine and return the
    text (or 500 if it raises — the original exception text is swallowed so
    the client never sees filesystem paths).

We mount the router on a minimal FastAPI app and inject a tiny fake backend
that records the kwargs it was called with. No project-level mocks beyond
that single boundary; everything else (validation, task manager, background
task plumbing) runs for real.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routes.llm import router as llm_router
from backend.services import llm as llm_service
from backend.utils.tasks import get_task_manager


# ---------------------------------------------------------------------------
# Fake LLM backend — the single test double at the services.llm boundary.
# ---------------------------------------------------------------------------


class FakeLLMBackend:
    """In-memory stand-in for the LLMBackend protocol.

    Records calls to ``generate`` and ``load_model`` so tests can assert on
    them. ``raise_on_generate`` lets us exercise the 500 branch.
    """

    def __init__(
        self,
        *,
        model_size: str = "0.6B",
        loaded: bool = True,
        cached_sizes: Optional[set[str]] = None,
        raise_on_generate: Optional[Exception] = None,
        raise_on_load: Optional[Exception] = None,
        generate_text: str = "the assistant reply",
    ) -> None:
        self.model_size = model_size
        self._loaded = loaded
        self._cached_sizes = cached_sizes if cached_sizes is not None else {"0.6B", "1.7B", "4B"}
        self._raise_on_generate = raise_on_generate
        self._raise_on_load = raise_on_load
        self._generate_text = generate_text
        self.generate_calls: list[dict] = []
        self.load_calls: list[str] = []

    def is_loaded(self) -> bool:
        return self._loaded

    def _is_model_cached(self, model_size: str) -> bool:
        return model_size in self._cached_sizes

    async def load_model(self, model_size: str) -> None:
        self.load_calls.append(model_size)
        if self._raise_on_load is not None:
            raise self._raise_on_load
        self._cached_sizes.add(model_size)
        self._loaded = True
        self.model_size = model_size

    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        model_size: Optional[str] = None,
        examples=None,
    ) -> str:
        self.generate_calls.append(
            {
                "prompt": prompt,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "model_size": model_size,
                "examples": examples,
            }
        )
        if self._raise_on_generate is not None:
            raise self._raise_on_generate
        return self._generate_text

    def unload_model(self) -> None:
        self._loaded = False


# ---------------------------------------------------------------------------
# App fixtures
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(llm_router)
    return app


@pytest.fixture
def fake_backend(monkeypatch):
    """Default fake backend: model is loaded, all sizes cached, generate works."""
    backend = FakeLLMBackend()
    monkeypatch.setattr(llm_service, "get_llm_model", lambda: backend)
    # Reset global task manager state across tests.
    get_task_manager().clear_all()
    return backend


@pytest.fixture
def client(fake_backend):
    app = _build_app()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_returns_generated_text_with_default_model_size(client, fake_backend):
    """A simple prompt returns 200 and the backend's text under the default size."""
    r = client.post("/llm/generate", json={"prompt": "Hello, world."})
    assert r.status_code == 200
    body = r.json()
    assert body == {"text": "the assistant reply", "model_size": "0.6B"}

    # The backend was called once with the request's prompt and the resolved size.
    assert len(fake_backend.generate_calls) == 1
    call = fake_backend.generate_calls[0]
    assert call["prompt"] == "Hello, world."
    assert call["model_size"] == "0.6B"
    assert call["examples"] is None


def test_request_size_overrides_backend_default(client, fake_backend):
    """When the request specifies model_size, that wins over the backend's loaded size."""
    fake_backend.model_size = "0.6B"
    r = client.post(
        "/llm/generate",
        json={"prompt": "Summarize: foo.", "model_size": "1.7B"},
    )
    assert r.status_code == 200
    assert r.json()["model_size"] == "1.7B"
    assert fake_backend.generate_calls[0]["model_size"] == "1.7B"


def test_forwards_system_max_tokens_and_temperature(client, fake_backend):
    """All optional generation knobs are passed through verbatim to the backend."""
    r = client.post(
        "/llm/generate",
        json={
            "prompt": "Translate this.",
            "system": "You are a translator.",
            "max_tokens": 128,
            "temperature": 0.2,
        },
    )
    assert r.status_code == 200
    call = fake_backend.generate_calls[0]
    assert call["system"] == "You are a translator."
    assert call["max_tokens"] == 128
    assert call["temperature"] == pytest.approx(0.2)


def test_examples_are_forwarded_as_tuples(client, fake_backend):
    """Few-shot examples are forwarded as (user, assistant) tuples."""
    r = client.post(
        "/llm/generate",
        json={
            "prompt": "Refine this.",
            "examples": [["bad input", "good output"], ["another in", "another out"]],
        },
    )
    assert r.status_code == 200
    examples = fake_backend.generate_calls[0]["examples"]
    assert examples == [("bad input", "good output"), ("another in", "another out")]
    # And specifically tuples, not lists — the backend protocol declares tuples.
    assert all(isinstance(p, tuple) for p in examples)


# ---------------------------------------------------------------------------
# Validation branches
# ---------------------------------------------------------------------------


def test_unknown_model_size_returns_400_via_app_validation(client, fake_backend):
    """A model_size outside the known LLM configs is rejected.

    The Pydantic request model has a regex on model_size, so this is caught
    at the validation layer (422). The handler's own valid_sizes branch is
    exercised via the override path below.
    """
    r = client.post("/llm/generate", json={"prompt": "hi", "model_size": "13B"})
    assert r.status_code == 422


def test_unknown_model_size_at_handler_level_returns_400(monkeypatch):
    """When something coerces an out-of-range size past the request validator, the handler still rejects it.

    The Pydantic request model has a regex that catches bad sizes at the 422
    layer, but the handler keeps its own ``valid_sizes`` check as defensive
    code. We exercise that branch directly by calling the handler with a
    request constructed past the validator.
    """
    from backend import models
    from backend.routes.llm import llm_generate
    from fastapi import HTTPException

    backend = FakeLLMBackend(model_size="0.6B")
    monkeypatch.setattr(llm_service, "get_llm_model", lambda: backend)
    get_task_manager().clear_all()

    # Bypass the Pydantic regex by constructing the request via model_construct,
    # which skips validation. The handler must still reject the bad size.
    bad_request = models.LLMGenerateRequest.model_construct(
        prompt="hi",
        system=None,
        model_size="not-a-real-size",
        max_tokens=512,
        temperature=0.7,
        examples=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(llm_generate(bad_request))

    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert "not-a-real-size" in detail
    assert "Must be one of" in detail


def test_malformed_example_pair_returns_400(client, fake_backend):
    """Each example must be a 2-element [user, assistant] pair."""
    r = client.post(
        "/llm/generate",
        json={
            "prompt": "Refine.",
            "examples": [["only one element"]],
        },
    )
    assert r.status_code == 400
    assert "user, assistant" in r.json()["detail"]
    # And we never reached the backend.
    assert fake_backend.generate_calls == []


# ---------------------------------------------------------------------------
# Download branch
# ---------------------------------------------------------------------------


def test_uncached_model_triggers_background_download_and_returns_202(monkeypatch):
    """When the requested size isn't cached, return 202 and kick off a background load."""
    # 0.6B is the only cached size; request 1.7B to force the download path.
    download_started = asyncio.Event()

    class SlowLoadBackend(FakeLLMBackend):
        async def load_model(self, model_size: str) -> None:
            self.load_calls.append(model_size)
            download_started.set()
            # Yield long enough that the response returns before completion runs.
            await asyncio.sleep(0)
            self._cached_sizes.add(model_size)
            self._loaded = True
            self.model_size = model_size

    backend = SlowLoadBackend(model_size="0.6B", cached_sizes={"0.6B"})
    monkeypatch.setattr(llm_service, "get_llm_model", lambda: backend)
    get_task_manager().clear_all()

    app = _build_app()
    with TestClient(app) as c:
        r = c.post("/llm/generate", json={"prompt": "hi", "model_size": "1.7B"})

    assert r.status_code == 202
    body = r.json()
    assert body["downloading"] is True
    # The handler lowercases the size when building the progress key.
    assert body["model_name"] == "qwen3-1.7b"
    assert "1.7B" in body["message"]

    # The background download was scheduled — start_download flipped on, and
    # by the time the TestClient context exits the load coroutine has run.
    assert backend.load_calls == ["1.7B"]
    # And the backend was never called for generation.
    assert backend.generate_calls == []


def test_download_failure_records_error_on_task_manager(monkeypatch):
    """If load_model raises inside the background task, the error path on the task manager fires."""
    boom = RuntimeError("network down")

    class FailingLoadBackend(FakeLLMBackend):
        async def load_model(self, model_size: str) -> None:
            self.load_calls.append(model_size)
            raise boom

    backend = FailingLoadBackend(model_size="0.6B", cached_sizes={"0.6B"})
    monkeypatch.setattr(llm_service, "get_llm_model", lambda: backend)
    tm = get_task_manager()
    tm.clear_all()

    app = _build_app()
    with TestClient(app) as c:
        r = c.post("/llm/generate", json={"prompt": "hi", "model_size": "4B"})

    assert r.status_code == 202
    assert backend.load_calls == ["4B"]

    # The handler starts the download under the lowercased key, then the
    # background task's failure path stamps status="error" on it.
    downloads = {d.model_name: d for d in tm.get_active_downloads()}
    assert "qwen3-4b" in downloads
    entry = downloads["qwen3-4b"]
    assert entry.status == "error"
    assert entry.error == "network down"


# ---------------------------------------------------------------------------
# Generation-failure branch
# ---------------------------------------------------------------------------


def test_backend_exception_is_translated_to_generic_500(monkeypatch):
    """When backend.generate raises, the client gets a generic 500 — never the original message."""
    secret_path = "/home/someone/secret/path/qwen3-0.6b/weights.bin"
    backend = FakeLLMBackend(
        raise_on_generate=RuntimeError(f"CUDA OOM at {secret_path}"),
    )
    monkeypatch.setattr(llm_service, "get_llm_model", lambda: backend)
    get_task_manager().clear_all()

    app = _build_app()
    with TestClient(app) as c:
        r = c.post("/llm/generate", json={"prompt": "hi"})

    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "LLM generation failed"
    # The implementation detail (file path, OOM string) must not leak.
    assert secret_path not in r.text
    assert "CUDA" not in r.text


# ---------------------------------------------------------------------------
# Pydantic request-model validation (cheap, but exercises model wiring)
# ---------------------------------------------------------------------------


def test_empty_prompt_is_rejected(client, fake_backend):
    """The request model requires a non-empty prompt."""
    r = client.post("/llm/generate", json={"prompt": ""})
    assert r.status_code == 422
    assert fake_backend.generate_calls == []


def test_max_tokens_out_of_range_is_rejected(client, fake_backend):
    """max_tokens has a 1..4096 range on the request model."""
    r = client.post("/llm/generate", json={"prompt": "hi", "max_tokens": 99999})
    assert r.status_code == 422
    assert fake_backend.generate_calls == []
