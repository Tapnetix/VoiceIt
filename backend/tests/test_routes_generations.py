"""Unit tests for backend.routes.generations.

Covers the FastAPI endpoints (TestClient) and the in-module helpers
(``_get_or_create_import_profile``, ``_resolve_generation_engine``) and
guarantees that every code path in the route module is exercised at least
once. The TTS queue and the heavy ``run_generation`` coroutine are replaced
with lightweight stubs so the tests do not depend on a GPU or models.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import uuid
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import (
    Base,
    Generation as DBGeneration,
    VoiceProfile as DBVoiceProfile,
    get_db,
)


# ---------------------------------------------------------------------------
# Fixtures: temp DB + isolated FastAPI app with the generations router only
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine_and_session(tmp_path):
    db_path = tmp_path / "test_routes_generations.db"
    eng = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    return eng, TestSession


@pytest.fixture()
def temp_db(engine_and_session):
    _, TestSession = engine_and_session
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def stub_run_generation(monkeypatch):
    """Replace the route module's run_generation reference with a coroutine
    factory that returns a harmless awaitable (closed immediately).

    The router enqueues whatever the call returns; the enqueue stub drains it.
    """
    calls: list[dict] = []

    async def _empty_coro():
        return None

    def _stub(**kwargs):
        calls.append(kwargs)
        return _empty_coro()

    import backend.routes.generations as gens_module
    monkeypatch.setattr(gens_module, "run_generation", _stub)
    return calls


@pytest.fixture()
def enqueue_calls(monkeypatch):
    """Capture enqueue_generation calls without running the coroutine."""
    calls: list[tuple[str, object]] = []

    def _stub(gen_id, coro):
        # Close the coroutine so we don't get "never awaited" warnings.
        try:
            coro.close()
        except Exception:
            pass
        calls.append((gen_id, coro))

    import backend.routes.generations as gens_module
    monkeypatch.setattr(gens_module, "enqueue_generation", _stub)
    return calls


@pytest.fixture()
def cancel_state(monkeypatch):
    """Stub cancel_generation_job inside the routes module. Returns a
    mutable holder so individual tests can choose the value to return."""
    state = {"value": "running"}

    def _stub(generation_id):
        return state["value"]

    import backend.routes.generations as gens_module
    monkeypatch.setattr(gens_module, "cancel_generation_job", _stub)
    return state


@pytest.fixture()
def client(engine_and_session, tmp_path, monkeypatch, enqueue_calls, stub_run_generation):
    """Build a FastAPI app with only the generations router and a temp DB."""
    _, TestSession = engine_and_session

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    # Pin config data dir under tmp_path so import_audio writes into a sandbox.
    monkeypatch.setenv("VOICEIT_DATA_DIR", str(tmp_path))
    import backend.config as _cfg
    _cfg._data_dir = tmp_path

    from backend.routes.generations import router as gen_router

    app = FastAPI()
    app.include_router(gen_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_preset_profile(db, profile_id: str | None = None) -> DBVoiceProfile:
    """Insert a preset profile (kokoro engine) so /generate works."""
    profile = DBVoiceProfile(
        id=profile_id or str(uuid.uuid4()),
        name="Preset Test Voice",
        description="Test voice",
        language="en",
        voice_type="preset",
        preset_engine="kokoro",
        preset_voice_id="af_heart",
        default_engine="kokoro",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _make_generation_row(
    db, profile_id: str, *, status: str = "completed", engine: str = "kokoro"
) -> DBGeneration:
    gen = DBGeneration(
        id=str(uuid.uuid4()),
        profile_id=profile_id,
        text="Hello world.",
        language="en",
        seed=42,
        instruct=None,
        engine=engine,
        model_size="1.7B",
        status=status,
        audio_path="generations/test.wav",
        duration=1.0,
        source="manual",
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen


def _write_tiny_wav() -> bytes:
    """Build an in-memory mono 16-bit PCM WAV that load_audio can decode."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        # 0.1s of silence
        wav.writeframes(b"\x00\x00" * 2400)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _resolve_generation_engine — pure helper
