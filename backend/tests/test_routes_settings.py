"""Tests for the /settings router (U-py-013).

Spins up a minimal FastAPI app with the settings router and a temp SQLite DB
so every endpoint in ``backend/routes/settings.py`` is exercised end-to-end
against the real service layer — no first-party mocks.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base, get_db
from backend.routes.settings import router as settings_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def client_and_session(tmp_path):
    """Build a minimal app with a temp SQLite DB and the settings router only."""
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(settings_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c, TestSession


@pytest.fixture(scope="function")
def client(client_and_session):
    c, _ = client_and_session
    return c


# ---------------------------------------------------------------------------
# GET /settings/captures
# ---------------------------------------------------------------------------


def test_get_capture_settings_creates_row_with_defaults_when_missing(client):
    """First GET on a fresh DB lazily seeds the singleton with declared defaults."""
    r = client.get("/settings/captures")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["stt_model"] == "turbo"
    assert body["language"] == "auto"
    assert body["auto_refine"] is True
    assert body["llm_model"] == "0.6B"
    assert body["smart_cleanup"] is True
    assert body["self_correction"] is True
    assert body["preserve_technical"] is True
    assert body["allow_auto_paste"] is True
    assert body["default_playback_voice_id"] is None
    assert body["hotkey_enabled"] is False
    # Chord lists come from the platform-default helpers.
    assert isinstance(body["chord_push_to_talk_keys"], list)
    assert len(body["chord_push_to_talk_keys"]) >= 1
    assert isinstance(body["chord_toggle_to_talk_keys"], list)
    assert len(body["chord_toggle_to_talk_keys"]) >= 1


def test_get_capture_settings_is_idempotent(client, client_and_session):
    """Repeated GETs do not create new rows — still a single singleton."""
    _, TestSession = client_and_session
    client.get("/settings/captures")
    client.get("/settings/captures")
    client.get("/settings/captures")

    from backend.database import CaptureSettings as DBCaptureSettings

    db = TestSession()
    try:
        rows = db.query(DBCaptureSettings).all()
    finally:
        db.close()
    assert len(rows) == 1
    assert rows[0].id == 1


# ---------------------------------------------------------------------------
# PUT /settings/captures
# ---------------------------------------------------------------------------


def test_update_capture_settings_persists_changed_fields(client):
    """PUT mutates the singleton; a subsequent GET reflects the new values."""
    r = client.put(
        "/settings/captures",
        json={
            "stt_model": "medium",
            "language": "en",
            "auto_refine": False,
            "llm_model": "4B",
            "smart_cleanup": False,
            "self_correction": False,
            "preserve_technical": False,
            "allow_auto_paste": False,
            "hotkey_enabled": True,
            "chord_push_to_talk_keys": ["ControlLeft", "AltLeft"],
            "chord_toggle_to_talk_keys": ["ControlLeft", "AltLeft", "KeyT"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stt_model"] == "medium"
    assert body["language"] == "en"
    assert body["auto_refine"] is False
    assert body["llm_model"] == "4B"
    assert body["smart_cleanup"] is False
    assert body["self_correction"] is False
    assert body["preserve_technical"] is False
    assert body["allow_auto_paste"] is False
    assert body["hotkey_enabled"] is True
    assert body["chord_push_to_talk_keys"] == ["ControlLeft", "AltLeft"]
    assert body["chord_toggle_to_talk_keys"] == [
        "ControlLeft",
        "AltLeft",
        "KeyT",
    ]

    got = client.get("/settings/captures").json()
    assert got["stt_model"] == "medium"
    assert got["language"] == "en"
    assert got["auto_refine"] is False
    assert got["llm_model"] == "4B"
    assert got["hotkey_enabled"] is True
    assert got["chord_push_to_talk_keys"] == ["ControlLeft", "AltLeft"]


def test_update_capture_settings_partial_patch_leaves_other_fields_untouched(client):
    """Omitted fields keep their prior values (model_dump exclude_unset)."""
    # Seed non-default state first.
    client.put(
        "/settings/captures",
        json={
            "stt_model": "small",
            "language": "fr",
            "auto_refine": False,
        },
    )
    # Now patch only one field.
    r = client.put("/settings/captures", json={"stt_model": "large"})
    assert r.status_code == 200
    body = r.json()
    assert body["stt_model"] == "large"
    # Untouched fields keep their earlier values, not the original defaults.
    assert body["language"] == "fr"
    assert body["auto_refine"] is False


def test_update_capture_settings_clears_nullable_default_voice_id(client):
    """default_playback_voice_id is nullable — explicit None must clear it."""
    # Set a value first.
    client.put(
        "/settings/captures",
        json={"default_playback_voice_id": "voice-xyz"},
    )
    got = client.get("/settings/captures").json()
    assert got["default_playback_voice_id"] == "voice-xyz"

    # Clear it.
    r = client.put(
        "/settings/captures",
        json={"default_playback_voice_id": None},
    )
    assert r.status_code == 200
    assert r.json()["default_playback_voice_id"] is None

    got = client.get("/settings/captures").json()
    assert got["default_playback_voice_id"] is None


def test_update_capture_settings_empty_patch_is_noop(client):
    """An empty PUT body returns the current row unchanged."""
    client.put("/settings/captures", json={"language": "de"})
    r = client.put("/settings/captures", json={})
    assert r.status_code == 200
    assert r.json()["language"] == "de"


def test_update_capture_settings_rejects_invalid_stt_model(client):
    """Pydantic rejects an stt_model outside the allowed pattern."""
    r = client.put("/settings/captures", json={"stt_model": "huge"})
    assert r.status_code == 422


def test_update_capture_settings_rejects_invalid_llm_model(client):
    """Pydantic rejects an llm_model outside the allowed pattern."""
    r = client.put("/settings/captures", json={"llm_model": "70B"})
    assert r.status_code == 422


def test_update_capture_settings_rejects_empty_chord_list(client):
    """Chord patches must have at least one key — empty list is 422."""
    r = client.put(
        "/settings/captures", json={"chord_push_to_talk_keys": []}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /settings/generation
# ---------------------------------------------------------------------------


def test_get_generation_settings_creates_row_with_defaults_when_missing(client):
    """First GET on a fresh DB lazily seeds the generation singleton."""
    r = client.get("/settings/generation")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_chunk_chars"] == 800
    assert body["crossfade_ms"] == 50
    assert body["normalize_audio"] is True
    assert body["autoplay_on_generate"] is True


def test_get_generation_settings_is_idempotent(client, client_and_session):
    """Repeated GETs reuse the same singleton row."""
    _, TestSession = client_and_session
    client.get("/settings/generation")
    client.get("/settings/generation")

    from backend.database import GenerationSettings as DBGenerationSettings

    db = TestSession()
    try:
        rows = db.query(DBGenerationSettings).all()
    finally:
        db.close()
    assert len(rows) == 1
    assert rows[0].id == 1


# ---------------------------------------------------------------------------
# PUT /settings/generation
# ---------------------------------------------------------------------------


def test_update_generation_settings_persists_changed_fields(client):
    """PUT writes a full payload and the next GET reflects every change."""
    r = client.put(
        "/settings/generation",
        json={
            "max_chunk_chars": 1200,
            "crossfade_ms": 200,
            "normalize_audio": False,
            "autoplay_on_generate": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_chunk_chars"] == 1200
    assert body["crossfade_ms"] == 200
    assert body["normalize_audio"] is False
    assert body["autoplay_on_generate"] is False

    got = client.get("/settings/generation").json()
    assert got["max_chunk_chars"] == 1200
    assert got["crossfade_ms"] == 200
    assert got["normalize_audio"] is False
    assert got["autoplay_on_generate"] is False


def test_update_generation_settings_partial_patch_leaves_other_fields_untouched(
    client,
):
    """Omitted fields keep their prior values."""
    client.put(
        "/settings/generation",
        json={"max_chunk_chars": 1500, "crossfade_ms": 100},
    )
    r = client.put(
        "/settings/generation", json={"normalize_audio": False}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["normalize_audio"] is False
    assert body["max_chunk_chars"] == 1500
    assert body["crossfade_ms"] == 100


def test_update_generation_settings_rejects_out_of_range_chunk_chars(client):
    """max_chunk_chars must stay within [100, 5000]."""
    r = client.put(
        "/settings/generation", json={"max_chunk_chars": 50}
    )
    assert r.status_code == 422

    r = client.put(
        "/settings/generation", json={"max_chunk_chars": 9999}
    )
    assert r.status_code == 422


def test_update_generation_settings_rejects_out_of_range_crossfade(client):
    """crossfade_ms must stay within [0, 500]."""
    r = client.put(
        "/settings/generation", json={"crossfade_ms": -1}
    )
    assert r.status_code == 422

    r = client.put(
        "/settings/generation", json={"crossfade_ms": 1000}
    )
    assert r.status_code == 422


def test_update_generation_settings_empty_patch_is_noop(client):
    """An empty PUT body returns the current row unchanged."""
    client.put("/settings/generation", json={"max_chunk_chars": 2000})
    r = client.put("/settings/generation", json={})
    assert r.status_code == 200
    assert r.json()["max_chunk_chars"] == 2000


# ---------------------------------------------------------------------------
# Cross-domain isolation
# ---------------------------------------------------------------------------


def test_capture_and_generation_settings_are_independent_singletons(client):
    """Writing one domain does not bleed into the other."""
    client.put("/settings/captures", json={"language": "es"})
    client.put("/settings/generation", json={"max_chunk_chars": 1300})

    cap = client.get("/settings/captures").json()
    gen = client.get("/settings/generation").json()

    assert cap["language"] == "es"
    # Generation defaults stay intact after a captures write.
    assert gen["max_chunk_chars"] == 1300
    assert gen["normalize_audio"] is True
    assert cap["allow_auto_paste"] is True