# ---------------------------------------------------------------------------


class _DummyData:
    def __init__(self, engine=None):
        self.engine = engine


class _DummyProfile:
    def __init__(self, default_engine=None, preset_engine=None):
        self.default_engine = default_engine
        self.preset_engine = preset_engine


def test_resolve_engine_prefers_explicit_request_engine():
    from backend.routes.generations import _resolve_generation_engine

    data = _DummyData(engine="chatterbox")
    profile = _DummyProfile(default_engine="kokoro", preset_engine="kokoro")

    assert _resolve_generation_engine(data, profile) == "chatterbox"


def test_resolve_engine_falls_back_to_default_engine():
    from backend.routes.generations import _resolve_generation_engine

    data = _DummyData(engine=None)
    profile = _DummyProfile(default_engine="kokoro", preset_engine=None)

    assert _resolve_generation_engine(data, profile) == "kokoro"


def test_resolve_engine_falls_back_to_preset_engine():
    from backend.routes.generations import _resolve_generation_engine

    data = _DummyData(engine=None)
    profile = _DummyProfile(default_engine=None, preset_engine="kokoro")

    assert _resolve_generation_engine(data, profile) == "kokoro"


def test_resolve_engine_defaults_to_qwen_when_nothing_set():
    from backend.routes.generations import _resolve_generation_engine

    data = _DummyData(engine=None)
    profile = _DummyProfile(default_engine=None, preset_engine=None)

    assert _resolve_generation_engine(data, profile) == "qwen"


# ---------------------------------------------------------------------------
# _get_or_create_import_profile — singleton helper
# ---------------------------------------------------------------------------


def test_get_or_create_import_profile_creates_singleton(temp_db):
    from backend.routes.generations import _get_or_create_import_profile

    first = _get_or_create_import_profile(temp_db)
    second = _get_or_create_import_profile(temp_db)

    assert first.id == second.id
    assert first.name == "Imported Audio"
    assert first.voice_type == "import"


def test_get_or_create_import_profile_returns_existing_row(temp_db):
    """If a row with the singleton name already exists, return it as-is."""
    from backend.routes.generations import (
        _get_or_create_import_profile,
        IMPORTED_AUDIO_PROFILE_NAME,
    )

    existing = DBVoiceProfile(
        id="existing-import-id",
        name=IMPORTED_AUDIO_PROFILE_NAME,
        voice_type="import",
        language="en",
    )
    temp_db.add(existing)
    temp_db.commit()

    found = _get_or_create_import_profile(temp_db)
    assert found.id == "existing-import-id"


# ---------------------------------------------------------------------------
# POST /generate
# ---------------------------------------------------------------------------


def test_generate_returns_404_when_profile_missing(client):
    resp = client.post(
        "/generate",
        json={
            "profile_id": "does-not-exist",
            "text": "Hello.",
            "language": "en",
            "engine": "kokoro",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Profile not found"


def test_generate_creates_generation_and_enqueues(
    client, engine_and_session, enqueue_calls
):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    profile_id = profile.id
    db.close()

    resp = client.post(
        "/generate",
        json={
            "profile_id": profile_id,
            "text": "Hello world.",
            "language": "en",
            "engine": "kokoro",
            "model_size": "1.7B",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile_id"] == profile_id
    assert body["text"] == "Hello world."
    assert body["status"] == "generating"
    assert body["source"] == "manual"

    # Persisted as a Generation row
    db = TestSession()
    saved = db.query(DBGeneration).filter_by(id=body["id"]).first()
    assert saved is not None
    assert saved.status == "generating"
    db.close()

    # Enqueue hit with the new generation_id
    assert len(enqueue_calls) == 1
    assert enqueue_calls[0][0] == body["id"]


def test_generate_rejects_engine_incompatible_with_preset(
    client, engine_and_session
):
    """A preset profile (kokoro) cannot be driven with a different engine."""
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    profile_id = profile.id
    db.close()

    resp = client.post(
        "/generate",
        json={
            "profile_id": profile_id,
            "text": "Hello.",
            "language": "en",
            "engine": "qwen",  # mismatched: preset only supports kokoro
        },
    )
    assert resp.status_code == 400
    assert "kokoro" in resp.json()["detail"]


def test_generate_passes_effects_chain_from_request_body(
    client, engine_and_session, enqueue_calls, stub_run_generation
):
    """Explicit effects_chain in the request overrides any profile default."""
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    profile_id = profile.id
    # Profile has a default effects chain — request override must win
    profile.effects_chain = json.dumps([{"type": "reverb", "wet": 0.4}])
    db.add(profile)
    db.commit()
    db.close()

    resp = client.post(
        "/generate",
        json={
            "profile_id": profile_id,
            "text": "Hello.",
            "language": "en",
            "engine": "kokoro",
            "effects_chain": [
                {"type": "compressor", "enabled": True, "params": {"threshold_db": -20.0}}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    # run_generation captured the effects_chain from the request, not the profile.
    assert stub_run_generation
    last = stub_run_generation[-1]
    assert last["effects_chain"] == [
        {"type": "compressor", "enabled": True, "params": {"threshold_db": -20.0}}
    ]


def test_generate_falls_back_to_profile_effects_chain(
    client, engine_and_session, enqueue_calls, stub_run_generation
):
    """When no request effects_chain, the profile's stored chain is used."""
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    profile_id = profile.id
    profile.effects_chain = json.dumps([{"type": "reverb", "wet": 0.5}])
    db.add(profile)
    db.commit()
    db.close()

    resp = client.post(
        "/generate",
        json={
            "profile_id": profile_id,
            "text": "Hello.",
            "language": "en",
            "engine": "kokoro",
        },
    )
    assert resp.status_code == 200, resp.text

    last = stub_run_generation[-1]
    assert last["effects_chain"] == [{"type": "reverb", "wet": 0.5}]


def test_generate_swallows_corrupt_profile_effects_chain(
    client, engine_and_session, enqueue_calls, stub_run_generation
):
    """A JSON-broken stored chain doesn't crash /generate; chain stays None."""
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    profile_id = profile.id
    profile.effects_chain = "not-valid-json"
    db.add(profile)
    db.commit()
    db.close()

    resp = client.post(
        "/generate",
        json={
            "profile_id": profile_id,
            "text": "Hello.",
            "language": "en",
            "engine": "kokoro",
        },
    )
    assert resp.status_code == 200, resp.text
    last = stub_run_generation[-1]
    assert last["effects_chain"] is None


def test_generate_uses_personality_rewrite_when_enabled(
    client, engine_and_session, monkeypatch, enqueue_calls, stub_run_generation
):
    """personality=True with a profile.personality routes text through LLM rewrite."""
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    profile_id = profile.id
    profile.personality = "Speak like a pirate."
    db.add(profile)
    db.commit()
    db.close()

    async def fake_rewrite(personality, text):
        class _R:
            pass
        r = _R()
        r.text = "Arrr, ahoy matey!"
        return r

    import backend.services.personality as personality_mod
    monkeypatch.setattr(personality_mod, "rewrite_as_profile", fake_rewrite)

    resp = client.post(
        "/generate",
        json={
            "profile_id": profile_id,
            "text": "Hello world.",
            "language": "en",
            "engine": "kokoro",
            "personality": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "Arrr, ahoy matey!"
    assert body["source"] == "personality_speak"


def test_generate_400_when_personality_rewrite_raises_value_error(
    client, engine_and_session, monkeypatch
):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    profile_id = profile.id
    profile.personality = "Speak like a pirate."
    db.add(profile)
    db.commit()
    db.close()

    async def boom(personality, text):
        raise ValueError("bad personality prompt")

    import backend.services.personality as personality_mod
    monkeypatch.setattr(personality_mod, "rewrite_as_profile", boom)

    resp = client.post(
        "/generate",
        json={
            "profile_id": profile_id,
            "text": "Hello.",
            "language": "en",
            "engine": "kokoro",
            "personality": True,
        },
    )
    assert resp.status_code == 400
    assert "bad personality prompt" in resp.json()["detail"]


def test_generate_500_when_personality_rewrite_returns_empty(
    client, engine_and_session, monkeypatch
):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    profile_id = profile.id
    profile.personality = "Speak like a pirate."
    db.add(profile)
    db.commit()
    db.close()

    async def empty(personality, text):
        class _R:
            pass
        r = _R()
        r.text = "   "  # whitespace stripped -> empty
        return r

    import backend.services.personality as personality_mod
    monkeypatch.setattr(personality_mod, "rewrite_as_profile", empty)

    resp = client.post(
        "/generate",
        json={
            "profile_id": profile_id,
            "text": "Hello.",
            "language": "en",
            "engine": "kokoro",
            "personality": True,
        },
    )
    assert resp.status_code == 500
    assert "empty" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /generate/{id}/retry
# ---------------------------------------------------------------------------


def test_retry_returns_404_for_unknown_generation(client):
    resp = client.post("/generate/does-not-exist/retry")
    assert resp.status_code == 404


def test_retry_rejects_non_failed_generation(client, engine_and_session):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    gen = _make_generation_row(db, profile.id, status="completed")
    gen_id = gen.id
    db.close()

    resp = client.post(f"/generate/{gen_id}/retry")
    assert resp.status_code == 400
    assert "failed" in resp.json()["detail"].lower()


def test_retry_resets_failed_generation_to_generating(
    client, engine_and_session, enqueue_calls
):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    gen = _make_generation_row(db, profile.id, status="failed")
    gen.error = "oops"
    db.add(gen)
    db.commit()
    gen_id = gen.id
    db.close()

    resp = client.post(f"/generate/{gen_id}/retry")
    assert resp.status_code == 200, resp.text

    db = TestSession()
    refreshed = db.query(DBGeneration).filter_by(id=gen_id).first()
    assert refreshed.status == "generating"
    assert refreshed.error is None
    assert refreshed.audio_path == ""
    assert refreshed.duration == 0
    db.close()

    assert len(enqueue_calls) == 1
    assert enqueue_calls[0][0] == gen_id


# ---------------------------------------------------------------------------
# POST /generate/{id}/regenerate
# ---------------------------------------------------------------------------


def test_regenerate_returns_404_for_unknown_generation(client):
    resp = client.post("/generate/missing/regenerate")
    assert resp.status_code == 404


def test_regenerate_rejects_non_completed_generation(client, engine_and_session):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    gen = _make_generation_row(db, profile.id, status="generating")
    gen_id = gen.id
    db.close()

    resp = client.post(f"/generate/{gen_id}/regenerate")
    assert resp.status_code == 400
    assert "completed" in resp.json()["detail"].lower()


def test_regenerate_resets_completed_generation_to_generating(
    client, engine_and_session, enqueue_calls
):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    gen = _make_generation_row(db, profile.id, status="completed")
    gen_id = gen.id
    db.close()

    resp = client.post(f"/generate/{gen_id}/regenerate")
    assert resp.status_code == 200, resp.text

    db = TestSession()
    refreshed = db.query(DBGeneration).filter_by(id=gen_id).first()
    assert refreshed.status == "generating"
    assert refreshed.error is None
    db.close()

    assert len(enqueue_calls) == 1


# ---------------------------------------------------------------------------
# POST /generate/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_returns_404_for_unknown_generation(client, cancel_state):
    resp = client.post("/generate/missing/cancel")
    assert resp.status_code == 404


def test_cancel_rejects_terminal_generation(client, engine_and_session, cancel_state):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    gen = _make_generation_row(db, profile.id, status="completed")
    gen_id = gen.id
    db.close()

    resp = client.post(f"/generate/{gen_id}/cancel")
    assert resp.status_code == 400
    assert "active" in resp.json()["detail"].lower()


def test_cancel_running_generation_returns_pending_message(
    client, engine_and_session, cancel_state
):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    gen = _make_generation_row(db, profile.id, status="generating")
    gen_id = gen.id
    db.close()

    cancel_state["value"] = "running"
    resp = client.post(f"/generate/{gen_id}/cancel")
    assert resp.status_code == 200
    assert "cancellation requested" in resp.json()["message"].lower()


def test_cancel_queued_generation_marks_failed_and_returns_message(
    client, engine_and_session, cancel_state
):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    gen = _make_generation_row(db, profile.id, status="generating")
    gen_id = gen.id
    db.close()

    cancel_state["value"] = "queued"
    resp = client.post(f"/generate/{gen_id}/cancel")
    assert resp.status_code == 200
    assert "queued" in resp.json()["message"].lower()

    db = TestSession()
    refreshed = db.query(DBGeneration).filter_by(id=gen_id).first()
    assert refreshed.status == "failed"
    assert refreshed.error == "Generation cancelled"
    db.close()


def test_cancel_orphaned_generation_clears_row(
    client, engine_and_session, cancel_state
):
    """When the queue says 'unknown' but the row is active, the row is failed
    with an orphaned error message."""
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    gen = _make_generation_row(db, profile.id, status="generating")
    gen_id = gen.id
    db.close()

    cancel_state["value"] = None
    resp = client.post(f"/generate/{gen_id}/cancel")
    assert resp.status_code == 200
    assert "orphaned" in resp.json()["message"].lower()

    db = TestSession()
    refreshed = db.query(DBGeneration).filter_by(id=gen_id).first()
    assert refreshed.status == "failed"
    assert refreshed.error == "Generation orphaned by worker"
    db.close()


# ---------------------------------------------------------------------------
# GET /generate/{id}/status (SSE)
# ---------------------------------------------------------------------------


def test_status_stream_yields_not_found_for_missing_generation(client):
    with client.stream("GET", "/generate/missing-id/status") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    # The SSE stream emits a single not_found payload and exits.
    assert "not_found" in body
    assert "missing-id" in body


def test_status_stream_yields_terminal_status_and_closes(client, engine_and_session):
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    gen = _make_generation_row(db, profile.id, status="completed")
    gen_id = gen.id
    db.close()

    with client.stream("GET", f"/generate/{gen_id}/status") as resp:
        assert resp.status_code == 200
        # Read one frame - it should report 'completed' and then close.
        body = ""
        for chunk in resp.iter_text():
            body += chunk
            if "completed" in body:
                break

    # Parse the SSE data line
    data_line = [ln for ln in body.splitlines() if ln.startswith("data: ")][0]
    payload = json.loads(data_line[len("data: "):])
    assert payload["id"] == gen_id
    assert payload["status"] == "completed"


# ---------------------------------------------------------------------------
# POST /generate/import
# ---------------------------------------------------------------------------


def test_import_audio_rejects_unsupported_extension(client):
    resp = client.post(
        "/generate/import",
        files={"file": ("song.xyz", b"\x00\x00", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


def test_import_audio_rejects_empty_payload(client):
    resp = client.post(
        "/generate/import",
        files={"file": ("clip.wav", b"", "audio/wav")},
    )
    assert resp.status_code == 400
    assert "Empty" in resp.json()["detail"]


def test_import_audio_rejects_oversize_payload(client, monkeypatch):
    """The size cap is enforced as chunks are read off the wire."""
    import backend.routes.generations as gens_module
    monkeypatch.setattr(gens_module, "IMPORT_AUDIO_MAX_BYTES", 1024)

    payload = b"\x00" * 4096  # 4 KB > 1 KB cap
    resp = client.post(
        "/generate/import",
        files={"file": ("clip.wav", payload, "audio/wav")},
    )
    assert resp.status_code == 413
    assert "limit" in resp.json()["detail"].lower()


def test_import_audio_returns_400_on_decode_failure(client, monkeypatch, tmp_path):
    """A bytes-blob that load_audio can't decode results in a 400 and the
    target file is cleaned up."""

    written: list[Path] = []
    original_write_bytes = Path.write_bytes

    def tracking_write(self, data):
        written.append(self)
        return original_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", tracking_write)

    def broken_load(path):
        raise RuntimeError("can't decode")

    import backend.routes.generations as gens_module
    monkeypatch.setattr(gens_module, "load_audio", broken_load)

    resp = client.post(
        "/generate/import",
        files={"file": ("clip.wav", b"\x00" * 64, "audio/wav")},
    )
    assert resp.status_code == 400
    assert "decode" in resp.json()["detail"].lower()
    # The target file should have been unlinked on decode failure.
    for p in written:
        assert not p.exists(), f"expected {p} to be removed after decode failure"


def test_import_audio_happy_path_creates_generation(client, monkeypatch):
    """A valid WAV upload is decoded, persisted, and returned as a Generation."""

    def fake_load(path):
        # 1 second of audio at 24kHz
        return np.zeros(24000, dtype=np.float32), 24000

    import backend.routes.generations as gens_module
    monkeypatch.setattr(gens_module, "load_audio", fake_load)

    resp = client.post(
        "/generate/import",
        files={"file": ("my clip.wav", _write_tiny_wav(), "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["engine"] == "import"
    assert body["source"] == "import"
    assert body["status"] == "completed"
    assert body["language"] == "en"
    # display name derived from filename stem
    assert body["text"] == "my clip"
    # Duration computed as len(audio)/sr
    assert body["duration"] == pytest.approx(1.0)


def test_import_audio_strips_extension_from_display_name(
    client, monkeypatch
):
    """The display name (Generation.text) is the upload filename's stem."""

    def fake_load(path):
        return np.zeros(2400, dtype=np.float32), 24000

    import backend.routes.generations as gens_module
    monkeypatch.setattr(gens_module, "load_audio", fake_load)

    resp = client.post(
        "/generate/import",
        files={"file": ("background_music.wav", _write_tiny_wav(), "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "background_music"


# ---------------------------------------------------------------------------
# POST /generate/stream
# ---------------------------------------------------------------------------


def test_stream_speech_returns_404_when_profile_missing(client):
    resp = client.post(
        "/generate/stream",
        json={
            "profile_id": "nope",
            "text": "Hello.",
            "language": "en",
            "engine": "kokoro",
        },
    )
    assert resp.status_code == 404


def test_stream_speech_returns_wav_bytes(client, engine_and_session, monkeypatch):
    """End-to-end stream: stub the backend chain and verify WAV bytes flow back."""
    _, TestSession = engine_and_session
    db = TestSession()
    profile = _make_preset_profile(db)
    profile_id = profile.id
    db.close()

    # Stub the heavy backend collaborators.
    import backend.backends as backends_mod

    class _FakeModel:
        def is_loaded(self):
            return True

    monkeypatch.setattr(
        backends_mod, "get_tts_backend_for_engine", lambda engine: _FakeModel()
    )

    async def _noop_cache(*a, **k):
        return None

    async def _noop_load(*a, **k):
        return None

    monkeypatch.setattr(backends_mod, "ensure_model_cached_or_raise", _noop_cache)
    monkeypatch.setattr(backends_mod, "load_engine_model", _noop_load)
    monkeypatch.setattr(backends_mod, "engine_needs_trim", lambda engine: False)

    # Stub create_voice_prompt_for_profile to skip real prompt building.
    import backend.services.profiles as profiles_mod

    async def fake_voice_prompt(profile_id, db, engine="kokoro"):
        return {"engine": engine, "voice_id": "af_heart"}

    monkeypatch.setattr(
        profiles_mod, "create_voice_prompt_for_profile", fake_voice_prompt
    )

    # Stub generate_chunked to return a known audio array.
    import backend.utils.chunked_tts as chunked_mod

    async def fake_generate_chunked(*args, **kwargs):
        return np.zeros(24000, dtype=np.float32), 24000

    monkeypatch.setattr(chunked_mod, "generate_chunked", fake_generate_chunked)

    # Stub audio_to_wav_bytes to return a sentinel WAV blob.
    import backend.services.tts as tts_mod

    sentinel = b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt "
    monkeypatch.setattr(tts_mod, "audio_to_wav_bytes", lambda audio, sr: sentinel)

    resp = client.post(
        "/generate/stream",
        json={
            "profile_id": profile_id,
            "text": "Hello.",
            "language": "en",
            "engine": "kokoro",
            "normalize": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content == sentinel
